"""A plan in the chat: the card and its buttons, quick answers, answering a step that is the user's, and the worker passing the runner (channels/, plans/)."""

from __future__ import annotations

import pytest

from agent_control.channels.base import resume_clarifying_reply
from agent_control.channels.quick_answers import clarifying_options
from agent_control.channels.telegram_notifications import TelegramTaskNotifier
from agent_control.clarification import find_clarifying_task, find_plan_step_answer, resume_clarifying_task
from agent_control.orchestration import TaskWorker
from agent_control.plans.card import card_keyboard, card_text
from agent_control.plans.context import open_plan_note
from agent_control.plans.model import Plan, PlanStatus, PlanStep, StepOwner, StepStatus
from agent_control.plans.runner import PlanRunner
from agent_control.schemas import ChannelType, InboundMessage, MessageKind, TaskStatus, utc_now
from helpers import make_repos

ROUTING = {"source_channel": "telegram", "source_chat_id": "42", "source_sender_id": "7"}


class _Bot:
    def __init__(self) -> None:
        self.sent = []

    async def send_message(self, chat_id, text, reply_markup=None, **_):
        self.sent.append((chat_id, text, reply_markup))

    async def delete_message(self, *_):
        return None


def _plan(**kwargs) -> Plan:
    return Plan(
        goal="Back up my photos weekly", routing=dict(ROUTING),
        steps=[PlanStep(position=1, title="Choose a drive", owner=StepOwner.USER), PlanStep(position=2, title="Write the script")], **kwargs,
    )


def _reply(text: str, conversation_id: str) -> InboundMessage:
    return InboundMessage(channel=ChannelType.TELEGRAM, kind=MessageKind.TEXT, sender_id="7", chat_id="42", text=text)


# --- the card ---------------------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "buttons"),
    [
        (PlanStatus.PROPOSED, ["start", "cancel"]), (PlanStatus.ACTIVE, ["pause", "cancel"]), (PlanStatus.PAUSED, ["resume", "cancel"]),
        (PlanStatus.DONE, None), (PlanStatus.CANCELLED, None),
    ],
)
def test_the_card_has_the_buttons_that_fit_where_the_plan_stands(status, buttons):
    plan = _plan(status=status)
    keyboard = card_keyboard(plan)
    if buttons is None:
        assert keyboard is None
    else:
        assert [b["callback_data"] for b in keyboard["inline_keyboard"][0]] == [f"plan:{plan.id}:{a}" for a in buttons]
        assert all(len(b["callback_data"]) <= 64 for b in keyboard["inline_keyboard"][0])  # Telegram's limit
    assert card_text(plan).startswith("Plan: Back up my photos weekly") and "[ ] 1. Choose a drive (you)" in card_text(plan)


@pytest.mark.asyncio
async def test_the_answer_that_drafted_a_plan_is_replaced_by_the_plan_with_its_buttons(tmp_path):
    repos, _ = make_repos(tmp_path)
    plan = repos.plans.create(_plan())
    task = repos.tasks.create("Back up my photos", metadata={**ROUTING, "plan_card": plan.id, "synthesized_answer": "Here is a plan"})
    done = repos.tasks.update_status(task.id, TaskStatus.COMPLETED)
    bot = _Bot()
    await TelegramTaskNotifier(bot, plans=repos.plans).notify(done)
    chat, text, markup = bot.sent[0]
    assert chat == "42" and text.startswith("Plan: Back up my photos weekly") and "Start it when you are ready" in text
    assert markup["inline_keyboard"][0][0]["callback_data"] == f"plan:{plan.id}:start"
    # Without the plans repository (or once the task is not complete) the ordinary answer goes out.
    bot2 = _Bot()
    await TelegramTaskNotifier(bot2).notify(done)
    assert bot2.sent[0][1].startswith("Here is a plan") and bot2.sent[0][2] is None


@pytest.mark.asyncio
async def test_a_line_outside_any_task_reaches_the_chat_the_plan_came_from():
    bot = _Bot()
    notifier = TelegramTaskNotifier(bot)
    await notifier.say(ROUTING, "Step 2 of 5: choose a drive")
    await notifier.say({"source_channel": "whatsapp", "source_chat_id": "9"}, "not for telegram")
    await notifier.say({"source_channel": "telegram"}, "no chat")
    assert bot.sent == [("42", "Step 2 of 5: choose a drive", None)]


# --- quick answers ----------------------------------------------------------------------------------------------------------------------------


def test_a_question_with_options_gets_one_button_each_and_only_sane_ones(tmp_path):
    repos, _ = make_repos(tmp_path)
    task = repos.tasks.create("x", metadata={"clarifying_options": ["Done", "Skip this step", "Done", "", "x" * 41, "a", "b", "c"]})
    asking = repos.tasks.update_status(task.id, TaskStatus.CLARIFYING)
    assert clarifying_options(asking) == ["Done", "Skip this step", "a", "b"]
    assert clarifying_options(repos.tasks.update_status(task.id, TaskStatus.RUNNING)) == []  # not a question any more


# --- answering a step that is the user's ------------------------------------------------------------------------------------------------------


async def _waiting_step(repos, audit):
    plan = repos.plans.create(_plan(conversation_id=repos.conversations.get_or_create(ChannelType.TELEGRAM, "42")))
    runner = PlanRunner(repos, audit)
    await runner.start(plan.id)
    plan = repos.plans.get(plan.id)
    return plan, repos.tasks.get(plan.steps[0].task_id)


@pytest.mark.asyncio
async def test_a_step_that_waits_for_the_user_does_not_take_whatever_they_say_next(tmp_path):
    repos, audit = make_repos(tmp_path)
    plan, step_task = await _waiting_step(repos, audit)
    conversation = plan.conversation_id
    assert step_task.status == TaskStatus.CLARIFYING
    assert find_clarifying_task(repos, conversation_id=conversation, chat_id="42") is None  # an ordinary request goes on to intake
    assert resume_clarifying_reply(repos, audit, _reply("What is the weather like?", conversation), conversation) is None
    assert find_plan_step_answer(repos, conversation_id=conversation, chat_id="42", text="what is the weather") is None
    assert repos.tasks.get(step_task.id).status == TaskStatus.CLARIFYING


@pytest.mark.asyncio
async def test_the_words_its_buttons_offer_answer_it_and_done_moves_the_plan_on(tmp_path):
    repos, audit = make_repos(tmp_path)
    plan, step_task = await _waiting_step(repos, audit)
    reply = resume_clarifying_reply(repos, audit, _reply("done", plan.conversation_id), plan.conversation_id)
    assert reply.text == "Noted. On to the next step."
    finished = repos.tasks.get(step_task.id)
    assert finished.status == TaskStatus.COMPLETED and finished.metadata["plan_step_answer"] == "done"
    await PlanRunner(repos, audit).tick()
    plan = repos.plans.get(plan.id)
    assert plan.steps[0].status == StepStatus.DONE and plan.steps[1].status == StepStatus.RUNNING


@pytest.mark.asyncio
async def test_skip_leaves_the_step_out_whether_typed_or_tapped(tmp_path):
    repos, audit = make_repos(tmp_path)
    plan, step_task = await _waiting_step(repos, audit)
    reply = resume_clarifying_reply(repos, audit, _reply("Skip this step", plan.conversation_id), plan.conversation_id)
    assert reply.text == "Okay, I'll leave that step out." and repos.tasks.get(step_task.id).status == TaskStatus.CANCELLED
    await PlanRunner(repos, audit).tick()
    assert repos.plans.get(plan.id).steps[0].status == StepStatus.SKIPPED
    # a tap goes straight to the task
    plan2, task2 = await _waiting_step(repos, audit)
    result = resume_clarifying_task(
        repos, audit, task2, text="Done", actor="telegram:7", message_id="m", received_at=utc_now()
    )
    assert result.plan_step and not result.cancelled and repos.tasks.get(task2.id).status == TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_a_step_waiting_for_days_is_not_expired_like_an_ordinary_question(tmp_path):
    from datetime import timedelta

    from agent_control.clarification import expire_stale_clarifications

    repos, audit = make_repos(tmp_path)
    _, step_task = await _waiting_step(repos, audit)
    assert expire_stale_clarifications(repos, audit, now=utc_now() + timedelta(days=5)) == []
    assert repos.tasks.get(step_task.id).status == TaskStatus.CLARIFYING


# --- the plan the Operator is told about ------------------------------------------------------------------------------------------------------


def test_an_open_plan_is_named_in_the_prompt_of_a_task_in_its_chat_and_nothing_is_added_otherwise(tmp_path):
    repos, _ = make_repos(tmp_path)
    conversation = repos.conversations.get_or_create(ChannelType.TELEGRAM, "42")
    assert open_plan_note(repos.plans, conversation) == "" and open_plan_note(repos.plans, None) == ""
    plan = repos.plans.create(_plan(conversation_id=conversation))
    note = open_plan_note(repos.plans, conversation)
    assert "## A plan is open in this chat" in note and "Goal: Back up my photos weekly (the plan is proposed)" in note
    assert "1. Choose a drive - to do" in note and "plan.manage" in note
    assert open_plan_note(repos.plans, repos.conversations.get_or_create(ChannelType.TELEGRAM, "other")) == ""
    repos.plans.set_status(plan.id, PlanStatus.DONE)
    assert open_plan_note(repos.plans, conversation) == ""


# --- the worker -------------------------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_worker_lets_the_plan_runner_move_on_before_it_looks_for_a_task(tmp_path):
    repos, audit = make_repos(tmp_path)
    calls = []

    class Runner:
        async def tick(self) -> int:
            calls.append("tick")
            raise RuntimeError("a plan's trouble")

    worker = TaskWorker(repos, audit, plan_runner=Runner())
    assert await worker.process_next() is None  # no task, and the runner's failure did not stop it
    assert calls == ["tick"]
