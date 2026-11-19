"""Move plans forward: start the next step, settle the ones that finished, wait for people.

A plan's steps are carried out by the machinery every request already goes through. A step that is the assistant's own becomes an ordinary
task (the same Operator loop, tools, approvals and checks), addressed to the chat the plan came from. A step that is the user's becomes a
task that asks the user a question and is answered by them (the same clarification every question uses), so "buy the parts" is a message with
"Done" and "Skip" under it, not a new kind of thing to wait for.

The runner keeps no state of its own. Every pass reads the plan and its tasks from the database and does the one thing that is next, so a
restart, or several workers calling it at once, uses one transaction to claim a step and link its task.
One step at a time: the local model serves one request at a time, and the user reads one message at a time.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import date, timedelta

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
#: Allow an older worker time to finish linking a claimed step before requesting review.
CLAIM_GRACE = timedelta(seconds=60)
SKIP_WORDS = ("skip",)
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
    earlier = []
    for previous in sorted(plan.steps, key=lambda item: item.position):
        if previous.position >= step.position:
            continue
        if previous.status == StepStatus.DONE and previous.result:
            earlier.append(f"- {previous.position}. {previous.title}: {previous.result[:EARLIER_RESULT_CHARS]}")
        elif previous.status == StepStatus.SKIPPED:
            earlier.append(f"- {previous.position}. {previous.title}: skipped; no completion was recorded")
    lines = [
        f"Step {step.position} of {len(plan.steps)} of a plan.",
        f"Goal of the plan: {plan.goal}",
        f"This step: {step.title}",
    ]
    if step.detail:
        lines.append(step.detail)
    if plan.notes.strip():
        lines.append("What the user has told you about this project:\n" + plan.notes.strip())
    if earlier:
        lines.append("What the earlier steps found:\n" + "\n".join(earlier))
    if any(s.position < step.position and s.status == StepStatus.SKIPPED for s in plan.steps):
        lines.append("Do not assume skipped work happened. Check prerequisites and explain an available recovery if one is missing.")
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
    lines.append("Reply Done when it is complete, or say skip to leave this step out.")
    return "\n".join(lines)


def audit_plan(audit: AuditLogger, plan: Plan, event: str, **payload: object) -> None:
    audit.append(AuditEventType.TASK_STATE_CHANGED, actor="plan", task_id=plan.source_task_id, payload={"reason": event, "plan_id": plan.id, **payload})


def pause_plan(repositories: Repositories, audit: AuditLogger, plan_id: str) -> Plan | None:
    """Hold an active plan: nothing new starts. The step in flight finishes, and "start" carries on from there."""
    plan = repositories.plans.get(plan_id)
    if plan is not None and repositories.plans.transition_status(plan_id, PlanStatus.PAUSED, (PlanStatus.ACTIVE,)):
        audit_plan(audit, plan, "plan_paused")
    return repositories.plans.get(plan_id)


def cancel_plan(repositories: Repositories, audit: AuditLogger, plan_id: str) -> Plan | None:
    """Stop a plan for good: its open tasks are cancelled and its steps that never ran are skipped."""
    plan = repositories.plans.get(plan_id)
    if plan is not None and repositories.plans.cancel_active(plan_id):
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
        if not self.repositories.plans.transition_status(plan_id, PlanStatus.ACTIVE, (PlanStatus.PROPOSED, PlanStatus.PAUSED)):
            return self.repositories.plans.get(plan_id)
        if plan.check_in_days:
            self.repositories.plans.update_plan(plan_id, last_check_in_at=utc_now())  # the first check-in is a period from the start
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
                if await self._check_in(self.repositories.plans.get(plan.id) or plan):
                    changed += 1
            except Exception:  # noqa: BLE001 - one plan's trouble must not stop the others, or the worker
                logger.warning("plan_advance_failed", plan_id=plan.id, exc_info=True)
        return changed

    async def _advance(self, plan: Plan) -> bool:
        changed = await self._settle(plan)
        plan = self.repositories.plans.get(plan.id) or plan
        if plan.status != PlanStatus.ACTIVE:
            return changed
        if plan.finished():
            if not self.repositories.plans.finish_if_complete(plan.id):
                return changed
            self._audit(plan, "plan_done")
            await self._tell(plan, _finished_text(plan))
            return True
        if any(step.status in (StepStatus.RUNNING, StepStatus.WAITING) for step in plan.steps):
            return changed
        ready = plan.ready_steps()
        if not ready:  # nothing to do yet: the next step's day has not come near, or a step is in someone's hands
            skipped = plan.skipped_prerequisite()
            if skipped and not plan._unblocked():
                step, prerequisite = skipped
                if not self.repositories.plans.transition_status(plan.id, PlanStatus.PAUSED, (PlanStatus.ACTIVE,), expected_revision=plan.revision):
                    return changed
                self._audit(plan, "plan_paused", step=step.position, reason="prerequisite_skipped")
                await self._tell(plan, f"I paused the plan: '{step.title}' needs '{prerequisite.title}', which was skipped. "
                                 "Please revise the plan or complete that prerequisite before continuing.")
                return True
            return changed
        return await self._begin(plan, ready[0]) or changed

    async def _check_in(self, plan: Plan) -> bool:
        """Tell the user how an active plan stands when the days they asked for have passed. No model: the numbers come from the plan."""
        if plan.status != PlanStatus.ACTIVE or not plan.check_in_days:
            return False
        last = plan.last_check_in_at or plan.created_at
        if utc_now() - last < timedelta(days=plan.check_in_days):
            return False
        self.repositories.plans.update_plan(plan.id, last_check_in_at=utc_now())
        self._audit(plan, "plan_check_in")
        await self._tell(plan, check_in_text(plan))
        return True

    async def _settle(self, plan: Plan) -> bool:
        """Read the tasks of the steps that are in flight and record how each ended."""
        changed = False
        for step in plan.steps:
            if step.status not in (StepStatus.RUNNING, StepStatus.WAITING):
                continue
            if not step.task_id:
                if utc_now() - step.updated_at > CLAIM_GRACE:
                    reason = "This older step has no linked task; inspect earlier work and its effects before retrying."
                    if self.repositories.plans.settle_step(plan.id, step, None, status=StepStatus.FAILED, result=reason, pause=True):
                        self._audit(plan, "plan_paused", step=step.position, reason="legacy_unlinked_step")
                        await self._tell(plan, f"I paused '{step.title}'. {reason}")
                        changed = True
                continue
            task = self.repositories.tasks.get(step.task_id)
            if task is None:
                changed = self.repositories.plans.settle_step(plan.id, step, None, status=StepStatus.FAILED,
                                                               result="its task is gone", pause=True) or changed
            elif task.status == TaskStatus.COMPLETED:
                answer = str(task.metadata.get("plan_step_answer") or task.metadata.get("synthesized_answer") or "done")
                if task.metadata.get("plan_delegated_from_owner"):
                    contract = task.metadata.get("delegated_choice_contract")
                    choice = str(task.metadata.get("synthesized_answer") or "").strip()
                    decision_only = (task.metadata.get("plan_delegated_from_owner") == StepOwner.USER.value and step.choice_only
                                     and not step.needs and bool(choice) and contract == {"title": step.title, "detail": step.detail,
                                                                                         "needs": step.needs, "choice_only": True})
                    if decision_only:
                        changed = self.repositories.plans.settle_step(plan.id, step, task, status=StepStatus.DONE,
                                                                       result=choice[:RESULT_CHARS]) or changed
                        continue
                    completion = "say it's ready" if task.metadata["plan_delegated_from_owner"] == StepOwner.ACCESS.value else "say Done"
                    choice_line = f"My choice for '{step.title}': {choice[:RESULT_CHARS]}" if choice else f"I could not find a recorded choice for '{step.title}'."
                    tail = (f"If that completes the whole step, {completion}; otherwise complete the remaining action or tell me what is needed."
                            if choice else "The step remains open. Please choose or tell me what is needed.")
                    question = f"{choice_line}\n{tail}"
                    reopened = self.repositories.plans.return_delegated_choice(plan.id, step, task, question=question)
                    if reopened is not None:
                        if self._announce is not None:
                            await self._announce(reopened)
                        changed = True
                    continue
                if step.owner != StepOwner.YBM and task.metadata.get("plan_step_answer"):
                    answer = f"User reported: {answer}"
                changed = self.repositories.plans.settle_step(plan.id, step, task, status=StepStatus.DONE,
                                                               result=answer[:RESULT_CHARS]) or changed
            elif task.status in (TaskStatus.FAILED, TaskStatus.BLOCKED):
                reason = str(task.metadata.get("last_worker_error") or task.metadata.get("last_error") or task.status.value)
                if self.repositories.plans.settle_step(plan.id, step, task, status=StepStatus.FAILED,
                                                        result=reason[:RESULT_CHARS], pause=True):
                    self._audit(plan, "plan_paused", step=step.position, reason=reason[:200])
                    changed = True
            elif task.status == TaskStatus.CANCELLED:
                if step.status == StepStatus.WAITING:  # the user said skip
                    settled = self.repositories.plans.settle_step(plan.id, step, task, status=StepStatus.SKIPPED, result="skipped")
                else:  # the user stopped the work: the step goes back, the plan waits for them
                    settled = self.repositories.plans.settle_step(plan.id, step, task, status=StepStatus.PENDING, clear_task=True, pause=True)
                changed = settled or changed
        return changed

    async def _begin(self, plan: Plan, step: PlanStep) -> bool:
        waiting = step.owner != StepOwner.YBM
        conversation_id = plan.conversation_id
        base = {**plan.routing, "plan_id": plan.id, "plan_step_id": step.id}
        if waiting:
            question = waiting_question(plan, step)
            task = TaskRecord(
                objective=f"Plan step {step.position}: {step.title}",
                conversation_id=conversation_id,
                status=TaskStatus.CLARIFYING,
                metadata={**base, "original_message_text": step.title, "clarifying_question": question,
                          "clarifying_options": ACCESS_OPTIONS if step.owner == StepOwner.ACCESS else USER_OPTIONS},
            )
            if not self.repositories.plans.begin_step(plan, step, task):
                return False
            self._audit(plan, "plan_step_waiting", step=step.position, task_id=task.id)
            if self._announce is not None:
                await self._announce(self.repositories.tasks.get(task.id) or task)
            return True
        objective = step_objective(plan, step)
        task = TaskRecord(
            objective=objective, conversation_id=conversation_id, metadata={**base, "original_message_text": objective}
        )
        if not self.repositories.plans.begin_step(plan, step, task):
            return False
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


def _finished_text(plan: Plan) -> str:
    done = sum(1 for step in plan.steps if step.status == StepStatus.DONE)
    skipped = len(plan.steps) - done
    tail = f" ({skipped} skipped)" if skipped else ""
    return f"The plan is finished: {plan.goal}\n{done} of {len(plan.steps)} steps done{tail}."


def day_text(day: date, today: date | None = None) -> str:
    """A day as people say it: "Dec 6", with the year only when it is not this one."""
    today = today or date.today()
    text = f"{day.strftime('%b')} {day.day}"
    return text if day.year == today.year else f"{text}, {day.year}"


def check_in_text(plan: Plan, today: date | None = None) -> str:
    """How a plan stands, from the plan alone: what is done, what is next and when, what waits for the user."""
    today = today or date.today()
    done = sum(1 for step in plan.steps if step.status == StepStatus.DONE)
    skipped = sum(1 for step in plan.steps if step.status == StepStatus.SKIPPED)
    lines = [f"Check-in on your plan: {plan.goal}", f"{done} of {len(plan.steps)} steps done."]
    if skipped:
        lines.append(f"{skipped} {'step' if skipped == 1 else 'steps'} skipped.")
    waiting = [step for step in plan.steps if step.status == StepStatus.WAITING]
    if waiting:
        lines.append(f"Waiting for you: {waiting[0].title}.")
    nxt = plan.next_held(today)
    pending = [step for step in sorted(plan.steps, key=lambda s: s.position) if step.status in (StepStatus.PENDING, StepStatus.RUNNING)]
    if nxt is not None:
        lines.append(f"Next: {nxt.title}, due {day_text(nxt.due, today)}.")
    elif pending and not waiting:
        lines.append(f"Next: {pending[0].title}.")
    late = [step for step in pending if step.due is not None and step.due < today]
    if late:
        lines.append(f"Past its day: {late[0].title} (was due {day_text(late[0].due, today)}).")
    lines.append("Tell me if anything changed and I will move the plan.")
    return "\n".join(lines)


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
        when = f"{day_text(step.due)} - " if step.due is not None else ""
        lines.append(f"{marks[step.status]} {step.position}. {when}{step.title}{owners[step.owner]}")
        if step.needs and step.status in (StepStatus.PENDING, StepStatus.WAITING):
            lines.append("      needs: " + "; ".join(step.needs))
        if results and step.result and step.status == StepStatus.DONE:
            lines.append(f"      {step.result[:200]}")
    return "\n".join(lines)
