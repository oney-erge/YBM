"""What YBM says while it works and when it stops: plain sentences, one progress bubble,
a straight answer to "how's it going?", and no endless retries against a rule."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from agent_control.channels.base import answer_status_question
from agent_control.channels.task_notify import format_task_message, progress_text
from agent_control.channels.telegram_notifications import TelegramTaskNotifier
from agent_control.orchestration.worker import _policy_refusal, _repeat_block_message, _repeated_no_progress_call
from agent_control.schemas import (
    ChannelType, InboundMessage, MessageKind, TaskRecord, TaskStatus, utc_now,
)
from agent_control.task_inbox import is_status_question, status_reply
from helpers import make_repos


def _task(status=TaskStatus.RUNNING, history=None, **metadata) -> TaskRecord:
    return TaskRecord(
        id="task_1", objective="find my resume", status=status,
        metadata={"source_channel": "telegram", "source_chat_id": "42", **metadata,
                  **({"operator_history": history} if history is not None else {})},
    )


def _step(tool, operation, status="succeeded", **extra):
    return {"tool_name": tool, "input": {"operation": operation}, "status": status, **extra}


# ---- progress and failure wording ---------------------------------------------------------

def test_progress_is_one_plain_sentence():
    text = format_task_message(_task(history=[_step("filesystem.manage", "search")]))
    assert text == "Working on it: looking through your files…"


def test_progress_hides_tool_names_outcome_codes_and_model_reasoning():
    text = format_task_message(_task(history=[_step("filesystem.manage", "search", reasoning="Because the user wants it")]))
    for leaked in ("filesystem.manage", "search", "succeeded", "already_done", "Why:", "Latest attempt"):
        assert leaked not in text


def test_progress_after_a_failed_step_says_so_without_codes():
    text = format_task_message(_task(history=[_step("browser.control", "fill_form", "denied", error="risk_exceeds_capability_policy")]))
    assert "did not work" in text
    assert "risk_exceeds" not in text and "denied" not in text


def test_progress_with_no_steps_yet():
    assert format_task_message(_task(history=[])) == "On it."


def test_progress_says_how_long_a_long_job_has_run():
    task = _task(history=[_step("workspace.manage", "write_files")])
    task = task.model_copy(update={"created_at": utc_now() - timedelta(minutes=5)})
    assert progress_text(task).endswith("(5 min so far)")


def test_a_blocked_task_explains_what_was_tried_in_words():
    task = _task(TaskStatus.BLOCKED, history=[_step("browser.control", "fill_form", "denied", error="risk_exceeds_capability_policy")],
                 last_worker_error="I can't go on with filling in a form: your safety settings do not allow that action.")
    text = format_task_message(task)
    assert "I can't go on with filling in a form" in text
    assert "The last thing I tried was filling in a form, and I was not allowed to." in text
    assert "browser.control" not in text and "fill_form" not in text


def test_an_answer_with_paths_is_readable(tmp_path):
    folder = tmp_path / "Reports"
    folder.mkdir()
    task = _task(TaskStatus.COMPLETED, synthesized_answer=f"The report is {folder / 'q3.pdf'} and it is 3 pages.")
    text = format_task_message(task)
    assert "q3.pdf (in " in text
    assert text.endswith("and it is 3 pages.")


# ---- one progress bubble ------------------------------------------------------------------

class _Client:
    def __init__(self):
        self.calls = []

    async def send_message(self, chat_id, text, reply_markup=None):
        self.calls.append(("send", chat_id, text))
        return {"result": {"message_id": 7}}

    async def edit_message_text(self, chat_id, message_id, text):
        self.calls.append(("edit", chat_id, message_id, text))

    async def delete_message(self, chat_id, message_id):
        self.calls.append(("delete", chat_id, message_id))

    async def send_photo_file(self, *args, **kwargs):  # pragma: no cover - not used here
        raise AssertionError


def _notify(notifier, task):
    asyncio.run(notifier.notify(task))


def test_progress_edits_the_one_message_and_is_rate_limited():
    client = _Client()
    clock = iter([100.0, 100.5, 103.0])
    notifier = TelegramTaskNotifier(client, clock=lambda: next(clock))
    base = dict(progress_message_id="55", source_chat_id="42")
    _notify(notifier, _task(history=[_step("filesystem.manage", "search")], **base))
    _notify(notifier, _task(history=[_step("filesystem.manage", "search"), _step("filesystem.manage", "read_file")], **base))
    _notify(notifier, _task(history=[_step("filesystem.manage", "search"), _step("filesystem.manage", "read_file")], **base))
    kinds = [c[0] for c in client.calls]
    assert kinds == ["edit", "edit"]  # the second came 0.5 s after the first and was skipped
    assert client.calls[0][2] == "55"
    assert "looking through your files" in client.calls[0][3]
    assert "reading a file" in client.calls[1][3]


def test_the_answer_is_a_new_message_and_the_progress_bubble_goes_away():
    client = _Client()
    notifier = TelegramTaskNotifier(client)
    _notify(notifier, _task(TaskStatus.COMPLETED, progress_message_id="55", synthesized_answer="It is 13."))
    assert [c[0] for c in client.calls] == ["send", "delete"]
    assert client.calls[0][2] == "It is 13."
    assert client.calls[1][2] == "55"


def test_without_an_anchor_progress_still_sends_a_message():
    client = _Client()
    _notify(TelegramTaskNotifier(client), _task(history=[_step("filesystem.manage", "search")]))
    assert [c[0] for c in client.calls] == ["send"]


# ---- "how's it going?" --------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "how's it going?", "any update?", "what are you doing right now?", "Are you done yet?", "status",
    "how long will it take?", "is it done?", "where are you at", "hey, how are things going",
])
def test_status_questions_are_recognised(text):
    assert is_status_question(text)


@pytest.mark.parametrize("text", [
    "what is the status of bitcoin?", "how is the weather going to be", "how long is the Nile?",
    "find my resume", "what are you?", "update my resume", "",
])
def test_other_requests_are_not_mistaken_for_status_questions(text):
    assert not is_status_question(text)


def _conv(repos):
    return repos.conversations.get_or_create(ChannelType.TELEGRAM, "42")


def _running(repos, objective="Build a recipe app", steps=2):
    task = repos.tasks.create(objective, conversation_id=_conv(repos), metadata={"source_chat_id": "42"})
    history = [_step("workspace.manage", "write_files") for _ in range(steps - 1)] + [_step("workspace.manage", "launch_static")]
    return repos.tasks.update_metadata(task.id, {**task.metadata, "operator_history": history}, TaskStatus.RUNNING)


def test_status_reply_describes_the_running_task(tmp_path):
    repos, _ = make_repos(tmp_path)
    _running(repos)
    reply = status_reply(repos, conversation_id=_conv(repos), chat_id="42")
    assert 'Still on "Build a recipe app"' in reply
    assert "2 steps so far" in reply
    assert "starting the app" in reply
    assert "workspace.manage" not in reply


def test_status_reply_when_waiting_for_the_user(tmp_path):
    repos, _ = make_repos(tmp_path)
    task = repos.tasks.create("Sign me up", conversation_id=_conv(repos), metadata={"source_chat_id": "42", "clarifying_question": "What is your name?"})
    repos.tasks.update_metadata(task.id, task.metadata, TaskStatus.CLARIFYING)
    reply = status_reply(repos, conversation_id=_conv(repos), chat_id="42")
    assert "waiting for your answer" in reply and "What is your name?" in reply


def test_status_reply_after_a_recent_finish_and_after_a_long_time(tmp_path):
    repos, _ = make_repos(tmp_path)
    task = repos.tasks.create("Count my games", conversation_id=_conv(repos), metadata={"source_chat_id": "42"})
    repos.tasks.update_metadata(task.id, task.metadata, TaskStatus.COMPLETED)
    reply = status_reply(repos, conversation_id=_conv(repos), chat_id="42")
    assert reply == 'Nothing is running right now. The last thing was "Count my games": I finished that.'


def test_status_reply_is_none_with_no_tasks(tmp_path):
    repos, _ = make_repos(tmp_path)
    assert status_reply(repos, conversation_id=_conv(repos), chat_id="42") is None


def test_answer_status_question_makes_no_task(tmp_path):
    repos, _ = make_repos(tmp_path)
    running = _running(repos)
    inbound = InboundMessage(channel=ChannelType.TELEGRAM, chat_id="42", sender_id="1", text="what are you doing right now?",
                             kind=MessageKind.TEXT)
    outbound = answer_status_question(repos, inbound, _conv(repos))
    assert outbound is not None and "Build a recipe app" in outbound.text
    assert [t.id for t in repos.tasks.list_recent(10)] == [running.id]


# ---- a rule is not a hiccup ---------------------------------------------------------------

def _denied(operation):
    return _step("browser.control", operation, "denied", error="risk_exceeds_capability_policy")


def test_one_policy_refusal_is_information_not_a_stop():
    assert _policy_refusal([_denied("fill_form")]) is None


def test_two_policy_refusals_in_a_row_are_a_wall():
    assert _policy_refusal([_denied("fill_form"), _denied("fill_form_step")]) == ("browser.control", "fill_form_step")
    assert _repeated_no_progress_call([_denied("fill_form"), _denied("fill_form_step")]) == ("browser.control", "fill_form_step")


def test_a_different_tool_between_refusals_is_not_a_wall():
    history = [_denied("fill_form"), _step("computer.use", "observe"), _denied("fill_form")]
    assert _policy_refusal(history) is None


def test_an_ordinary_failure_is_not_treated_as_a_policy_wall():
    failed = _step("browser.control", "fill_form", "failed", error="element not found")
    assert _policy_refusal([failed, failed]) is None


def test_the_wall_message_is_a_sentence_for_a_person():
    message = _repeat_block_message([_denied("fill_form"), _denied("fill_form_step")], "browser.control:fill_form_step")
    assert message.startswith("I can't go on with filling in a form")
    assert "safety settings" in message
    assert "browser.control" not in message and "risk_exceeds" not in message
