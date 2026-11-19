"""Reopen the steps of a plan that were marked done without anything done behind them.

Before steps were completed from evidence, a reply could finish one: on 2026-11-16 the replies "Add googlecast to allowlist", "Use googlecast" and "No thanks"
marked the steps "install the packages", "draft the script" and "run it" done, and the plan then waited for the person to choose among scan results that no scan had
produced. This is the inspectable repair for such plans (the earlier result is kept in the step's detail, and a note says what was reopened); it is also what a person
can ask for when a plan looks finished and is not.

A done step of the assistant's own is proven when the task behind it has a tool call that succeeded (not a check, not a question), and, when that task is known to need an
install, an install that succeeded (the same test the task itself must pass: orchestration/fulfillment.py). A step that depends on a reopened step is reopened with it.
"""

from __future__ import annotations

from agent_control.orchestration.fulfillment import validate_fulfillment
from agent_control.plans.model import Plan, PlanStatus, PlanStep, StepOwner, StepStatus
from agent_control.schemas import PostconditionType
from agent_control.storage.repositories import Repositories


def _proof_missing(repositories: Repositories, step: PlanStep) -> str | None:
    """Why a done step has nothing behind it, or None when it is proven (or cannot be checked: no task is linked)."""
    task = repositories.tasks.get(step.task_id) if step.task_id else None
    if task is None:
        return None
    history = task.metadata.get("operator_history")
    worked = any(
        isinstance(entry, dict) and entry.get("status") == "succeeded" and not str(entry.get("tool_name") or "").startswith("_")
        for entry in (history if isinstance(history, list) else [])
    )
    if not worked:
        return "no tool call of its task succeeded"
    if PostconditionType.PACKAGE_INSTALLED in validate_fulfillment(task).missing:
        return "it needed an install and none succeeded"
    return None


def reopen_unproven(repositories: Repositories, plan_id: str) -> list[str]:
    """Reopen the done steps with nothing behind them (and the steps that depend on them); the titles reopened, in order."""
    plan = repositories.plans.get(plan_id)
    if plan is None:
        raise KeyError(f"plan not found: {plan_id}")
    reopened: dict[int, str] = {}
    for step in plan.steps:
        if step.status == StepStatus.DONE and step.owner == StepOwner.YBM:
            reason = _proof_missing(repositories, step)
            if reason:
                reopened[step.position] = reason
    changed = True
    while changed:  # a step that rests on a reopened one cannot stay done
        changed = False
        for step in plan.steps:
            if step.status == StepStatus.DONE and step.position not in reopened and set(step.depends_on) & set(reopened):
                reopened[step.position] = "it depends on a step that was reopened"
                changed = True
    titles = []
    for step in plan.steps:
        if step.position not in reopened:
            continue
        earlier = f" Earlier result, with nothing behind it ({reopened[step.position]}): {step.result}" if step.result else ""
        repositories.plans.update_step(step.id, status=StepStatus.PENDING, task_id=None, result="", detail=(step.detail + earlier)[:2000].strip())
        titles.append(step.title)
    if titles:
        _note(repositories, plan, "Reopened because nothing was done behind them: " + "; ".join(titles))
        if plan.status in (PlanStatus.DONE,):
            repositories.plans.set_status(plan_id, PlanStatus.PAUSED)
    return titles


def _note(repositories: Repositories, plan: Plan, text: str) -> None:
    notes = (plan.notes + "\n" if plan.notes else "") + text
    repositories.plans.update_plan(plan.id, notes=notes[-2000:])
