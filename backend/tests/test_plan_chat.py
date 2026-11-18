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


def test_finished_plan_card_does_not_call_skipped_steps_done():
    plan = Plan(goal="Prepare a backup", status=PlanStatus.DONE, steps=[
        PlanStep(position=1, title="Choose a disk", status=StepStatus.SKIPPED),
        PlanStep(position=2, title="Write a note", status=StepStatus.DONE),
    ])
    text = card_text(plan)
    assert "Finished with 1 step skipped." in text and "All done." not in text


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
async def test_reply_does_not_complete_a_plan_step_that_changed_while_waiting(tmp_path):
    from agent_control.schemas import utc_now

    repos, audit = make_repos(tmp_path)
    plan, step_task = await _waiting_step(repos, audit)
    repos.plans.update_step(plan.steps[0].id, status=StepStatus.PENDING)
    result = resume_clarifying_task(repos, audit, step_task, text="Done", actor="test", message_id="old", received_at=utc_now())
    assert not result.accepted
    assert repos.tasks.get(step_task.id).status == TaskStatus.CLARIFYING


def test_multiple_open_questions_require_a_specific_reply(tmp_path):
    repos, audit = make_repos(tmp_path)
    conversation = repos.conversations.get_or_create(ChannelType.TELEGRAM, "42")
    tasks = []
    for objective in ("First project", "Second project"):
        task = repos.tasks.create(objective, conversation_id=conversation, metadata={"clarifying_question": "Which option?"})
        tasks.append(repos.tasks.update_status(task.id, TaskStatus.CLARIFYING))
    ambiguous = resume_clarifying_reply(repos, audit, _reply("yes", conversation), conversation)
    assert "More than one question" in ambiguous.text
    assert all(repos.tasks.get(task.id).status == TaskStatus.CLARIFYING for task in tasks)
    directed = resume_clarifying_reply(repos, audit, _reply(f"reply {tasks[0].id}: first option", conversation), conversation)
    assert "resuming" in directed.text
    assert repos.tasks.get(tasks[0].id).metadata["clarification_answer"] == "first option"
    assert repos.tasks.get(tasks[1].id).status == TaskStatus.CLARIFYING


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
    assert plan.steps[0].result == "User reported: done"


@pytest.mark.asyncio
@pytest.mark.parametrize("message", ["No", "No thanks", "Not yet", "I need more time", "Please explain this"])
async def test_unconfirmed_reply_does_not_complete_or_skip_a_user_step(tmp_path, message):
    repos, audit = make_repos(tmp_path)
    plan, task = await _waiting_step(repos, audit)
    result = resume_clarifying_task(repos, audit, task, text=message, actor="test", message_id="m", received_at=utc_now())
    assert not result.accepted and "not marked this step done" in result.reason
    await PlanRunner(repos, audit).tick()
    assert repos.tasks.get(task.id).status == TaskStatus.CLARIFYING
    assert repos.plans.get(plan.id).steps[0].status == StepStatus.WAITING


@pytest.mark.asyncio
async def test_punctuated_completion_is_still_a_report_for_the_waiting_step(tmp_path):
    repos, audit = make_repos(tmp_path)
    plan, task = await _waiting_step(repos, audit)
    reply = resume_clarifying_reply(repos, audit, _reply("Done!", plan.conversation_id), plan.conversation_id)
    assert "On to the next step" in reply.text
    assert repos.tasks.get(task.id).status == TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_short_deferral_does_not_spawn_or_skip_a_user_step(tmp_path):
    repos, audit = make_repos(tmp_path)
    plan, task = await _waiting_step(repos, audit)
    reply = resume_clarifying_reply(repos, audit, _reply("Not yet", plan.conversation_id), plan.conversation_id)
    assert "not marked this step done" in reply.text
    assert repos.tasks.get(task.id).status == TaskStatus.CLARIFYING
    assert repos.plans.get(plan.id).steps[0].status == StepStatus.WAITING


@pytest.mark.asyncio
async def test_stop_does_not_silently_skip_the_waiting_step(tmp_path):
    repos, audit = make_repos(tmp_path)
    plan, task = await _waiting_step(repos, audit)
    reply = resume_clarifying_reply(repos, audit, _reply("stop", plan.conversation_id), plan.conversation_id)
    assert "Pause or Cancel" in reply.text
    assert repos.tasks.get(task.id).status == TaskStatus.CLARIFYING
    assert repos.plans.get(plan.id).steps[0].status == StepStatus.WAITING


@pytest.mark.asyncio
@pytest.mark.parametrize("reply_text", ["You choose", "Please decide for me.", "It's up to you", "Use your judgment", "Could you choose one?"])
async def test_delegating_a_pending_choice_resumes_the_step_without_completing_it(tmp_path, reply_text):
    repos, audit = make_repos(tmp_path)
    plan, task = await _waiting_step(repos, audit)
    reply = resume_clarifying_reply(repos, audit, _reply(reply_text, plan.conversation_id), plan.conversation_id)
    assert "make the choice" in reply.text
    saved = repos.tasks.get(task.id)
    step = repos.plans.get(plan.id).steps[0]
    assert saved.status == TaskStatus.RECEIVED and step.status == StepStatus.RUNNING and step.owner == StepOwner.YBM
    assert saved.metadata["plan_delegated_from_owner"] == "user" and saved.metadata["clarification_answer"] == reply_text
    assert "choice does not itself complete other parts" in saved.objective
    await PlanRunner(repos, audit).tick()
    assert repos.plans.get(plan.id).steps[0].status == StepStatus.RUNNING


@pytest.mark.asyncio
async def test_delegated_choice_does_not_complete_a_combined_step(tmp_path):
    repos, audit = make_repos(tmp_path)
    plan, task = await _waiting_step(repos, audit)
    repos.plans.update_step(plan.steps[0].id, title="Choose and buy a drive")
    resume_clarifying_reply(repos, audit, _reply("You choose", plan.conversation_id), plan.conversation_id)
    current = repos.tasks.get(task.id)
    repos.tasks.update_metadata(task.id, {**current.metadata, "synthesized_answer": "I chose drive A."}, TaskStatus.COMPLETED)
    announced = []
    async def announce(item):
        announced.append(item)
    await PlanRunner(repos, audit, announce=announce).tick()
    current, result = repos.tasks.get(task.id), repos.plans.get(plan.id)
    assert current.status == TaskStatus.COMPLETED
    assert result.steps[0].task_id != task.id
    assert repos.tasks.get(result.steps[0].task_id).status == TaskStatus.CLARIFYING
    assert result.steps[0].owner == StepOwner.USER and result.steps[0].status == StepStatus.WAITING
    assert "I chose drive A" in announced[0].metadata["clarifying_question"]
    assert result.steps[1].status == StepStatus.PENDING
    resume_clarifying_reply(repos, audit, _reply("Done", plan.conversation_id), plan.conversation_id)
    await PlanRunner(repos, audit).tick()
    assert "I chose drive A" in repos.plans.get(plan.id).steps[0].result


@pytest.mark.asyncio
async def test_explicit_choice_only_step_advances_after_the_choice(tmp_path):
    repos, audit = make_repos(tmp_path)
    plan, task = await _waiting_step(repos, audit)
    repos.plans.update_step(plan.steps[0].id, choice_only=True)
    resume_clarifying_reply(repos, audit, _reply("You choose", plan.conversation_id), plan.conversation_id)
    current = repos.tasks.get(task.id)
    repos.tasks.update_metadata(task.id, {**current.metadata, "synthesized_answer": "I chose drive A."}, TaskStatus.COMPLETED)
    await PlanRunner(repos, audit).tick()
    result = repos.plans.get(plan.id)
    assert result.steps[0].status == StepStatus.DONE and result.steps[0].result == "I chose drive A."
    assert result.steps[1].status == StepStatus.RUNNING


@pytest.mark.asyncio
async def test_changed_choice_contract_does_not_complete_the_step(tmp_path):
    repos, audit = make_repos(tmp_path)
    plan, task = await _waiting_step(repos, audit)
    repos.plans.update_step(plan.steps[0].id, choice_only=True)
    resume_clarifying_reply(repos, audit, _reply("You choose", plan.conversation_id), plan.conversation_id)
    repos.plans.update_step(plan.steps[0].id, title="Choose and buy a drive")
    current = repos.tasks.get(task.id)
    repos.tasks.update_metadata(task.id, {**current.metadata, "synthesized_answer": "I chose drive A."}, TaskStatus.COMPLETED)
    await PlanRunner(repos, audit).tick()
    result = repos.plans.get(plan.id)
    assert result.steps[0].status == StepStatus.WAITING and result.steps[1].status == StepStatus.PENDING


@pytest.mark.asyncio
async def test_decision_only_marker_without_a_recorded_choice_stays_waiting(tmp_path):
    repos, audit = make_repos(tmp_path)
    plan, task = await _waiting_step(repos, audit)
    repos.plans.update_step(plan.steps[0].id, choice_only=True)
    resume_clarifying_reply(repos, audit, _reply("You choose", plan.conversation_id), plan.conversation_id)
    current = repos.tasks.get(task.id)
    repos.tasks.update_metadata(task.id, current.metadata, TaskStatus.COMPLETED)
    await PlanRunner(repos, audit).tick()
    result = repos.plans.get(plan.id)
    assert result.steps[0].status == StepStatus.WAITING and result.steps[1].status == StepStatus.PENDING
    waiting = repos.tasks.get(result.steps[0].task_id)
    assert "could not find a recorded choice" in waiting.metadata["clarifying_question"]


@pytest.mark.asyncio
async def test_cancelling_after_delegated_choice_does_not_claim_step_was_done(tmp_path):
    repos, audit = make_repos(tmp_path)
    plan, task = await _waiting_step(repos, audit)
    resume_clarifying_reply(repos, audit, _reply("You choose", plan.conversation_id), plan.conversation_id)
    current = repos.tasks.get(task.id)
    repos.tasks.update_metadata(task.id, {**current.metadata, "synthesized_answer": "I chose drive A."}, TaskStatus.COMPLETED)
    await PlanRunner(repos, audit).cancel(plan.id)
    result = repos.plans.get(plan.id)
    assert result.status == PlanStatus.CANCELLED and result.steps[0].status == StepStatus.SKIPPED
    assert "I chose drive A" in result.steps[0].result


@pytest.mark.asyncio
async def test_delegated_access_choice_keeps_access_step_waiting(tmp_path):
    repos, audit = make_repos(tmp_path)
    plan, task = await _waiting_step(repos, audit)
    repos.plans.update_step(plan.steps[0].id, owner=StepOwner.ACCESS, choice_only=True)
    resume_clarifying_reply(repos, audit, _reply("You choose", plan.conversation_id), plan.conversation_id)
    current = repos.tasks.get(task.id)
    repos.tasks.update_metadata(task.id, {**current.metadata, "synthesized_answer": "Use provider A."}, TaskStatus.COMPLETED)
    await PlanRunner(repos, audit).tick()
    result = repos.plans.get(plan.id)
    waiting = repos.tasks.get(result.steps[0].task_id)
    assert result.steps[0].owner == StepOwner.ACCESS and result.steps[0].status == StepStatus.WAITING
    assert "It's ready" in waiting.metadata["clarifying_options"]
    assert "say it's ready" in waiting.metadata["clarifying_question"]


@pytest.mark.asyncio
async def test_delegated_step_that_cannot_be_done_pauses_instead_of_completing(tmp_path):
    repos, audit = make_repos(tmp_path)
    plan, task = await _waiting_step(repos, audit)
    resume_clarifying_reply(repos, audit, _reply("You choose", plan.conversation_id), plan.conversation_id)
    current = repos.tasks.get(task.id)
    repos.tasks.update_metadata(task.id, {**current.metadata, "last_worker_error": "A physical choice needs input"}, TaskStatus.BLOCKED)
    await PlanRunner(repos, audit).tick()
    result = repos.plans.get(plan.id)
    assert result.status == PlanStatus.PAUSED and result.steps[0].status == StepStatus.FAILED


@pytest.mark.asyncio
async def test_delegation_does_not_start_work_while_plan_is_paused(tmp_path):
    repos, audit = make_repos(tmp_path)
    plan, task = await _waiting_step(repos, audit)
    await PlanRunner(repos, audit).pause(plan.id)
    reply = resume_clarifying_reply(repos, audit, _reply("You choose", plan.conversation_id), plan.conversation_id)
    assert "plan is paused" in reply.text
    assert repos.tasks.get(task.id).status == TaskStatus.CLARIFYING
    assert repos.plans.get(plan.id).steps[0].status == StepStatus.WAITING


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


@pytest.mark.asyncio
async def test_answer_to_assistant_step_resumes_work_instead_of_completing_plan(tmp_path):
    repos, audit = make_repos(tmp_path)
    conversation = repos.conversations.get_or_create(ChannelType.TELEGRAM, "42")
    plan = repos.plans.create(Plan(
        goal="Set up a device", conversation_id=conversation, routing=dict(ROUTING),
        steps=[PlanStep(position=1, title="Install the library"), PlanStep(position=2, title="Run the script")],
    ))
    runner = PlanRunner(repos, audit)
    await runner.start(plan.id)
    plan = repos.plans.get(plan.id)
    task = repos.tasks.get(plan.steps[0].task_id)
    repos.tasks.update_metadata(task.id, {**task.metadata, "clarifying_question": "Which library?"}, TaskStatus.CLARIFYING)

    reply = resume_clarifying_reply(repos, audit, _reply("You choose", conversation), conversation)

    task = repos.tasks.get(task.id)
    assert reply.text == "Got it - resuming the task with your answer."
    assert task.status == TaskStatus.RECEIVED
    assert task.metadata["clarification_answer"] == "You choose"
    assert "plan_step_answer" not in task.metadata
    await runner.tick()
    plan = repos.plans.get(plan.id)
    assert plan.steps[0].status == StepStatus.RUNNING
    assert plan.steps[1].status == StepStatus.PENDING


@pytest.mark.asyncio
async def test_declining_handoff_on_assistant_plan_step_does_not_cancel_or_complete_it(tmp_path):
    repos, audit = make_repos(tmp_path)
    conversation = repos.conversations.get_or_create(ChannelType.TELEGRAM, "42")
    plan = repos.plans.create(Plan(
        goal="Investigate a device", conversation_id=conversation, routing=dict(ROUTING),
        steps=[PlanStep(position=1, title="Investigate"), PlanStep(position=2, title="Verify")],
    ))
    runner = PlanRunner(repos, audit)
    await runner.start(plan.id)
    task = repos.tasks.get(repos.plans.get(plan.id).steps[0].task_id)
    question = "Hand this to Claude Code?"
    repos.tasks.update_metadata(task.id, {
        **task.metadata, "clarifying_question": question,
        "claude_code_offer": {"question": question, "state": "pending"},
    }, TaskStatus.CLARIFYING)

    resume_clarifying_reply(repos, audit, _reply("no", conversation), conversation)

    task = repos.tasks.get(task.id)
    assert task.status == TaskStatus.RECEIVED
    assert task.metadata["clarification_answer"] == "no"
    await runner.tick()
    plan = repos.plans.get(plan.id)
    assert plan.steps[0].status == StepStatus.RUNNING
    assert plan.steps[1].status == StepStatus.PENDING


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
