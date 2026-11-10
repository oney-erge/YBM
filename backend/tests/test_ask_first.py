"""A request that says to ask first makes the question part of the work: no guard may turn the question away."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_control.orchestration.worker import (
    _clarification_recovery_reason,
    _satisfied_task_does_not_need_clarification,
    _user_asked_to_be_asked_first,
)
from helpers import make_repos

READ = [{
    "tool_name": "filesystem.manage", "input": {"operation": "read_file", "path": "C:/x/retention-policy.md"},
    "status": "succeeded", "output_summary": "Read retention-policy.md. Audit events are retained for 400 days.",
}]


def _task(tmp_path, objective: str):
    repos, _audit = make_repos(tmp_path)
    task = repos.tasks.create(objective)
    return task.model_copy(update={"metadata": {
        **task.metadata, "operator_history": READ, "expected_postconditions": ["file_organization"], "original_message_text": objective,
    }})


@pytest.mark.parametrize("text", [
    "Rewrite the file so it says 30 days. Ask me before changing anything on disk.",
    "Check with me first before deleting anything.",
    "Wait for my approval and then send it.",
    "I want your permission before you move files.",
    "Let me approve the plan.",
    "Please confirm with me before sending.",
])
def test_requests_that_say_to_ask_first_are_recognised(text) -> None:
    class Task:
        metadata: dict = {}
        objective = text

    assert _user_asked_to_be_asked_first(Task()) is True


@pytest.mark.parametrize("text", [
    "Rewrite the file so it says 30 days.",
    "Ask the team lead about it in the doc.",
    "Tell me what changed.",
    "Remind me to ask my doctor about the form.",
])
def test_ordinary_requests_are_not(text) -> None:
    class Task:
        metadata: dict = {}
        objective = text

    assert _user_asked_to_be_asked_first(Task()) is False


def test_the_already_satisfied_guard_stands_down_for_an_ask_first_request(tmp_path, monkeypatch) -> None:
    # Live: the request's inferred postcondition counted as met once the file was read.
    monkeypatch.setattr(
        "agent_control.orchestration.worker.validate_fulfillment",
        lambda task: SimpleNamespace(expected=True, ok=True, missing=(), first_gap=None),
    )
    plain = _task(tmp_path / "a", "Rewrite C:/x/retention-policy.md so it says 30 days instead of 400.")
    asked = _task(tmp_path / "b", "Rewrite C:/x/retention-policy.md so it says 30 days instead of 400. Ask me before changing anything on disk.")

    assert _satisfied_task_does_not_need_clarification(plain, READ) is True   # the old behavior, kept for ordinary requests
    assert _satisfied_task_does_not_need_clarification(asked, READ) is False  # the question is the work
    assert _clarification_recovery_reason(plain, READ, "Shall I change it?") is not None
    assert _clarification_recovery_reason(asked, READ, "Shall I change the retention line?") is None
