"""A request that a skill marks as a project goes to the planner, not the Operator loop.

Which requests are projects is not decided here: a skill says so in its frontmatter (`route: plan`) and the decision model confirms it for a
request (abilities/context.py), and the worker (which owns every task's lifecycle) hands such a task to this stage before its first
Operator step. The stage only draws up what should happen and reports it; it changes no task.

    ask    the goal cannot be told at all: one short question with tap-able options, asked once
    plan   the plan is saved (proposed, not started) and its card goes under the task's answer
    none   the planner could not be reached or did not answer with a plan: the Operator loop takes the request as it always did
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import structlog

from agent_control.plans.model import MAX_STEPS, Plan, PlanStep, in_date_order
from agent_control.orchestration.initiative import initiative_guidance
from agent_control.plans.planner import draft_plan
from agent_control.plans.runner import audit_plan, render_plan, routing_of
from agent_control.schemas import TaskRecord
from agent_control.storage.audit import AuditLogger
from agent_control.storage.repositories import Repositories

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class StageOutcome:
    kind: str  # "ask" | "plan" | "none"
    question: str = ""
    options: tuple[str, ...] = ()
    plan: Plan | None = None


async def run_plan_stage(
    repositories: Repositories, audit: AuditLogger, provider: Any, can_do: str, task: TaskRecord
) -> StageOutcome:
    # Intake may have resolved a short follow-up into a complete objective. Keep
    # the user's original words as context, but don't save an unresolved "it"
    # as the goal that every future step will receive.
    original = str(task.metadata.get("original_message_text") or "").strip()
    goal = task.objective.split("\n[Question to the user:", 1)[0].strip() or original
    context = initiative_guidance(task.metadata.get("initiative_level")) + str(task.metadata.get("goal_notes") or "")
    if original and original != goal:
        context += f"\nThe user's message: {original}"
    context += "\n" + task.objective
    memory = str(task.metadata.get("memory_context") or "").strip()
    if memory:
        context += f"\nRecent conversation context (may be incomplete):\n{memory}"
    asked_before = int(task.metadata.get("clarify_count", 0)) > 0
    try:
        draft = await draft_plan(provider, goal[:500], context, can_do, may_ask=not asked_before)
    except Exception:  # noqa: BLE001 - the Operator loop is the fallback for everything this stage cannot do
        logger.warning("plan_stage_draft_failed", task_id=task.id, exc_info=True)
        return StageOutcome("none")
    if not draft.steps:
        if asked_before:
            return StageOutcome("none")
        return StageOutcome("ask", question=draft.question.strip(), options=tuple(o.strip() for o in draft.options if o.strip()))
    try:
        steps = [PlanStep(position=i, **step.model_dump()) for i, step in enumerate(draft.steps[:MAX_STEPS], 1)]
        if any(dep >= step.position or dep < 1 for step in steps for dep in step.depends_on):
            steps = [step.model_copy(update={"depends_on": []}) for step in steps]  # a dependency that points forward is dropped, not trusted
        steps = in_date_order(steps)
        plan = repositories.plans.create(Plan(
            conversation_id=task.conversation_id, goal=goal[:500], summary=draft.summary.strip()[:1200],
            source_task_id=task.id, routing=routing_of(task), steps=steps,
        ))
    except Exception:  # noqa: BLE001
        logger.warning("plan_stage_plan_invalid", task_id=task.id, exc_info=True)
        return StageOutcome("none")
    audit_plan(audit, plan, "plan_proposed")
    return StageOutcome("plan", plan=plan)


def answer_for(plan: Plan) -> str:
    """The text the task finishes with: the plan, short. (The chat shows the card, with its buttons, in its place.)"""
    offer = f"It runs for about {plan.weeks_ahead()} weeks: say \"check in every week\" and I will tell you how it stands.\n\n" if plan.weeks_ahead() else ""
    return f"{render_plan(plan, details=True)}\n\n{offer}Start it when you are ready, or tell me what to change."
