"""A local decision model for the first step of intake: which route is this message?

The LLM concierge (llm/classifier.py) decides chat-versus-task and the route in one
generative call and reports a confidence that is always 0.95 to 0.98, so nothing
downstream can tell a sure call from a guess. A decision model answers the same
question in one forward pass and returns a real probability for every route. This
module asks one, served by LocalDeploy at ``POST /v1/systemone``, and lets
``DecisionConfig.mode`` decide how much to trust it:

* ``off``      the concierge decides, as before (this module is not even built).
* ``shadow``   the concierge decides; the decision model's answer is recorded next to it.
* ``cascade``  when the decision model is confident about a route that needs nothing the
               concierge extracts, it decides and the concierge call is skipped. Anything
               else, and any failure, goes to the concierge unchanged.
* ``decision`` the decision model decides every message it is confident about, chat and
               status included (a chat reply is still written by the LLM responder). The
               concierge takes the rest, and every failure.

In ``cascade`` and ``decision`` a message that arrives while a task is running in the same
chat always goes to the concierge: it may be a correction to that task, which only the
concierge recognises.
"""

from __future__ import annotations

import asyncio
import statistics
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx
import structlog

from agent_control.config import DecisionConfig
from agent_control.config_sync import read_env_value
from agent_control.llm.classifier import MessageClassifier
from agent_control.schemas import (
    AuditEventType,
    InboundMessage,
    IntentRoute,
    MessageClassification,
    OrchestrationIntent,
    TaskType,
)
from agent_control.storage.audit import AuditLogger

logger = structlog.get_logger(__name__)

#: One short line per route, in the words the decision model was measured with (LocalDeploy
#: examples/decision/routing_eval_compact.json, with "what the bot can do" and "Claude Code" added:
#: d3-lite stayed at 93% on those 98 messages and went from 84% to 86% on the 43 held-out ones).
#: Short on purpose: on a CPU a decision takes time in proportion to the prompt, and d3-lite scored
#: the same with these lines as with full sentences at about 60% of the time. ``unknown`` is absent:
#: a decision model should pick a real route or hand the message to the concierge.
ROUTE_DESCRIPTIONS: dict[IntentRoute, str] = {
    IntentRoute.CONVERSATION: "chat, thanks, general knowledge, questions about what the bot can do",
    IntentRoute.STATUS: "the bot's own tasks and progress",
    IntentRoute.DESKTOP_OBSERVE: "screenshot, what is on screen",
    IntentRoute.COMPUTER_USE: "open apps, click, type, press keys",
    IntentRoute.BROWSER_OPEN: "open a site, web search, research",
    IntentRoute.BROWSER_CONTROL: "click or fill inside a web page",
    IntentRoute.FILESYSTEM_MANAGE: "files and folders: list, read, write, move, delete",
    IntentRoute.DOCUMENT_MANAGE: "PDF or slide deck work",
    IntentRoute.ARTIFACT_DELIVERY: "send a file back to the chat",
    IntentRoute.CODE_INTERPRETER: "write and run a small script",
    IntentRoute.CODING_AGENT: "hand coding to Codex, Claude Code or Copilot",
    IntentRoute.SCHEDULE_MANAGE: "reminders and recurring jobs",
    IntentRoute.ADAPTER_FACTORY: "build a new tool or integration",
    IntentRoute.WORKSPACE_MANAGE: "set up a project workspace or preview",
    IntentRoute.CONFIGURATION: "change models, access or settings",
}

#: Routes the concierge keeps even in ``decision`` mode. ``coding.agent`` hands the folder the
#: concierge extracts to the coding CLI as its workspace (orchestration/worker.py); without it
#: the CLI would work in the default workspace instead of the one the user named.
CONCIERGE_ONLY_ROUTES: frozenset[IntentRoute] = frozenset({IntentRoute.CODING_AGENT})

ROUTE_QUESTION: dict[str, Any] = {
    "type": "choice",
    "instructions": "Which route should handle this message to a bot that does things on the user's computer?",
    "criteria": {route.value: description for route, description in ROUTE_DESCRIPTIONS.items()},
}

#: Asked in the same call. A route says which tool a request needs, so "can you read PDFs?" looks like
#: document work to it (d3-lite: 98% sure). This yes/no question separates asking about an ability
#: from asking for the work: on LocalDeploy's 141 routing messages the three ability questions scored
#: 0.93 to 0.95 and no request scored above 0.81. Such a message goes to the concierge, which knows
#: what is enabled and writes the answer.
ABILITY_QUESTION: dict[str, Any] = {
    "type": "noul",
    "instructions": "Is the message only asking whether the bot is able to do something, rather than asking it to do something now?",
}
ABILITY_THRESHOLD = 0.9

#: What a message sent while a task is running is to that task. Asked in shadow mode only (the answer is recorded next to the
#: concierge's and decides nothing) until it agrees with the concierge often enough, on enough real messages, to take over:
#: it would replace a ~10 s model call, queued behind the task, with a forward pass on the CPU.
MESSAGE_KINDS: dict[str, str] = {
    "status": "asks how the running job is going, what it is doing, or how long it will take",
    "steer": "changes or adds to the running job: a correction, an extra requirement, a different choice",
    "stop": "tells the bot to stop, cancel or drop the running job",
    "new": "a different, unrelated request that is not about the running job",
    "chat": "small talk, thanks, or a general question",
}
KIND_QUESTION: dict[str, Any] = {
    "type": "choice",
    "instructions": "The bot is busy working on a job. What is this new message to that job?",
    "criteria": dict(MESSAGE_KINDS),
}


@dataclass(frozen=True)
class DecisionOpinion:
    route: IntentRoute
    confidence: float
    probabilities: dict[str, float]
    latency_ms: float
    #: Probability that the message only asks what the bot can do (ABILITY_QUESTION); None if not answered.
    ability: float | None = None
    #: The mid-task kind (KIND_QUESTION) and how sure the model is of it; None when it was not asked (no task running).
    kind: str | None = None
    kind_confidence: float | None = None


class DecisionModelError(RuntimeError):
    """The decision model could not be asked or gave an answer that cannot be used."""


def decides_alone(config: DecisionConfig, opinion: DecisionOpinion) -> bool:
    """Whether this opinion is used without the concierge under ``config.mode``."""
    if opinion.confidence < config.min_confidence:
        return False
    if opinion.ability is not None and opinion.ability >= ABILITY_THRESHOLD:
        return False
    if config.mode == "cascade":
        return opinion.route in config.cascade_routes
    if config.mode == "decision":
        return opinion.route not in CONCIERGE_ONLY_ROUTES
    return False


#: Routes the concierge keeps even when the model is sure it is a task: the concierge extracts what they need (a provider and a folder,
#: the setting to change) or writes the reply, and a wrong guess here costs more than the seconds saved.
KEEP_WITH_CONCIERGE: frozenset[IntentRoute] = CONCIERGE_ONLY_ROUTES | {IntentRoute.CONFIGURATION, IntentRoute.DESKTOP_OBSERVE}
_NOT_TASKS = {IntentRoute.CONVERSATION.value, IntentRoute.STATUS.value, IntentRoute.UNKNOWN.value}
#: How much probability on the routes above is too much for a task to be settled without the concierge.
_KEEP_MASS_LIMIT = 0.05


def task_probability(opinion: DecisionOpinion) -> float:
    """How likely it is that the message is a task at all, whatever tool it needs: the probability on every tool route added up."""
    return sum(value for route, value in opinion.probabilities.items() if route not in _NOT_TASKS)


def settles_as_a_task(config: DecisionConfig, opinion: DecisionOpinion) -> bool:
    """Whether the model is sure enough this is a task, not chat or a status question, to skip the concierge though no single tool is sure."""
    if not config.settle_tasks_alone or config.mode not in ("cascade", "decision"):
        return False
    if opinion.ability is not None and opinion.ability >= ABILITY_THRESHOLD:
        return False
    if opinion.route in KEEP_WITH_CONCIERGE or opinion.route in (IntentRoute.CONVERSATION, IntentRoute.STATUS):
        return False
    if config.mode == "cascade" and opinion.route not in config.cascade_routes:
        return False  # `cascade` lists the routes it may decide; the same list applies here
    if sum(opinion.probabilities.get(route.value, 0.0) for route in KEEP_WITH_CONCIERGE) >= _KEEP_MASS_LIMIT:
        return False
    return task_probability(opinion) >= config.task_bar


def summarize_kinds(payloads: list[dict[str, Any]], *, bar: float = 0.95, minimum: int = 100) -> dict[str, Any]:
    """How the mid-task kind question compares with the concierge, and whether it has earned the right to take over.

    It has when it agrees on at least `bar` of at least `minimum` real messages.
    """
    compared = [p for p in payloads if p.get("decision_kind") and p.get("final_kind")]
    agreed = sum(1 for p in compared if p.get("kind_agrees"))
    rate = agreed / len(compared) if compared else None
    return {
        "compared": len(compared), "agreed": agreed, "agreement": round(rate, 4) if rate is not None else None,
        "ready": bool(compared) and len(compared) >= minimum and rate is not None and rate >= bar,
        "needs_more_messages": max(0, minimum - len(compared)),
        "disagreements": [
            {"text": str(p.get("text") or "")[:200], "decision_kind": p.get("decision_kind"), "llm_kind": p.get("final_kind")}
            for p in compared if not p.get("kind_agrees")
        ][:10],
    }


def summarize_decisions(payloads: list[dict[str, Any]]) -> dict[str, Any]:
    """What recorded ``intake_decision`` events show, for the console.

    Agreement only counts messages the concierge decided: where the decision model
    decided alone there is no second opinion to agree with.
    """
    answered = [p for p in payloads if p.get("decision_route")]
    compared = [p for p in answered if not p.get("used") and p.get("final_route")]
    agreed = sum(1 for p in compared if p.get("agrees"))
    times = [float(p["decision_ms"]) for p in answered if p.get("decision_ms") is not None]
    disagreements = [
        {
            "text": str(p.get("text") or "")[:200],
            "decision_route": p.get("decision_route"),
            "decision_confidence": p.get("decision_confidence"),
            "llm_route": p.get("final_route"),
        }
        for p in compared
        if not p.get("agrees")
    ]
    return {
        "messages": len(payloads),
        "decided_alone": sum(1 for p in payloads if p.get("used")),
        "unanswered": len(payloads) - len(answered),
        "compared": len(compared),
        "agreed": agreed,
        "agreement": round(agreed / len(compared), 4) if compared else None,
        "median_ms": round(statistics.median(times)) if times else None,
        "recent_disagreements": disagreements[:5],
    }


class DecisionModelClient:
    """Asks LocalDeploy's ``/v1/systemone`` for the route of one message."""

    def __init__(self, config: DecisionConfig, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.config = config
        self._transport = transport

    async def route_of(self, text: str, *, with_kind: bool = False) -> DecisionOpinion:
        questions: dict[str, Any] = {"route": ROUTE_QUESTION, "ability": ABILITY_QUESTION}
        if with_kind:
            questions["kind"] = KIND_QUESTION
        body: dict[str, Any] = {"state": text, "questions": questions}
        if self.config.profile:
            body["model"] = self.config.profile
        headers = {}
        token = read_env_value(self.config.api_token_env) if self.config.api_token_env else None
        if token:
            headers["Authorization"] = f"Bearer {token}"
        started = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=self.config.timeout_seconds, transport=self._transport) as client:
                response = await client.post(f"{self.config.base_url}/v1/systemone", json=body, headers=headers)
        except httpx.HTTPError as exc:
            raise DecisionModelError(f"decision model unreachable: {type(exc).__name__}: {exc}") from exc
        if response.status_code != 200:
            raise DecisionModelError(f"decision model answered HTTP {response.status_code}: {response.text[:200]}")
        try:
            answers = response.json()["answers"]
            answer = answers["route"]
            route = IntentRoute(answer["choice"])
            probabilities = {str(k): float(v) for k, v in (answer.get("probabilities") or {}).items()}
            confidence = float(answer.get("confidence", probabilities.get(route.value, 0.0)))
        except (KeyError, TypeError, ValueError) as exc:
            raise DecisionModelError(f"decision model returned an unusable answer: {exc}") from exc
        kind, kind_confidence = _kind_of(answers.get("kind")) if with_kind else (None, None)
        return DecisionOpinion(
            route, confidence, probabilities, (time.monotonic() - started) * 1000, ability=_yes_probability(answers.get("ability")),
            kind=kind, kind_confidence=kind_confidence,
        )


def _kind_of(answer: Any) -> tuple[str | None, float | None]:
    """The mid-task kind from a /v1/systemone answer, or (None, None) when it is absent or not one of MESSAGE_KINDS."""
    if not isinstance(answer, dict):
        return None, None
    choice = str(answer.get("choice") or "")
    if choice not in MESSAGE_KINDS:
        return None, None
    probabilities = answer.get("probabilities") or {}
    try:
        return choice, float(answer.get("confidence", probabilities.get(choice, 0.0)))
    except (TypeError, ValueError):
        return choice, None


def kind_of_classification(classification: MessageClassification) -> str:
    """What the concierge decided a message sent mid-task is, in the same words as MESSAGE_KINDS."""
    if classification.steers_active_task:
        return "steer"
    if classification.task_type == TaskType.STATUS_REQUEST:
        return "status"
    route = route_of_classification(classification)
    if route == IntentRoute.STATUS:
        return "status"
    if route == IntentRoute.CONVERSATION or not classification.is_task:
        return "chat"
    return "new"


def _yes_probability(answer: Any) -> float | None:
    """A yes/no answer as a probability: ``{"noul": p}`` from /v1/systemone, or a bare number. None if absent."""
    value = answer.get("noul") if isinstance(answer, dict) else answer
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


class DecisionModelClassifier:
    """Wraps the LLM concierge with a local decision model, as ``DecisionConfig.mode`` directs."""

    def __init__(
        self,
        fallback: MessageClassifier,
        config: DecisionConfig,
        audit: AuditLogger | None = None,
        client: DecisionModelClient | None = None,
        busy: Callable[[InboundMessage], bool] | None = None,
    ) -> None:
        self.fallback = fallback
        self.config = config
        self.audit = audit
        self.client = client or DecisionModelClient(config)
        # True when a task is in flight for the message's chat (see the module docstring).
        self.busy = busy
        self._down_until = 0.0

    async def classify(self, message: InboundMessage, context: str | None = None) -> MessageClassification:
        text = (message.text or "").strip()
        if self.config.mode == "off" or not text:
            return await self._concierge(message, context)
        if self.config.mode == "shadow":
            return await self._shadow(message, context, text)
        if self._busy(message):
            return await self._shadow(message, context, text, busy=True)
        return await self._cascade(message, context, text)

    def _busy(self, message: InboundMessage) -> bool:
        if self.busy is None:
            return False
        try:
            return bool(self.busy(message))
        except Exception:  # an unknown answer counts as busy: the concierge is the safe side
            logger.exception("decision_model_busy_check_failed")
            return True

    async def _concierge(self, message: InboundMessage, context: str | None) -> MessageClassification:
        try:
            return await self.fallback.classify(message, context=context)
        except TypeError:  # a classifier that predates the context argument
            return await self.fallback.classify(message)

    async def _ask(self, text: str, *, with_kind: bool = False) -> tuple[DecisionOpinion | None, str | None]:
        """The opinion, or why there is none. Never raises: this is an optional helper."""
        if time.monotonic() < self._down_until:
            return None, "skipped: the decision model failed recently"
        try:
            return (await self.client.route_of(text, with_kind=True) if with_kind else await self.client.route_of(text)), None
        except DecisionModelError as exc:
            self._down_until = time.monotonic() + self.config.cooldown_seconds
            logger.warning("decision_model_unavailable", error=str(exc))
            return None, str(exc)
        except Exception as exc:  # a bug here must never cost a user their message
            self._down_until = time.monotonic() + self.config.cooldown_seconds
            logger.exception("decision_model_failed")
            return None, f"{type(exc).__name__}: {exc}"

    async def _shadow(
        self, message: InboundMessage, context: str | None, text: str, *, busy: bool = False
    ) -> MessageClassification:
        asked = asyncio.ensure_future(self._ask(text, with_kind=busy))
        try:
            classification = await self._concierge(message, context)
        except BaseException:
            asked.cancel()
            raise
        opinion, error = await asked
        self._record(message, text, opinion, error, used=False, classification=classification, busy=busy)
        return classification

    async def _cascade(self, message: InboundMessage, context: str | None, text: str) -> MessageClassification:
        opinion, error = await self._ask(text)
        if opinion is not None and decides_alone(self.config, opinion):
            decided = classification_from_opinion(text, opinion)
            self._record(message, text, opinion, error, used=True, classification=decided)
            return decided
        if opinion is not None and settles_as_a_task(self.config, opinion):
            decided = classification_as_a_task(text, opinion)
            self._record(message, text, opinion, error, used=True, classification=decided, settled_by="task_probability")
            return decided
        classification = await self._concierge(message, context)
        self._record(message, text, opinion, error, used=False, classification=classification)
        return classification

    def _record(
        self,
        message: InboundMessage,
        text: str,
        opinion: DecisionOpinion | None,
        error: str | None,
        *,
        used: bool,
        classification: MessageClassification,
        busy: bool = False,
        settled_by: str | None = None,
    ) -> None:
        if self.audit is None:
            return
        decided = route_of_classification(classification)
        self.audit.append(
            AuditEventType.INTAKE_DECISION,
            actor="decision_model",
            correlation_id=message.correlation_id,
            payload={
                "message_id": message.id,
                "text": text,
                "mode": self.config.mode,
                "used": used,
                # "task_probability": sure it is a task, not which tool; the route is left open. None: the route itself decided, or not used.
                "settled_by": settled_by if used else None,
                "task_probability": round(task_probability(opinion), 4) if opinion else None,
                # A task was running in this chat, so the concierge decided whatever the model said.
                "chat_busy": busy,
                "decision_route": opinion.route.value if opinion else None,
                "decision_confidence": round(opinion.confidence, 4) if opinion else None,
                # How sure the model is that the message only asks what the bot can do (ABILITY_QUESTION).
                "decision_ability": round(opinion.ability, 4) if opinion and opinion.ability is not None else None,
                "decision_top3": (
                    dict(sorted(opinion.probabilities.items(), key=lambda kv: -kv[1])[:3]) if opinion else None
                ),
                "decision_ms": round(opinion.latency_ms, 1) if opinion else None,
                "decision_error": error,
                "final_route": decided.value if decided else None,
                "agrees": (opinion.route == decided) if opinion and decided else None,
                # Only while a task runs (shadow): what the decision model thinks this message is to that task, and what the
                # concierge decided. Recorded, never acted on.
                "decision_kind": opinion.kind if opinion else None,
                "decision_kind_confidence": (
                    round(opinion.kind_confidence, 4) if opinion and opinion.kind_confidence is not None else None
                ),
                "final_kind": kind_of_classification(classification) if busy else None,
                "kind_agrees": (opinion.kind == kind_of_classification(classification)) if busy and opinion and opinion.kind else None,
            },
        )


def route_of_classification(classification: MessageClassification) -> IntentRoute | None:
    """The route intake acts on (channels/base.py): a status request is handled as status whatever
    its route says, then the route, then chat for a classification that is not a task."""
    if classification.task_type == TaskType.STATUS_REQUEST:
        return IntentRoute.STATUS
    if classification.intent is not None:
        return classification.intent.route
    return IntentRoute.CONVERSATION if not classification.is_task else None


def classification_from_opinion(text: str, opinion: DecisionOpinion) -> MessageClassification:
    """The classification intake acts on, shaped the way the concierge shapes its own.

    Chat carries no reply, so intake asks the LLM chat responder for one (channels/base.py).
    Status is a status request rather than a task, as the concierge marks it.
    """
    if opinion.route == IntentRoute.STATUS:
        task_type = TaskType.STATUS_REQUEST
    elif opinion.route == IntentRoute.DESKTOP_OBSERVE:
        task_type = TaskType.DESKTOP_OBSERVATION
    else:
        task_type = TaskType.OTHER
    return MessageClassification(
        is_task=opinion.route not in (IntentRoute.CONVERSATION, IntentRoute.STATUS),
        task_type=task_type,
        normalized_objective=text,
        confidence=round(opinion.confidence, 4),
        reason=f"Local decision model chose {opinion.route.value} at {opinion.confidence:.2f}.",
        intent=OrchestrationIntent(
            route=opinion.route,
            objective=text,
            reasoning=f"The local decision model chose {opinion.route.value} with probability {opinion.confidence:.2f}.",
        ),
    )


def classification_as_a_task(text: str, opinion: DecisionOpinion) -> MessageClassification:
    """A task whose tool the model is not sure of: the route is left "unknown", as the concierge leaves it for most requests, which keeps
    the operator's full tool list and the check before a first question about something no tool can do."""
    return MessageClassification(
        is_task=True,
        task_type=TaskType.OTHER,
        normalized_objective=text,
        confidence=round(task_probability(opinion), 4),
        reason=f"Local decision model: a task ({task_probability(opinion):.2f}), tool not settled (best: {opinion.route.value} {opinion.confidence:.2f}).",
        intent=OrchestrationIntent(
            route=IntentRoute.UNKNOWN,
            objective=text,
            reasoning=f"The local decision model is {task_probability(opinion):.2f} sure this is a task; the best single tool was {opinion.route.value}.",
        ),
    )


def build_decision_classifier(
    fallback: MessageClassifier,
    config: DecisionConfig,
    audit: AuditLogger | None,
    busy: Callable[[InboundMessage], bool] | None = None,
) -> MessageClassifier:
    """The classifier intake should use: the concierge itself when the decision model is off."""
    if config.mode == "off":
        return fallback
    return DecisionModelClassifier(fallback, config, audit, busy=busy)
