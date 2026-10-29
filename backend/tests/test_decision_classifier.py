"""The local decision model in front of the LLM concierge (llm/decision.py).

No model runs here: a fake client stands in for LocalDeploy's /v1/systemone and a fake concierge
counts how often it is asked.
"""
from __future__ import annotations

import httpx
import pytest
from pydantic import ValidationError

from agent_control.config import AppSettings, DecisionConfig
from agent_control.llm.classifier import CLASSIFIER_SYSTEM_PROMPT
from agent_control.llm.decision import (
    ROUTE_DESCRIPTIONS,
    ROUTE_QUESTION,
    DecisionModelClassifier,
    DecisionModelClient,
    DecisionModelError,
    DecisionOpinion,
    build_decision_classifier,
)
from agent_control.schemas import (
    AuditEventType,
    ChannelType,
    InboundMessage,
    IntentRoute,
    MessageClassification,
    MessageKind,
    OrchestrationIntent,
    TaskType,
)
from helpers import make_repos


def _message(text: str | None) -> InboundMessage:
    return InboundMessage(channel=ChannelType.TELEGRAM, kind=MessageKind.TEXT, sender_id="1", chat_id="1", text=text)


def _opinion(route: IntentRoute, confidence: float) -> DecisionOpinion:
    return DecisionOpinion(route, confidence, {route.value: confidence, "conversation": 1 - confidence}, 12.0)


class _Client:
    def __init__(self, opinion: DecisionOpinion | None = None, error: Exception | None = None) -> None:
        self.opinion, self.error, self.calls = opinion, error, 0

    async def route_of(self, text: str) -> DecisionOpinion:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.opinion


class _Concierge:
    """Stands in for the LLM concierge and says what it was asked."""

    def __init__(self, route: IntentRoute = IntentRoute.FILESYSTEM_MANAGE) -> None:
        self.route, self.calls = route, 0

    async def classify(self, message, context=None):
        self.calls += 1
        is_task = self.route != IntentRoute.CONVERSATION
        return MessageClassification(
            is_task=is_task, task_type=TaskType.OTHER, confidence=0.97, reason="concierge",
            intent=OrchestrationIntent(route=self.route, reasoning="concierge", objective="x"),
            reply=None if is_task else "hi there",
        )


def _make(mode, client, concierge=None, tmp_path=None, **config):
    concierge = concierge or _Concierge()
    audit = make_repos(tmp_path)[1] if tmp_path is not None else None
    classifier = DecisionModelClassifier(concierge, DecisionConfig(mode=mode, **config), audit, client)
    return classifier, concierge


def _events(tmp_path):
    repos, _ = make_repos(tmp_path)  # same database file as the classifier's audit logger
    return [e for e in repos.audit.list_recent(50) if e.type == AuditEventType.INTAKE_DECISION]


# --- modes ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_off_is_the_concierge_alone() -> None:
    client = _Client(_opinion(IntentRoute.FILESYSTEM_MANAGE, 0.99))
    classifier, concierge = _make("off", client)

    result = await classifier.classify(_message("what's in Downloads?"))

    assert concierge.calls == 1 and client.calls == 0
    assert result.reason == "concierge"


def test_build_returns_the_concierge_itself_when_off() -> None:
    concierge = _Concierge()
    assert build_decision_classifier(concierge, DecisionConfig(mode="off"), None) is concierge
    assert isinstance(build_decision_classifier(concierge, DecisionConfig(mode="shadow"), None), DecisionModelClassifier)


@pytest.mark.asyncio
async def test_shadow_uses_the_concierge_and_records_whether_they_agree(tmp_path) -> None:
    classifier, concierge = _make("shadow", _Client(_opinion(IntentRoute.FILESYSTEM_MANAGE, 0.95)), tmp_path=tmp_path)

    result = await classifier.classify(_message("what's in Downloads?"))

    assert result.reason == "concierge" and concierge.calls == 1
    [event] = _events(tmp_path)
    assert event.payload["mode"] == "shadow" and event.payload["used"] is False
    assert event.payload["decision_route"] == "filesystem.manage" and event.payload["final_route"] == "filesystem.manage"
    assert event.payload["agrees"] is True and event.payload["decision_confidence"] == 0.95


@pytest.mark.asyncio
async def test_shadow_records_a_disagreement(tmp_path) -> None:
    classifier, _ = _make(
        "shadow", _Client(_opinion(IntentRoute.CONVERSATION, 0.9)), _Concierge(IntentRoute.FILESYSTEM_MANAGE), tmp_path
    )
    await classifier.classify(_message("what's in Downloads?"))
    assert _events(tmp_path)[0].payload["agrees"] is False


@pytest.mark.asyncio
async def test_shadow_treats_a_concierge_chat_answer_as_the_conversation_route(tmp_path) -> None:
    classifier, _ = _make(
        "shadow", _Client(_opinion(IntentRoute.CONVERSATION, 0.9)), _Concierge(IntentRoute.CONVERSATION), tmp_path
    )
    await classifier.classify(_message("hello"))
    assert _events(tmp_path)[0].payload["agrees"] is True


@pytest.mark.asyncio
async def test_shadow_survives_a_broken_decision_model(tmp_path) -> None:
    classifier, concierge = _make("shadow", _Client(error=DecisionModelError("connection refused")), tmp_path=tmp_path)

    result = await classifier.classify(_message("what's in Downloads?"))

    assert result.reason == "concierge" and concierge.calls == 1
    event = _events(tmp_path)[0]
    assert event.payload["decision_route"] is None and "connection refused" in event.payload["decision_error"]
    assert event.payload["agrees"] is None


# --- cascade ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_cascade_decides_alone_when_confident_about_an_allowed_route(tmp_path) -> None:
    classifier, concierge = _make("cascade", _Client(_opinion(IntentRoute.FILESYSTEM_MANAGE, 0.97)), tmp_path=tmp_path)

    result = await classifier.classify(_message("what's in my Downloads folder?"))

    assert concierge.calls == 0
    assert result.is_task is True and result.intent.route == IntentRoute.FILESYSTEM_MANAGE
    assert result.normalized_objective == "what's in my Downloads folder?"
    assert result.intent.objective == "what's in my Downloads folder?"
    assert result.confidence == 0.97 and "decision model" in result.reason
    assert _events(tmp_path)[0].payload["used"] is True


@pytest.mark.asyncio
async def test_cascade_marks_desktop_observation_with_its_task_type() -> None:
    classifier, _ = _make("cascade", _Client(_opinion(IntentRoute.DESKTOP_OBSERVE, 0.99)))
    result = await classifier.classify(_message("take a screenshot"))
    assert result.task_type == TaskType.DESKTOP_OBSERVATION


@pytest.mark.asyncio
async def test_cascade_hands_an_unsure_answer_to_the_concierge() -> None:
    client = _Client(_opinion(IntentRoute.FILESYSTEM_MANAGE, 0.62))
    classifier, concierge = _make("cascade", client)
    result = await classifier.classify(_message("do the thing with the files"))
    assert concierge.calls == 1 and result.reason == "concierge"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "route",
    [IntentRoute.CONVERSATION, IntentRoute.STATUS, IntentRoute.CONFIGURATION, IntentRoute.CODING_AGENT,
     IntentRoute.SCHEDULE_MANAGE, IntentRoute.ARTIFACT_DELIVERY, IntentRoute.COMPUTER_USE, IntentRoute.BROWSER_CONTROL],
)
async def test_cascade_never_decides_routes_that_need_the_concierge(route) -> None:
    """Chat and status need the concierge's reply; a provider, schedule or delivery kind comes from it too."""
    classifier, concierge = _make("cascade", _Client(_opinion(route, 0.99)))
    await classifier.classify(_message("anything"))
    assert concierge.calls == 1


@pytest.mark.asyncio
async def test_cascade_honours_a_custom_threshold_and_route_list() -> None:
    client = _Client(_opinion(IntentRoute.SCHEDULE_MANAGE, 0.8))
    classifier, concierge = _make("cascade", client, min_confidence=0.75, cascade_routes=[IntentRoute.SCHEDULE_MANAGE])
    result = await classifier.classify(_message("remind me tomorrow"))
    assert concierge.calls == 0 and result.intent.route == IntentRoute.SCHEDULE_MANAGE


@pytest.mark.asyncio
async def test_empty_text_goes_straight_to_the_concierge() -> None:
    client = _Client(_opinion(IntentRoute.FILESYSTEM_MANAGE, 0.99))
    classifier, concierge = _make("cascade", client)
    await classifier.classify(_message("   "))
    assert client.calls == 0 and concierge.calls == 1


@pytest.mark.asyncio
async def test_a_failure_falls_back_then_pauses_the_decision_model_for_the_cooldown(monkeypatch) -> None:
    client = _Client(error=DecisionModelError("timed out"))
    classifier, concierge = _make("cascade", client, cooldown_seconds=60)

    await classifier.classify(_message("first"))
    await classifier.classify(_message("second"))
    assert client.calls == 1 and concierge.calls == 2  # the second message did not wait on a dead model

    classifier._down_until = 0.0  # the cooldown has passed
    await classifier.classify(_message("third"))
    assert client.calls == 2


@pytest.mark.asyncio
async def test_an_unexpected_bug_in_the_client_never_costs_the_user_their_message() -> None:
    classifier, concierge = _make("cascade", _Client(error=RuntimeError("bug")))
    result = await classifier.classify(_message("what's in Downloads?"))
    assert concierge.calls == 1 and result.reason == "concierge"


# --- the HTTP client --------------------------------------------------------


def _client(handler, **config) -> DecisionModelClient:
    return DecisionModelClient(DecisionConfig(mode="cascade", **config), transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_client_posts_the_route_question_and_reads_the_answer(monkeypatch) -> None:
    monkeypatch.setenv("LD_TOKEN", "s3cret")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen["url"], seen["auth"], seen["body"] = str(request.url), request.headers.get("authorization"), json.loads(request.content)
        return httpx.Response(200, json={"answers": {"route": {
            "choice": "filesystem.manage", "confidence": 0.91,
            "probabilities": {"filesystem.manage": 0.91, "conversation": 0.05},
        }}})

    opinion = await _client(handler, profile="d1_3b_decision", api_token_env="LD_TOKEN").route_of("list my downloads")

    assert seen["url"] == "http://127.0.0.1:8000/v1/systemone" and seen["auth"] == "Bearer s3cret"
    assert seen["body"]["model"] == "d1_3b_decision" and seen["body"]["state"] == "list my downloads"
    assert seen["body"]["questions"]["route"] == ROUTE_QUESTION
    assert opinion.route == IntentRoute.FILESYSTEM_MANAGE and opinion.confidence == 0.91


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response, fragment",
    [
        (httpx.Response(503, text="model not loaded"), "HTTP 503"),
        (httpx.Response(200, json={"answers": {}}), "unusable"),
        (httpx.Response(200, json={"answers": {"route": {"choice": "teleport", "confidence": 0.9}}}), "unusable"),
    ],
)
async def test_client_errors_are_one_kind_of_error(response, fragment) -> None:
    with pytest.raises(DecisionModelError, match=fragment):
        await _client(lambda request: response).route_of("hi")


@pytest.mark.asyncio
async def test_client_reports_an_unreachable_server() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(DecisionModelError, match="unreachable"):
        await _client(refuse).route_of("hi")


# --- the route table and the config ------------------------------------------


def test_every_route_is_described_and_named_in_the_concierge_prompt() -> None:
    """The decision model and the concierge must be choosing from the same routes."""
    expected = set(IntentRoute) - {IntentRoute.UNKNOWN}
    assert set(ROUTE_DESCRIPTIONS) == expected
    assert set(ROUTE_QUESTION["criteria"]) == {route.value for route in expected}
    for route in expected:
        assert f"`{route.value}`" in CLASSIFIER_SYSTEM_PROMPT, f"{route.value} is not in the concierge prompt"


def test_the_decision_model_is_off_by_default_and_local_only() -> None:
    assert AppSettings().decision.mode == "off"
    with pytest.raises(ValidationError, match="loopback"):
        DecisionConfig(base_url="http://example.com:8000")
    assert DecisionConfig(base_url="http://localhost:8000/").base_url == "http://localhost:8000"


def test_a_misspelled_route_in_the_config_is_rejected() -> None:
    with pytest.raises(ValidationError):
        DecisionConfig(cascade_routes=["filesystem.manager"])
