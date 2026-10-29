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
"""

from __future__ import annotations

import asyncio
import time
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

#: One line per route, in the words the decision model was measured with
#: (LocalDeploy examples/decision/routing_eval.json). ``unknown`` is deliberately absent: a
#: decision model should pick a real route or hand the message to the concierge.
ROUTE_DESCRIPTIONS: dict[IntentRoute, str] = {
    IntentRoute.CONVERSATION: "pure chat, greeting, thanks, a question about the bot's abilities, or a general-knowledge question that needs nothing from this computer",
    IntentRoute.STATUS: "a question about the bot's own running tasks, queue or progress",
    IntentRoute.DESKTOP_OBSERVE: "look at the screen or desktop as a picture: screenshot, what is open, what window is in front",
    IntentRoute.COMPUTER_USE: "do something in a desktop app: open an app, click, type, press keys, close a window",
    IntentRoute.BROWSER_OPEN: "open a website or Chrome, search the web, summarize pages, research across pages, screenshot a page",
    IntentRoute.BROWSER_CONTROL: "act inside a web page: log in, click buttons, fill and submit forms, scroll, extract page data",
    IntentRoute.FILESYSTEM_MANAGE: "inspect, search, read, write, create, rename, move, delete or organize files and folders",
    IntentRoute.DOCUMENT_MANAGE: "read or summarize a PDF, or create or revise a presentation or document",
    IntentRoute.ARTIFACT_DELIVERY: "send an existing file or screenshot back to the user's chat",
    IntentRoute.CODE_INTERPRETER: "write and run a small script for a calculation, a data transform or a chart",
    IntentRoute.CODING_AGENT: "hand a coding job to Codex or GitHub Copilot, only when the user names one of them",
    IntentRoute.SCHEDULE_MANAGE: "create, list, pause, resume, delete or run recurring or timed jobs and reminders",
    IntentRoute.ADAPTER_FACTORY: "build a new tool or integration the bot does not have yet",
    IntentRoute.WORKSPACE_MANAGE: "prepare a project workspace, materialize files, launch a local preview",
    IntentRoute.CONFIGURATION: "change model profiles, access modes or runtime settings",
}

ROUTE_QUESTION: dict[str, Any] = {
    "type": "choice",
    "instructions": (
        "A user sent this message to a bot that does things on their own Windows computer. Which route should "
        "handle it? Pick conversation for greetings, thanks, questions about the bot itself, and general-knowledge "
        "questions that need nothing from this computer. Pick status only for questions about the bot's own running "
        "tasks or queue. Anything that has to look at, change, open, send, schedule or build something on this "
        "computer or the web belongs to one of the other routes."
    ),
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
    ) -> None:
        self.fallback = fallback
        self.config = config
        self.audit = audit
        self.client = client or DecisionModelClient(config)
        self._down_until = 0.0

    async def classify(self, message: InboundMessage, context: str | None = None) -> MessageClassification:
        text = (message.text or "").strip()
        if self.config.mode == "off" or not text:
            return await self._concierge(message, context)
        if self.config.mode == "shadow":
            return await self._shadow(message, context, text)
        return await self._cascade(message, context, text)

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

    async def _shadow(self, message: InboundMessage, context: str | None, text: str) -> MessageClassification:
        asked = asyncio.ensure_future(self._ask(text))
        try:
            classification = await self._concierge(message, context)
        except BaseException:
            asked.cancel()
            raise
        opinion, error = await asked
        self._record(message, text, opinion, error, used=False, classification=classification)
        return classification

    async def _cascade(self, message: InboundMessage, context: str | None, text: str) -> MessageClassification:
        opinion, error = await self._ask(text)
        if opinion is not None and opinion.route in self.config.cascade_routes and opinion.confidence >= self.config.min_confidence:
            decided = _task_from_opinion(text, opinion)
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
    ) -> None:
        if self.audit is None:
            return
        decided = classification.intent.route if classification.intent else None
        if decided is None and not classification.is_task:
            decided = IntentRoute.CONVERSATION
        self.audit.append(
            AuditEventType.INTAKE_DECISION,
            actor="decision_model",
            correlation_id=message.correlation_id,
            payload={
                "message_id": message.id,
                "text": text,
                "mode": self.config.mode,
                "used": used,
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


def _task_from_opinion(text: str, opinion: DecisionOpinion) -> MessageClassification:
    task_type = TaskType.DESKTOP_OBSERVATION if opinion.route == IntentRoute.DESKTOP_OBSERVE else TaskType.OTHER
    return MessageClassification(
        is_task=True,
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
    fallback: MessageClassifier, config: DecisionConfig, audit: AuditLogger | None
) -> MessageClassifier:
    """The classifier intake should use: the concierge itself when the decision model is off."""
    if config.mode == "off":
        return fallback
    return DecisionModelClassifier(fallback, config, audit)
