"""Progress messages should say what is happening and why.

The running/retrying update read "Latest attempt: filesystem.manage ended
with succeeded" - the tool name and nothing else. Which file, which query,
and the model's own stated reason for choosing that step were all available
and none of them were shown, so "I want to see it happening" meant opening a
trace after the fact.
"""

from __future__ import annotations

from agent_control.channels.task_notify import _latest_attempt_summary, format_task_message
from agent_control.schemas import TaskRecord, TaskStatus


def _task(history: list[dict]) -> TaskRecord:
    return TaskRecord(
        id="task_1", objective="do the thing", status=TaskStatus.RUNNING,
        metadata={"operator_history": history},
    )


def test_summary_names_the_operation_and_target() -> None:
    summary = _latest_attempt_summary(_task([
        {
            "tool_name": "filesystem.manage",
            "input": {"operation": "read_file", "path": "C:/Users/me/budget.csv"},
            "status": "succeeded",
        }
    ]))

    assert "read_file" in summary
    assert "budget.csv" in summary


def test_summary_includes_the_models_own_reason() -> None:
    summary = _latest_attempt_summary(_task([
        {
            "tool_name": "web.search",
            "input": {"operation": "search", "query": "postmortem template"},
            "reasoning": "Need current sources before summarising.",
            "status": "succeeded",
        }
    ]))

    assert "postmortem template" in summary
    assert "Need current sources" in summary


def test_errors_are_still_reported_for_failed_steps() -> None:
    summary = _latest_attempt_summary(_task([
        {
            "tool_name": "filesystem.manage",
            "input": {"operation": "inspect_folder", "root": "/nope"},
            "status": "failed",
            "error": "path is outside allowed roots",
        }
    ]))

    assert "failed" in summary
    assert "outside allowed roots" in summary


def test_a_step_with_no_input_still_summarises() -> None:
    """Older history entries predate the richer fields, and must not crash
    the notification path."""
    summary = _latest_attempt_summary(_task([{"tool_name": "task.status", "status": "succeeded"}]))

    assert "task.status" in summary


def test_no_history_yields_nothing() -> None:
    assert _latest_attempt_summary(_task([])) is None


_RAW_REPORT = (
    "invalid input for filesystem.manage: 1 validation error for FilesystemSearchInput\n"
    "query\n"
    "  Field required [type=missing, input_value={'operation': 'search'}, input_type=dict]\n"
    "    For further information visit https://errors.pydantic.dev/2.14/v/missing"
)


def test_a_failed_step_does_not_leak_the_raw_validation_report() -> None:
    summary = _latest_attempt_summary(_task([
        {
            "tool_name": "filesystem.manage",
            "input": {"operation": "search", "root": "desktop"},
            "status": "failed",
            "error": _RAW_REPORT,
        }
    ]))

    assert "missing required input (query)" in summary
    assert "pydantic" not in summary and "input_value" not in summary


def test_a_failed_task_message_uses_the_plain_sentence_too() -> None:
    task = TaskRecord(
        id="task_2", objective="find a txt", status=TaskStatus.FAILED,
        metadata={"last_worker_error": _RAW_REPORT},
    )

    message = format_task_message(task)

    assert "The filesystem.manage step was missing required input (query)." in message
    assert "pydantic" not in message and "type=missing" not in message
