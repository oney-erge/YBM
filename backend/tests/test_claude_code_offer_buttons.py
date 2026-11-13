"""The Claude Code offer ("shall I hand this to Claude Code?") carries Yes / No buttons; one tap answers it like typing does."""

from __future__ import annotations

import pytest

from agent_control.channels.telegram_notifications import TelegramTaskNotifier
from agent_control.config import TelegramConfig
from agent_control.schemas import ChannelType, TaskRecord, TaskStatus
from test_telegram import _FakeBotApiForCallbacks, _service
from test_telegram_notifications import FakeTelegramClient


def _offer_task(*, state: str = "pending", status: TaskStatus = TaskStatus.CLARIFYING) -> TaskRecord:
    question = "I could not finish this on the local model. Want me to hand it to Claude Code?"
    return TaskRecord(
        objective="Fix the build", status=status,
        metadata={
            "source_channel": "telegram", "source_chat_id": "100", "clarifying_question": question,
            "claude_code_offer": {"question": question, "state": state},
        },
    )


@pytest.mark.asyncio
async def test_the_offer_is_sent_with_a_yes_and_a_no_button():
    client = FakeTelegramClient()
    task = _offer_task()
    await TelegramTaskNotifier(client).notify(task)  # type: ignore[arg-type]
    buttons = [button for row in client.last_reply_markup["inline_keyboard"] for button in row]
    assert [button["text"] for button in buttons] == ["Yes, ask Claude Code", "No thanks"]
    assert [button["callback_data"] for button in buttons] == [f"answer:{task.id}:yes", f"answer:{task.id}:no"]
    assert all(len(button["callback_data"].encode()) <= 64 for button in buttons)


@pytest.mark.asyncio
async def test_other_questions_and_answered_offers_get_no_buttons():
    for task in (
        _offer_task(state="accepted"),
        _offer_task(state="declined"),
        TaskRecord(objective="x", status=TaskStatus.CLARIFYING, metadata={"source_channel": "telegram", "source_chat_id": "100", "clarifying_question": "Which folder?"}),
    ):
        client = FakeTelegramClient()
        await TelegramTaskNotifier(client).notify(task)  # type: ignore[arg-type]
        assert client.last_reply_markup is None


def _press(service, task_id: str, answer: str, callback_id: str = "cbq1"):
    return service.handle_update({
        "update_id": 1,
        "callback_query": {"id": callback_id, "from": {"id": 42}, "message": {"chat": {"id": 100}}, "data": f"answer:{task_id}:{answer}"},
    })


def _waiting_task(repos):
    conversation_id = repos.conversations.get_or_create(ChannelType.TELEGRAM, "100")
    task = repos.tasks.create(
        "Fix the build", conversation_id=conversation_id,
        metadata={
            "source_chat_id": "100", "clarifying_question": "Hand it to Claude Code?",
            "claude_code_offer": {"question": "Hand it to Claude Code?", "state": "pending"},
        },
    )
    repos.tasks.update_metadata(task.id, task.metadata, TaskStatus.CLARIFYING)
    return task


def _setup(tmp_path):
    bot_api = _FakeBotApiForCallbacks()
    service, repos = _service(tmp_path, TelegramConfig(enabled=True, allowed_user_ids=[42], allowed_chat_ids=[100]), bot_api=bot_api)
    return service, repos, bot_api


def test_tapping_yes_answers_the_question_and_resumes_the_task(tmp_path):
    service, repos, bot_api = _setup(tmp_path)
    task = _waiting_task(repos)

    _press(service, task.id, "yes")

    resumed = repos.tasks.get(task.id)
    assert resumed.status == TaskStatus.RECEIVED
    assert resumed.metadata["clarification_answer"] == "Yes, please ask Claude Code"
    assert resumed.metadata["answered_clarifying_question"] == "Hand it to Claude Code?"
    assert bot_api.answered == [("cbq1", "Okay, asking Claude Code.")]


def test_tapping_no_declines_and_does_not_cancel_the_task(tmp_path):
    service, repos, bot_api = _setup(tmp_path)
    task = _waiting_task(repos)

    _press(service, task.id, "no")

    resumed = repos.tasks.get(task.id)
    assert resumed.status == TaskStatus.RECEIVED, "a bare 'no' would have cancelled it"
    assert resumed.metadata["clarification_answer"] == "No thanks"
    assert bot_api.answered == [("cbq1", "Okay, I'll answer with what I have.")]


def test_a_second_tap_or_a_stale_button_does_nothing(tmp_path):
    service, repos, bot_api = _setup(tmp_path)
    task = _waiting_task(repos)

    _press(service, task.id, "yes", "cbq1")
    _press(service, task.id, "no", "cbq2")
    _press(service, "task_that_does_not_exist", "yes", "cbq3")

    assert repos.tasks.get(task.id).metadata["clarification_answer"] == "Yes, please ask Claude Code"
    assert [text for _, text in bot_api.answered[1:]] == ["That question was already answered."] * 2


def test_a_button_for_an_ordinary_question_is_ignored(tmp_path):
    service, repos, bot_api = _setup(tmp_path)
    conversation_id = repos.conversations.get_or_create(ChannelType.TELEGRAM, "100")
    task = repos.tasks.create("x", conversation_id=conversation_id, metadata={"source_chat_id": "100", "clarifying_question": "Which folder?"})
    repos.tasks.update_metadata(task.id, task.metadata, TaskStatus.CLARIFYING)

    _press(service, task.id, "yes")

    assert repos.tasks.get(task.id).status == TaskStatus.CLARIFYING
    assert bot_api.answered == [("cbq1", "That question was already answered.")]
