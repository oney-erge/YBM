"""Move plans forward: start the next step, settle the ones that finished, wait for people.

A plan's steps are carried out by the machinery every request already goes through. A step that is the assistant's own becomes an ordinary
task (the same Operator loop, tools, approvals and checks), addressed to the chat the plan came from. A step that is the user's becomes a
task that asks the user a question and is answered by them (the same clarification every question uses), so "buy the parts" is a message with
"Done" and "Skip" under it, not a new kind of thing to wait for.

The runner keeps no state of its own. Every pass reads the plan and its tasks from the database and does the one thing that is next, so a
restart, or several workers calling it at once, cannot lose a step or run one twice (a step is claimed atomically before its task exists).
One step at a time: the local model serves one request at a time, and the user reads one message at a time.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import timedelta

import structlog

from agent_control.plans.model import Plan, PlanStatus, PlanStep, StepOwner, StepStatus
from agent_control.schemas import AuditEventType, TaskRecord, TaskStatus, utc_now
from agent_control.storage.audit import AuditLogger
from agent_control.storage.repositories import Repositories

logger = structlog.get_logger(__name__)

#: Sends a line to the chat a plan belongs to: (routing, text). None in tests and where no channel is configured.
Say = Callable[[dict[str, str], str], Awaitable[None]]
#: Announces a task that was created and is waiting on the user (the normal task notification, with its buttons).
Announce = Callable[[TaskRecord], Awaitable[None]]

RESULT_CHARS = 1500
EARLIER_RESULT_CHARS = 300
#: A step claimed but never given a task (the process stopped between the two) is given back after this long.
CLAIM_GRACE = timedelta(seconds=60)
SKIP_WORDS = ("skip", "not now", "leave it", "leave this")
USER_OPTIONS = ["Done", "Skip this step"]
ACCESS_OPTIONS = ["It's ready", "Skip this step"]

_ROUTING_KEYS = ("source_channel", "source_chat_id", "source_sender_id")


def routing_of(task: TaskRecord) -> dict[str, str]:
    """Where a task's messages go, to be kept with a plan and given to every task the plan starts."""
    return {key: str(task.metadata[key]) for key in _ROUTING_KEYS if task.metadata.get(key)}


def is_skip(text: str) -> bool:
    lowered = (text or "").strip().lower()
    return any(lowered.startswith(word) for word in SKIP_WORDS)


def step_objective(plan: Plan, step: PlanStep) -> str:
    """What the task for a step is asked to do: the step, the goal it serves, and what the earlier steps found."""
    earlier = [
        f"- {s.position}. {s.title}: {s.result[:EARLIER_RESULT_CHARS]}"
        for s in sorted(plan.steps, key=lambda s: s.position)
        if s.position < step.position and s.status == StepStatus.DONE and s.result
    ]
    lines = [
        f"Step {step.position} of {len(plan.steps)} of a plan.",
        f"Goal of the plan: {plan.goal}",
        f"This step: {step.title}",
    ]
    if step.detail:
        lines.append(step.detail)
    if earlier:
        lines.append("What the earlier steps found:\n" + "\n".join(earlier))
    lines.append(
        "Do only this step. Answer with what you did and what you found. If it needs something you do not have, say exactly what you need."
    )
    return "\n".join(lines)


def waiting_question(plan: Plan, step: PlanStep) -> str:
    """The question for a step only the user can do, or one that needs something from them first."""
    head = f"Your turn (step {step.position} of {len(plan.steps)}): {step.title}"
    if step.owner == StepOwner.ACCESS:
        head = f"I need something from you to go on (step {step.position} of {len(plan.steps)}): {step.title}"
    lines = [head]
    if step.detail:
        lines.append(step.detail)
    if step.needs:
        lines.append("I need: " + "; ".join(step.needs))
    lines.append("Tell me when it is done, or say skip to leave this step out.")
    return "\n".join(lines)


def audit_plan(audit: AuditLogger, plan: Plan, event: str, **payload: object) -> None:
    audit.append(AuditEventType.TASK_STATE_CHANGED, actor="plan", task_id=plan.source_task_id, payload={"reason": event, "plan_id": plan.id, **payload})


def pause_plan(repositories: Repositories, audit: AuditLogger, plan_id: str) -> Plan | None:
    """Hold an active plan: nothing new starts. The step in flight finishes, and "start" carries on from there."""
    plan = repositories.plans.get(plan_id)
    if plan is not None and plan.status == PlanStatus.ACTIVE:
        repositories.plans.set_status(plan_id, PlanStatus.PAUSED)
        audit_plan(audit, plan, "plan_paused")
    return repositories.plans.get(plan_id)


def cancel_plan(repositories: Repositories, audit: AuditLogger, plan_id: str) -> Plan | None:
    """Stop a plan for good: its open tasks are cancelled and its steps that never ran are skipped."""
    plan = repositories.plans.get(plan_id)
    if plan is None or plan.status in (PlanStatus.DONE, PlanStatus.CANCELLED):
        return plan
    for step in plan.steps:
        if step.status in (StepStatus.RUNNING, StepStatus.WAITING) and step.task_id:
            task = repositories.tasks.get(step.task_id)
            if task is not None and task.status not in _FINISHED:
                repositories.tasks.update_status(task.id, TaskStatus.CANCELLED)
        if step.status in (StepStatus.PENDING, StepStatus.RUNNING, StepStatus.WAITING):
            repositories.plans.update_step(step.id, status=StepStatus.SKIPPED)
    repositories.plans.set_status(plan_id, PlanStatus.CANCELLED)
    audit_plan(audit, plan, "plan_cancelled")
    return repositories.plans.get(plan_id)


class PlanRunner:
    def __init__(
        self,
        repositories: Repositories,
        audit: AuditLogger,
        *,
        say: Say | None = None,
        announce: Announce | None = None,
    ) -> None:
        self.repositories = repositories
        self.audit = audit
        self._say = say
        self._announce = announce

    # --- what the user and the Operator ask for ---------------------------------------------------------------------------------------------

    async def start(self, plan_id: str) -> Plan | None:
        """Begin a proposed plan, or carry on with a paused one, and start its next step now instead of at the next pass."""
        plan = self.repositories.plans.get(plan_id)
        if plan is None or plan.status not in (PlanStatus.PROPOSED, PlanStatus.PAUSED):
            return plan
        self.repositories.plans.set_status(plan_id, PlanStatus.ACTIVE)
        self._audit(plan, "plan_started")
        await self._advance(self.repositories.plans.get(plan_id) or plan)
        return self.repositories.plans.get(plan_id)

    async def pause(self, plan_id: str) -> Plan | None:
        return pause_plan(self.repositories, self.audit, plan_id)

    async def cancel(self, plan_id: str) -> Plan | None:
        return cancel_plan(self.repositories, self.audit, plan_id)

    # --- one pass ---------------------------------------------------------------------------------------------------------------------------

    async def tick(self) -> int:
        """Move every active plan as far as it can go without a person. Returns how many plans changed."""
        changed = 0
        for plan in self.repositories.plans.list(statuses=[PlanStatus.ACTIVE]):
            try:
                if await self._advance(plan):
                    changed += 1
            except Exception:  # noqa: BLE001 - one plan's trouble must not stop the others, or the worker
                logger.warning("plan_advance_failed", plan_id=plan.id, exc_info=True)
        return changed

    async def _advance(self, plan: Plan) -> bool:
        changed = self._settle(plan)
        plan = self.repositories.plans.get(plan.id) or plan
        if plan.status != PlanStatus.ACTIVE:
            return changed
        if plan.finished():
            self.repositories.plans.set_status(plan.id, PlanStatus.DONE)
            self._audit(plan, "plan_done")
            await self._tell(plan, _finished_text(plan))
            return True
        if any(step.status in (StepStatus.RUNNING, StepStatus.WAITING) for step in plan.steps):
            return changed
        ready = plan.ready_steps()
        if not ready:
            return changed
        return await self._begin(plan, ready[0]) or changed

    def _settle(self, plan: Plan) -> bool:
        """Read the tasks of the steps that are in flight and record how each ended."""
        changed = False
        for step in plan.steps:
            if step.status not in (StepStatus.RUNNING, StepStatus.WAITING):
                continue
            if not step.task_id:
                if utc_now() - step.updated_at > CLAIM_GRACE:  # claimed, then the process stopped before a task existed
                    self.repositories.plans.update_step(step.id, status=StepStatus.PENDING)
                    changed = True
                continue
            task = self.repositories.tasks.get(step.task_id)
            if task is None:
                self.repositories.plans.update_step(step.id, status=StepStatus.FAILED, result="its task is gone")
                self.repositories.plans.set_status(plan.id, PlanStatus.PAUSED)
                changed = True
            elif task.status == TaskStatus.COMPLETED:
                answer = str(task.metadata.get("plan_step_answer") or task.metadata.get("synthesized_answer") or "done")
                self.repositories.plans.update_step(step.id, status=StepStatus.DONE, result=answer[:RESULT_CHARS])
                changed = True
            elif task.status in (TaskStatus.FAILED, TaskStatus.BLOCKED):
                reason = str(task.metadata.get("last_worker_error") or task.metadata.get("last_error") or task.status.value)
                self.repositories.plans.update_step(step.id, status=StepStatus.FAILED, result=reason[:RESULT_CHARS])
                self.repositories.plans.set_status(plan.id, PlanStatus.PAUSED)
                self._audit(plan, "plan_paused", step=step.position, reason=reason[:200])
                changed = True
            elif task.status == TaskStatus.CANCELLED:
                if step.status == StepStatus.WAITING:  # the user said skip
                    self.repositories.plans.update_step(step.id, status=StepStatus.SKIPPED, result="skipped")
                else:  # the user stopped the work: the step goes back, the plan waits for them
                    self.repositories.plans.update_step(step.id, status=StepStatus.PENDING, task_id=None)
                    self.repositories.plans.set_status(plan.id, PlanStatus.PAUSED)
                changed = True
        return changed

    async def _begin(self, plan: Plan, step: PlanStep) -> bool:
        waiting = step.owner != StepOwner.YBM
        if not self.repositories.plans.claim_step(step.id, StepStatus.WAITING if waiting else StepStatus.RUNNING):
            return False  # another worker took it
        conversation_id = plan.conversation_id
        base = {**plan.routing, "plan_id": plan.id, "plan_step_id": step.id}
        if waiting:
            question = waiting_question(plan, step)
            task = self.repositories.tasks.create(
                f"Plan step {step.position}: {step.title}",
                conversation_id=conversation_id,
                metadata={**base, "original_message_text": step.title, "clarifying_question": question,
                          "clarifying_options": ACCESS_OPTIONS if step.owner == StepOwner.ACCESS else USER_OPTIONS},
            )
            self.repositories.tasks.update_status(task.id, TaskStatus.CLARIFYING)
            self.repositories.plans.update_step(step.id, task_id=task.id)
            self._audit(plan, "plan_step_waiting", step=step.position, task_id=task.id)
            if self._announce is not None:
                await self._announce(self.repositories.tasks.get(task.id) or task)
            return True
        objective = step_objective(plan, step)
        task = self.repositories.tasks.create(
            objective, conversation_id=conversation_id, metadata={**base, "original_message_text": objective}
        )
        self.repositories.plans.update_step(step.id, task_id=task.id)
        self.audit.append(AuditEventType.TASK_CREATED, actor="plan", task_id=task.id, payload={"plan_id": plan.id, "step": step.position})
        await self._tell(plan, f"Step {step.position} of {len(plan.steps)}: {step.title}")
        return True

    async def _tell(self, plan: Plan, text: str) -> None:
        if self._say is None or not plan.routing:
            return
        try:
            await self._say(plan.routing, text)
        except Exception:  # noqa: BLE001 - a missed line is not worth stopping a plan for
            logger.debug("plan_say_failed", plan_id=plan.id, exc_info=True)

    def _audit(self, plan: Plan, event: str, **payload: object) -> None:
        audit_plan(self.audit, plan, event, **payload)


_FINISHED = {TaskStatus.COMPLETED, TaskStatus.BLOCKED, TaskStatus.FAILED, TaskStatus.CANCELLED}


def _finished_text(plan: Plan) -> str:
    done = sum(1 for step in plan.steps if step.status == StepStatus.DONE)
    skipped = len(plan.steps) - done
    tail = f" ({skipped} skipped)" if skipped else ""
    return f"The plan is finished: {plan.goal}\n{done} of {len(plan.steps)} steps done{tail}."


def render_plan(plan: Plan, *, results: bool = False) -> str:
    """The plan as text for a chat: the goal, one line per step with where it stands and who does it."""
    marks = {
        StepStatus.PENDING: "[ ]", StepStatus.RUNNING: "[...]", StepStatus.WAITING: "[you]", StepStatus.DONE: "[x]",
        StepStatus.FAILED: "[!]", StepStatus.SKIPPED: "[-]",
    }
    owners = {StepOwner.YBM: "", StepOwner.USER: " (you)", StepOwner.ACCESS: " (needs you first)"}
    lines = [f"Plan: {plan.goal}"]
    if plan.summary:
        lines.append(plan.summary)
    for step in sorted(plan.steps, key=lambda s: s.position):
        lines.append(f"{marks[step.status]} {step.position}. {step.title}{owners[step.owner]}")
        if step.needs and step.status in (StepStatus.PENDING, StepStatus.WAITING):
            lines.append("      needs: " + "; ".join(step.needs))
        if results and step.result and step.status == StepStatus.DONE:
            lines.append(f"      {step.result[:200]}")
    return "\n".join(lines)
