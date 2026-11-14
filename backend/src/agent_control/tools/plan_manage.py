"""Draft, show and change a plan: the Operator's way to turn a goal too big for one task into steps that each have an owner (plans/).

The Operator drafts the plan (the steps are its own writing, from what it knows and what the user said); this tool checks that the plan is
a plan (steps in order, an owner on each, dependencies that point backwards, a length that fits a phone) and keeps it. It never runs a step:
the user starts a plan, and plans/runner.py carries it out one step at a time, each as an ordinary task.
"""

from __future__ import annotations

import inspect
from typing import Any

from pydantic import ValidationError

from agent_control.config import AppSettings
from agent_control.plans.model import MAX_STEPS, Plan, PlanStatus, PlanStep, StepOwner, StepStatus
from agent_control.plans.planner import can_do_text, draft_plan
from agent_control.plans.runner import audit_plan, cancel_plan, pause_plan, render_plan, routing_of
from agent_control.schemas import Capability, ToolCallRequest, ToolCallResult, ToolResultStatus
from agent_control.storage.audit import AuditLogger
from agent_control.storage.repositories import Repositories
from agent_control.tools.contracts import PlanManageInput, PlanManageOutput
from agent_control.tools.spec import Adapters, Definitions, RegistryDeps, ToolDefinition, capability_enabled, failed_result, same_output_schema

#: Where an unfinished plan can be found again: the newest one of this chat that has not ended.
_OPEN = [PlanStatus.PROPOSED, PlanStatus.ACTIVE, PlanStatus.PAUSED]


def _steps_from(raw: list[dict[str, Any]]) -> list[PlanStep]:
    if not 1 <= len(raw) <= MAX_STEPS:
        raise ValueError(f"a plan has between 1 and {MAX_STEPS} steps, not {len(raw)}; split a bigger goal into plans")
    steps = []
    for position, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            raise ValueError(f"step {position} must be an object with a title")
        try:
            step = PlanStep(position=position, **{k: v for k, v in item.items() if k in _STEP_FIELDS})
        except ValidationError as exc:
            raise ValueError(f"step {position} is not valid: {exc.errors()[0]['msg']}") from exc
        if any(dep >= position or dep < 1 for dep in step.depends_on):
            raise ValueError(f"step {position} depends on steps {step.depends_on}; a step can only wait for earlier ones")
        steps.append(step)
    return steps


_STEP_FIELDS = {"title", "detail", "owner", "needs", "depends_on", "ability"}


class PlanManageAdapter:
    def __init__(self, repositories: Repositories, audit: AuditLogger, provider: Any | None = None) -> None:
        self.repositories = repositories
        self.audit = audit
        # The model that drafts a plan when the Operator gives only the goal (plans/planner.py).
        self.provider = provider
        self._catalog: Any | None = None

    def bind(self, catalog: Any) -> None:
        """The ability index, so the planner is told what the assistant can do and what it cannot."""
        self._catalog = catalog

    def _can_do(self) -> str:
        return can_do_text(self._catalog)

    async def execute(self, request: ToolCallRequest) -> ToolCallResult:
        operation = str(request.input.get("operation") or "show")
        task = self.repositories.tasks.get(request.task_id)
        try:
            handler = {
                "create": self._create, "show": self._show, "add_step": self._add_step,
                "update_step": self._update_step, "set_status": self._set_status,
            }.get(operation)
            if handler is None:
                return failed_result(request, f"unsupported plan operation: {operation}")
            outcome = handler(request, task)
            plan, text = await outcome if inspect.isawaitable(outcome) else outcome
        except ValueError as exc:
            return failed_result(request, str(exc))
        if task is not None and plan.status in _OPEN:
            # Whatever the Operator did to a plan that has not ended, the plan goes under this task's answer as a card with the buttons that
            # fit where it stands, in whatever words the model wraps it in. A plan the model only mentioned never gets a card.
            self.repositories.tasks.update_metadata(task.id, {**(self.repositories.tasks.get(task.id) or task).metadata, "plan_card": plan.id})
        output: dict[str, Any] = {
            "operation": operation, "summary": text.splitlines()[0][:200], "plan_id": plan.id,
            "plan": {"id": plan.id, "status": plan.status.value, "goal": plan.goal, "steps": len(plan.steps)},
            "terminal_output": [{"content": text, "exit_code": 0, "is_final": True}],
        }
        return ToolCallResult(request_id=request.id, status=ToolResultStatus.SUCCEEDED, output=output)

    # --- operations -------------------------------------------------------------------------------------------------------------------------

    async def _create(self, request: ToolCallRequest, task: Any) -> tuple[Plan, str]:
        data = request.input
        goal = str(data.get("goal") or "").strip()[:500]
        summary = str(data.get("summary") or "").strip()[:1200]
        raw_steps = list(data.get("steps") or [])
        if not raw_steps:
            raw_steps, drafted = await self._draft(goal, task)
            summary = summary or drafted
        steps = _steps_from(raw_steps)
        plan = self.repositories.plans.create(
            Plan(
                conversation_id=task.conversation_id if task else None, goal=goal, summary=summary,
                source_task_id=request.task_id, routing=routing_of(task) if task else {}, steps=steps,
            )
        )
        audit_plan(self.audit, plan, "plan_proposed")
        return plan, (
            f"Plan drafted and saved (not started).\n{render_plan(plan)}\n"
            "Answer the user with a short message that shows this plan and asks them to start it: Start and Cancel buttons are attached "
            "to your answer. Do not start any step yourself."
        )

    async def _draft(self, goal: str, task: Any) -> tuple[list[dict[str, Any]], str]:
        """The steps for a goal the Operator gave no steps for: the planner's draft, from what the user said and what the assistant can do."""
        if self.provider is None:
            raise ValueError("give the steps yourself: no planner model is available here")
        said = str(getattr(task, "objective", "") or "")
        try:
            draft = await draft_plan(self.provider, goal, said, self._can_do())
        except Exception as exc:  # noqa: BLE001 - say what happened; the Operator can write the steps itself
            raise ValueError(f"the planner could not draft the plan ({type(exc).__name__}); give the steps yourself") from exc
        if not draft.steps:  # the goal is too vague to plan: the Operator asks the user the planner's question
            raise ValueError(f"the goal is too vague to plan. Ask the user this one question (ask_user): {draft.question}")
        return [step.model_dump() for step in draft.steps], draft.summary

    def _find(self, request: ToolCallRequest, task: Any) -> Plan:
        plan_id = str(request.input.get("plan_id") or "").strip()
        if plan_id:
            plan = self.repositories.plans.get(plan_id)
        else:
            candidates = self.repositories.plans.list(statuses=_OPEN, conversation_id=task.conversation_id if task else None, limit=1)
            plan = candidates[0] if candidates else None
        if plan is None:
            raise ValueError("there is no plan to work on here; create one first")
        return plan

    def _show(self, request: ToolCallRequest, task: Any) -> tuple[Plan, str]:
        plan = self._find(request, task)
        return plan, f"{render_plan(plan, results=True)}\nStatus: {plan.status.value}."

    def _add_step(self, request: ToolCallRequest, task: Any) -> tuple[Plan, str]:
        plan = self._find(request, task)
        if plan.status in (PlanStatus.DONE, PlanStatus.CANCELLED):
            raise ValueError(f"the plan is {plan.status.value}")
        if len(plan.steps) >= MAX_STEPS:
            raise ValueError(f"a plan has at most {MAX_STEPS} steps")
        data = request.input
        position = min(int(data.get("position") or len(plan.steps) + 1), len(plan.steps) + 1)
        step = _steps_from([{k: v for k, v in data.items() if k in _STEP_FIELDS}])[0].model_copy(update={"position": position})
        self.repositories.plans.add_step(plan.id, step)
        updated = self.repositories.plans.get(plan.id) or plan
        return updated, f"Step added.\n{render_plan(updated)}"

    def _update_step(self, request: ToolCallRequest, task: Any) -> tuple[Plan, str]:
        plan = self._find(request, task)
        data = request.input
        step = plan.step(int(data["position"]))
        if step is None:
            raise ValueError(f"the plan has no step {data['position']}")
        fields: dict[str, Any] = {}
        for name in ("title", "detail", "needs", "depends_on", "ability"):
            if data.get(name) is not None:
                fields[name] = data[name]
        if data.get("owner"):
            fields["owner"] = StepOwner(data["owner"])
        if data.get("status"):
            wanted = StepStatus(data["status"])
            if wanted not in (StepStatus.DONE, StepStatus.SKIPPED, StepStatus.PENDING):
                raise ValueError("a step can be set to done, skipped or pending")
            fields["status"] = wanted
            if wanted == StepStatus.DONE and not step.result:
                fields["result"] = "marked done"
        if not fields:
            raise ValueError("nothing to change: give a title, detail, owner, needs, depends_on, ability or status")
        self.repositories.plans.update_step(step.id, **fields)
        updated = self.repositories.plans.get(plan.id) or plan
        return updated, f"Step {step.position} updated.\n{render_plan(updated)}"

    def _set_status(self, request: ToolCallRequest, task: Any) -> tuple[Plan, str]:
        plan = self._find(request, task)
        wanted = PlanStatus(request.input["status"])
        if wanted == PlanStatus.CANCELLED:
            updated = cancel_plan(self.repositories, self.audit, plan.id) or plan
        elif wanted == PlanStatus.PAUSED:
            updated = pause_plan(self.repositories, self.audit, plan.id) or plan
        else:
            if plan.status not in (PlanStatus.PROPOSED, PlanStatus.PAUSED):
                raise ValueError(f"the plan is {plan.status.value}, so it cannot be started")
            self.repositories.plans.set_status(plan.id, PlanStatus.ACTIVE)  # the runner starts its first step on its next pass
            updated = self.repositories.plans.get(plan.id) or plan
        return updated, f"The plan is now {updated.status.value}.\n{render_plan(updated)}"


def register(deps: RegistryDeps, definitions: Definitions, adapters: Adapters) -> None:
    settings: AppSettings = deps.settings
    enabled = deps.repositories is not None and deps.audit_logger is not None and settings.plans.enabled and capability_enabled(
        settings, Capability.AGENT_CORE
    )
    definitions.append(
        ToolDefinition(
            name="plan.manage",
            group="planning",
            provides=("plan", "project", "steps", "roadmap", "milestone", "build", "organize", "prepare", "goal", "design", "launch"),
            capability=Capability.AGENT_CORE,
            enabled=enabled,
            named_when_disabled=False,
            description=(
                "turn a goal too big for one task (build something, set something up, plan a trip) into a plan the user can start: give the "
                "goal and a planner drafts the steps, each owned by you (ybm), by the user (user) or by you once the user gives you something "
                "(access); the plan is saved and the user starts it. Use this instead of asking a list of questions: at most one question first"
            ),
            operations=("create", "show", "add_step", "update_step", "set_status"),
            input_schema=PlanManageInput,
            output_schema=PlanManageOutput,
            operation_output_schemas=same_output_schema(("create", "show", "add_step", "update_step", "set_status"), PlanManageOutput),
            default_operation="show",
            examples=(
                {"operation": "create", "goal": "Build a small indoor quadcopter with a camera and a small language model on it, about 300 dollars"},
                {"operation": "show"},
                {"operation": "update_step", "position": 1, "status": "done"},
            ),
        )
    )
    if deps.repositories is not None and deps.audit_logger is not None:
        adapters["plan.manage"] = PlanManageAdapter(deps.repositories, deps.audit_logger, deps.provider)  # type: ignore[arg-type]
