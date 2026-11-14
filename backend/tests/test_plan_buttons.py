"""The buttons of a plan card and of a question's quick answers, tapped in Telegram (channels/telegram.py)."""

from __future__ import annotations

import pytest

from agent_control.config import TelegramConfig
from agent_control.plans.model import Plan, PlanStatus, PlanStep, StepOwner, StepStatus
from agent_control.schemas import ChannelType, TaskStatus
from test_telegram import _FakeBotApiForCallbacks, _service

ROUTING = {"source_channel": "telegram", "source_chat_id": "100", "source_sender_id": "42"}


class _Bot(_FakeBotApiForCallbacks):
    def __init__(self) -> None:
        super().__init__()
        self.edits: list[tuple] = []

    async def edit_message_text(self, chat_id, message_id, text, reply_markup=None):
        self.edits.append((chat_id, message_id, text, reply_markup))
        return {"ok": True}


def _setup(tmp_path):
    bot = _Bot()
    service, repos = _service(tmp_path, TelegramConfig(enabled=True, allowed_user_ids=[42], allowed_chat_ids=[100]), bot_api=bot)
    return service, repos, bot


async def _tap(service, data: str, callback_id: str = "cbq1"):
    update = {
        "update_id": 1,
        "callback_query": {"id": callback_id, "from": {"id": 42}, "message": {"message_id": 7, "chat": {"id": 100}}, "data": data},
    }
    return await service.handle_update_async(update)


def _plan(repos, status: PlanStatus = PlanStatus.PROPOSED) -> Plan:
    conversation = repos.conversations.get_or_create(ChannelType.TELEGRAM, "100")
    return repos.plans.create(Plan(
        goal="Set up a weekly backup", conversation_id=conversation, routing=dict(ROUTING), status=status,
        steps=[PlanStep(position=1, title="Choose a drive", owner=StepOwner.USER), PlanStep(position=2, title="Write the script")],
    ))


@pytest.mark.asyncio
async def test_tapping_start_makes_the_plan_active_and_redraws_the_card_with_its_new_buttons(tmp_path):
    service, repos, bot = _setup(tmp_path)
    plan = _plan(repos)
    await _tap(service, f"plan:{plan.id}:start")
    assert repos.plans.get(plan.id).status == PlanStatus.ACTIVE
    assert bot.answered == [("cbq1", "Starting.")]
    chat, message_id, text, markup = bot.edits[0]
    assert (chat, message_id) == ("100", 7) and text.startswith("Plan: Set up a weekly backup") and "I am working through it" in text
    assert [b["callback_data"] for b in markup["inline_keyboard"][0]] == [f"plan:{plan.id}:pause", f"plan:{plan.id}:cancel"]


@pytest.mark.asyncio
async def test_pause_continue_and_cancel_from_the_card(tmp_path):
    service, repos, bot = _setup(tmp_path)
    plan = _plan(repos, PlanStatus.ACTIVE)
    await _tap(service, f"plan:{plan.id}:pause", "a")
    assert repos.plans.get(plan.id).status == PlanStatus.PAUSED
    await _tap(service, f"plan:{plan.id}:resume", "b")
    assert repos.plans.get(plan.id).status == PlanStatus.ACTIVE
    await _tap(service, f"plan:{plan.id}:cancel", "c")
    cancelled = repos.plans.get(plan.id)
    assert cancelled.status == PlanStatus.CANCELLED and {s.status for s in cancelled.steps} == {StepStatus.SKIPPED}
    assert [text for _, text in bot.answered] == ["Paused after the step in progress.", "Carrying on.", "Plan cancelled."]
    assert bot.edits[-1][3] is None  # a cancelled plan has no buttons


@pytest.mark.asyncio
async def test_a_tap_on_a_plan_that_has_moved_on_or_is_gone_says_so_and_changes_nothing(tmp_path):
    service, repos, bot = _setup(tmp_path)
    plan = _plan(repos, PlanStatus.ACTIVE)
    await _tap(service, f"plan:{plan.id}:start", "a")
    assert bot.answered[-1] == ("a", "The plan is active.") and repos.plans.get(plan.id).status == PlanStatus.ACTIVE
    await _tap(service, "plan:plan_nope:start", "b")
    assert bot.answered[-1] == ("b", "I cannot find that plan any more.")


@pytest.mark.asyncio
async def test_a_quick_answer_button_answers_the_question_once(tmp_path):
    service, repos, bot = _setup(tmp_path)
    conversation = repos.conversations.get_or_create(ChannelType.TELEGRAM, "100")
    task = repos.tasks.create(
        "Build me a drone", conversation_id=conversation,
        metadata={"source_chat_id": "100", "clarifying_question": "What kind?", "clarifying_options": ["Indoor", "Outdoor camera"]},
    )
    repos.tasks.update_status(task.id, TaskStatus.CLARIFYING)
    await _tap(service, f"answer:{task.id}:o1", "a")
    resumed = repos.tasks.get(task.id)
    assert resumed.status == TaskStatus.RECEIVED and resumed.metadata["clarification_answer"] == "Outdoor camera"
    assert "[User clarification: Outdoor camera]" in resumed.objective and bot.answered[-1] == ("a", "Okay: Outdoor camera")
    await _tap(service, f"answer:{task.id}:o1", "b")  # a second tap on the same button
    assert bot.answered[-1] == ("b", "That question was already answered.")


@pytest.mark.asyncio
async def test_a_tap_on_a_button_that_is_not_there_changes_nothing(tmp_path):
    service, repos, bot = _setup(tmp_path)
    conversation = repos.conversations.get_or_create(ChannelType.TELEGRAM, "100")
    task = repos.tasks.create("x", conversation_id=conversation, metadata={"source_chat_id": "100", "clarifying_options": ["Yes"]})
    repos.tasks.update_status(task.id, TaskStatus.CLARIFYING)
    await _tap(service, f"answer:{task.id}:o5")
    assert repos.tasks.get(task.id).status == TaskStatus.CLARIFYING and bot.answered[-1][1] == "That question was already answered."
