"""Choosing the message classifier from the console (admin /api/config/decision) and the
intake paths that follow that choice. No model runs here: the decision model client and the
LLM concierge are replaced by fakes."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
import yaml

import agent_control.admin as admin_module
import agent_control.llm.classifier as classifier_module
import agent_control.llm.providers as providers_module
from agent_control.admin import create_admin_router
from agent_control.channels.base import classify_and_spawn_task
from agent_control.config import AppSettings, DecisionConfig
from agent_control.llm.decision import DecisionModelClassifier, DecisionModelClient, DecisionModelError, DecisionOpinion
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
from agent_control.storage import AuditLogger, Database, Repositories
from agent_control.tools.vscode_bridge import VSCodeBridgeStore


def _client(tmp_path, monkeypatch) -> tuple[TestClient, Repositories]:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AGENT_ADMIN_TOKEN", raising=False)
    database = Database(f"sqlite:///{tmp_path / 'admin.db'}")
    database.initialize()
    repositories = Repositories.for_database(database)
    app = FastAPI()
    app.include_router(create_admin_router(lambda: AppSettings(_env_file=None), lambda: repositories, VSCodeBridgeStore()))
    return TestClient(app), repositories


def _opinion(route: IntentRoute, confidence: float) -> DecisionOpinion:
    return DecisionOpinion(route, confidence, {route.value: confidence, "conversation": 1 - confidence}, 640.0)


def test_the_console_reads_the_classifier_choice_and_the_route_list(tmp_path, monkeypatch) -> None:
    client, _ = _client(tmp_path, monkeypatch)

    body = client.get("/admin/api/config/decision").json()

    assert body["decision"]["mode"] == "off"
    assert len(body["routes"]) == 15 and "unknown" not in body["routes"]
    assert body["concierge_only_routes"] == ["coding.agent"]
    assert body["stats"]["messages"] == 0 and body["stats"]["agreement"] is None


def test_the_console_saves_the_classifier_choice_to_config_yaml(tmp_path, monkeypatch) -> None:
    client, _ = _client(tmp_path, monkeypatch)

    response = client.post(
        "/admin/api/config/decision",
        json={"mode": "decision", "profile": "d3_lite_decision", "min_confidence": 0.85},
    )

    assert response.status_code == 200
    saved = yaml.safe_load((tmp_path / "config" / "config.yaml").read_text(encoding="utf-8"))
    assert saved["decision"] == {"mode": "decision", "profile": "d3_lite_decision", "min_confidence": 0.85}
    body = client.get("/admin/api/config/decision").json()
    assert body["decision"]["mode"] == "decision" and body["decision"]["min_confidence"] == 0.85

    # Only what is sent changes; a blank profile means LocalDeploy's default decision profile.
    client.post("/admin/api/config/decision", json={"mode": "shadow", "profile": " "})
    saved = yaml.safe_load((tmp_path / "config" / "config.yaml").read_text(encoding="utf-8"))
    assert saved["decision"] == {"mode": "shadow", "profile": None, "min_confidence": 0.85}


def test_the_console_cannot_point_the_decision_model_off_this_machine(tmp_path, monkeypatch) -> None:
    client, _ = _client(tmp_path, monkeypatch)

    response = client.post("/admin/api/config/decision", json={"mode": "shadow", "base_url": "http://example.com:8000"})

    assert response.status_code == 400 and "loopback" in response.json()["detail"]
    assert not (tmp_path / "config" / "config.yaml").exists()
    assert client.post("/admin/api/config/decision", json={"mode": "auto"}).status_code == 422
    assert client.post("/admin/api/config/decision", json={"mode": "cascade", "cascade_routes": ["files"]}).status_code == 422


def test_the_test_call_reports_the_route_and_whether_it_would_be_used(tmp_path, monkeypatch) -> None:
    client, _ = _client(tmp_path, monkeypatch)

    async def route_of(self, text):
        return _opinion(IntentRoute.FILESYSTEM_MANAGE, 0.97)

    monkeypatch.setattr(DecisionModelClient, "route_of", route_of)
    off = client.post("/admin/api/config/decision/test", json={"text": "what's in Downloads?"}).json()
    assert off["decision"]["route"] == "filesystem.manage" and off["decision"]["latency_ms"] == 640
    assert off["decision"]["decides_alone"] is False  # mode is off
    assert "llm" not in off

    client.post("/admin/api/config/decision", json={"mode": "decision"})
    on = client.post("/admin/api/config/decision/test", json={"text": "what's in Downloads?"}).json()
    assert on["mode"] == "decision" and on["decision"]["decides_alone"] is True


def test_the_test_call_says_when_the_decision_model_is_unreachable(tmp_path, monkeypatch) -> None:
    client, _ = _client(tmp_path, monkeypatch)

    async def route_of(self, text):
        raise DecisionModelError("decision model unreachable: ConnectError: refused")

    monkeypatch.setattr(DecisionModelClient, "route_of", route_of)
    body = client.post("/admin/api/config/decision/test", json={"text": "hi"}).json()
    assert "unreachable" in body["decision"]["error"]


def test_the_console_shows_agreement_from_recorded_decisions(tmp_path, monkeypatch) -> None:
    client, repositories = _client(tmp_path, monkeypatch)
    audit = AuditLogger(repositories.audit, [])
    for decision_route, final_route in (("browser.open", "browser.open"), ("conversation", "filesystem.manage")):
        audit.append(
            AuditEventType.INTAKE_DECISION,
            actor="decision_model",
            payload={"used": False, "decision_route": decision_route, "final_route": final_route,
                     "agrees": decision_route == final_route, "decision_ms": 600.0, "text": "x"},
        )

    stats = client.get("/admin/api/config/decision").json()["stats"]

    assert stats["compared"] == 2 and stats["agreement"] == 0.5 and stats["median_ms"] == 600


# --- the paths that act on the choice -------------------------------------------


class _Concierge:
    def __init__(self, *args, **kwargs) -> None:
        _Concierge.calls = 0

    async def classify(self, message, context=None):
        _Concierge.calls += 1
        return MessageClassification(
            is_task=False, task_type=TaskType.OTHER, confidence=0.97, reason="concierge", reply="Hello!",
            intent=OrchestrationIntent(route=IntentRoute.CONVERSATION, reasoning="chat", objective="hi"),
        )


@pytest.fixture
def web_concierge(monkeypatch):
    monkeypatch.setattr(providers_module, "build_role_llm_provider", lambda settings, role: object())
    monkeypatch.setattr(classifier_module, "LLMMessageClassifier", _Concierge)

    def use(route: IntentRoute, mode: str = "decision"):
        async def route_of(self, text):
            return _opinion(route, 0.99)

        monkeypatch.setattr(DecisionModelClient, "route_of", route_of)
        return AppSettings(_env_file=None, decision=DecisionConfig(mode=mode))

    return use


@pytest.mark.asyncio
async def test_web_chat_skips_the_llm_when_the_decision_model_is_sure_it_is_a_task(tmp_path, web_concierge) -> None:
    settings = web_concierge(IntentRoute.FILESYSTEM_MANAGE)
    database = Database(f"sqlite:///{tmp_path / 'web.db'}")
    database.initialize()
    repositories = Repositories.for_database(database)

    reply, _ = await admin_module._web_concierge(settings, "list my Downloads", repositories, None)

    assert reply is None and _Concierge.calls == 0  # the caller creates the task straight away


@pytest.mark.asyncio
async def test_web_chat_still_asks_the_llm_for_a_chat_reply(tmp_path, web_concierge) -> None:
    settings = web_concierge(IntentRoute.CONVERSATION)
    database = Database(f"sqlite:///{tmp_path / 'web.db'}")
    database.initialize()
    repositories = Repositories.for_database(database)

    reply, _ = await admin_module._web_concierge(settings, "hi", repositories, None)

    assert reply == "Hello!" and _Concierge.calls == 1


class _Responder:
    def __init__(self) -> None:
        self.calls = 0

    async def answer(self, inbound, conversation_id):
        self.calls += 1
        return "Hi! What can I do for you?"


class _Client:
    def __init__(self, opinion: DecisionOpinion) -> None:
        self.opinion = opinion

    async def route_of(self, text):
        return self.opinion


async def _no_progress(chat_id: str, text: str) -> None:
    return None


@pytest.mark.asyncio
async def test_a_chat_message_decided_by_the_decision_model_is_answered_by_the_responder(tmp_path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'intake.db'}")
    database.initialize()
    repositories = Repositories.for_database(database)
    audit = AuditLogger(repositories.audit, [])
    concierge, responder = _Concierge(), _Responder()
    classifier = DecisionModelClassifier(
        concierge, DecisionConfig(mode="decision"), audit, _Client(_opinion(IntentRoute.CONVERSATION, 0.99))
    )
    inbound = InboundMessage(channel=ChannelType.TELEGRAM, kind=MessageKind.TEXT, sender_id="1", chat_id="1", text="hey")
    conversation_id = repositories.conversations.get_or_create(ChannelType.TELEGRAM, "1")

    result = await classify_and_spawn_task(
        inbound, conversation_id, repositories=repositories, audit=audit, classifier=classifier,
        responder=responder, send_progress=_no_progress,
    )

    assert result.task is None and _Concierge.calls == 0 and responder.calls == 1
    assert result.outbound_message.text == "Hi! What can I do for you?"


@pytest.mark.asyncio
async def test_a_status_question_decided_by_the_decision_model_reaches_the_status_path(tmp_path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'intake.db'}")
    database.initialize()
    repositories = Repositories.for_database(database)
    audit = AuditLogger(repositories.audit, [])
    classifier = DecisionModelClassifier(
        _Concierge(), DecisionConfig(mode="decision"), audit, _Client(_opinion(IntentRoute.STATUS, 0.97))
    )
    inbound = InboundMessage(
        channel=ChannelType.TELEGRAM, kind=MessageKind.TEXT, sender_id="1", chat_id="1", text="what are you doing?"
    )
    conversation_id = repositories.conversations.get_or_create(ChannelType.TELEGRAM, "1")

    result = await classify_and_spawn_task(
        inbound, conversation_id, repositories=repositories, audit=audit, classifier=classifier,
        responder=_Responder(), send_progress=_no_progress,
    )

    # The same shape the concierge gives a status question: a status_request task.
    assert result.task is not None and result.task.metadata["task_type"] == TaskType.STATUS_REQUEST.value


# --- ybm doctor ---------------------------------------------------------------


class _Listing:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self) -> bytes:
        return self.body


def _listing(*profiles: tuple[str, str]) -> bytes:
    import json

    return json.dumps(
        {"data": [{"id": name, "localdeploy": {"kind": kind}} for name, kind in profiles]}
    ).encode("utf-8")


def _settings(mode: str, profile: str | None = None) -> AppSettings:
    return AppSettings(_env_file=None, decision=DecisionConfig(mode=mode, profile=profile))


def test_doctor_says_nothing_to_check_when_the_decision_model_is_off() -> None:
    from agent_control.bootstrap import check_decision_model

    check = check_decision_model(_settings("off"))
    assert check.status == "ok" and "off" in check.detail


def test_doctor_finds_the_enabled_decision_profile(monkeypatch) -> None:
    import agent_control.bootstrap as bootstrap

    monkeypatch.setattr(bootstrap, "urlopen", lambda request, timeout: _Listing(_listing(("qwen", "chat"), ("d3_lite_decision", "decision"))))
    assert bootstrap.check_decision_model(_settings("decision")).status == "ok"
    assert bootstrap.check_decision_model(_settings("shadow", "d3_lite_decision")).status == "ok"

    check = bootstrap.check_decision_model(_settings("cascade", "d3_nano_decision"))
    assert check.status == "warn" and "d3_nano_decision" in check.detail and "d3_lite_decision" in check.detail


def test_doctor_warns_when_localdeploy_has_no_decision_profile_or_is_down(monkeypatch) -> None:
    import agent_control.bootstrap as bootstrap
    from urllib.error import URLError

    monkeypatch.setattr(bootstrap, "urlopen", lambda request, timeout: _Listing(_listing(("qwen", "chat"))))
    check = bootstrap.check_decision_model(_settings("shadow"))
    assert check.status == "warn" and "d3_lite_decision" in check.detail

    def down(request, timeout):
        raise URLError("refused")

    monkeypatch.setattr(bootstrap, "urlopen", down)
    check = bootstrap.check_decision_model(_settings("decision"))
    assert check.status == "warn" and "not reachable" in check.detail
