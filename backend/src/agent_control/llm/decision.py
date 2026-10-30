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

#: One short line per route, in the words the decision model was measured with
#: (LocalDeploy examples/decision/routing_eval_compact.json). Short on purpose: on a CPU a decision
#: takes time in proportion to the prompt, and d3-lite scored the same with these lines as with
#: full sentences at about 60% of the time. ``unknown`` is absent: a decision model should pick a
#: real route or hand the message to the concierge.
ROUTE_DESCRIPTIONS: dict[IntentRoute, str] = {
    IntentRoute.CONVERSATION: "chat, thanks, general knowledge, questions about the bot",
    IntentRoute.STATUS: "the bot's own tasks and progress",
    IntentRoute.DESKTOP_OBSERVE: "screenshot, what is on screen",
    IntentRoute.COMPUTER_USE: "open apps, click, type, press keys",
    IntentRoute.BROWSER_OPEN: "open a site, web search, research",
    IntentRoute.BROWSER_CONTROL: "click or fill inside a web page",
    IntentRoute.FILESYSTEM_MANAGE: "files and folders: list, read, write, move, delete",
    IntentRoute.DOCUMENT_MANAGE: "PDF or slide deck work",
    IntentRoute.ARTIFACT_DELIVERY: "send a file back to the chat",
    IntentRoute.CODE_INTERPRETER: "write and run a small script",
    IntentRoute.CODING_AGENT: "hand coding to Codex or Copilot",
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


@dataclass(frozen=True)
class DecisionOpinion:
    route: IntentRoute
    confidence: float
    probabilities: dict[str, float]
    latency_ms: float


class DecisionModelError(RuntimeError):
    """The decision model could not be asked or gave an answer that cannot be used."""


def decides_alone(config: DecisionConfig, opinion: DecisionOpinion) -> bool:
    """Whether this opinion is used without the concierge under ``config.mode``."""
    if opinion.confidence < config.min_confidence:
        return False
    if config.mode == "cascade":
        return opinion.route in config.cascade_routes
    if config.mode == "decision":
        return opinion.route not in CONCIERGE_ONLY_ROUTES
    return False


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

    async def route_of(self, text: str) -> DecisionOpinion:
        body: dict[str, Any] = {"state": text, "questions": {"route": ROUTE_QUESTION}}
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
            answer = response.json()["answers"]["route"]
            route = IntentRoute(answer["choice"])
            probabilities = {str(k): float(v) for k, v in (answer.get("probabilities") or {}).items()}
            confidence = float(answer.get("confidence", probabilities.get(route.value, 0.0)))
        except (KeyError, TypeError, ValueError) as exc:
            raise DecisionModelError(f"decision model returned an unusable answer: {exc}") from exc
        return DecisionOpinion(route, confidence, probabilities, (time.monotonic() - started) * 1000)


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

    async def _ask(self, text: str) -> tuple[DecisionOpinion | None, str | None]:
        """The opinion, or why there is none. Never raises: this is an optional helper."""
        if time.monotonic() < self._down_until:
            return None, "skipped: the decision model failed recently"
        try:
            return await self.client.route_of(text), None
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
        asked = asyncio.ensure_future(self._ask(text))
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
                # A task was running in this chat, so the concierge decided whatever the model said.
                "chat_busy": busy,
                "decision_route": opinion.route.value if opinion else None,
                "decision_confidence": round(opinion.confidence, 4) if opinion else None,
                "decision_top3": (
                    dict(sorted(opinion.probabilities.items(), key=lambda kv: -kv[1])[:3]) if opinion else None
                ),
                "decision_ms": round(opinion.latency_ms, 1) if opinion else None,
                "decision_error": error,
                "final_route": decided.value if decided else None,
                "agrees": (opinion.route == decided) if opinion and decided else None,
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
