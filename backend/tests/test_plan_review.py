"""A plan whose steps were marked done by replies is repaired: the steps with nothing behind them are reopened, the earlier result is kept (plans/review.py)."""

from __future__ import annotations

import pytest

from agent_control.orchestration.request_facts import CHECKS_KEY
from agent_control.plans.model import Plan, PlanStatus, PlanStep, StepOwner, StepStatus
from agent_control.plans.review import reopen_unproven
from helpers import make_repos


def _done_step(repos, position: int, title: str, result: str, history: list[dict], *, depends_on=None, metadata=None, owner=StepOwner.YBM) -> PlanStep:
    task = repos.tasks.create(title, metadata={"operator_history": history, **(metadata or {})})
    return PlanStep(position=position, title=title, owner=owner, status=StepStatus.DONE, task_id=task.id, result=result, depends_on=depends_on or [])


def test_the_steps_finished_by_replies_are_reopened_and_the_proven_ones_stay(tmp_path) -> None:
    """11/16: "Add googlecast to allowlist", "Use googlecast" and "No thanks" had marked the install, the script and its run as done."""
    repos, _audit = make_repos(tmp_path)
    steps = [
        _done_step(repos, 1, "Create a workspace", "Task workspace created", [{"tool_name": "workspace.manage", "status": "succeeded"}]),
        _done_step(repos, 2, "Install the required Python packages", "Add googlecast to allowlist", [{"tool_name": "dependencies.install", "status": "failed"}, {"tool_name": "filesystem.manage", "status": "succeeded"}], metadata={CHECKS_KEY: {"installs_software": True}}),
        _done_step(repos, 3, "Draft a script that scans the network", "Add googlecast to allowlist", []),
        _done_step(repos, 4, "Review the script", "Use googlecast", [], depends_on=[3]),
        _done_step(repos, 5, "Run the script and report what it found", "No thanks", [{"tool_name": "code.interpreter", "status": "failed"}], depends_on=[4]),
        PlanStep(position=6, title="Choose the device", owner=StepOwner.USER, status=StepStatus.DONE, result="skipped"),
    ]
    plan = repos.plans.create(Plan(goal="Cast to the TV", routing={"source_chat_id": "42"}, steps=steps, status=PlanStatus.PAUSED))

    reopened = reopen_unproven(repos, plan.id)

    assert reopened == ["Install the required Python packages", "Draft a script that scans the network", "Review the script", "Run the script and report what it found"]
    saved = repos.plans.get(plan.id)
    by_position = {step.position: step for step in saved.steps}
    assert by_position[1].status == StepStatus.DONE  # real work behind it: left alone
    assert [by_position[n].status for n in (2, 3, 4, 5)] == [StepStatus.PENDING] * 4
    assert by_position[2].result == "" and by_position[2].task_id is None
    assert "Earlier result, with nothing behind it (it needed an install and none succeeded): Add googlecast to allowlist" in by_position[2].detail
    assert "(no tool call of its task succeeded)" in by_position[3].detail and "(no tool call of its task succeeded)" in by_position[5].detail
    assert by_position[6].status == StepStatus.DONE  # a step of the person's is theirs to report
    assert "Reopened because nothing was done behind them" in saved.notes and saved.status == PlanStatus.PAUSED
    assert reopen_unproven(repos, plan.id) == []  # nothing left to reopen: running it again changes nothing


def test_a_plan_with_evidence_for_every_step_is_not_touched_and_an_unknown_plan_is_an_error(tmp_path) -> None:
    repos, _audit = make_repos(tmp_path)
    steps = [_done_step(repos, 1, "Look up the models", "Found three", [{"tool_name": "web.search", "status": "succeeded"}])]
    plan = repos.plans.create(Plan(goal="Pick a model", routing={"source_chat_id": "42"}, steps=steps, status=PlanStatus.ACTIVE))
    assert reopen_unproven(repos, plan.id) == [] and repos.plans.get(plan.id).notes == ""
    with pytest.raises(KeyError):
        reopen_unproven(repos, "plan_missing")
