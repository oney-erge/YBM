"""A plan as a message with the buttons that fit where it stands: what a chat shows, so a plan can be started, paused and stopped with a tap.

One place builds it, so the card under the answer that drafted the plan, the card `/plan` shows and the card after a tap are the same text and
the same buttons. Callback data is `plan:<id>:<action>` (channels/telegram.py parses it and plans/runner.py does the work).
"""

from __future__ import annotations

from typing import Any

from agent_control.plans.model import Plan, PlanStatus, StepStatus
from agent_control.plans.runner import day_text, render_plan

_FOOTERS = {
    PlanStatus.PROPOSED: "Start it when you are ready, or tell me what to change.",
    PlanStatus.ACTIVE: "I am working through it one step at a time.",
    PlanStatus.PAUSED: "It is paused. Continue when you are ready.",
    PlanStatus.DONE: "All done.",
    PlanStatus.CANCELLED: "Stopped.",
}

#: status -> the buttons under the card, as (label, action). A finished or stopped plan has none.
_BUTTONS: dict[PlanStatus, list[tuple[str, str]]] = {
    PlanStatus.PROPOSED: [("Start", "start"), ("Cancel", "cancel")],
    PlanStatus.ACTIVE: [("Pause", "pause"), ("Cancel", "cancel")],
    PlanStatus.PAUSED: [("Continue", "resume"), ("Cancel", "cancel")],
}


def _footer(plan: Plan) -> str:
    if plan.status == PlanStatus.DONE:
        skipped = sum(step.status == StepStatus.SKIPPED for step in plan.steps)
        if skipped:
            return f"Finished with {skipped} {'step' if skipped == 1 else 'steps'} skipped."
    if plan.status == PlanStatus.ACTIVE and not any(step.status in (StepStatus.RUNNING, StepStatus.WAITING) for step in plan.steps):
        held = plan.next_held()
        if held is not None:
            return f"Nothing to do until {day_text(held.due)}: {held.title}. I will start it then."
    if plan.status == PlanStatus.PROPOSED and not plan.check_in_days and plan.weeks_ahead():
        return f"{_FOOTERS[plan.status]} It runs for about {plan.weeks_ahead()} weeks: say \"check in every week\" and I will tell you how it stands."
    return _FOOTERS[plan.status]


def card_text(plan: Plan) -> str:
    return f"{render_plan(plan, results=plan.status != PlanStatus.PROPOSED)}\n\n{_footer(plan)}"[:3900]


def card_keyboard(plan: Plan) -> dict[str, Any] | None:
    buttons = _BUTTONS.get(plan.status)
    if not buttons:
        return None
    return {"inline_keyboard": [[{"text": label, "callback_data": f"plan:{plan.id}:{action}"} for label, action in buttons]]}
