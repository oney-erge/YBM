from __future__ import annotations

from datetime import timedelta

import httpx
import pytest

from agent_control.channels.telegram import TelegramAdapter, TelegramBotApi, TelegramIntakeService, TelegramPollingRunner
from agent_control.channels.memory import ConversationMemoryService
from agent_control.channels.responder import StaticChatResponder
from agent_control.config import AppSettings, CapabilityPolicy, DesktopAdapterConfig, StorageConfig, TelegramConfig
from agent_control.llm import LLMMessageClassifier, StaticMessageClassifier
from agent_control.observation import ArtifactService, ScreenshotService
from agent_control.schemas import ApprovalRequest, ApprovalStatus, AuditEventType, ChannelType, MemorySource, MessageClassification, TaskStatus, TaskType
from agent_control.schemas import Capability, RiskLevel
from agent_control.schemas import utc_now
from agent_control.storage import AuditLogger, Database, Repositories


def _service(
    tmp_path,
    config: TelegramConfig,
    settings: AppSettings | None = None,
    screenshot_service: ScreenshotService | None = None,
    classifier=None,
    memory_service: ConversationMemoryService | None = None,
    bot_api=None,
) -> tuple[TelegramIntakeService, Repositories]:
    database = Database(f"sqlite:///{tmp_path / 'agent.db'}")
    database.initialize()
    repos = Repositories.for_database(database)
    audit = AuditLogger(repos.audit)
    adapter = TelegramAdapter(config, audit)
    return TelegramIntakeService(
        adapter,
        repos,
        audit,
        settings=settings,
        bot_api=bot_api,
        screenshot_service=screenshot_service,
        classifier=classifier,
        memory_service=memory_service,
    ), repos


def test_telegram_text_update_creates_task(tmp_path) -> None:
    service, repos = _service(
        tmp_path,
        TelegramConfig(enabled=True, allowed_user_ids=[42], allowed_chat_ids=[100]),
        classifier=StaticMessageClassifier(),
    )

    result = service.handle_update(
        {
            "message": {
                "message_id": 1,
                "from": {"id": 42},
                "chat": {"id": 100},
                "text": "Build a todo app",
            }
        }
    )

    assert result.authorized is True
    assert result.task is not None
    assert result.task.objective == "Build a todo app"
    assert result.task.metadata["task_type"] == TaskType.DEVELOPMENT.value
    assert result.outbound_message is None
    assert repos.tasks.get(result.task.id) is not None


def test_telegram_reply_resumes_a_clarifying_task_instead_of_spawning_a_new_one(tmp_path) -> None:
    """clarification.py's resume_clarifying_task, called from
    _resume_clarifying_task - this file's own regression coverage for the
    logic that moved out of this module during the web-chat sharing
    refactor (admin.py gained the same behavior; this locks in Telegram's
    unchanged)."""
    service, repos = _service(
        tmp_path,
        TelegramConfig(enabled=True, allowed_user_ids=[42], allowed_chat_ids=[100]),
        classifier=StaticMessageClassifier(),
    )
    created = service.handle_update(
        {
            "message": {
                "message_id": 1,
                "from": {"id": 42},
                "chat": {"id": 100},
                "text": "organize my files",
            }
        }
    )
    task = created.task
    assert task is not None
    repos.tasks.update_metadata(
        task.id, {**task.metadata, "clarifying_question": "Which folder?"}, TaskStatus.CLARIFYING,
    )

    reply = service.handle_update(
        {
            "message": {
                "message_id": 2,
                "from": {"id": 42},
                "chat": {"id": 100},
                "text": "the Downloads folder",
            }
        }
    )

    assert reply.task is None  # resumed the existing task, did not spawn a new one
    assert reply.outbound_message is not None
    assert "resuming" in reply.outbound_message.text.lower()
    resumed = repos.tasks.get(task.id)
    assert resumed.status == TaskStatus.RECEIVED
    assert "[User clarification: the Downloads folder]" in resumed.objective
    assert len(repos.tasks.list_recent(limit=10)) == 1


def test_telegram_reply_cancel_word_cancels_the_clarifying_task(tmp_path) -> None:
    service, repos = _service(
        tmp_path,
        TelegramConfig(enabled=True, allowed_user_ids=[42], allowed_chat_ids=[100]),
        classifier=StaticMessageClassifier(),
    )
    created = service.handle_update(
        {
            "message": {
                "message_id": 1,
                "from": {"id": 42},
                "chat": {"id": 100},
                "text": "organize my files",
            }
        }
    )
    task = created.task
    assert task is not None
    repos.tasks.update_metadata(
        task.id, {**task.metadata, "clarifying_question": "Which folder?"}, TaskStatus.CLARIFYING,
    )

    reply = service.handle_update(
        {
            "message": {
                "message_id": 2,
                "from": {"id": 42},
                "chat": {"id": 100},
                "text": "never mind",
            }
        }
    )

    assert "cancelled" in reply.outbound_message.text.lower()
    assert repos.tasks.get(task.id).status == TaskStatus.CANCELLED


@pytest.mark.asyncio
async def test_telegram_intake_sends_user_facing_progress_without_task_id(tmp_path) -> None:
    service, repos = _service(
        tmp_path,
        TelegramConfig(enabled=True, allowed_user_ids=[42], allowed_chat_ids=[100]),
        classifier=StaticMessageClassifier(),
    )
    client = FakeTelegramClient([])
    service.bot_api = client  # type: ignore[assignment]

    result = await service.handle_update_async(
        {
            "message": {
                "message_id": 15,
                "from": {"id": 42},
                "chat": {"id": 100},
                "text": "Build a todo app",
            }
        }
    )

    assert result.task is not None
    assert result.outbound_message is None
    sent_text = "\n".join(text for _, text in client.sent)
    lower = sent_text.lower()
    assert "got your message" in lower or "figuring out" in lower or "on it" in lower
    assert result.task.id not in sent_text


def test_telegram_duplicate_message_is_ignored_without_crashing_or_spawning_again(tmp_path) -> None:
    service, repos = _service(
        tmp_path,
        TelegramConfig(enabled=True, allowed_user_ids=[42], allowed_chat_ids=[100]),
        classifier=StaticMessageClassifier(),
    )
    update = {
        "message": {
            "message_id": 1,
            "from": {"id": 42},
            "chat": {"id": 100},
            "text": "Build a todo app",
        }
    }

    first = service.handle_update(update)
    second = service.handle_update(update)

    assert first.task is not None
    assert second.authorized is True
    assert second.task is None
    assert second.outbound_message is None
    assert len(repos.tasks.list_recent(limit=10)) == 1


def test_telegram_caption_update_creates_task(tmp_path) -> None:
    service, repos = _service(
        tmp_path,
        TelegramConfig(enabled=True, allowed_user_ids=[42], allowed_chat_ids=[100]),
        classifier=StaticMessageClassifier(),
    )

    result = service.handle_update(
        {
            "message": {
                "message_id": 8,
                "from": {"id": 42},
                "chat": {"id": 100},
                "caption": "Build from forwarded caption",
                "forward_origin": {"type": "user", "sender_user": {"id": 99}},
            }
        }
    )

    assert result.task is not None
    assert result.task.objective == "Build from forwarded caption"
    assert repos.tasks.get(result.task.id) is not None


def test_telegram_classifier_can_reject_task_spawn(tmp_path) -> None:
    service, repos = _service(
        tmp_path,
        TelegramConfig(enabled=True, allowed_user_ids=[42], allowed_chat_ids=[100]),
        classifier=StaticMessageClassifier(
            MessageClassification(
                is_task=False,
                task_type=TaskType.QUESTION,
                confidence=0.9,
                reason="question only",
            )
        ),
    )

    result = service.handle_update(
        {
            "message": {
                "message_id": 9,
                "from": {"id": 42},
                "chat": {"id": 100},
                "text": "what is the status?",
            }
        }
    )

    events = repos.audit.list_by_type(AuditEventType.TASK_SPAWN_FAILED)

    assert result.task is None
    assert result.outbound_message is not None
    assert "I could not start this request" in (result.outbound_message.text or "")
    assert events[0].payload["reason"] == "question only"


def test_telegram_non_task_question_gets_llm_response(tmp_path) -> None:
    responder = StaticChatResponder("I can answer questions and route development tasks.")
    service, repos = _service(
        tmp_path,
        TelegramConfig(enabled=True, allowed_user_ids=[42], allowed_chat_ids=[100]),
        classifier=StaticMessageClassifier(
            MessageClassification(
                is_task=False,
                task_type=TaskType.QUESTION,
                confidence=0.9,
                reason="question only",
            )
        ),
    )
    service.responder = responder

    result = service.handle_update(
        {
            "message": {
                "message_id": 12,
                "from": {"id": 42},
                "chat": {"id": 100},
                "text": "what can you do?",
            }
        }
    )

    assert result.task is None
    assert result.outbound_message is not None
    assert result.outbound_message.text == "I can answer questions and route development tasks."
    assert repos.audit.list_by_type(AuditEventType.TASK_SPAWN_FAILED) == []


def test_telegram_non_task_uses_concierge_reply_without_calling_responder(tmp_path) -> None:
    """The Concierge composes the chat reply in the same call it classifies
    (prompts/base/concierge_system.md) - when `.reply` is populated, the
    separate `responder` round trip must not happen at all."""
    responder = StaticChatResponder("responder should not be called")
    service, repos = _service(
        tmp_path,
        TelegramConfig(enabled=True, allowed_user_ids=[42], allowed_chat_ids=[100]),
        classifier=StaticMessageClassifier(
            MessageClassification(
                is_task=False,
                task_type=TaskType.QUESTION,
                confidence=0.9,
                reason="question only",
                reply="I can inspect files, control the browser, and run scheduled jobs.",
            )
        ),
    )
    service.responder = responder

    result = service.handle_update(
        {
            "message": {
                "message_id": 13,
                "from": {"id": 42},
                "chat": {"id": 100},
                "text": "what can you do?",
            }
        }
    )

    assert result.task is None
    assert result.outbound_message is not None
    assert result.outbound_message.text == "I can inspect files, control the browser, and run scheduled jobs."
    assert responder.messages == []


def test_telegram_non_task_falls_back_to_responder_when_concierge_reply_is_empty(tmp_path) -> None:
    """`reply` is optional on MessageClassification (schemas.py) - the prompt
    instructs the Concierge to always populate it for is_task=false, but
    nothing enforces that at the schema level, and a weaker/local model can
    still return it empty. This is the safety net that keeps the fallback
    responder path (docs/archive/HISTORY.md Part 1 P3 item 4) genuinely load-bearing
    rather than dead code - confirmed by inspection, this test proves it by
    execution."""
    responder = StaticChatResponder("Fallback answer from the separate responder call.")
    service, repos = _service(
        tmp_path,
        TelegramConfig(enabled=True, allowed_user_ids=[42], allowed_chat_ids=[100]),
        classifier=StaticMessageClassifier(
            MessageClassification(
                is_task=False,
                task_type=TaskType.QUESTION,
                confidence=0.9,
                reason="question only",
                reply=None,
            )
        ),
    )
    service.responder = responder

    result = service.handle_update(
        {
            "message": {
                "message_id": 14,
                "from": {"id": 42},
                "chat": {"id": 100},
                "text": "what can you do?",
            }
        }
    )

    assert result.task is None
    assert result.outbound_message is not None
    assert result.outbound_message.text == "Fallback answer from the separate responder call."
    assert len(responder.messages) == 1


class _ContextRecordingClassifier:
    """Records the `context` argument classify() was called with, so the
    Concierge merge's context-building choice (gateway_context vs the
    narrower memory_context) can be asserted on directly."""

    def __init__(self, classification: MessageClassification) -> None:
        self.classification = classification
        self.contexts: list[str | None] = []

    async def classify(self, message, context: str | None = None) -> MessageClassification:
        self.contexts.append(context)
        return self.classification


def test_telegram_classification_uses_full_gateway_context_when_settings_available(tmp_path) -> None:
    """The Concierge answers capability questions in the same call it
    classifies (see prompts/base/concierge_system.md) - that needs the same
    richer runtime context (capabilities, recent tasks) the old separate
    responder call used, not just the conversation-memory summary."""
    classifier = _ContextRecordingClassifier(
        MessageClassification(is_task=False, task_type=TaskType.QUESTION, confidence=0.9, reason="chat", reply="hi")
    )
    settings = AppSettings(
        _env_file=None,
        capabilities={Capability.TELEGRAM_SEND: CapabilityPolicy(enabled=True, requires_approval=False, max_risk_level=RiskLevel.LOW)},
    )
    service, _ = _service(
        tmp_path,
        TelegramConfig(enabled=True, allowed_user_ids=[42], allowed_chat_ids=[100]),
        settings=settings,
        classifier=classifier,
    )

    service.handle_update(
        {
            "message": {
                "message_id": 14,
                "from": {"id": 42},
                "chat": {"id": 100},
                "text": "what can you do?",
            }
        }
    )

    assert len(classifier.contexts) == 1
    assert "LLM profile:" in (classifier.contexts[0] or "")


def test_telegram_plain_status_does_not_require_slash(tmp_path) -> None:
    service, repos = _service(
        tmp_path,
        TelegramConfig(enabled=True, allowed_user_ids=[42], allowed_chat_ids=[100]),
    )
    task = repos.tasks.create("Build app")

    result = service.handle_update(
        {
            "message": {
                "message_id": 13,
                "from": {"id": 42},
                "chat": {"id": 100},
                "text": "status",
            }
        }
    )

    assert result.outbound_message is not None
    assert task.id not in (result.outbound_message.text or "")
    assert "received: Build app" in (result.outbound_message.text or "")


def test_telegram_rich_status_question_spawns_traceable_status_task(tmp_path) -> None:
    service, repos = _service(
        tmp_path,
        TelegramConfig(enabled=True, allowed_user_ids=[42], allowed_chat_ids=[100]),
        classifier=StaticMessageClassifier(
            MessageClassification(
                is_task=False,
                task_type=TaskType.STATUS_REQUEST,
                normalized_objective="Report current workflow status.",
                confidence=0.9,
                reason="status question",
                intent={
                    "route": "status",
                    "operation": "status",
                    "objective": "Report current workflow status.",
                    "reasoning": "The user asks for status.",
                },
            )
        ),
    )

    result = service.handle_update(
        {
            "message": {
                "message_id": 14,
                "from": {"id": 42},
                "chat": {"id": 100},
                "text": "Where are we? Tell me what remains and whether anything is blocked.",
            }
        }
    )

    assert result.task is not None
    assert result.task.metadata["task_type"] == TaskType.STATUS_REQUEST.value
    assert result.outbound_message is None
    assert repos.tasks.get(result.task.id) is not None


def test_telegram_plain_approve_approves_latest_pending_task(tmp_path) -> None:
    service, repos = _service(
        tmp_path,
        TelegramConfig(enabled=True, allowed_user_ids=[42], allowed_chat_ids=[100]),
    )
    conversation_id = repos.conversations.get_or_create(ChannelType.TELEGRAM, "100")
    task = repos.tasks.create(
        "Run gated task",
        conversation_id=conversation_id,
        metadata={"source_chat_id": "100"},
    )
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

    result = service.handle_update(
        {
            "message": {
                "message_id": 14,
                "from": {"id": 42},
                "chat": {"id": 100},
                "text": "approve",
            }
        }
    )

    updated = repos.approvals.list_for_task(task.id)[0]
    assert updated.id == approval.id
    assert updated.status == ApprovalStatus.APPROVED
    assert result.outbound_message is not None
    # docs/archive/UI_UX_AUDIT.md Phase 8, second review: a decided approval must
    # make the task claimable again, not leave it stuck in a status
    # claim_next no longer re-selects.
    assert repos.tasks.get(task.id).status == TaskStatus.RUNNING


def test_telegram_remember_that_stores_a_user_stated_fact_without_the_llm(tmp_path) -> None:
    """docs/archive/UI_UX_AUDIT.md Phase 15: provenance must be decided by the
    runtime, never selectable by the model - no classifier is configured
    here at all, proving the detection and storage happen before any LLM
    would even be reachable."""
    service, repos = _service(
        tmp_path,
        TelegramConfig(enabled=True, allowed_user_ids=[42], allowed_chat_ids=[100]),
    )

    result = service.handle_update(
        {
            "message": {
                "message_id": 15,
                "from": {"id": 42},
                "chat": {"id": 100},
                "text": "Remember that I prefer dark roast coffee",
            }
        }
    )

    assert result.outbound_message is not None
    assert "dark roast coffee" in result.outbound_message.text
    facts = repos.memory_facts.list_all()
    assert len(facts) == 1
    assert facts[0].source == MemorySource.USER_STATED
    assert facts[0].content == "I prefer dark roast coffee"
    assert facts[0].task_id is None


def test_telegram_remember_that_does_not_match_unrelated_messages(tmp_path) -> None:
    service, repos = _service(
        tmp_path,
        TelegramConfig(enabled=True, allowed_user_ids=[42], allowed_chat_ids=[100]),
    )

    service.handle_update(
        {
            "message": {
                "message_id": 16,
                "from": {"id": 42},
                "chat": {"id": 100},
                "text": "remembering our trip was fun",
            }
        }
    )

    assert repos.memory_facts.list_all() == []


def test_telegram_remember_to_is_a_reminder_not_a_memory_fact(tmp_path) -> None:
    """"remember to X" is reminder-shaped, not "remember that X" - it must
    still reach the classifier and become a task, not get silently
    swallowed into a memory fact just because it starts with "remember"."""
    service, repos = _service(
        tmp_path,
        TelegramConfig(enabled=True, allowed_user_ids=[42], allowed_chat_ids=[100]),
    )

    service.handle_update(
        {
            "message": {
                "message_id": 17,
                "from": {"id": 42},
                "chat": {"id": 100},
                "text": "remember to call the plumber tomorrow",
            }
        }
    )

    assert repos.memory_facts.list_all() == []


class _FakeBotApiForCallbacks:
    def __init__(self) -> None:
        self.answered: list[tuple[str, str | None]] = []

    async def answer_callback_query(self, callback_query_id: str, text: str | None = None) -> dict:
        self.answered.append((callback_query_id, text))
        return {"ok": True}


def test_telegram_inline_keyboard_reject_denies_approval_and_answers_callback(tmp_path) -> None:
    bot_api = _FakeBotApiForCallbacks()
    service, repos = _service(
        tmp_path,
        TelegramConfig(enabled=True, allowed_user_ids=[42], allowed_chat_ids=[100]),
        bot_api=bot_api,
    )
    conversation_id = repos.conversations.get_or_create(ChannelType.TELEGRAM, "100")
    task = repos.tasks.create(
        "Run gated task",
        conversation_id=conversation_id,
        metadata={"source_chat_id": "100"},
    )
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

    result = service.handle_update(
        {
            "update_id": 99,
            "callback_query": {
                "id": "cbq1",
                "from": {"id": 42},
                "message": {"chat": {"id": 100}},
                "data": f"approval:{approval.id}:reject",
            },
        }
    )

    updated = repos.approvals.list_for_task(task.id)[0]
    assert updated.status == ApprovalStatus.REJECTED
    assert bot_api.answered == [("cbq1", "Rejected.")]
    assert result.authorized is True
    # docs/archive/UI_UX_AUDIT.md Phase 8, second review: a rejection must requeue
    # the task too, not just an approval - it's the worker's next
    # process_task() call (separately tested) that turns a rejected
    # decision into BLOCKED; this layer's job is only to make the task
    # claimable again instead of leaving it stuck in a status claim_next
    # no longer re-selects.
    assert repos.tasks.get(task.id).status == TaskStatus.RUNNING


def test_telegram_inline_keyboard_approve_grants_approval(tmp_path) -> None:
    bot_api = _FakeBotApiForCallbacks()
    service, repos = _service(
        tmp_path,
        TelegramConfig(enabled=True, allowed_user_ids=[42], allowed_chat_ids=[100]),
        bot_api=bot_api,
    )
    conversation_id = repos.conversations.get_or_create(ChannelType.TELEGRAM, "100")
    task = repos.tasks.create(
        "Run gated task",
        conversation_id=conversation_id,
        metadata={"source_chat_id": "100"},
    )
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

    service.handle_update(
        {
            "update_id": 100,
            "callback_query": {
                "id": "cbq2",
                "from": {"id": 42},
                "message": {"chat": {"id": 100}},
                "data": f"approval:{approval.id}:approve",
            },
        }
    )

    updated = repos.approvals.list_for_task(task.id)[0]
    assert updated.status == ApprovalStatus.APPROVED
    # The toast text is the only feedback a button press can produce -
    # _command_response() builds no reply for callbacks. Answering with None
    # (the previous behavior) just stopped the spinner, so a granted approval
    # and a dead button were indistinguishable to the operator.
    assert bot_api.answered == [("cbq2", "Approved - continuing.")]
    assert repos.tasks.get(task.id).status == TaskStatus.RUNNING


def test_telegram_inline_keyboard_reports_already_decided_approval(tmp_path) -> None:
    """A second press on an older message must say why nothing happened."""
    bot_api = _FakeBotApiForCallbacks()
    service, repos = _service(
        tmp_path,
        TelegramConfig(enabled=True, allowed_user_ids=[42], allowed_chat_ids=[100]),
        bot_api=bot_api,
    )
    conversation_id = repos.conversations.get_or_create(ChannelType.TELEGRAM, "100")
    task = repos.tasks.create(
        "Run gated task",
        conversation_id=conversation_id,
        metadata={"source_chat_id": "100"},
    )
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
    press = {
        "update_id": 100,
        "callback_query": {
            "id": "cbq1",
            "from": {"id": 42},
            "message": {"chat": {"id": 100}},
            "data": f"approval:{approval.id}:approve",
        },
    }
    service.handle_update(press)
    service.handle_update({**press, "callback_query": {**press["callback_query"], "id": "cbq2"}})

    assert bot_api.answered[0] == ("cbq1", "Approved - continuing.")
    assert bot_api.answered[1][0] == "cbq2"
    assert "Already approved" in bot_api.answered[1][1]


def test_telegram_updates_conversation_memory(tmp_path) -> None:
    service, repos = _service(
        tmp_path,
        TelegramConfig(enabled=True, allowed_user_ids=[42], allowed_chat_ids=[100]),
    )

    service.handle_update(
        {
            "message": {
                "message_id": 15,
                "from": {"id": 42},
                "chat": {"id": 100},
                "text": "My name is Oney.",
            }
        }
    )

    memory = repos.conversation_memory.get("conv_telegram_100")

    assert memory is not None
    assert "Oney" in memory["summary"]
    assert memory["facts"]["strategy"] == "rolling_summary_with_recent_turns"
    assert memory["facts"]["recent_turns"][-1]["text"] == "My name is Oney."


def test_conversation_memory_context_is_bounded(tmp_path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'agent.db'}")
    database.initialize()
    repos = Repositories.for_database(database)
    conversation_id = repos.conversations.get_or_create(ChannelType.TELEGRAM, "100")
    repos.conversation_memory.upsert(
        conversation_id,
        "Stable fact. " * 400,
        {
            "recent_turns": [
                {"role": "user", "text": "A" * 500},
                {"role": "task", "text": "B" * 500},
            ]
        },
    )

    from agent_control.channels.memory import memory_context

    context = memory_context(repos.conversation_memory.get(conversation_id), recent_turns=2, max_chars=900)

    assert len(context) <= 900


class StaticMemoryProvider:
    async def generate_text(self, system_prompt, user_prompt):
        return "User is Oney. They want concise Telegram gateway memory and local workspace automation."

    async def generate_structured(self, system_prompt, user_prompt, output_model, **_ignored_kwargs):
        raise NotImplementedError


def test_telegram_memory_can_use_llm_summary(tmp_path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'agent.db'}")
    database.initialize()
    repos = Repositories.for_database(database)
    audit = AuditLogger(repos.audit)
    service = TelegramIntakeService(
        TelegramAdapter(TelegramConfig(enabled=True, allowed_user_ids=[42], allowed_chat_ids=[100]), audit),
        repos,
        audit,
        memory_service=ConversationMemoryService(repos, provider=StaticMemoryProvider()),
    )

    service.handle_update(
        {
            "message": {
                "message_id": 16,
                "from": {"id": 42},
                "chat": {"id": 100},
                "text": "Remember that I prefer concise updates and local workspaces.",
            }
        }
    )

    memory = repos.conversation_memory.get("conv_telegram_100")

    assert memory is not None
    assert "local workspace automation" in memory["summary"]


class FailingClassifierProvider:
    async def generate_structured(self, system_prompt, user_prompt, output_model, **_ignored_kwargs):
        raise ValueError("bad json")

    async def generate_text(self, system_prompt, user_prompt):
        return "unused"


def test_llm_classifier_fallback_allows_direct_greeting_response(tmp_path) -> None:
    service, _ = _service(
        tmp_path,
        TelegramConfig(enabled=True, allowed_user_ids=[42], allowed_chat_ids=[100]),
        classifier=LLMMessageClassifier(FailingClassifierProvider()),
    )
    service.responder = StaticChatResponder("Hello. I can answer questions or route tasks.")

    result = service.handle_update(
        {
            "message": {
                "message_id": 14,
                "from": {"id": 42},
                "chat": {"id": 100},
                "text": "Hi",
            }
        }
    )

    assert result.task is None
    assert result.outbound_message is not None
    assert result.outbound_message.text == "Hello. I can answer questions or route tasks."


def test_telegram_without_classifier_does_not_spawn_task(tmp_path) -> None:
    service, repos = _service(
        tmp_path,
        TelegramConfig(enabled=True, allowed_user_ids=[42], allowed_chat_ids=[100]),
    )

    result = service.handle_update(
        {
            "message": {
                "message_id": 10,
                "from": {"id": 42},
                "chat": {"id": 100},
                "text": "Build a todo app",
            }
        }
    )

    assert result.task is None
    assert result.outbound_message is not None
    assert repos.tasks.list_recent() == []


def test_telegram_unauthorized_update_is_denied_and_audited(tmp_path) -> None:
    service, repos = _service(
        tmp_path,
        TelegramConfig(enabled=True, allowed_user_ids=[42], allowed_chat_ids=[100]),
    )

    result = service.handle_update(
        {
            "message": {
                "message_id": 1,
                "from": {"id": 99},
                "chat": {"id": 100},
                "text": "Nope",
            }
        }
    )

    events = repos.audit.list_by_type(AuditEventType.TELEGRAM_ACCESS_DECISION)

    assert result.authorized is False
    assert events[0].payload["allowed"] is False


def test_telegram_empty_allowlist_is_denied_and_audited(tmp_path) -> None:
    service, repos = _service(
        tmp_path,
        TelegramConfig(enabled=True),
    )

    result = service.handle_update(
        {
            "message": {
                "message_id": 11,
                "from": {"id": 42},
                "chat": {"id": 100},
                "text": "Build a todo app",
            }
        }
    )
    events = repos.audit.list_by_type(AuditEventType.TELEGRAM_ACCESS_DECISION)

    assert result.authorized is False
    assert events[0].payload["reason"] == "allowlist_empty"


def test_telegram_pause_command_updates_task(tmp_path) -> None:
    service, repos = _service(
        tmp_path,
        TelegramConfig(enabled=True, allowed_user_ids=[42], allowed_chat_ids=[100]),
    )
    task = repos.tasks.create("Build app")

    result = service.handle_update(
        {
            "message": {
                "message_id": 2,
                "from": {"id": 42},
                "chat": {"id": 100},
                "text": f"/pause {task.id}",
            }
        }
    )

    updated = repos.tasks.get(task.id)

    assert result.signal is not None
    assert updated is not None
    assert updated.status == TaskStatus.PAUSED


def test_telegram_tasks_command_returns_summary(tmp_path) -> None:
    service, repos = _service(
        tmp_path,
        TelegramConfig(enabled=True, allowed_user_ids=[42], allowed_chat_ids=[100]),
    )
    task = repos.tasks.create("Build app")

    result = service.handle_update(
        {
            "message": {
                "message_id": 3,
                "from": {"id": 42},
                "chat": {"id": 100},
                "text": "/tasks",
            }
        }
    )

    assert result.outbound_message is not None
    assert task.id in (result.outbound_message.text or "")


def test_telegram_logs_command_returns_recent_events(tmp_path) -> None:
    service, repos = _service(
        tmp_path,
        TelegramConfig(enabled=True, allowed_user_ids=[42], allowed_chat_ids=[100]),
    )
    task = repos.tasks.create("Build app")
    service.audit.append(
        AuditEventType.TASK_CREATED,
        actor="test",
        task_id=task.id,
        payload={"objective": task.objective},
    )

    result = service.handle_update(
        {
            "message": {
                "message_id": 4,
                "from": {"id": 42},
                "chat": {"id": 100},
                "text": f"/logs {task.id}",
            }
        }
    )

    assert result.outbound_message is not None
    assert "task_created" in (result.outbound_message.text or "")


def test_telegram_screenshot_command_reports_disabled(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    service, _ = _service(
        tmp_path,
        TelegramConfig(enabled=True, allowed_user_ids=[42], allowed_chat_ids=[100]),
        settings=AppSettings(_env_file=None),
    )

    result = service.handle_update(
        {
            "message": {
                "message_id": 5,
                "from": {"id": 42},
                "chat": {"id": 100},
                "text": "/screenshot",
            }
        }
    )

    assert result.outbound_message is not None
    # Both switches have to be named. Saying only "desktop.screenshot is
    # disabled" sent people to the capability of that exact name, which on its
    # own does not make capture work - adapters.desktop.screenshot_enabled
    # gates it too, and the message afterwards was word-for-word identical.
    text = result.outbound_message.text
    assert "desktop.screenshot" in text
    assert "adapters.desktop.screenshot_enabled" in text


class FakeTelegramClient:
    def __init__(self, updates: list[dict]) -> None:
        self.updates = updates
        self.sent: list[tuple[str | int, str]] = []

    async def get_updates(self, offset: int | None = None, timeout: int = 30) -> list[dict]:
        return self.updates

    async def send_message(self, chat_id: str | int, text: str) -> dict:
        self.sent.append((chat_id, text))
        return {"ok": True}

    async def send_photo_file(self, chat_id: str | int, path: str, caption: str | None = None) -> dict:
        self.sent.append((chat_id, f"photo:{caption}:{path}"))
        return {"ok": True}


@pytest.mark.asyncio
async def test_polling_runner_sends_outbound_command_response(tmp_path) -> None:
    service, _ = _service(
        tmp_path,
        TelegramConfig(enabled=True, allowed_user_ids=[42], allowed_chat_ids=[100]),
    )
    client = FakeTelegramClient(
        [
            {
                "update_id": 10,
                "message": {
                    "message_id": 6,
                    "from": {"id": 42},
                    "chat": {"id": 100},
                    "text": "/status",
                },
            }
        ]
    )
    runner = TelegramPollingRunner(client, service)  # type: ignore[arg-type]

    next_offset, _ = await runner.poll_once()

    assert next_offset == 11
    assert client.sent == [("100", "0 recent task(s), 0 active.")]


@pytest.mark.asyncio
async def test_polling_runner_advances_offset_after_update_processing_error(tmp_path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'agent.db'}")
    database.initialize()
    repos = Repositories.for_database(database)
    audit = AuditLogger(repos.audit)

    class BrokenIntake:
        def __init__(self) -> None:
            self.audit = audit
            self.repositories = repos

        async def handle_update_async(self, update: dict) -> None:
            raise RuntimeError("boom")

    client = FakeTelegramClient(
        [
            {
                "update_id": 12,
                "message": {
                    "message_id": 8,
                    "from": {"id": 42},
                    "chat": {"id": 100},
                    "text": "poison",
                },
            }
        ]
    )
    runner = TelegramPollingRunner(client, BrokenIntake())  # type: ignore[arg-type]

    next_offset, results = await runner.poll_once()

    assert next_offset == 13
    assert results == []
    events = repos.audit.list_recent(limit=5)
    assert events[0].type == AuditEventType.ERROR
    assert events[0].payload["error"] == "update_processing_failed"


class FakeScreenshotAdapter:
    def capture_png(self) -> bytes:
        return b"png-bytes"


@pytest.mark.asyncio
async def test_polling_runner_sends_screenshot_artifact(tmp_path) -> None:
    settings = AppSettings(
        _env_file=None,
        capabilities={
            Capability.DESKTOP_SCREENSHOT: CapabilityPolicy(
                enabled=True,
                requires_approval=False,
                max_risk_level=RiskLevel.MEDIUM,
            )
        },
    )
    service, repos = _service(
        tmp_path,
        TelegramConfig(enabled=True, allowed_user_ids=[42], allowed_chat_ids=[100]),
        settings=settings,
    )
    screenshot_service = ScreenshotService(
        DesktopAdapterConfig(screenshot_enabled=True),
        ArtifactService(StorageConfig(artifact_dir=str(tmp_path / "artifacts")), repos.artifacts),
        adapter=FakeScreenshotAdapter(),
    )
    service.screenshot_service = screenshot_service
    client = FakeTelegramClient(
        [
            {
                "update_id": 11,
                "message": {
                    "message_id": 7,
                    "from": {"id": 42},
                    "chat": {"id": 100},
                    "text": "/screenshot",
                },
            }
        ]
    )
    runner = TelegramPollingRunner(client, service)  # type: ignore[arg-type]

    _, results = await runner.poll_once()

    assert results[0].outbound_message is not None
    assert results[0].outbound_message.artifact_ids
    assert client.sent[0] == ("100", "Screenshot captured.")
    assert client.sent[1][1].startswith("photo:desktop screenshot:")


class _FakeTelegramHttpResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class _FakeTelegramHttpClient:
    def __init__(self, timeout: int) -> None:
        self.timeout = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def post(self, url: str, *, json: dict | None = None, data: dict | None = None, files=None):
        return _FakeTelegramHttpResponse({"ok": True, "result": {"message_id": 555}})


class _FailingTelegramHttpClient(_FakeTelegramHttpClient):
    async def post(self, url: str, *, json: dict | None = None, data: dict | None = None, files=None):
        request = httpx.Request("POST", url)
        response = httpx.Response(400, request=request)
        raise httpx.HTTPStatusError("bad request", request=request, response=response)


@pytest.mark.asyncio
async def test_telegram_bot_api_persists_outbound_message_audit_record(tmp_path, monkeypatch) -> None:
    database = Database(f"sqlite:///{tmp_path / 'agent.db'}")
    database.initialize()
    repos = Repositories.for_database(database)
    audit = AuditLogger(repos.audit)
    monkeypatch.setattr("agent_control.channels.telegram.httpx.AsyncClient", _FakeTelegramHttpClient)
    client = TelegramBotApi("token123", audit=audit)

    await client.send_message("100", "hello from ybm")

    events = repos.audit.list_by_type(AuditEventType.MESSAGE_SENT)
    assert len(events) == 1
    assert events[0].payload["chat_id"] == "100"
    assert events[0].payload["kind"] == "text"
    assert events[0].payload["text"] == "hello from ybm"
    assert events[0].payload["telegram_message_id"] == 555


@pytest.mark.asyncio
async def test_telegram_bot_api_without_audit_still_sends(monkeypatch) -> None:
    monkeypatch.setattr("agent_control.channels.telegram.httpx.AsyncClient", _FakeTelegramHttpClient)
    client = TelegramBotApi("token123")

    data = await client.send_message("100", "hello from ybm")

    assert data["ok"] is True


@pytest.mark.asyncio
async def test_telegram_bot_api_error_does_not_expose_token_url(monkeypatch) -> None:
    monkeypatch.setattr("agent_control.channels.telegram.httpx.AsyncClient", _FailingTelegramHttpClient)
    client = TelegramBotApi("123456:super-secret-token-value-that-must-not-leak")

    with pytest.raises(RuntimeError) as error:
        await client.send_message("100", "hello")

    message = str(error.value)
    assert message == "Telegram Bot API request failed: sendMessage (HTTP 400)"
    assert "super-secret" not in message
