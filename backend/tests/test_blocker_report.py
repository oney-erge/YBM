"""A task that cannot go on ends with a blocker report: what was tried, the ways forward, a pick; never a bare "I could not" (channels/task_notify.py)."""

from __future__ import annotations

from agent_control.channels.task_notify import format_task_message
from agent_control.schemas import TaskStatus
from helpers import make_repos


def _blocked(tmp_path, error: str, history: list[dict], **metadata):
    repos, _audit = make_repos(tmp_path)
    task = repos.tasks.create("Cast to the TV", metadata={"last_worker_error": error, "operator_history": history, **metadata})
    return task.model_copy(update={"status": TaskStatus.BLOCKED})


def test_a_blocked_task_says_what_was_tried_and_offers_ways_forward_with_a_pick(tmp_path) -> None:
    history = [
        {"tool_name": "browser.open", "input": {"operation": "search", "query": "samsung tv cast"}, "status": "succeeded"},
        {"tool_name": "computer.use", "input": {"operation": "run_goal", "goal": "open Smart View"}, "status": "failed", "error": "no window found"},
    ]
    text = format_task_message(_blocked(tmp_path, "no window found", history))

    assert text.startswith("I could not complete this request.")
    assert "What I tried:" in text and "worked" in text and "did not work" in text
    assert "Ways forward:" in text and "A. Say 'try again'" in text and "hand it to Claude Code" in text and "Tell me what you see" in text
    assert "My pick: A." in text and "keeping this open" in text


def test_a_switched_off_capability_is_the_first_way_forward_and_a_declined_hand_over_is_not_offered_again(tmp_path) -> None:
    text = format_task_message(_blocked(tmp_path, "capability disabled: desktop.control", [], declined_methods=["claude_code"]))
    assert "A. Turn the missing access on in Settings" in text
    assert "Claude Code" not in text.split("Ways forward:", 1)[1]
    assert "What I tried:" not in text  # nothing was tried, so nothing is claimed


def test_a_finished_task_has_no_report(tmp_path) -> None:
    repos, _audit = make_repos(tmp_path)
    task = repos.tasks.create("What is 2 plus 2?", metadata={"synthesized_answer": "4"}).model_copy(update={"status": TaskStatus.COMPLETED})
    assert format_task_message(task) == "4"
