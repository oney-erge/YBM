"""A plan as a message with the buttons that fit where it stands: what a chat shows, so a plan can be started, paused and stopped with a tap.

One place builds it, so the card under the answer that drafted the plan, the card `/plan` shows and the card after a tap are the same text and
the same buttons. Callback data is `plan:<id>:<action>` (channels/telegram.py parses it and plans/runner.py does the work).
"""

from __future__ import annotations

from typing import Any

from agent_control.plans.model import Plan, PlanStatus
from agent_control.plans.runner import render_plan

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


def card_text(plan: Plan) -> str:
    return f"{render_plan(plan, results=plan.status != PlanStatus.PROPOSED)}\n\n{_FOOTERS[plan.status]}"[:3900]


def card_keyboard(plan: Plan) -> dict[str, Any] | None:
    buttons = _BUTTONS.get(plan.status)
    if not buttons:
        return None
    return {"inline_keyboard": [[{"text": label, "callback_data": f"plan:{plan.id}:{action}"} for label, action in buttons]]}
