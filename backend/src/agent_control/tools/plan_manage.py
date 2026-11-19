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
from agent_control.orchestration.initiative import initiative_guidance
from agent_control.plans.model import MAX_CHECK_IN_DAYS, MAX_STEPS, Plan, PlanStatus, PlanStep, StepOwner, StepStatus, in_date_order
from agent_control.plans.planner import can_do_text, draft_plan, revise_plan, too_short
from agent_control.plans.runner import audit_plan, cancel_plan, pause_plan, render_plan, routing_of
from agent_control.schemas import Capability, ToolCallRequest, ToolCallResult, ToolResultStatus, utc_now
from agent_control.storage.audit import AuditLogger
from agent_control.storage.repositories import Repositories
from agent_control.tools.contracts import PlanManageInput, PlanManageOutput
from agent_control.tools.spec import Adapters, Definitions, RegistryDeps, ToolDefinition, capability_enabled, failed_result, same_output_schema

#: Where an unfinished plan can be found again: the newest one of this chat that has not ended.
_OPEN = [PlanStatus.PROPOSED, PlanStatus.ACTIVE, PlanStatus.PAUSED]
#: The most days a shift may move the steps: a year either way.
MAX_SHIFT_DAYS = 366


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
    return in_date_order(steps)


_STEP_FIELDS = {"title", "detail", "owner", "choice_only", "needs", "depends_on", "ability", "due"}
#: Each line a person tells the assistant about the project, and how many lines a plan keeps (the oldest go first).
NOTE_CHARS = 300


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
                "create": self._create, "show": self._show, "revise": self._revise, "add_step": self._add_step,
                "update_step": self._update_step, "set_status": self._set_status, "set_check_in": self._set_check_in,
                "shift_dates": self._shift_dates, "add_note": self._add_note,
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
        metadata = getattr(task, "metadata", {}) or {}
        said = initiative_guidance(metadata.get("initiative_level")) + "\n" + str(getattr(task, "objective", "") or "")
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
        return plan, f"{render_plan(plan, results=True, details=True)}\nStatus: {plan.status.value}."

    async def _revise(self, request: ToolCallRequest, task: Any) -> tuple[Plan, str]:
        """The user wants the plan changed ("revise it", "add X", "I do not see Y in it"): the planner, which wrote it, rewrites the steps that have not started, from the
        plan as it stands and the user's words. Steps that started stay. This is one call with one argument; editing steps one by one is for small changes."""
        plan = self._find(request, task)
        if plan.status in (PlanStatus.DONE, PlanStatus.CANCELLED):
            raise ValueError(f"the plan is {plan.status.value}")
        instruction = " ".join(str(request.input.get("instruction") or "").split())[:800]
        if not instruction:
            raise ValueError("give the instruction: what the user wants changed, in their words")
        if self.provider is None:
            raise ValueError("no planner model is available here to revise the plan; change it with update_step and add_step")
        kept = sum(1 for step in plan.steps if step.status != StepStatus.PENDING or step.task_id is not None)
        before = len(plan.steps) - kept
        try:
            asked = {"kept": kept, "steps_before": before}
            draft = await revise_plan(self.provider, plan.goal, render_plan(plan, results=True, details=True), instruction, self._can_do(), **asked)
            if draft.steps and too_short(len(draft.steps), before):
                draft = await revise_plan(self.provider, plan.goal, render_plan(plan, results=True, details=True), instruction, self._can_do(), insist=True, **asked)
        except Exception as exc:  # noqa: BLE001 - say what happened; the Operator can change the steps itself
            raise ValueError(f"the planner could not revise the plan ({type(exc).__name__}); change it with update_step and add_step") from exc
        if not draft.steps:
            raise ValueError("the planner gave no steps; say the change in other words")
        if too_short(len(draft.steps), before):
            # A plan that lost most of its steps is not a revision. The plan is left as it was, and the Operator is told how to make the change.
            raise ValueError(
                f"the planner returned {len(draft.steps)} steps for a plan of {before}, so the plan was left as it was; make the change with add_step or update_step, "
                "or say the instruction in more detail"
            )
        steps = []
        for index, item in enumerate(draft.steps, start=1):
            fields = {k: v for k, v in item.model_dump().items() if k in _STEP_FIELDS}
            fields["depends_on"] = [kept + dep for dep in fields.get("depends_on") or [] if 1 <= dep < index]
            try:
                steps.append(PlanStep(position=kept + index, **fields))
            except ValidationError as exc:
                raise ValueError(f"revised step {index} is not valid: {exc.errors()[0]['msg']}") from exc
        self.repositories.plans.replace_unstarted_steps(plan.id, steps, summary=draft.summary or plan.summary, expected_revision=plan.revision)
        version = 2 + sum(1 for line in plan.notes.splitlines() if line.startswith("- Revised"))
        notes = [*[line for line in plan.notes.splitlines() if line.strip()], f"- Revised (version {version}): {instruction[:200]}"]
        while len("\n".join(notes)) > 2000:
            notes.pop(0)
        self.repositories.plans.update_plan(plan.id, notes="\n".join(notes))
        updated = self.repositories.plans.get(plan.id) or plan
        return updated, (
            f"Plan revised (version {version}); it is the same plan, changed.\n{render_plan(updated, details=True)}\n"
            "Answer the user with a short message that says what changed in the plan and where to read it: the card with Start and Cancel is attached. "
            "Do not start any step yourself."
        )

    def _add_step(self, request: ToolCallRequest, task: Any) -> tuple[Plan, str]:
        plan = self._find(request, task)
        if plan.status in (PlanStatus.DONE, PlanStatus.CANCELLED):
            raise ValueError(f"the plan is {plan.status.value}")
        if len(plan.steps) >= MAX_STEPS:
            raise ValueError(f"a plan has at most {MAX_STEPS} steps")
        data = request.input
        position = min(int(data.get("position") or len(plan.steps) + 1), len(plan.steps) + 1)
        try:
            step = PlanStep(position=position, **{k: v for k, v in data.items() if k in _STEP_FIELDS})
        except ValidationError as exc:
            raise ValueError(f"step is not valid: {exc.errors()[0]['msg']}") from exc
        self.repositories.plans.add_step(plan.id, step, expected_revision=plan.revision)
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
        if any(name in fields for name in ("title", "detail", "needs")) or data.get("owner"):
            fields["choice_only"] = False
        if data.get("choice_only") is not None:
            if step.status != StepStatus.PENDING:
                raise ValueError("change the decision-only marker before the step starts")
            if not isinstance(data["choice_only"], bool):
                raise ValueError("choice_only must be true or false")
            fields["choice_only"] = data["choice_only"]
        if data.get("due") is not None:
            wanted_day = PlanStep(position=1, title="x", due=data["due"]).due
            if wanted_day is None and str(data["due"]).strip():
                raise ValueError("a day is written YYYY-MM-DD")
            fields["due"] = wanted_day
        if data.get("owner"):
            fields["owner"] = StepOwner(data["owner"])
        if data.get("status"):
            wanted = StepStatus(data["status"])
            if wanted not in (StepStatus.DONE, StepStatus.SKIPPED, StepStatus.PENDING):
                raise ValueError("a step can be set to done, skipped or pending")
            if wanted == StepStatus.DONE:
                raise ValueError("completion is recorded by the plan runner after its linked task finishes or the user reports their step done")
            fields["status"] = wanted
        if not fields:
            raise ValueError("nothing to change: give a title, detail, owner, needs, depends_on, ability, due or status")
        if not self.repositories.plans.edit_pending_step(plan, step, **fields):
            raise ValueError("the plan changed or the step has already started; check its status before editing")
        updated = self.repositories.plans.get(plan.id) or plan
        return updated, f"Step {step.position} updated.\n{render_plan(updated)}"

    def _set_check_in(self, request: ToolCallRequest, task: Any) -> tuple[Plan, str]:
        """The user asked to hear how the plan stands every so many days. The first check-in comes that many days from now."""
        plan = self._find(request, task)
        days = request.input.get("days")
        if days is None or not 0 <= int(days) <= MAX_CHECK_IN_DAYS:
            raise ValueError(f"days is how often to check in, 1 to {MAX_CHECK_IN_DAYS}, or 0 to stop")
        self.repositories.plans.update_plan(plan.id, check_in_days=int(days), last_check_in_at=utc_now())
        updated = self.repositories.plans.get(plan.id) or plan
        said = f"I will tell the user how the plan stands every {int(days)} days while it is active." if int(days) else "No more check-ins."
        return updated, f"{said}\n{render_plan(updated)}"

    def _shift_dates(self, request: ToolCallRequest, task: Any) -> tuple[Plan, str]:
        """The project slipped, or moved: every step that is not finished moves by the same number of days."""
        plan = self._find(request, task)
        if plan.status in (PlanStatus.DONE, PlanStatus.CANCELLED):
            raise ValueError(f"the plan is {plan.status.value}")
        days = request.input.get("days")
        if days is None or not -MAX_SHIFT_DAYS <= int(days) <= MAX_SHIFT_DAYS or int(days) == 0:
            raise ValueError(f"days is how far to move the unfinished steps, between -{MAX_SHIFT_DAYS} and {MAX_SHIFT_DAYS}, not 0")
        moved = self.repositories.plans.shift_dates(plan.id, int(days), expected_revision=plan.revision)
        if not moved:
            raise ValueError("no unfinished step has a day to move")
        updated = self.repositories.plans.get(plan.id) or plan
        return updated, f"{moved} steps moved by {int(days)} days.\n{render_plan(updated)}"

    def _add_note(self, request: ToolCallRequest, task: Any) -> tuple[Plan, str]:
        """Something the user said about the project that later steps need (the place, the budget, a choice): kept with the plan."""
        plan = self._find(request, task)
        note = " ".join(str(request.input.get("note") or "").split())[:NOTE_CHARS]
        if not note:
            raise ValueError("give the note: one fact the user told you about this project")
        lines = [*[line for line in plan.notes.splitlines() if line.strip()], f"- {note}"]
        while len("\n".join(lines)) > 2000:
            lines.pop(0)
        self.repositories.plans.update_plan(plan.id, notes="\n".join(lines))
        updated = self.repositories.plans.get(plan.id) or plan
        return updated, f"Noted with the plan: {note}"

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
            if not self.repositories.plans.transition_status(plan.id, PlanStatus.ACTIVE, (PlanStatus.PROPOSED, PlanStatus.PAUSED)):
                raise ValueError("the plan changed before it could be started; check its status and retry")
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
                "(access), each with a day when the goal has a date or a stretch of time; the plan is saved and the user starts it. To change the plan the "
                "user is looking at (\"revise it\", \"add X\", \"I do not see Y in it\") use revise with the user's words as the instruction: the planner rewrites the steps "
                "that have not started and the plan stays the same plan. Also: check in "
                "every N days (set_check_in), move the unfinished days when the project slips (shift_dates), keep a fact the user told you with "
                "the plan (add_note). Edit unstarted steps only; the runner records completion from the linked task or user report. "
                "Use this instead of asking a list of questions: at most one question first"
            ),
            operations=("create", "show", "revise", "add_step", "update_step", "set_status", "set_check_in", "shift_dates", "add_note"),
            input_schema=PlanManageInput,
            output_schema=PlanManageOutput,
            operation_output_schemas=same_output_schema(
                ("create", "show", "revise", "add_step", "update_step", "set_status", "set_check_in", "shift_dates", "add_note"), PlanManageOutput
            ),
            default_operation="show",
            examples=(
                {"operation": "create", "goal": "Build a small indoor quadcopter with a camera and a small language model on it, about 300 dollars"},
                {"operation": "show"},
                {"operation": "revise", "instruction": "Add a short animation when the result appears, and say where the images come from."},
                {"operation": "update_step", "position": 1, "title": "Compare the available options"},
                {"operation": "set_check_in", "days": 7},
                {"operation": "shift_dates", "days": 14},
                {"operation": "add_note", "note": "The plot is 600 square meters outside Bursa; the budget is 250,000 euros."},
            ),
        )
    )
    if deps.repositories is not None and deps.audit_logger is not None:
        adapters["plan.manage"] = PlanManageAdapter(deps.repositories, deps.audit_logger, deps.provider)  # type: ignore[arg-type]
