"""Tell the Operator that a plan is open in this chat, so "go ahead", "change step 3" and "how is it going" mean that plan.

A message about a plan arrives as an ordinary request. Without a line in the prompt the Operator has no reason to look for one, and "how is
it going" becomes a guess. When a plan that has not ended is open in the task's chat, a few lines say what it is and which tool changes it;
a chat with no open plan adds nothing.
"""

from __future__ import annotations

from agent_control.plans.model import PlanStatus, StepStatus
from agent_control.storage.repositories import PlanRepository

OPEN_STATUSES = [PlanStatus.PROPOSED, PlanStatus.ACTIVE, PlanStatus.PAUSED]
_MARKS = {
    StepStatus.PENDING: "to do", StepStatus.RUNNING: "being done now", StepStatus.WAITING: "waiting for the user", StepStatus.DONE: "done",
    StepStatus.FAILED: "failed", StepStatus.SKIPPED: "skipped",
}


def open_plan_note(plans: PlanRepository, conversation_id: str | None) -> str:
    if conversation_id is None:
        return ""
    found = plans.list(statuses=OPEN_STATUSES, conversation_id=conversation_id, limit=1)
    if not found:
        return ""
    plan = found[0]
    lines = ["\n\n## A plan is open in this chat", f"Goal: {plan.goal} (the plan is {plan.status.value})"]
    for step in sorted(plan.steps, key=lambda s: s.position)[:12]:
        when = f" (due {step.due.isoformat()})" if step.due is not None else ""
        lines.append(f"{step.position}. {step.title}{when} - {_MARKS[step.status]}")
    if plan.notes.strip():
        lines.append("What the user has told you about it:\n" + plan.notes.strip())
    if plan.check_in_days:
        lines.append(f"You check in with the user every {plan.check_in_days} days.")
    lines.append(
        "If the user is talking about this plan, use plan.manage to show it. When they ask to change it (\"revise it\", \"add X\", \"I do not see Y in it\") use revise, with their "
        "words as the instruction: the planner rewrites the steps that have not started and it stays the same plan, not a new one. For one small edit, edit an unstarted step, add a step or change the plan status. "
        "The runner records completion; if a user-owned step is waiting, ask the user to reply Done on that step rather than declaring it done. "
        "If it slips or moves, shift_dates moves every unfinished step; if they ask to be checked on, set_check_in; "
        "a fact they give about the project goes in with add_note. A new, unrelated request is not about the plan."
    )
    return "\n".join(lines)
