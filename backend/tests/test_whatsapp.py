from __future__ import annotations

from datetime import timedelta

import pytest

from agent_control.channels.base import ACKNOWLEDGMENT_TEXT, TASK_STARTED_TEXT
from agent_control.channels.whatsapp import WhatsAppAdapter, WhatsAppIntakeService, WhatsAppPollingRunner
from agent_control.config import WhatsAppConfig
from agent_control.llm import StaticMessageClassifier
from agent_control.schemas import (
    ApprovalRequest,
    ApprovalStatus,
    AuditEventType,
    Capability,
    ChannelType,
    RiskLevel,
    TaskStatus,
    TaskType,
    utc_now,
)
from agent_control.storage import AuditLogger, Database, Repositories


NUMBER = "15551234567"
JID = f"{NUMBER}@s.whatsapp.net"


def _service(
    tmp_path, config: WhatsAppConfig, classifier=None, bridge_client=None
) -> tuple[WhatsAppIntakeService, Repositories, AuditLogger]:
    database = Database(f"sqlite:///{tmp_path / 'agent.db'}")
    database.initialize()
    repos = Repositories.for_database(database)
    audit = AuditLogger(repos.audit)
    adapter = WhatsAppAdapter(config, audit)
    service = WhatsAppIntakeService(adapter, repos, audit, classifier=classifier, bridge_client=bridge_client)
    return service, repos, audit


def _update(text: str, message_id: str = "1") -> dict:
    return {"id": message_id, "from": JID, "chat_id": JID, "text": text}


def test_whatsapp_adapter_denies_when_disabled(tmp_path) -> None:
    service, _repos, audit = _service(tmp_path, WhatsAppConfig(enabled=False, allowed_numbers=[NUMBER]))

    result = service.adapter.normalize_update(_update("hello"))

    assert result.authorized is False
    assert result.inbound_message is None
    events = audit.repository.list_by_type(AuditEventType.CHANNEL_ACCESS_DECISION)
    assert events[-1].payload["reason"] == "whatsapp_disabled"


def test_whatsapp_adapter_denies_when_allowlist_empty(tmp_path) -> None:
    service, repos, _audit = _service(tmp_path, WhatsAppConfig(enabled=True, allowed_numbers=[]))

    result = service.adapter.normalize_update(_update("hello"))

    assert result.authorized is False
    events = repos.audit.list_by_type(AuditEventType.CHANNEL_ACCESS_DECISION)
    assert len(events) == 1
    assert events[0].payload["reason"] == "allowlist_empty"
    assert events[0].payload["channel"] == "whatsapp"


def test_whatsapp_adapter_denies_a_number_not_on_the_allowlist(tmp_path) -> None:
    service, repos, _audit = _service(tmp_path, WhatsAppConfig(enabled=True, allowed_numbers=["19998887777"]))

    result = service.adapter.normalize_update(_update("hello"))

    assert result.authorized is False
    events = repos.audit.list_by_type(AuditEventType.CHANNEL_ACCESS_DECISION)
    assert events[-1].payload["reason"] == "number_not_allowed"
    assert events[-1].payload["sender_number"] == NUMBER


def test_whatsapp_adapter_labels_a_lid_addressed_sender_distinctly(tmp_path) -> None:
    """A LID-addressed sender's "number" is an opaque id, not a real phone
    number - it can never be added to allowed_numbers, so the denial
    reason must say so distinctly rather than reading like an ordinary
    "add this number to the allowlist" case."""
    service, repos, _audit = _service(tmp_path, WhatsAppConfig(enabled=True, allowed_numbers=[NUMBER]))
    lid_jid = "123456789@lid"
    update = {"id": "1", "from": lid_jid, "chat_id": lid_jid, "text": "hello", "is_lid": True}

    result = service.adapter.normalize_update(update)

    assert result.authorized is False
    events = repos.audit.list_by_type(AuditEventType.CHANNEL_ACCESS_DECISION)
    assert events[-1].payload["reason"] == "lid_jid_no_resolvable_number"


def test_whatsapp_adapter_allows_a_listed_number(tmp_path) -> None:
    service, _repos, _audit = _service(tmp_path, WhatsAppConfig(enabled=True, allowed_numbers=[NUMBER]))

    result = service.adapter.normalize_update(_update("hello"))

    assert result.authorized is True
    assert result.inbound_message is not None
    assert result.inbound_message.channel == ChannelType.WHATSAPP
    assert result.inbound_message.sender_id == NUMBER
    assert result.inbound_message.text == "hello"


@pytest.mark.asyncio
async def test_whatsapp_text_update_creates_task(tmp_path) -> None:
    service, repos, _audit = _service(
        tmp_path,
        WhatsAppConfig(enabled=True, allowed_numbers=[NUMBER]),
        classifier=StaticMessageClassifier(),
    )

    result = await service.handle_update_async(_update("Build a todo app"))

    assert result.authorized is True
    assert result.task is not None
    assert result.task.objective == "Build a todo app"
    assert result.task.metadata["task_type"] == TaskType.DEVELOPMENT.value
    assert result.task.metadata["source_channel"] == ChannelType.WHATSAPP.value
    assert result.task.metadata["source_chat_id"] == JID
    assert repos.tasks.get(result.task.id) is not None


class FakeBridgeClientForProgress:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send_message(self, chat_id: str, text: str) -> dict:
        self.sent.append((chat_id, text))
        return {"ok": True}


@pytest.mark.asyncio
async def test_whatsapp_skips_the_pre_classification_acknowledgment_but_sends_task_started(tmp_path) -> None:
    """docs/archive/UI_UX_AUDIT.md Phase 16 review: WhatsApp is an unofficial
    client (Baileys) with real account-flagging risk, unlike Telegram's
    official bot API - the pure-filler "got your message" ping is skipped
    to trim outbound volume, but "task started" is still sent since a real
    task can take minutes and silence until completion would be worse."""
    bridge_client = FakeBridgeClientForProgress()
    service, _repos, _audit = _service(
        tmp_path,
        WhatsAppConfig(enabled=True, allowed_numbers=[NUMBER]),
        classifier=StaticMessageClassifier(),
        bridge_client=bridge_client,
    )

    result = await service.handle_update_async(_update("Build a todo app"))

    assert result.task is not None
    sent_texts = [text for _chat_id, text in bridge_client.sent]
    assert ACKNOWLEDGMENT_TEXT not in sent_texts
    assert TASK_STARTED_TEXT in sent_texts


@pytest.mark.asyncio
async def test_whatsapp_reply_resumes_a_clarifying_task_instead_of_spawning_a_new_one(tmp_path) -> None:
    service, repos, _audit = _service(
        tmp_path,
        WhatsAppConfig(enabled=True, allowed_numbers=[NUMBER]),
        classifier=StaticMessageClassifier(),
    )
    created = await service.handle_update_async(_update("organize my files", message_id="1"))
    task = created.task
    assert task is not None
    repos.tasks.update_metadata(
        task.id, {**task.metadata, "clarifying_question": "Which folder?"}, TaskStatus.CLARIFYING,
    )

    reply = await service.handle_update_async(_update("the Downloads folder", message_id="2"))

    assert reply.task is None
    assert reply.outbound_message is not None
    assert "resuming" in reply.outbound_message.text.lower()
    resumed = repos.tasks.get(task.id)
    assert resumed.status == TaskStatus.RECEIVED
    assert len(repos.tasks.list_recent(limit=10)) == 1


@pytest.mark.asyncio
async def test_whatsapp_plain_status_command_is_deterministic_no_llm(tmp_path) -> None:
    service, _repos, _audit = _service(tmp_path, WhatsAppConfig(enabled=True, allowed_numbers=[NUMBER]))

    result = await service.handle_update_async(_update("status"))

    assert result.outbound_message is not None
    assert "recent task" in result.outbound_message.text


@pytest.mark.asyncio
async def test_whatsapp_plain_approve_approves_latest_pending_task(tmp_path) -> None:
    service, repos, _audit = _service(tmp_path, WhatsAppConfig(enabled=True, allowed_numbers=[NUMBER]))
    conversation_id = repos.conversations.get_or_create(ChannelType.WHATSAPP, JID)
    task = repos.tasks.create("Run gated task", conversation_id=conversation_id, metadata={"source_chat_id": JID})
    repos.tasks.update_metadata(task.id, task.metadata, TaskStatus.AWAITING_APPROVAL)
    approval = repos.approvals.create(
        ApprovalRequest(
            task_id=task.id,
            capability=Capability.DESKTOP_CONTROL,
            risk_level=RiskLevel.CRITICAL,
            summary="Approve desktop control",
            expires_at=utc_now() + timedelta(minutes=15),
        )
    )

    result = await service.handle_update_async(_update("approve"))

    updated = repos.approvals.list_for_task(task.id)[0]
    assert updated.id == approval.id
    assert updated.status == ApprovalStatus.APPROVED
    assert result.outbound_message is not None
    assert repos.tasks.get(task.id).status == TaskStatus.RUNNING


class FakeWhatsAppBridgeClient:
    def __init__(self, updates: list[dict]) -> None:
        self.updates = updates
        self.sent: list[tuple[str, str]] = []

    async def get_updates(self, offset: int = 0) -> tuple[list[dict], int]:
        return self.updates, offset + len(self.updates)

    async def send_message(self, chat_id: str, text: str) -> dict:
        self.sent.append((chat_id, text))
        return {"ok": True}


@pytest.mark.asyncio
async def test_whatsapp_polling_runner_sends_outbound_command_response(tmp_path) -> None:
    service, _repos, _audit = _service(tmp_path, WhatsAppConfig(enabled=True, allowed_numbers=[NUMBER]))
    client = FakeWhatsAppBridgeClient([_update("status")])
    runner = WhatsAppPollingRunner(client, service)  # type: ignore[arg-type]

    next_offset, results = await runner.poll_once(offset=0)

    assert next_offset == 1
    assert len(results) == 1
    assert client.sent == [(JID, results[0].outbound_message.text)]


@pytest.mark.asyncio
async def test_whatsapp_polling_runner_advances_offset_after_update_processing_error(tmp_path) -> None:
    service, _repos, audit = _service(tmp_path, WhatsAppConfig(enabled=True, allowed_numbers=[NUMBER]))

    class BrokenIntake:
        def __init__(self) -> None:
            self.audit = audit

        async def handle_update_async(self, update: dict) -> None:
            raise RuntimeError("boom")

    client = FakeWhatsAppBridgeClient([_update("status")])
    runner = WhatsAppPollingRunner(client, BrokenIntake())  # type: ignore[arg-type]

    next_offset, results = await runner.poll_once(offset=5)

    assert next_offset == 6
    assert results == []
    errors = audit.repository.list_by_type(AuditEventType.ERROR)
    assert any(e.payload.get("error") == "update_processing_failed" for e in errors)
