"""You can talk to work that is already running.

Before this, a task in flight heard only pause/resume/cancel. Anything else
was answered as chat or spawned a second task, so "make it 5 not 3" left the
original running with 3 while a rival task started. The only channel inward
opened when the agent asked YOU a question - you could answer, never begin.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from agent_control.heartbeat import due_for_heartbeat
from agent_control.schemas import ChannelType, TaskStatus, utc_now
from agent_control.task_inbox import (
    NOTE_ENTRY_NAME,
    acknowledgement,
    attach_note,
    find_steerable_task,
)
from helpers import make_repos



def _conv(repos, name: str) -> str:
    """Tasks carry a FK to conversations, so the row has to exist first."""
    return repos.conversations.get_or_create(ChannelType.TELEGRAM, name.replace("conv_", ""))


def test_a_note_reaches_the_running_task(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    conv_a = _conv(repos, "a")
    task = repos.tasks.create("organize the folder", conversation_id=conv_a)
    repos.tasks.update_metadata(task.id, task.metadata, TaskStatus.RUNNING)

    steerable = find_steerable_task(repos, conversation_id=conv_a, chat_id=None)
    updated = attach_note(repos, audit, steerable, text="skip the archive folder", actor="user")

    history = updated.metadata["operator_history"]
    assert history[-1]["tool_name"] == NOTE_ENTRY_NAME
    assert "skip the archive folder" in history[-1]["output_summary"]
    assert updated.metadata["user_note_count"] == 1
    # Steering must not restart or re-status the task - the work continues.
    assert updated.status == TaskStatus.RUNNING


def test_notes_do_not_disturb_the_tool_history(tmp_path) -> None:
    """The `_` prefix keeps it out of "latest tool attempt" summaries, which
    scan for real tool calls."""
    repos, audit = make_repos(tmp_path)
    conv_a = _conv(repos, "a")
    task = repos.tasks.create("do a thing", conversation_id=conv_a)
    repos.tasks.update_metadata(
        task.id, {**task.metadata, "operator_history": [{"tool_name": "llm", "status": "succeeded"}]},
        TaskStatus.RUNNING,
    )

    updated = attach_note(repos, audit, repos.tasks.get(task.id), text="be brief", actor="user")

    assert updated.metadata["operator_history"][-1]["tool_name"].startswith("_")


def test_finished_tasks_are_not_steerable(tmp_path) -> None:
    repos, _audit = make_repos(tmp_path)
    conv_a = _conv(repos, "a")
    task = repos.tasks.create("already done", conversation_id=conv_a)
    repos.tasks.update_metadata(task.id, task.metadata, TaskStatus.COMPLETED)

    assert find_steerable_task(repos, conversation_id=conv_a, chat_id=None) is None


def test_the_newest_in_flight_task_wins(tmp_path) -> None:
    """Two live tasks means the operator almost certainly means the one they
    just watched start."""
    repos, _audit = make_repos(tmp_path)
    conv_a = _conv(repos, "a")
    older = repos.tasks.create("older job", conversation_id=conv_a)
    repos.tasks.update_metadata(older.id, older.metadata, TaskStatus.RUNNING)
    newer = repos.tasks.create("newer job", conversation_id=conv_a)
    repos.tasks.update_metadata(newer.id, newer.metadata, TaskStatus.RUNNING)

    assert find_steerable_task(repos, conversation_id=conv_a, chat_id=None).id == newer.id


def test_another_conversation_is_never_steered(tmp_path) -> None:
    repos, _audit = make_repos(tmp_path)
    conv_a = _conv(repos, "a")
    other = repos.tasks.create("someone else's job", conversation_id=_conv(repos, "b"))
    repos.tasks.update_metadata(other.id, other.metadata, TaskStatus.RUNNING)

    assert find_steerable_task(repos, conversation_id=conv_a, chat_id=None) is None


def test_acknowledgement_names_the_task_being_steered(tmp_path) -> None:
    """A note landing on the wrong job must be obvious at once, not at the end."""
    repos, _audit = make_repos(tmp_path)
    conv_a = _conv(repos, "a")
    task = repos.tasks.create("organize my downloads folder", conversation_id=conv_a)

    assert "organize my downloads folder" in acknowledgement(task)


def test_empty_note_changes_nothing(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    conv_a = _conv(repos, "a")
    task = repos.tasks.create("a job", conversation_id=conv_a)

    assert attach_note(repos, audit, task, text="   ", actor="user").metadata.get("user_note_count") is None


# ---- heartbeats ----------------------------------------------------------


def _task(repos, status: TaskStatus, *, updated_ago_s: int = 0):
    task = repos.tasks.create("long job", conversation_id=_conv(repos, "a"))
    metadata = dict(task.metadata)
    if updated_ago_s:
        metadata["last_heartbeat_at"] = (utc_now() - timedelta(seconds=updated_ago_s)).isoformat()
    return repos.tasks.update_metadata(task.id, metadata, status)


def test_a_quiet_running_task_is_due(tmp_path) -> None:
    repos, _audit = make_repos(tmp_path)

    assert due_for_heartbeat(_task(repos, TaskStatus.RUNNING, updated_ago_s=600), interval_seconds=300)


def test_a_task_that_just_spoke_is_not_due(tmp_path) -> None:
    repos, _audit = make_repos(tmp_path)

    assert not due_for_heartbeat(_task(repos, TaskStatus.RUNNING, updated_ago_s=10), interval_seconds=300)


@pytest.mark.parametrize(
    "status", [TaskStatus.AWAITING_APPROVAL, TaskStatus.COMPLETED, TaskStatus.CLARIFYING]
)
def test_tasks_that_already_explained_themselves_are_never_nagged(tmp_path, status) -> None:
    """A task waiting on approval, or parked on a limit, already said why.
    Repeating it on a timer is noise - and noisy heartbeats get muted, which
    loses the real ones too."""
    repos, _audit = make_repos(tmp_path)

    assert not due_for_heartbeat(_task(repos, status, updated_ago_s=99999), interval_seconds=300)


def test_what_the_person_added_is_shown_to_the_model_as_their_words_not_as_one_more_tool_result() -> None:
    """Live 11/14: "The Word one, not the PDF." was a step called _user_note in the history, and the answer listed both files."""
    from agent_control.orchestration.operator import _format_history

    history = [
        {"tool_name": "filesystem.manage", "input": {"operation": "search"}, "status": "succeeded", "output_summary": "resume.docx, resume.pdf"},
        {"tool_name": NOTE_ENTRY_NAME, "input": None, "status": "user_note", "output_summary": "The Word one, not the PDF."},
    ]
    shown = _format_history(history)
    assert '2. THE PERSON ADDED, while you were working: "The Word one, not the PDF."' in shown
    assert "Your answer has to reflect it." in shown and "_user_note" not in shown
    assert "1. filesystem.manage (succeeded)" in shown  # the real step is shown as before


def test_a_note_sent_while_a_step_runs_is_not_overwritten_by_the_step_saving_its_own_copy_of_the_history(tmp_path) -> None:
    """Verdict 2 fin2_e1: "Find the tax letter", then "sorry, the one from the council". The assistant said "Noted" and answered about the other letter: the step saved its
    copy of the history over the stored one, and the note was gone when the model next looked."""
    repos, audit = make_repos(tmp_path)
    conv = _conv(repos, "a")
    task = repos.tasks.create("Find the tax letter", conversation_id=conv)
    first = {"tool_name": "filesystem.manage", "status": "succeeded", "input": {"operation": "inspect_folder"}, "output_summary": "2 files"}
    repos.tasks.update_metadata(task.id, {**task.metadata, "operator_history": [first]}, TaskStatus.RUNNING)
    snapshot = list(repos.tasks.get(task.id).metadata["operator_history"])  # the step reads the history, then works for a while

    attach_note(repos, audit, repos.tasks.get(task.id), text="Sorry, I meant the one from the council.", actor="user")  # the person writes meanwhile

    second = {"tool_name": "filesystem.manage", "status": "succeeded", "input": {"operation": "read_file"}, "output_summary": "revenue letter"}
    saved = repos.tasks.update_metadata(task.id, {**repos.tasks.get(task.id).metadata, "operator_history": [*snapshot, second]})  # the step saves its copy
    history = saved.metadata["operator_history"]
    assert [entry["tool_name"] for entry in history] == ["filesystem.manage", "filesystem.manage", NOTE_ENTRY_NAME]
    assert "council" in history[-1]["output_summary"] and saved.metadata["user_note_count"] == 1
    again = repos.tasks.update_metadata(task.id, {**saved.metadata, "operator_history": [*history]})  # saving it again does not add it twice
    assert [entry["tool_name"] for entry in again.metadata["operator_history"]].count(NOTE_ENTRY_NAME) == 1
