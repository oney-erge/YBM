from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime
import json
import logging
import os
from pathlib import Path
import re
from typing import Any, Protocol
from uuid import uuid4

from agent_control.channels.memory import ConversationMemoryService
from agent_control.error_text import describe_exception
from agent_control.logging_setup import bind_task_context
from agent_control.orchestration.auditor import AuditorService
from agent_control.orchestration.executor import ToolExecutor
from agent_control.orchestration.fulfillment import (
    deliverable_evidence,
    fulfillment_guidance,
    validate_fulfillment,
)
from agent_control.orchestration.operator import OperatorLoopService
from agent_control.orchestration.signals import sweep_expired_approvals
from agent_control.recovery import RetryPolicy
from agent_control.recovery.usage_limits import describe_wait, next_attempt_at
from agent_control.schemas import (
    ApprovalRequest,
    ApprovalStatus,
    AuditEventType,
    ErrorClass,
    LLMCallRecord,
    OperatorAction,
    OperatorDecision,
    ParallelToolCall,
    PostconditionType,
    RiskLevel,
    TaskRecord,
    TaskStatus,
    ToolCallRequest,
    ToolCallResult,
    ToolResultStatus,
    new_id,
    utc_now,
)
from agent_control.storage.audit import AuditLogger
from agent_control.storage.redaction import redact_payload
from agent_control.storage.repositories import Repositories
from agent_control.tools.mcp_client import mcp_output_text

logger = logging.getLogger(__name__)

class TaskNotificationSink(Protocol):
    async def notify(self, task: TaskRecord) -> None:
        ...

WORKABLE_STATUSES = [
    TaskStatus.RECEIVED,
    TaskStatus.INTERPRETING,
    TaskStatus.PLANNED,
    TaskStatus.RUNNING,
    TaskStatus.RETRYING,
]

NOTIFIABLE_STATUSES = {
    TaskStatus.AWAITING_APPROVAL,
    TaskStatus.AWAITING_EXTERNAL,
    TaskStatus.BLOCKED,
    TaskStatus.CANCELLED,
    TaskStatus.CLARIFYING,
    TaskStatus.COMPLETED,
    TaskStatus.FAILED,
    TaskStatus.RETRYING,
    TaskStatus.RUNNING,
}

# Statuses that only make sense while a specific worker process is actively
# holding them. If a task is still RUNNING or INTERPRETING when a *new*
# worker starts up, the process that was supposed to finish it is gone
# (crashed, killed, restarted before its claim naturally expired via
# claim_next()'s claim_expiry_seconds). Left alone, the task would either
# sit stuck until that stale claim eventually times out, or get silently
# re-claimed and re-run from a status that assumes in-flight state which no
# longer exists - re-running from the top could duplicate side effects (a
# second Telegram send, a second file write), and there's no checkpoint to
# resume from mid-flight. See docs/HISTORY.md P6.
ORPHANABLE_STATUSES = (TaskStatus.RUNNING, TaskStatus.INTERPRETING)

# Pseudo-entries the fulfillment/audit gap paths append to operator_history so
# the next decide() call can see why `done` was rejected. They are observations,
# not work - _tool_call_count() excludes them from the step budget.
CHECK_ENTRY_FULFILLMENT = "_fulfillment_check"
CHECK_ENTRY_AUDIT = "_audit_check"
CHECK_ENTRY_CLARIFICATION = "_clarification_check"
CHECK_ENTRY_NAMES = frozenset(
    {CHECK_ENTRY_FULFILLMENT, CHECK_ENTRY_AUDIT, CHECK_ENTRY_CLARIFICATION}
)

def _in_flight_is_ambiguous(in_flight: dict[str, Any]) -> bool:
    """Whether a call caught mid-dispatch might have changed something.

    A read that never returned can simply be run again. A write might have
    half-happened, and nobody can tell from here - not the worker, and not the
    user unless they are told. Risk level is the signal the policy engine
    already uses for "consequential", so it is the signal used here.
    """
    if str(in_flight.get("risk_level") or "low") in {"high", "critical"}:
        return True
    capability = str(in_flight.get("capability") or "")
    return any(
        capability.startswith(prefix)
        for prefix in ("filesystem.write", "terminal", "desktop", "browser.control", "vscode")
    )


# A clean rejection (the request never actually ran) versus an unknown
# outcome (the request may have reached the far end before the connection
# was lost). Only the second kind is worth warning the model about before
# it retries a write - a 429/quota response means nothing happened yet.
_AMBIGUOUS_COMPLETION_ERRORS = {ErrorClass.TRANSIENT}


def _retry_error_text(result: ToolCallResult, request: ToolCallRequest | None) -> str | None:
    """The error text recorded in history for an automatically-retried call.

    Reuses _in_flight_is_ambiguous's own "is this a risky write" judgment
    (same reasoning as reconcile_orphaned_tasks' crash recovery: a plain
    retry silently re-decided by the model next step is exactly the
    "silently retrying can do a thing twice" risk that function exists to
    avoid for a crash - here nothing crashed, but a TIMEOUT/TRANSIENT result
    is the same kind of unknown outcome, not the clean pre-execution
    rejection a RATE_LIMITED/USAGE_LIMITED result is). The next decide()
    call sees this text verbatim (operator.py's _format_history), so this is
    the only lever available to make that risk visible to the model instead
    of a generic "timed out" it will most naturally read as "try again".
    """
    text = result.error_message
    if request is None:
        return text
    ambiguous_outcome = result.status == ToolResultStatus.TIMEOUT or result.error_class in _AMBIGUOUS_COMPLETION_ERRORS
    if not ambiguous_outcome:
        return text
    risky = _in_flight_is_ambiguous({"risk_level": request.risk_level.value, "capability": request.capability.value})
    if not risky:
        return text
    base = text or "the call did not return in time"
    return f"{base} - this may have already taken effect before the connection was lost; verify before repeating it."


def reconcile_orphaned_tasks(repositories: Repositories, audit: AuditLogger) -> int:
    """Recover tasks left RUNNING/INTERPRETING by a worker that never finished.

    This used to fail every one of them, because "re-running from the top could
    duplicate side effects and there's no checkpoint to resume from mid-flight".
    There is one now: operator_history records every completed call, and
    operator_in_flight names the single call that was dispatched but had not
    returned. That turns one unanswerable question into three answerable ones:

      - nothing was in flight -> resume; the history says what is already done
      - a read was in flight   -> resume; running it again is harmless
      - a write was in flight  -> ask the user, because it may have half-run

    Only the third case interrupts anyone, and guessing there is worse than
    asking: silently retrying can do a thing twice, silently skipping can leave
    the job half done. Returns the number of tasks reconciled.
    """
    total = 0
    seen: set[str] = set()
    while True:
        orphaned = [
            task
            for task in repositories.tasks.list_by_statuses(list(ORPHANABLE_STATUSES), limit=100)
            if task.id not in seen
        ]
        if not orphaned:
            break
        for task in orphaned:
            seen.add(task.id)
            in_flight = task.metadata.get("operator_in_flight")
            in_flight = in_flight if isinstance(in_flight, dict) else None

            if in_flight is not None and _in_flight_is_ambiguous(in_flight):
                question = (
                    f"I was interrupted part-way through `{in_flight.get('tool_name')}` "
                    f"and cannot tell whether it finished. Check whether it took effect, "
                    f"then tell me to continue or to redo that step."
                )
                metadata = {
                    **{k: v for k, v in task.metadata.items() if k != "operator_in_flight"},
                    "clarifying_question": question,
                    "interrupted_step": in_flight,
                }
                updated = repositories.tasks.update_metadata(task.id, metadata, TaskStatus.CLARIFYING)
                reason = "interrupted mid-write; asking the user before continuing"
            else:
                metadata = {
                    k: v for k, v in task.metadata.items()
                    if k not in {"operator_in_flight", "last_worker_error"}
                }
                if in_flight is not None:
                    # Harmless to repeat, so the loop simply runs it again.
                    metadata["resumed_after_interrupt"] = in_flight.get("tool_name")
                updated = repositories.tasks.update_metadata(task.id, metadata, TaskStatus.RUNNING)
                reason = "resumed after an interrupted run"

            repositories.tasks.release_claim(updated.id)
            audit.task_state_changed("worker", task.id, task.status, updated.status)
            audit.append(
                AuditEventType.ERROR if updated.status == TaskStatus.CLARIFYING else AuditEventType.TASK_STATE_CHANGED,
                actor="worker",
                task_id=task.id,
                payload={"error": reason, "status": updated.status.value},
            )
            total += 1
    return total

class TaskWorker:
    # Default per-task wall-clock budget. A single task that exceeds this is
    # forcibly transitioned to FAILED so the worker can move on to the queue.
    # Override globally via ``settings.limits.task_budget_seconds`` (config) or
    # per-task via ``task.metadata["task_budget_seconds"]``.
    DEFAULT_TASK_BUDGET_SECONDS: float = 600.0
    # A delegated sub-task (docs/HISTORY.md Part 4 T1.2) gets its own small,
    # fixed step budget, independent of operator_max_steps - the whole point
    # of delegation is bounding how much a sub-task can explore before it
    # must report back, not inheriting the parent's full budget.
    DELEGATE_MAX_STEPS: int = 6

    def __init__(
        self,
        repositories: Repositories,
        audit: AuditLogger,
        executor: ToolExecutor | None = None,
        retry_policy: RetryPolicy | None = None,
        config_context: str = "No extra capability context provided.",
        config_context_factory: Callable[[], str] | None = None,
        notification_sink: TaskNotificationSink | None = None,
        task_budget_seconds: float | None = None,
        operator: OperatorLoopService | None = None,
        operator_max_steps: int = 12,
        auditor: AuditorService | None = None,
        persist_llm_calls: bool = True,
        llm_call_max_chars: int = 8000,
        redact_patterns: list[str] | tuple[str, ...] | None = None,
        fulfillment_mode: str = "auditor",
        audit_min_tool_calls: int = 2,
        persona_config: object | None = None,
        skills_config: object | None = None,
    ) -> None:
        self.repositories = repositories
        self.audit = audit
        self.executor = executor
        self.retry_policy = retry_policy
        self.config_context = config_context
        self.config_context_factory = config_context_factory
        self.notification_sink = notification_sink
        self.task_budget_seconds = (
            float(task_budget_seconds)
            if task_budget_seconds is not None
            else self.DEFAULT_TASK_BUDGET_SECONDS
        )
        # The observe/decide/act loop (docs/HISTORY.md P3 §2.2) - the sole
        # execution path. See orchestration/operator.py.
        self.operator = operator
        self.operator_max_steps = operator_max_steps
        # The Auditor (docs/HISTORY.md P3 §2.1) - grounds a `done` decision
        # against raw tool output before letting it complete. Optional: a
        # worker with no auditor configured skips straight to the
        # fulfillment-gap check, same as before this existed.
        self.auditor = auditor
        # Only the Auditor can own fulfillment, so a worker built without one
        # keeps the legacy gate rather than silently losing the check entirely.
        self._auditor_owns_fulfillment = fulfillment_mode == "auditor" and auditor is not None
        self.audit_min_tool_calls = max(1, int(audit_min_tool_calls))
        # None disables the persona learning pass entirely (tests, and any
        # embedder that never configured a persona file).
        self.persona_config = persona_config
        self.skills_config = skills_config
        # LLM-call persistence (docs/UI_UX_AUDIT.md Phase 14d) - the receipts
        # behind the Duration view's real (non-inferred) segments. See
        # _record_llm_call below.
        self.persist_llm_calls = persist_llm_calls
        self.llm_call_max_chars = llm_call_max_chars
        self.redact_patterns = redact_patterns
        # Stable id per worker process; written into tasks.claimed_by so
        # concurrent workers can't race on the same task.
        self.worker_id = f"worker-{uuid4().hex[:12]}"

    async def process_next(self) -> TaskRecord | None:
        # Atomically claim a task. Two workers running this call simultaneously
        # will produce at most one successful claim - the other returns None
        # and tries again on the next poll. Claim expires after budget+buffer
        # so a crashed worker doesn't strand the task.
        claim_expiry = int(self.task_budget_seconds) + 120
        task = self.repositories.tasks.claim_next(
            WORKABLE_STATUSES,
            worker_id=self.worker_id,
            claim_expiry_seconds=claim_expiry,
        )
        if task is None:
            return None
        budget = float(task.metadata.get("task_budget_seconds") or self.task_budget_seconds)
        try:
            processed = await asyncio.wait_for(self.process_task(task.id), timeout=budget)
            await self._notify_if_needed(processed)
            await self._maybe_learn_preference(processed)
            await self._maybe_learn_skill(processed)
            # Once the task is terminal, drop the claim so it can't be
            # accidentally re-claimed by a stale lookup. Best-effort.
            if processed.status in {
                TaskStatus.COMPLETED,
                TaskStatus.FAILED,
                TaskStatus.BLOCKED,
                TaskStatus.CANCELLED,
                TaskStatus.AWAITING_EXTERNAL,
                TaskStatus.AWAITING_APPROVAL,
            }:
                self.repositories.tasks.release_claim(processed.id)
            return processed
        except asyncio.TimeoutError:
            # Wall-clock budget exceeded - fail the task so the worker can keep
            # moving. This is the only way a stuck planner/tool call doesn't
            # starve every queued task behind it.
            latest = self.repositories.tasks.get(task.id) or task
            reason = f"task budget {budget:.0f}s exceeded; forcibly failed by worker"
            metadata = {**latest.metadata, "last_worker_error": reason}
            failed = self.repositories.tasks.update_metadata(latest.id, metadata, TaskStatus.FAILED)
            self.repositories.tasks.release_claim(failed.id)
            self.audit.task_state_changed("worker", latest.id, latest.status, failed.status)
            self.audit.append(
                AuditEventType.ERROR,
                actor="worker",
                task_id=latest.id,
                payload={"error": reason, "status": failed.status.value, "budget_seconds": budget},
            )
            await self._notify_if_needed(failed)
            return failed
        except Exception as exc:
            latest = self.repositories.tasks.get(task.id)
            if latest is None:
                raise
            metadata = {**latest.metadata, "last_worker_error": str(exc)}
            failed = self.repositories.tasks.update_metadata(latest.id, metadata, TaskStatus.FAILED)
            self.repositories.tasks.release_claim(failed.id)
            self.audit.task_state_changed("worker", latest.id, latest.status, failed.status)
            self.audit.append(
                AuditEventType.ERROR,
                actor="worker",
                task_id=latest.id,
                payload={"error": str(exc), "status": failed.status.value},
            )
            await self._notify_if_needed(failed)
            return failed

    async def run_forever(self, poll_interval_seconds: float = 3.0) -> None:
        consecutive_poll_failures = 0
        while True:
            try:
                processed = await self.process_next()
                consecutive_poll_failures = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # `process_next` handles task-scoped failures. What can still
                # escape here is infrastructure around claiming/persisting or
                # a secondary failure while reporting the first one. One such
                # transient must not terminate every parallel loop via
                # asyncio.gather and orphan an unrelated in-flight task.
                consecutive_poll_failures += 1
                logger.exception(
                    "worker_poll_failed",
                    extra={"consecutive_failures": consecutive_poll_failures},
                )
                try:
                    self.audit.append(
                        AuditEventType.ERROR,
                        actor="worker",
                        payload={
                            "error": "worker_poll_failed",
                            "reason": str(exc),
                            "consecutive_failures": consecutive_poll_failures,
                        },
                    )
                except Exception:
                    logger.exception("worker_poll_failure_audit_failed")
                if consecutive_poll_failures >= 5:
                    raise
                await asyncio.sleep(min(poll_interval_seconds * consecutive_poll_failures, 30.0))
                continue
            # AWAITING_APPROVAL is deliberately NOT in WORKABLE_STATUSES
            # (docs/UI_UX_AUDIT.md Phase 8, second pass) - claim_next never
            # re-selects it, the same way it already never re-selected
            # AWAITING_EXTERNAL. A task landing there has its claim released
            # in process_next above, freeing this worker to claim a
            # different queued task on its very next poll instead of
            # re-picking the same blocked one (the actual bug: with the
            # default max_parallel_tasks=1, a pending approval used to
            # prevent the one worker from ever reaching a later task).
            # orchestration/signals.py's requeue_after_approval_decision()
            # is the other half - it flips the task back to RUNNING the
            # moment a decision lands, so it becomes claimable again without
            # needing this same task re-polled.
            #
            # That handles a human deciding in time. sweep_expired_approvals()
            # is the timeout side of the same gap: nothing else calls it, so
            # without this a task whose approval simply expires - nobody
            # decided either way - sits in AWAITING_APPROVAL forever for an
            # operator who never opens the admin console (Telegram/WhatsApp
            # only). Every tick, not just when a task was actually claimed,
            # since the tasks it sweeps are never claimable by process_next.
            sweep_expired_approvals(self.repositories)
            if processed is None:
                await asyncio.sleep(poll_interval_seconds)

    async def process_task(self, task_id: str) -> TaskRecord:
        # Rebind (not merge) so every log line for this tick is greppable by
        # task_id alone - one grep, the whole story, instead of correlating
        # timestamps across an unstructured stdout capture (docs/HISTORY.md §2.1).
        # Rebinding fresh each call matters: this worker's asyncio Task is
        # long-lived (run_forever() polls it repeatedly), so a stale task_id
        # from a previous tick would otherwise leak into this one's logs.
        bind_task_context(task_id=task_id, worker_id=self.worker_id)
        task = self.repositories.tasks.get(task_id)
        if task is None:
            raise KeyError(f"task not found: {task_id}")
        return await self._process_operator_loop(task)

    async def _process_operator_loop(self, task: TaskRecord) -> TaskRecord:
        """One observe/decide/act tick - the sole execution path (P3 §2.2).
        See orchestration/operator.py.

        Approval flow: a tool call that comes back NEEDS_APPROVAL creates a
        real ApprovalRequest (same repository/audit path the plan-based flow
        used) and transitions to AWAITING_APPROVAL with the pending call
        stashed in metadata["operator_pending_call"]; resuming is handled by
        _process_operator_awaiting_approval below, which replays that exact
        call with its one-shot, parameter-bound approval.

        Fulfillment: a `done` decision is checked against
        `validate_fulfillment` (objective-inferred postconditions - there is
        no PlanModel here, so `plan=None`) before it is allowed to complete.
        A gap is appended to `history` as an observation and the loop
        continues - "the next decide() call sees the error in context" - capped
        at 2 gap cycles the same way clarify/replan attempts are capped
        elsewhere in this file, so a model that can't close the gap doesn't
        loop forever.

        Rate limits: RATE_LIMITED/USAGE_LIMITED results back off through
        _operator_retry_or_ask instead of retrying on the next ~3s poll tick.

        Background sessions: a tool that reports status=running with a
        session_id (coding.agent) transitions to AWAITING_EXTERNAL via
        _await_operator_external and is picked back up by
        _resume_operator_pending_external once metadata["pending_tool_result"]
        appears - written by the same completion callback the plan-based path
        used (cli.py), which is status-based, not plan-shaped.
        """
        # self.operator is checked further down, only on the branch that
        # actually needs it - a replay task (metadata["replay_plan"])
        # never calls decide() at all, so a worker with no LLM configured
        # can still run one (see _next_replay_decision's own docstring).
        if self.executor is None:
            return self._transition_operator(task, task.metadata, TaskStatus.BLOCKED, "operator_loop_not_configured")

        latest = self.repositories.tasks.get(task.id) or task
        if latest.status in {TaskStatus.PAUSED, TaskStatus.CANCELLED}:
            return latest

        # This used to dispatch on `status == AWAITING_APPROVAL` alone, which
        # made the resume unreachable for the one flow that matters: granting
        # an approval calls requeue_after_approval_decision(), and that flips
        # the task to RUNNING so the worker will claim it again. By the time
        # the worker looked, the status was never AWAITING_APPROVAL, so this
        # branch was skipped and the operator re-planned from scratch instead
        # of replaying the approved call - producing a *new* approval request
        # every time. Approving it simply created another one, forever: 14
        # approvals granted on a single observed task, none consumed, until
        # the run timed out.
        #
        # Keyed on operator_pending_call plus status == RUNNING specifically
        # (not "any workable status"): requeue_after_approval_decision is the
        # only path that stashes a pending call and flips to RUNNING for this
        # reason, so gating on that combination - rather than the pending call
        # alone - keeps a stale operator_pending_call left over on a RECEIVED,
        # RETRYING, or otherwise-workable task from being misread as an
        # approval resume it isn't.
        pending_call = latest.metadata.get("operator_pending_call")
        pending_batch = latest.metadata.get("operator_pending_batch")
        # Checked before the single-call resume: a batch stores its own key and
        # would otherwise fall through to a path that expects one pending call.
        if isinstance(pending_batch, dict) and pending_batch.get("approval_id") and (
            latest.status in {TaskStatus.AWAITING_APPROVAL, TaskStatus.RUNNING}
        ):
            return await self._process_operator_batch_awaiting_approval(latest)
        if latest.status == TaskStatus.AWAITING_APPROVAL or (
            latest.status == TaskStatus.RUNNING
            and isinstance(pending_call, dict)
            and pending_call.get("approval_id")
        ):
            return await self._process_operator_awaiting_approval(latest)

        if latest.status == TaskStatus.RETRYING:
            return self._process_operator_retrying(latest)

        if isinstance(latest.metadata.get("pending_tool_result"), dict):
            return self._resume_operator_pending_external(latest)

        if latest.status == TaskStatus.AWAITING_EXTERNAL:
            return latest

        history: list[dict[str, Any]] = list(latest.metadata.get("operator_history") or [])
        if not latest.metadata.get("operator_loop"):
            latest = self.repositories.tasks.update_metadata(
                latest.id, {**latest.metadata, "operator_loop": True}, TaskStatus.RUNNING
            )
            self.audit.task_state_changed("worker", task.id, task.status, TaskStatus.RUNNING)

        tool_call_count = _tool_call_count(history)
        fulfillment_repair_count = sum(
            1 for entry in history if entry.get("tool_name") == CHECK_ENTRY_FULFILLMENT
        )
        if tool_call_count >= self.operator_max_steps + fulfillment_repair_count:
            fulfillment = validate_fulfillment(latest)
            if fulfillment.expected and fulfillment.ok:
                # The model can keep exploring after it has already produced
                # every objective-derived deliverable. Failing solely because
                # that exploration consumed the budget turns successful work
                # into a red task. Only apply this when there is a non-empty,
                # fully satisfied contract; an evidence-free loop still fails.
                metadata = {
                    **latest.metadata,
                    "operator_history": history,
                    "operator_step_budget_completed_after_fulfillment": True,
                    "synthesized_answer": _fulfilled_step_budget_answer(latest, history),
                }
                return self._transition_operator(
                    latest,
                    metadata,
                    TaskStatus.COMPLETED,
                    "operator_step_budget_completed_after_fulfillment",
                )
            gap = fulfillment.first_gap
            if gap and fulfillment_repair_count < 2:
                # The normal budget bounds exploration. A concrete unmet
                # postcondition is different: give the Operator one targeted
                # call per gap check (at most two) so the final useful action
                # is not rejected merely because earlier retries consumed all
                # ordinary slots. CHECK entries increase the effective limit
                # by exactly one and are themselves excluded from tool calls.
                history.append({
                    "tool_name": CHECK_ENTRY_FULFILLMENT,
                    "input": None,
                    "status": "fulfillment_gap",
                    "error": (
                        f"The normal tool budget was reached, but the objective still expects: {gap}. "
                        f"One targeted repair is allowed. Next step: {fulfillment_guidance(gap)}"
                    ),
                })
                metadata = {
                    **latest.metadata,
                    "operator_history": history,
                    "operator_fulfillment_gap_count": max(
                        int(latest.metadata.get("operator_fulfillment_gap_count", 0)),
                        fulfillment_repair_count + 1,
                    ),
                }
                return self.repositories.tasks.update_metadata(
                    latest.id, metadata, TaskStatus.RUNNING
                )
            return self._transition_operator(
                latest, {**latest.metadata, "operator_history": history},
                TaskStatus.BLOCKED, "operator_step_budget_exhausted",
            )

        memory_context = str(latest.metadata.get("memory_context") or "")
        # docs/HISTORY.md Part 4 T2.6: a done that was already rejected once
        # (audit-gap or fulfillment-gap retry marker in history) is a
        # concrete, local, zero-cost-to-check sign the default model is
        # struggling on this task - worth the stronger model if one is
        # configured, same reasoning as the existing parse-failure escalation
        # in operator.py, just reacting to an earlier signal.
        prefer_major = any(entry.get("tool_name") in CHECK_ENTRY_NAMES for entry in history)
        # A stable id for this one observe/decide/act tick (docs/UI_UX_AUDIT.md
        # Phase 14e) - stamped onto the operator's own LLM call, the resulting
        # operator_history entry, and any ToolCallRequest.parent_step_id it
        # leads to, so the graph can be built from a real parent-child link
        # instead of inferring structure from `origin` tags alone. Survives an
        # approval or background-external wait via operator_pending_call/
        # awaiting_external, which stash it the same way they already stash
        # tool_name/tool_input.
        step_id = new_id("step")
        replay_plan = latest.metadata.get("replay_plan")
        if isinstance(replay_plan, list) and replay_plan:
            # docs/ROADMAP.md "reusable verified workflows": walk a recorded
            # plan instead of asking an LLM what to do next. Everything past
            # this point - approval, retry, verification, fulfillment - is
            # the exact same code a live decision goes through; only how
            # `decision` gets produced differs. See _next_replay_decision's
            # own docstring for why retry/failure handling needs its own
            # per-step attempt cap here.
            decision, metadata_updates = _next_replay_decision(replay_plan, history, latest.metadata)
            if metadata_updates is not latest.metadata:
                latest = self.repositories.tasks.update_metadata(latest.id, metadata_updates)
        elif self.operator is None:
            return self._transition_operator(latest, latest.metadata, TaskStatus.BLOCKED, "operator_loop_not_configured")
        else:
            try:
                decision = await self.operator.decide(
                    latest.objective, self._planner_context(), history,
                    memory_context=memory_context, prefer_major=prefer_major,
                )
                latest = self._record_llm_usage(
                    latest, "operator", getattr(self.operator, "last_usage", None),
                    fallback_used=getattr(self.operator, "last_fallback_used", False),
                )
                self._record_llm_call(latest.id, "operator", len(history), self.operator, step_id=step_id)
            except Exception as exc:
                # describe_exception, not str(exc): an empty message here produced
                # "operator_decide_failed: " with nothing after it, which is what a
                # failed model call looked like while three fixture recordings were
                # being retried as if they were flakes.
                reason = describe_exception(exc)
                self.audit.append(
                    AuditEventType.ERROR, actor="operator", task_id=latest.id,
                    payload={"error": "operator_decide_failed", "reason": reason},
                )
                return self._transition_operator(
                    latest, {**latest.metadata, "operator_history": history},
                    TaskStatus.FAILED, f"operator_decide_failed: {reason}"[:400],
                )

        self.audit.append(
            AuditEventType.TASK_STATE_CHANGED, actor="operator", task_id=latest.id,
            payload={
                "action": "operator_decision", "decision": decision.model_dump(mode="json"),
                "step_index": len(history), "step_id": step_id,
            },
        )

        if decision.action == OperatorAction.DONE:
            final_answer = _ground_operator_final_answer(decision.final_answer, history)
            # Skip the grounding pass on a task that made a single tool call:
            # the Operator has just read that one result directly and written
            # final_answer from it, and the Auditor costs ~11s against a whole
            # request that is often ~12s. Governed by
            # operator.audit_min_tool_calls (1 restores auditing everything),
            # because it is a real trade - see that setting's comment.
            worth_auditing = _tool_call_count(history) >= self.audit_min_tool_calls
            evidence_offset = _post_clarification_history_offset(latest, history)
            content_entry = (
                _last_content_tool_history_entry(history[evidence_offset:])
                if self.auditor is not None and worth_auditing
                else None
            )
            if content_entry is not None:
                audit_gap_count = int(latest.metadata.get("operator_audit_gap_count", 0))
                if audit_gap_count < 2:
                    # Audit the FULL recorded output, not the history entry's
                    # 2000-char display summary. The Auditor's whole job is
                    # count/section sufficiency ("are all 5 episodes here?") -
                    # judging a truncation produces false INSUFFICIENT verdicts
                    # on exactly the long-content objectives it exists for.
                    # See docs/HISTORY.md §3.2.
                    full_content_request_id = latest.metadata.get("last_content_tool_request_id")
                    raw_output = str(
                        latest.metadata.get("last_content_tool_output_text")
                        if full_content_request_id
                        and full_content_request_id == content_entry.get("request_id")
                        else content_entry.get("output_summary") or ""
                    )
                    audit_evidence = _operator_audit_evidence(
                        history[evidence_offset:], content_entry, raw_output
                    )
                    audit_result = await self.auditor.audit(
                        latest.objective,
                        audit_evidence,
                        original_message=str(latest.metadata.get("original_message_text") or "") or None,
                        deliverable_evidence=deliverable_evidence(latest) if self._auditor_owns_fulfillment else "",
                        response_context=_auditor_response_context(latest, memory_context),
                    )
                    latest = self._record_llm_usage(
                        latest, "auditor", getattr(self.auditor, "last_usage", None),
                        fallback_used=getattr(self.auditor, "last_fallback_used", False),
                    )
                    self._record_llm_call(latest.id, "auditor", len(history), self.auditor, step_id=step_id)
                    if not audit_result.sufficient:
                        history.append({
                            "tool_name": CHECK_ENTRY_AUDIT,
                            "input": None,
                            "status": "audit_gap",
                            "error": (
                                f"Declared done, but the auditor found the raw output insufficient: "
                                f"{audit_result.reason}. Keep working toward the objective - call "
                                "another tool, or explain to the user why it can't be met - before "
                                "declaring done again."
                            ),
                            "step_id": step_id,
                        })
                        metadata = {
                            **latest.metadata,
                            "operator_history": history,
                            "operator_audit_gap_count": audit_gap_count + 1,
                        }
                        return self.repositories.tasks.update_metadata(latest.id, metadata, TaskStatus.RUNNING)
                    if audit_result.answer:
                        final_answer = _ground_operator_final_answer(audit_result.answer, history)
                else:
                    metadata = {
                        **latest.metadata,
                        "operator_history": history,
                        "last_worker_error": (
                            "Blocked after two grounded-output audits found the available evidence "
                            "insufficient; stopped instead of reporting an unverified answer."
                        ),
                    }
                    return self._transition_operator(
                        latest,
                        metadata,
                        TaskStatus.BLOCKED,
                        "operator_audit_gap_exhausted",
                    )

            metadata = {**latest.metadata, "operator_history": history, "synthesized_answer": final_answer}
            # With the Auditor owning fulfillment, its INSUFFICIENT verdict
            # above is already the gap check - it saw the user's own words and
            # the factual deliverable list. Re-running the word-set gate here
            # would let hardcoded intent inference overrule the judgment we
            # just paid an LLM call for. _unsupported_write_claim is a
            # different kind of check - a narrow, deterministic claim-vs-
            # evidence regex, not an intent-inference heuristic - so it still
            # runs in both modes; it is what caught "The following files were
            # created:" against an empty workspace (docs/E2E_FINDINGS.md P0-2).
            gap = (
                None
                if self._auditor_owns_fulfillment
                else validate_fulfillment(latest.model_copy(update={"metadata": metadata})).first_gap
            ) or _unsupported_write_claim(final_answer, history, metadata)
            if gap:
                gap_count = int(latest.metadata.get("operator_fulfillment_gap_count", 0))
                if gap_count < 2:
                    history.append({
                        "tool_name": CHECK_ENTRY_FULFILLMENT,
                        "input": None,
                        "status": "fulfillment_gap",
                        "error": (
                            f"Declared done, but the objective still expects: {gap}. Keep working "
                            f"toward the objective. Next step: {fulfillment_guidance(gap)} "
                            "If that cannot run, explain the concrete blocker before declaring done again."
                        ),
                        "step_id": step_id,
                    })
                    metadata = {
                        **latest.metadata,
                        "operator_history": history,
                        "operator_fulfillment_gap_count": gap_count + 1,
                    }
                    return self.repositories.tasks.update_metadata(latest.id, metadata, TaskStatus.RUNNING)
                metadata.update(
                    {
                        "fulfillment_gap": gap,
                        "last_worker_error": (
                            f"Blocked after two recovery attempts because the required evidence is still missing: {gap}."
                        ),
                    }
                )
                return self._transition_operator(
                    latest,
                    metadata,
                    TaskStatus.BLOCKED,
                    "operator_fulfillment_gap_exhausted",
                )
            return self._transition_operator(latest, metadata, TaskStatus.COMPLETED, "operator_done")

        if decision.action == OperatorAction.ASK_USER:
            candidate = latest.model_copy(
                update={"metadata": {**latest.metadata, "operator_history": history}}
            )
            clarification_recovery = _clarification_recovery_reason(candidate, history)
            if clarification_recovery:
                recovery_count = int(
                    latest.metadata.get("operator_optional_question_recovery_count", 0)
                )
                if recovery_count < 2:
                    history.append(
                        {
                            "tool_name": CHECK_ENTRY_CLARIFICATION,
                            "input": None,
                            "status": "clarification_gap",
                            "error": (
                                f"Clarification rejected: {clarification_recovery} "
                                "Do not pause for optional refinement, repeated confirmation, or details "
                                "that can be derived from the objective and tool evidence. Take the next "
                                "available action, or return done with a grounded answer when complete."
                            ),
                            "step_id": step_id,
                        }
                    )
                    metadata = {
                        **latest.metadata,
                        "operator_history": history,
                        "operator_optional_question_recovery_count": recovery_count + 1,
                    }
                    return self.repositories.tasks.update_metadata(
                        latest.id, metadata, TaskStatus.RUNNING
                    )
            latest = self.repositories.tasks.update_metadata(latest.id, {**latest.metadata, "operator_history": history})
            return self._operator_ask_user(latest, decision.question or "Need more information to continue.")

        if decision.action == OperatorAction.BLOCKED:
            reason = _operator_blocked_reason(decision.reason, history)
            metadata = {**latest.metadata, "operator_history": history, "last_worker_error": reason}
            return self._transition_operator(latest, metadata, TaskStatus.BLOCKED, reason)

        if decision.action == OperatorAction.CALL_TOOLS_PARALLEL:
            # One approval for the whole batch, before anything runs. Without
            # this, every call needing approval failed inside the batch with
            # "reissue it alone via call_tool", which is what turned "organise
            # 128 files" into one prompt per file: the operator was pushed onto
            # the one-call-one-approval path by the only batching primitive it
            # had. The user now sees the whole batch and answers once.
            batch_approval = self._request_batch_approval(
                latest, decision.parallel_calls, history, step_id=step_id
            )
            if batch_approval is not None:
                return batch_approval
            entries = await self._run_parallel_calls(latest.id, decision.parallel_calls, step_id=step_id)
            history.extend(entries)
            # Each call promotes its result into task metadata. Reload after
            # the batch before adding history; writing the pre-batch `latest`
            # snapshot here erased changed_paths/workspace evidence produced
            # by every successful parallel call.
            recorded = self.repositories.tasks.get(latest.id) or latest
            return self.repositories.tasks.update_metadata(
                recorded.id,
                {**recorded.metadata, "operator_history": history},
            )

        if decision.action == OperatorAction.DELEGATE:
            latest, entry = await self._run_delegate(latest, decision, step_id=step_id)
            history.append(entry)
            return self.repositories.tasks.update_metadata(latest.id, {**latest.metadata, "operator_history": history})

        # CALL_TOOL
        requested_tool_name = decision.tool_name
        requested_tool_input = decision.tool_input
        tool_name, tool_input = _canonical_operator_tool_call(
            requested_tool_name,
            requested_tool_input,
            self.executor.tool_definitions,
        )
        tool_name, tool_input = _required_named_coding_agent_call(
            latest,
            tool_name,
            tool_input,
            self.executor.tool_definitions,
        )
        tool_name, tool_input = _ordered_artifact_delivery_call(
            latest,
            tool_name,
            tool_input,
            history,
            self.executor.tool_definitions,
        )
        tool_input = _filesystem_search_input_with_content_intent(latest, tool_name, tool_input)
        tool_name, tool_input = _stale_read_recovery_call(
            latest,
            tool_name,
            tool_input,
            history,
        )
        tool_input = _coding_agent_input_with_task_defaults(latest, tool_name, tool_input)
        tool_input = _coding_agent_input_with_explicit_workspace(latest, tool_name, tool_input)
        decision = decision.model_copy(update={"tool_name": tool_name, "tool_input": tool_input})
        normalization = _normalization_history_fields(
            requested_tool_name, requested_tool_input, tool_name, tool_input
        )
        tool_def = self.executor.tool_definitions.get(tool_name)
        if tool_def is None:
            history.append({
                "tool_name": tool_name, "input": tool_input,
                "status": "failed", "error": f"unregistered tool: {tool_name}",
                "step_id": step_id,
                **normalization,
            })
            return self.repositories.tasks.update_metadata(latest.id, {**latest.metadata, "operator_history": history})

        request = ToolCallRequest(
            task_id=latest.id,
            tool_name=tool_name,
            capability=tool_def.capability,
            risk_level=_effective_operator_risk(tool_def, decision.tool_input, decision.risk_level),
            input=tool_input,
            parent_step_id=step_id,
        )
        if request.risk_level != decision.risk_level:
            decision = decision.model_copy(update={"risk_level": request.risk_level})
        # Written before dispatch and cleared after the result is recorded, so
        # a worker that dies here leaves evidence of WHICH call was in the air.
        # Without it, a restarted worker cannot tell "the move finished" from
        # "the move was halfway through", and the only safe response to that
        # ambiguity was to fail the whole task.
        self._mark_in_flight(latest.id, request, step_id=step_id)
        result = await self.executor.execute(request)
        self._clear_in_flight(latest.id)

        if result.status == ToolResultStatus.NEEDS_APPROVAL:
            return self._await_operator_approval(
                latest,
                decision,
                history,
                approval_id=str(result.output.get("approval_id") or ""),
                step_id=step_id,
            )

        recorded = self._record_tool_result(latest.id, tool_name, result)
        if _is_background_external_tool_result(tool_name, result):
            return self._await_operator_external(recorded, decision, result, history, step_id=step_id)
        if result.status != ToolResultStatus.SUCCEEDED:
            retry_outcome = self._operator_retry_or_ask(recorded, decision, result, history, step_id=step_id, request=request)
            if retry_outcome is not None:
                return retry_outcome
        output_text = _tool_output_text(result) if result.status == ToolResultStatus.SUCCEEDED else None
        history.append({
            # tool_name/tool_input, not decision.tool_name/decision.tool_input:
            # by this point they may have passed through canonicalization or
            # stale-read recovery, so they are what actually ran, which can
            # differ from the model's original request.
            "tool_name": tool_name,
            "input": tool_input,
            # The model already explains each choice, and it was thrown away
            # every step - so progress messages could say what ran but never
            # why, and "see it happening" meant reading a trace afterwards.
            "reasoning": (decision.reasoning or "").strip()[:400] or None,
            "status": result.status.value,
            "output_summary": output_text[:2000] if output_text else None,
            "error": result.error_message,
            "request_id": result.request_id,
            "step_id": step_id,
            **normalization,
        })
        completed_coding_task = self._complete_fulfilled_coding_result(
            recorded,
            tool_name,
            result,
            history,
            {**recorded.metadata, "operator_history": history},
        )
        if completed_coding_task is not None:
            return completed_coding_task
        repeated_call = _repeated_no_progress_call(history)
        if repeated_call is not None:
            tool_name, operation = repeated_call
            metadata = {
                **recorded.metadata,
                "operator_history": history,
                "last_worker_error": (
                    f"Blocked after repeated {tool_name}:{operation} calls returned the same result; "
                    "stopped to avoid a no-progress loop. A different capability or user input is required."
                ),
            }
            return self._transition_operator(
                recorded,
                metadata,
                TaskStatus.BLOCKED,
                "operator_repeated_no_progress",
            )
        return self.repositories.tasks.update_metadata(recorded.id, {**recorded.metadata, "operator_history": history})

    def _mark_in_flight(self, task_id: str, request: ToolCallRequest, *, step_id: str) -> None:
        """Record that this exact call was dispatched but has not returned."""
        task = self.repositories.tasks.get(task_id)
        if task is None:
            return
        self.repositories.tasks.update_metadata(task_id, {
            **task.metadata,
            "operator_in_flight": {
                "tool_name": request.tool_name,
                "capability": request.capability.value,
                "risk_level": request.risk_level.value,
                "input": request.input,
                "step_id": step_id,
                "dispatched_at": utc_now().isoformat(),
            },
        })

    def _clear_in_flight(self, task_id: str) -> None:
        task = self.repositories.tasks.get(task_id)
        if task is None or "operator_in_flight" not in task.metadata:
            return
        self.repositories.tasks.update_metadata(
            task_id, {k: v for k, v in task.metadata.items() if k != "operator_in_flight"}
        )

    def _batch_requests(
        self, task_id: str, calls: list[ParallelToolCall], step_id: str
    ) -> list[tuple[ParallelToolCall, ToolCallRequest | None]]:
        """The exact ToolCallRequests a batch would dispatch.

        Built once and reused for both the policy pre-flight and the approval
        binding, so the call a human approves is byte-for-byte the call that
        later runs - the property PolicyEngine._approval_binding enforces.
        """
        built: list[tuple[ParallelToolCall, ToolCallRequest | None]] = []
        for call in calls:
            tool_name, tool_input = _canonical_operator_tool_call(
                call.tool_name, call.tool_input, self.executor.tool_definitions
            )
            tool_def = self.executor.tool_definitions.get(tool_name)
            if tool_def is None:
                built.append((call, None))
                continue
            built.append((
                call,
                ToolCallRequest(
                    task_id=task_id, tool_name=tool_name, capability=tool_def.capability,
                    risk_level=_effective_operator_risk(tool_def, tool_input, call.risk_level),
                    input=tool_input, parent_step_id=step_id,
                ),
            ))
        return built

    def _request_batch_approval(
        self,
        task: TaskRecord,
        calls: list[ParallelToolCall],
        history: list[dict[str, Any]],
        *,
        step_id: str,
    ) -> TaskRecord | None:
        """One approval covering every call in a batch that needs one.

        Returns None when nothing in the batch needs approval, so the common
        case keeps running without a round trip to a human.

        Each call that needs approval gets its own PENDING ApprovalRequest,
        bound to its exact request; the batch approval the human actually sees
        carries the list and the ids of those children. Approving the batch
        approves exactly those calls and nothing else - a later call the human
        never saw still has to ask, because there is no grant widening the
        tool or capability.
        """
        if not calls:
            return None
        built = self._batch_requests(task.id, calls, step_id)
        needing: list[ToolCallRequest] = []
        for _call, request in built:
            if request is None:
                continue
            decision = self.executor.policy.evaluate(request, approval=None, has_grant=False)
            if decision.needs_approval:
                needing.append(request)

        if not needing:
            return None

        first = needing[0]
        preview_lines = [
            f"- {request.tool_name} ({request.risk_level.value}): "
            f"{json.dumps(request.input, ensure_ascii=False)[:200]}"
            for request in needing
        ]
        # The batch is created first so each child can name its parent, which
        # is what keeps the console's pending list showing one decision
        # instead of N that each look separate.
        batch = self.executor.policy.approval_request(
            first,
            summary=f"Run {len(needing)} actions in one batch. Nothing runs until you approve.",
        )
        children: list[ApprovalRequest] = []
        for request in needing:
            definition = self.executor.tool_definitions.get(request.tool_name)
            reason = definition.approval_reason(request.input) if definition else None
            child = self.executor.policy.approval_request(
                request,
                summary=(f"{request.tool_name}: {reason}" if reason else None),
            )
            # action_payload is left exactly as the policy engine built it: it
            # IS the serialized ToolCallRequest that _approval_binding
            # re-validates, and ToolCallRequest forbids extra keys, so adding
            # a marker here silently breaks the binding check and denies the
            # call the human just approved. The batch/child link lives on the
            # parent instead.
            children.append(child)
        batch = batch.model_copy(update={"action_payload": {
            "tool_name": first.tool_name,
            "batch": [
                {
                    "tool_name": request.tool_name,
                    "risk_level": request.risk_level.value,
                    "input": request.input,
                    "approval_id": child.id,
                }
                for request, child in zip(needing, children)
            ],
        }})
        self.repositories.approvals.create(batch)
        for child in children:
            self.repositories.approvals.create(child)
        self.audit.append(
            AuditEventType.APPROVAL_REQUESTED,
            actor="policy",
            task_id=task.id,
            payload={"approval_id": batch.id, "batch_size": len(needing)},
        )

        metadata = {
            **task.metadata,
            "operator_history": history,
            "operator_pending_batch": {
                "approval_id": batch.id,
                "step_id": step_id,
                "calls": [
                    {
                        "tool_name": call.tool_name,
                        "tool_input": call.tool_input,
                        "risk_level": call.risk_level.value,
                    }
                    for call in calls
                ],
            },
            "pending_approval_preview": "\n".join(preview_lines),
        }
        return self._transition_operator(
            task, metadata, TaskStatus.AWAITING_APPROVAL, "operator_batch_approval_required"
        )

    async def _run_parallel_calls(
        self,
        task_id: str,
        calls: list[ParallelToolCall],
        *,
        origin_prefix: str = "",
        step_id: str,
        approvals_by_call: dict[tuple[str, str], str] | None = None,
    ) -> list[dict[str, Any]]:
        """Execute independent tool calls concurrently (docs/HISTORY.md Part 3
        T1.1) and return one history-entry dict per call, same shape as a
        normal call_tool entry plus ``"parallel": True``.

        Every call in one batch shares a single ``parallel_batch:<id>``
        ``ToolCallRequest.origin`` (docs/UI_REWRITE_PLAN.md §7/§9 Phase 0.6),
        so a trace UI can render them as siblings that ran at once instead of
        indistinguishable sequential calls. ``origin_prefix`` lets
        ``_run_delegate`` nest this correctly when a sub-task itself fans
        out - the batch's origin becomes ``subagent:<id>/parallel_batch:<id>``.

        Deliberately narrow, not a general CALL_TOOL replacement: skips the
        approval/retry/background-wait machinery entirely, because none of
        it generalizes cleanly to N calls at once (which one pauses the
        whole task for approval? what does "retry" mean when 2 of 5
        succeeded?). A call that needs approval or starts a background
        session fails with a message telling the model to reissue it alone
        via call_tool - a real, disclosed limitation. Intended for
        independent reads/lookups only; operator_system.md tells the model
        so directly.

        Safe to run concurrently against the same task's metadata: each
        call gets its own ToolCallRequest (its own id, so
        tool_invocations.create() is an independent insert per call, not a
        shared row), and _record_tool_result - a plain synchronous method
        with no ``await`` inside it - runs atomically with respect to the
        single-threaded asyncio event loop, so concurrent calls can't
        interleave a read and a write against each other. Which call's
        result ends up as last_tool_name/last_tool_output_text is simply
        whichever finishes last - there is no single well-defined "last"
        call when N ran at once, and that's an acceptable, disclosed
        property of a parallel batch, not a bug.
        """
        batch_origin = f"{origin_prefix}parallel_batch:{uuid4().hex[:12]}"

        async def _one(call: ParallelToolCall) -> dict[str, Any]:
            tool_name, tool_input = _canonical_operator_tool_call(
                call.tool_name,
                call.tool_input,
                self.executor.tool_definitions,
            )
            normalization = _normalization_history_fields(
                call.tool_name, call.tool_input, tool_name, tool_input
            )
            tool_def = self.executor.tool_definitions.get(tool_name)
            if tool_def is None:
                return {
                    "tool_name": tool_name, "input": tool_input,
                    "status": "failed", "error": f"unregistered tool: {tool_name}",
                    "parallel": True, "origin": batch_origin, "step_id": step_id,
                    **normalization,
                }
            request = ToolCallRequest(
                task_id=task_id, tool_name=tool_name, capability=tool_def.capability,
                risk_level=_effective_operator_risk(tool_def, tool_input, call.risk_level),
                input=tool_input, origin=batch_origin, parent_step_id=step_id,
            )
            # On a resumed batch each call carries the approval minted for it,
            # keyed by exactly what was shown to the human.
            approval_id = (approvals_by_call or {}).get(
                (tool_name, json.dumps(tool_input, sort_keys=True))
            )
            result = await self.executor.execute(request, approval_id=approval_id)
            if result.status == ToolResultStatus.NEEDS_APPROVAL:
                return {
                    "tool_name": tool_name, "input": tool_input,
                    "status": "failed",
                    "error": (
                        "this call still needs approval after the batch was decided - "
                        "it was not one of the calls the batch listed"
                    ),
                    "parallel": True, "origin": batch_origin, "step_id": step_id,
                    **normalization,
                }
            if _is_background_external_tool_result(tool_name, result):
                return {
                    "tool_name": tool_name, "input": tool_input,
                    "status": "failed",
                    "error": (
                        "this call started a background session, which call_tools_parallel does not "
                        "support - reissue it alone via call_tool"
                    ),
                    "parallel": True, "origin": batch_origin, "step_id": step_id,
                    **normalization,
                }
            self._record_tool_result(task_id, tool_name, result)
            output_text = _tool_output_text(result) if result.status == ToolResultStatus.SUCCEEDED else None
            return {
                "tool_name": tool_name, "input": tool_input,
                "status": result.status.value,
                "output_summary": output_text[:2000] if output_text else None,
                "error": result.error_message,
                "parallel": True, "origin": batch_origin,
                "request_id": result.request_id,
                "step_id": step_id,
                **normalization,
            }

        return list(await asyncio.gather(*[_one(call) for call in calls]))

    async def _run_delegate(
        self, task: TaskRecord, decision: OperatorDecision, *, step_id: str
    ) -> tuple[TaskRecord, dict[str, Any]]:
        """Run a delegated sub-task in an isolated context (docs/HISTORY.md
        Part 3 T1.2): its own operator loop, its own history starting from
        nothing, its own fixed step budget - only a compact summary crosses
        back into the parent's history. That's the entire value: a
        long/exploratory sub-task doesn't bloat the parent's context the way
        inlining the same steps via plain call_tool would.

        The parent task's own metadata (last_tool_name, last_tool_output_text,
        etc.) IS updated by the sub-task's tool calls, deliberately, not as a
        leak: it's what lets the audit gate ground a `done` that immediately
        follows a delegate step in the sub-agent's real last tool output
        (see CONTENT_TOOLS in orchestration/auditor.py, which lists
        "delegate" for exactly this). Only the step-by-step history stays
        isolated - the whole point of the isolation is a smaller prompt for
        the parent's next decide() call, not hiding what happened.

        Deliberately narrow, same reasoning as _run_parallel_calls: a
        sub-task cannot pause for approval, wait on a background session,
        ask the user, or delegate further - each needs task-level state a
        synchronous, in-process sub-loop doesn't have. Hitting one of those
        fails the sub-task with a clear reason instead of hanging; the
        parent sees the reason in its own history and can handle that step
        directly with a normal call_tool instead of delegating it.

        `step_id` is the parent tick's step - stamped onto the single
        "delegate" summary entry returned to the parent's history. Each
        sub-decision inside the loop below is its own step, with its own
        freshly generated step_id, the same way the parent's own tick loop
        works (docs/UI_UX_AUDIT.md Phase 14e).
        """
        delegate_origin = f"subagent:{uuid4().hex[:12]}"
        objective = decision.delegate_objective or ""
        allowed_tools = (
            {
                _canonical_operator_tool_call(name, {}, self.executor.tool_definitions)[0]
                for name in decision.delegate_tools
            }
            if decision.delegate_tools
            else None
        )
        extra_context = ""
        if allowed_tools:
            extra_context = (
                "\n\nFor this delegated sub-task, you may ONLY use these tools: "
                f"{', '.join(sorted(allowed_tools))}. Any other tool_name will be refused."
            )
        summary_input = {"objective": objective, "delegate_tools": decision.delegate_tools}
        sub_history: list[dict[str, Any]] = []

        for _step in range(self.DELEGATE_MAX_STEPS):
            sub_step_id = new_id("step")
            try:
                sub_decision = await self.operator.decide(
                    objective, self._planner_context(extra_context), sub_history, memory_context=""
                )
            except Exception as exc:
                return task, {
                    "tool_name": "delegate", "input": summary_input, "status": "failed",
                    "output_summary": None, "error": f"sub-task decide() failed: {exc}",
                    "origin": delegate_origin, "step_id": step_id,
                }
            task = self._record_llm_usage(
                task, "subagent", getattr(self.operator, "last_usage", None),
                fallback_used=getattr(self.operator, "last_fallback_used", False),
            )
            self._record_llm_call(task.id, "subagent", len(sub_history), self.operator, step_id=sub_step_id)

            if sub_decision.action == OperatorAction.DONE:
                return task, {
                    "tool_name": "delegate", "input": summary_input, "status": "succeeded",
                    "output_summary": (sub_decision.final_answer or "")[:2000], "error": None,
                    "origin": delegate_origin, "step_id": step_id,
                }
            if sub_decision.action == OperatorAction.BLOCKED:
                return task, {
                    "tool_name": "delegate", "input": summary_input, "status": "failed",
                    "output_summary": None,
                    "error": f"sub-task blocked: {sub_decision.reason or 'no reason given'}",
                    "origin": delegate_origin, "step_id": step_id,
                }
            if sub_decision.action == OperatorAction.ASK_USER:
                return task, {
                    "tool_name": "delegate", "input": summary_input, "status": "failed",
                    "output_summary": None,
                    "error": (
                        "sub-task needs user input, which delegation does not support: "
                        f"{sub_decision.question or ''}"
                    ),
                    "origin": delegate_origin, "step_id": step_id,
                }
            if sub_decision.action == OperatorAction.DELEGATE:
                sub_history.append({
                    "tool_name": "delegate", "input": None, "status": "failed",
                    "error": "delegation is not available inside a delegated sub-task",
                    "step_id": sub_step_id,
                })
                continue
            if sub_decision.action == OperatorAction.CALL_TOOLS_PARALLEL:
                calls = sub_decision.parallel_calls
                if allowed_tools and any(
                    _canonical_operator_tool_call(call.tool_name, call.tool_input, self.executor.tool_definitions)[0]
                    not in allowed_tools
                    for call in calls
                ):
                    sub_history.append({
                        "tool_name": "call_tools_parallel", "input": None, "status": "failed",
                        "error": f"one or more tools are not in this sub-task's allowed set: {sorted(allowed_tools)}",
                        "step_id": sub_step_id,
                    })
                    continue
                sub_history.extend(
                    await self._run_parallel_calls(
                        task.id, calls, origin_prefix=f"{delegate_origin}/", step_id=sub_step_id
                    )
                )
                continue

            # CALL_TOOL
            requested_tool_name = sub_decision.tool_name
            requested_tool_input = sub_decision.tool_input
            tool_name, tool_input = _canonical_operator_tool_call(
                requested_tool_name,
                requested_tool_input,
                self.executor.tool_definitions,
            )
            normalization = _normalization_history_fields(
                requested_tool_name, requested_tool_input, tool_name, tool_input
            )
            if allowed_tools and tool_name not in allowed_tools:
                sub_history.append({
                    "tool_name": tool_name, "input": tool_input, "status": "failed",
                    "error": f"tool not in this sub-task's allowed set: {sorted(allowed_tools)}",
                    "step_id": sub_step_id,
                    **normalization,
                })
                continue
            tool_def = self.executor.tool_definitions.get(tool_name)
            if tool_def is None:
                sub_history.append({
                    "tool_name": tool_name, "input": tool_input, "status": "failed",
                    "error": f"unregistered tool: {tool_name}",
                    "step_id": sub_step_id,
                    **normalization,
                })
                continue
            request = ToolCallRequest(
                task_id=task.id, tool_name=tool_name, capability=tool_def.capability,
                risk_level=_effective_operator_risk(tool_def, tool_input, sub_decision.risk_level),
                input=tool_input, origin=delegate_origin, parent_step_id=sub_step_id,
            )
            result = await self.executor.execute(request)
            if result.status == ToolResultStatus.NEEDS_APPROVAL:
                sub_history.append({
                    "tool_name": tool_name, "input": tool_input, "status": "failed",
                    "error": "this call needs approval, which delegation does not support",
                    "step_id": sub_step_id,
                    **normalization,
                })
                continue
            if _is_background_external_tool_result(tool_name, result):
                sub_history.append({
                    "tool_name": tool_name, "input": tool_input, "status": "failed",
                    "error": "this call started a background session, which delegation does not support",
                    "step_id": sub_step_id,
                    **normalization,
                })
                continue
            task = self._record_tool_result(task.id, tool_name, result)
            output_text = _tool_output_text(result) if result.status == ToolResultStatus.SUCCEEDED else None
            sub_history.append({
                "tool_name": tool_name, "input": tool_input,
                "status": result.status.value,
                "output_summary": output_text[:2000] if output_text else None,
                "error": result.error_message,
                "step_id": sub_step_id,
                **normalization,
            })

        return task, {
            "tool_name": "delegate", "input": summary_input, "status": "failed",
            "output_summary": None,
            "error": f"sub-task step budget ({self.DELEGATE_MAX_STEPS}) exhausted without finishing",
            "origin": delegate_origin, "step_id": step_id,
        }

    def _operator_retry_or_ask(
        self,
        task: TaskRecord,
        decision: OperatorDecision,
        result: ToolCallResult,
        history: list[dict[str, Any]],
        *,
        step_id: str,
        request: ToolCallRequest | None = None,
    ) -> TaskRecord | None:
        """Rate-limit/usage-limit backoff, ported from the plan-based path's
        _retry_decision so the loop doesn't hot-loop a rate-limited API on
        every ~3s poll tick. Returns None to fall through to the normal "log
        it and let the next decide() call see it in context" handling for
        every other kind of failure - that in-context recovery is deliberate
        (docs/HISTORY.md P3 §2.2); this is narrowly about pacing, not
        diagnosis, the same split the plan-based path draws.
        """
        if self.retry_policy is None:
            return None
        if result.error_class == ErrorClass.USAGE_LIMITED:
            # Wait the quota out instead of escalating. This used to call
            # _operator_ask_user, which stopped an unattended build dead until
            # a human happened to reply - for a limit that resolves itself on
            # a timer, the human adds only delay. Parked as RETRYING with a
            # future next_retry_at: _process_operator_retrying already returns
            # the task untouched until that passes, and claim_next skips
            # not-yet-due tasks so a long wait cannot starve the queue.
            resume_at, wait_seconds, from_provider = next_attempt_at(result.error_message)
            history.append({
                "tool_name": decision.tool_name, "input": decision.tool_input,
                "status": result.status.value, "output_summary": None,
                "error": (
                    f"{result.error_message or 'usage limit reached'} "
                    f"- waiting {describe_wait(wait_seconds, from_provider)} and continuing automatically."
                ),
                "request_id": result.request_id, "step_id": step_id,
            })
            metadata = {
                **task.metadata,
                "operator_history": history,
                "next_retry_at": resume_at.isoformat(),
                "usage_limit_wait": {
                    "tool_name": decision.tool_name,
                    "resume_at": resume_at.isoformat(),
                    "wait_seconds": wait_seconds,
                    "reset_time_from_provider": from_provider,
                },
            }
            return self._transition_operator(
                task, metadata, TaskStatus.RETRYING,
                f"usage_limited_waiting_{wait_seconds}s",
            )
        retry_count = int(task.metadata.get("operator_retry_count", 0))
        retry_decision = self.retry_policy.evaluate(result, retry_count)
        if not retry_decision.retry:
            return None
        history.append({
            "tool_name": decision.tool_name, "input": decision.tool_input,
            "status": result.status.value, "output_summary": None,
            "error": _retry_error_text(result, request),
            "request_id": result.request_id, "step_id": step_id,
        })
        metadata = {
            **task.metadata,
            "operator_history": history,
            "operator_retry_count": retry_decision.retry_count,
            "next_retry_at": retry_decision.next_retry_at,
        }
        return self._transition_operator(task, metadata, TaskStatus.RETRYING, retry_decision.reason)

    def _await_operator_external(
        self,
        task: TaskRecord,
        decision: OperatorDecision,
        result: ToolCallResult,
        history: list[dict[str, Any]],
        *,
        step_id: str,
    ) -> TaskRecord:
        """A tool (coding.agent today) reported a session still running in the
        background rather than a finished result. cli.py's
        _coding_session_completion_callback writes metadata["pending_tool_result"]
        and flips AWAITING_EXTERNAL back to RUNNING once the session actually
        finishes - it isn't plan-shaped (only checks task.status), so it works
        for the operator loop unchanged. _resume_operator_pending_external
        below is the other half: picking that result back up.
        """
        output = result.output if isinstance(result.output, dict) else {}
        history.append({
            "tool_name": decision.tool_name,
            "input": decision.tool_input,
            "status": "running",
            "output_summary": f"session {output.get('session_id')} started, running in background",
            "error": None,
            "step_id": step_id,
        })
        metadata = {
            **task.metadata,
            "operator_history": history,
            # step_id survives the background wait here, read back by
            # _resume_operator_pending_external below so the eventual
            # completion entry links to the same step as the "running" one
            # above (docs/UI_UX_AUDIT.md Phase 14e).
            "operator_pending_call": {"tool_name": decision.tool_name, "tool_input": decision.tool_input, "step_id": step_id},
            "awaiting_external": {
                "tool_name": decision.tool_name,
                "session_id": str(output.get("session_id") or ""),
                "provider": output.get("provider"),
                "status": output.get("status"),
                "request_id": result.request_id,
                "started_at": output.get("started_at"),
            },
        }
        updated = self.repositories.tasks.update_metadata(task.id, metadata, TaskStatus.AWAITING_EXTERNAL)
        self.audit.task_state_changed("worker", task.id, task.status, TaskStatus.AWAITING_EXTERNAL)
        self.audit.append(
            AuditEventType.TASK_STATE_CHANGED,
            actor="worker",
            task_id=task.id,
            payload={"reason": "awaiting_external_session", "session_id": output.get("session_id"), "tool": decision.tool_name},
        )
        return updated

    def _resume_operator_pending_external(self, task: TaskRecord) -> TaskRecord:
        history: list[dict[str, Any]] = list(task.metadata.get("operator_history") or [])
        pending = task.metadata.get("pending_tool_result") or {}
        pending_call = task.metadata.get("operator_pending_call")
        tool_input = pending_call.get("tool_input") if isinstance(pending_call, dict) else None
        tool_name = str(pending.get("tool_name") or (pending_call or {}).get("tool_name") or "coding.agent")
        step_id = (pending_call or {}).get("step_id") if isinstance(pending_call, dict) else None
        metadata = {**task.metadata}
        for key in ("pending_tool_result", "awaiting_external", "operator_pending_call"):
            metadata.pop(key, None)
        try:
            result = ToolCallResult.model_validate(pending.get("result"))
        except Exception:
            history.append({
                "tool_name": tool_name, "input": tool_input,
                "status": "failed", "error": "malformed pending_tool_result from external session callback",
                "step_id": step_id,
            })
            return self.repositories.tasks.update_metadata(
                task.id, {**metadata, "operator_history": history}, TaskStatus.RUNNING
            )
        recorded = self._record_tool_result(task.id, tool_name, result)
        output_text = _tool_output_text(result) if result.status == ToolResultStatus.SUCCEEDED else None
        history.append({
            "tool_name": tool_name,
            "input": tool_input,
            "status": result.status.value,
            "output_summary": output_text[:2000] if output_text else None,
            "error": result.error_message,
            "step_id": step_id,
        })
        final_metadata = {**metadata, **recorded.metadata, "operator_history": history}
        for key in ("pending_tool_result", "awaiting_external", "operator_pending_call"):
            final_metadata.pop(key, None)
        completed_coding_task = self._complete_fulfilled_coding_result(
            recorded,
            tool_name,
            result,
            history,
            final_metadata,
        )
        if completed_coding_task is not None:
            return completed_coding_task
        return self.repositories.tasks.update_metadata(task.id, final_metadata, TaskStatus.RUNNING)

    def _complete_fulfilled_coding_result(
        self,
        task: TaskRecord,
        tool_name: str,
        result: ToolCallResult,
        history: list[dict[str, Any]],
        metadata: dict[str, Any],
    ) -> TaskRecord | None:
        """Finish a successful external coding handoff without another poll.

        The session callback is authoritative terminal evidence. Asking the
        model what to do next can make it poll the same completed session until
        the no-progress guard fires. Auto-completion remains gated by the full
        typed fulfillment check, so compound requests (deliver, preview,
        schedule, and so on) continue through the operator loop.
        """
        answer = _completed_coding_agent_answer(tool_name, result)
        if not answer:
            return None
        completed_metadata = {**metadata, "operator_history": history, "synthesized_answer": answer}
        candidate = task.model_copy(update={"metadata": completed_metadata})
        validation = validate_fulfillment(candidate)
        # An empty expectation set is not proof of completion for an open-ended
        # request; let the operator interpret the result in that case.
        if not validation.expected:
            return None
        gap = validation.first_gap or _unsupported_write_claim(
            answer,
            history,
            completed_metadata,
        )
        if gap:
            return None
        return self._transition_operator(
            task,
            completed_metadata,
            TaskStatus.COMPLETED,
            "operator_external_completed",
        )

    def _process_operator_retrying(self, task: TaskRecord) -> TaskRecord:
        next_retry_at = task.metadata.get("next_retry_at")
        if next_retry_at and datetime.fromisoformat(next_retry_at) > utc_now():
            return task
        # Clear both markers on the way back to RUNNING. Leaving next_retry_at
        # behind would keep claim_next's new not-due filter excluding this task
        # forever - it only ever moves forward in time otherwise.
        metadata = {key: value for key, value in task.metadata.items()
                    if key not in ("next_retry_at", "usage_limit_wait")}
        return self._transition_operator(task, metadata, TaskStatus.RUNNING, "retry_due")

    def _await_operator_approval(
        self,
        task: TaskRecord,
        decision: OperatorDecision,
        history: list[dict[str, Any]],
        *,
        approval_id: str,
        step_id: str,
    ) -> TaskRecord:
        """self.executor.execute() above already created the ApprovalRequest
        (and its audit event) via the policy engine, same as the plan-based
        path - this only stashes what's needed to replay the exact call once
        that specific approval is granted, and surfaces a preview the same way
        _process_planned does.
        """
        preview = f"- {decision.tool_name} (risk: {decision.risk_level.value}): {json.dumps(decision.tool_input, ensure_ascii=False)}"
        metadata = {
            **task.metadata,
            "operator_history": history,
            "operator_pending_call": {
                "tool_name": decision.tool_name,
                "tool_input": decision.tool_input,
                "risk_level": decision.risk_level.value,
                "approval_id": approval_id,
                # Survives the approval wait, read back by
                # _process_operator_awaiting_approval below so the resumed
                # call's ToolCallRequest.parent_step_id and history entry
                # link to the same step that requested it (docs/UI_UX_AUDIT.md
                # Phase 14e).
                "step_id": step_id,
            },
            "pending_approval_preview": preview,
        }
        return self._transition_operator(task, metadata, TaskStatus.AWAITING_APPROVAL, "operator_approval_required")

    async def _process_operator_batch_awaiting_approval(self, task: TaskRecord) -> TaskRecord:
        """Resume a batch once its single approval is decided.

        Children were minted bound to their exact requests, so replay passes
        each call its own approval id and PolicyEngine._approval_matches still
        does the byte-for-byte check. Approving the batch never widens
        authority beyond the calls that were listed.
        """
        history: list[dict[str, Any]] = list(task.metadata.get("operator_history") or [])
        pending = task.metadata.get("operator_pending_batch")
        if not isinstance(pending, dict):
            return self._transition_operator(
                task, {**task.metadata, "operator_history": history}, TaskStatus.BLOCKED,
                "operator_pending_batch_missing",
            )
        approval = self.repositories.approvals.get(str(pending.get("approval_id") or ""))
        if approval is None or approval.task_id != task.id:
            return self._transition_operator(
                task, {**task.metadata, "operator_history": history}, TaskStatus.BLOCKED, "approval_not_granted",
            )
        if approval.status == ApprovalStatus.PENDING:
            return task
        if approval.status != ApprovalStatus.APPROVED or approval.expires_at <= utc_now():
            history.append({
                "tool_name": "call_tools_parallel", "input": None, "status": "failed",
                "error": "the batch was not approved", "step_id": pending.get("step_id"),
            })
            metadata = {
                k: v for k, v in task.metadata.items()
                if k not in {"operator_pending_batch", "pending_approval_preview"}
            }
            return self._transition_operator(
                task,
                {**metadata, "operator_history": history},
                TaskStatus.RUNNING,
                "operator_batch_rejected",
            )

        calls = [
            ParallelToolCall(
                tool_name=str(item.get("tool_name") or ""),
                tool_input=dict(item.get("tool_input") or {}),
                risk_level=RiskLevel(str(item.get("risk_level") or "low")),
            )
            for item in (pending.get("calls") or [])
            if isinstance(item, dict)
        ]
        approvals_by_call = {
            (str(entry.get("tool_name")), json.dumps(entry.get("input"), sort_keys=True)): str(entry.get("approval_id"))
            for entry in (approval.action_payload.get("batch") or [])
            if isinstance(entry, dict)
        }
        step_id = str(pending.get("step_id") or "")
        entries = await self._run_parallel_calls(
            task.id, calls, step_id=step_id, approvals_by_call=approvals_by_call
        )
        history.extend(entries)
        recorded = self.repositories.tasks.get(task.id) or task
        metadata = {k: v for k, v in recorded.metadata.items() if k not in {"operator_pending_batch", "pending_approval_preview"}}
        return self._transition_operator(
            recorded, {**metadata, "operator_history": history}, TaskStatus.RUNNING, "operator_batch_approved",
        )

    async def _process_operator_awaiting_approval(self, task: TaskRecord) -> TaskRecord:
        """Resume path for _request_operator_approval above: mirrors
        _process_awaiting_approval's status handling, but replays the exact
        pending tool call (stashed in metadata["operator_pending_call"])
        instead of assuming a plan_id/current_step_id to advance to.
        """
        history: list[dict[str, Any]] = list(task.metadata.get("operator_history") or [])
        pending_call = task.metadata.get("operator_pending_call")
        if not isinstance(pending_call, dict) or not pending_call.get("tool_name"):
            return self._transition_operator(
                task, {**task.metadata, "operator_history": history}, TaskStatus.BLOCKED,
                "operator_pending_call_missing",
            )
        approval_id = str(pending_call.get("approval_id") or "")
        approval = self.repositories.approvals.get(approval_id) if approval_id else None
        if approval is None or approval.task_id != task.id:
            return self._transition_operator(
                task, {**task.metadata, "operator_history": history}, TaskStatus.BLOCKED, "approval_not_granted",
            )
        if approval.status == ApprovalStatus.PENDING:
            return task
        if approval.expires_at <= utc_now() and approval.status == ApprovalStatus.APPROVED:
            self.repositories.approvals.set_status(approval.id, ApprovalStatus.EXPIRED)
        if approval.status != ApprovalStatus.APPROVED or approval.expires_at <= utc_now():
            return self._transition_operator(
                task, {**task.metadata, "operator_history": history}, TaskStatus.BLOCKED,
                "approval_not_granted",
            )
        tool_name = str(pending_call["tool_name"])
        tool_input = pending_call.get("tool_input") or {}
        step_id = pending_call.get("step_id")
        if self.executor is None:
            raise RuntimeError("cannot resume an approved tool call without an executor")
        tool_def = self.executor.tool_definitions.get(tool_name)
        metadata = {**task.metadata}
        metadata.pop("operator_pending_call", None)
        metadata.pop("pending_approval_preview", None)
        if tool_def is None:
            history.append({
                "tool_name": tool_name, "input": tool_input,
                "status": "failed", "error": f"unregistered tool: {tool_name}",
                "step_id": step_id,
            })
            return self.repositories.tasks.update_metadata(
                task.id, {**metadata, "operator_history": history}, TaskStatus.RUNNING
            )
        request = ToolCallRequest(
            task_id=task.id,
            tool_name=tool_name,
            capability=tool_def.capability,
            risk_level=RiskLevel(pending_call["risk_level"]) if pending_call.get("risk_level") else RiskLevel.LOW,
            input=tool_input,
            parent_step_id=step_id,
        )
        result = await self.executor.execute(request, approval_id=approval.id)
        recorded = self._record_tool_result(task.id, tool_name, result)
        output_text = _tool_output_text(result) if result.status == ToolResultStatus.SUCCEEDED else None
        history.append({
            "tool_name": tool_name,
            "input": tool_input,
            "status": result.status.value,
            "output_summary": output_text[:2000] if output_text else None,
            "error": result.error_message,
            "request_id": result.request_id,
            "step_id": step_id,
        })
        final_metadata = {**metadata, **recorded.metadata, "operator_history": history}
        final_metadata.pop("operator_pending_call", None)
        final_metadata.pop("pending_approval_preview", None)
        return self.repositories.tasks.update_metadata(task.id, final_metadata, TaskStatus.RUNNING)

    async def _maybe_learn_preference(self, task: TaskRecord) -> None:
        """Queue a durable preference from the operator's own words, if any.

        Runs after the task is finished and its result already sent, so a slow
        or failing extraction can never delay or break the actual answer -
        this is the least important thing happening on this tick.
        """
        if self.persona_config is None or self.operator is None:
            return
        if task.status not in {TaskStatus.COMPLETED, TaskStatus.FAILED}:
            return
        message = str(task.metadata.get("original_message_text") or task.objective or "")
        try:
            from agent_control.persona_learning import propose_from_message

            await propose_from_message(
                getattr(self.operator, "provider", None),
                self.persona_config,
                message,
                task_id=task.id,
            )
        except Exception:  # noqa: BLE001 - never surface as a task failure
            logger.warning("persona learning pass failed", exc_info=True)

    async def _maybe_learn_skill(self, task: TaskRecord) -> None:
        """Offer to save a successful multi-step run as a reusable procedure.

        After the answer has already gone out, like the persona pass - this is
        the least important work on the tick and must never delay or break a
        finished task.
        """
        if self.skills_config is None or self.operator is None:
            return
        if task.status != TaskStatus.COMPLETED:
            return
        try:
            from agent_control.skill_learning import propose_from_task

            await propose_from_task(getattr(self.operator, "provider", None), self.skills_config, task)
        except Exception:  # noqa: BLE001 - never surface as a task failure
            logger.warning("skill learning pass failed", exc_info=True)

    def _transition_operator(
        self, task: TaskRecord, metadata: dict[str, Any], status: TaskStatus, reason: str
    ) -> TaskRecord:
        """Terminal-state transition for the operator loop only.

        Deliberately does NOT call _transition(): that method's COMPLETED path
        runs fulfillment-gap recovery which can attach a plan-based recovery
        plan, crossing back into the plan-once path from inside this one.
        """
        if status in {TaskStatus.FAILED, TaskStatus.BLOCKED}:
            metadata = {**metadata}
            metadata.setdefault("last_worker_error", _operator_transition_error_message(reason, self.operator_max_steps))
        updated = self.repositories.tasks.update_metadata(task.id, metadata, status)
        self.audit.task_state_changed("worker", task.id, task.status, status)
        if status in {TaskStatus.FAILED, TaskStatus.BLOCKED}:
            self.audit.append(
                AuditEventType.ERROR, actor="worker", task_id=task.id,
                payload={"error": reason, "status": status.value},
            )
        return updated

    def _operator_ask_user(self, task: TaskRecord, question: str) -> TaskRecord:
        """The question is used verbatim - the operator already composed a
        specific question because it decided it needs one, so there's nothing
        to template from a failure reason. Two-question exhaustion limit,
        same as everywhere else in this file that can pause a task on the
        user (see clarify_count usage throughout).
        """
        latest = self.repositories.tasks.get(task.id) or task
        ask_count = int(latest.metadata.get("clarify_count", 0))
        if ask_count >= 2:
            return self._transition_operator(
                latest, latest.metadata, TaskStatus.FAILED, "clarification_attempts_exhausted"
            )
        metadata = {
            **latest.metadata,
            "clarify_count": ask_count + 1,
            "clarifying_question": question,
            "clarifying_reason": question[:400],
        }
        updated = self.repositories.tasks.update_metadata(latest.id, metadata, TaskStatus.CLARIFYING)
        self.audit.task_state_changed("worker", latest.id, latest.status, updated.status)
        self.audit.append(
            AuditEventType.TASK_STATE_CHANGED,
            actor="operator",
            task_id=latest.id,
            payload={"reason": "clarification_requested", "question": question, "clarify_count": ask_count + 1},
        )
        return updated

    def _planner_context(self, extra: str = "") -> str:
        if self.config_context_factory is None:
            return self.config_context + extra
        try:
            base = self.config_context_factory()
        except Exception:
            logger.warning("config context factory failed; falling back to startup context", exc_info=True)
            base = self.config_context
        return base + extra

    async def _notify_if_needed(self, task: TaskRecord) -> None:
        if task.status not in NOTIFIABLE_STATUSES:
            return
        # CLARIFYING can happen more than once per task; key by question number
        # so the second question is not swallowed by the dedupe set.
        status_key = task.status.value
        if task.status == TaskStatus.CLARIFYING:
            status_key = f"clarifying:{task.metadata.get('clarify_count', 0)}"
        elif task.status in {TaskStatus.RETRYING, TaskStatus.RUNNING}:
            # Key on how many steps the operator has taken, so a long task
            # sends a progress update per step instead of one "working on it"
            # and then silence. This used to key on metadata["attempt_history"]
            # + current_step_id - both plan-era fields with zero writers since
            # P3, which collapsed the key to the constant "running" and killed
            # progress reporting entirely. See docs/HISTORY.md §3.3.
            history = task.metadata.get("operator_history")
            if isinstance(history, list) and history:
                status_key = f"{task.status.value}:steps:{len(history)}"
        notified = set(task.metadata.get("notified_statuses", []))
        if self.notification_sink is not None and status_key not in notified:
            await self.notification_sink.notify(task)
        await self._remember_task_completion(task)
        latest = self.repositories.tasks.get(task.id)
        if latest is None:
            return
        updated_notified = sorted({*latest.metadata.get("notified_statuses", []), status_key})
        self.repositories.tasks.update_metadata(
            task.id,
            {**latest.metadata, "notified_statuses": updated_notified},
        )

    async def _remember_task_completion(self, task: TaskRecord) -> None:
        if not task.conversation_id or task.status not in {TaskStatus.COMPLETED, TaskStatus.BLOCKED, TaskStatus.FAILED}:
            return
        summary = _task_memory_summary(task)
        if not summary:
            return
        try:
            await ConversationMemoryService(self.repositories).update_from_task_summary(task.conversation_id, summary)
        except Exception as exc:
            self.audit.append(
                AuditEventType.ERROR,
                actor="memory",
                task_id=task.id,
                payload={"error": "task_memory_update_failed", "reason": str(exc)},
            )

    def _record_llm_usage(
        self, task: TaskRecord, source: str, usage: dict[str, Any] | None, *, fallback_used: bool = False,
    ) -> TaskRecord:
        """Accumulate one LLM call's token usage into task.metadata["token_usage"].

        `source` is "operator" or "auditor" - the two LLM calls the worker
        itself makes per step (docs/HISTORY.md Part 4 T1.4). Deliberately
        does NOT cover the Concierge/classifier/responder calls made before a
        task exists, or coding-agent-reported usage (already tracked
        separately as last_tool_usage/last_copilot_usage in
        _record_tool_result - a different cost source with different
        pricing, not merged with this one). `usage` is None whenever the
        provider didn't report it (replay in tests, or a server that omits
        the field) - a no-op, never a fabricated zero.

        `fallback_used` (docs/ROADMAP.md 4.4: "an unexplained fallback is a
        silent quality change") is sticky once set - a task where any call
        ever fell back to a secondary profile stays flagged for the rest of
        the task, even if a later call succeeds on the primary again.
        """
        if not usage:
            return task
        current = dict(task.metadata.get("token_usage") or {})
        current["calls"] = int(current.get("calls", 0)) + 1
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            value = usage.get(key)
            if isinstance(value, int | float):
                current[key] = int(current.get(key, 0)) + int(value)
        by_source = dict(current.get("by_source") or {})
        source_entry = dict(by_source.get(source) or {})
        source_entry["calls"] = int(source_entry.get("calls", 0)) + 1
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            value = usage.get(key)
            if isinstance(value, int | float):
                source_entry[key] = int(source_entry.get(key, 0)) + int(value)
        by_source[source] = source_entry
        current["by_source"] = by_source
        if usage.get("model"):
            current["last_model"] = usage["model"]
        if fallback_used:
            current["fallback_used"] = True
        return self.repositories.tasks.update_metadata(task.id, {**task.metadata, "token_usage": current})

    def _record_llm_call(
        self, task_id: str, source: str, step_index: int, service: Any, *, step_id: str
    ) -> None:
        """Persist one LLM call's request/response/timing (docs/UI_UX_AUDIT.md
        Phase 14d) - a sibling to _record_llm_usage's running token totals
        above, not a replacement: that stays the cheap, always-on counter;
        this is the full per-call record that turns the Duration view's
        inferred "operator thinking" gaps into measured latency.

        Reads last_request/last_response_text/last_model/last_started_at/
        last_latency_ms off `service` (the OperatorLoopService or
        AuditorService instance that just made the call) - the same
        provider-to-service proxying _record_llm_usage already reads
        last_usage from. A no-op if persistence is disabled, or if the call
        didn't actually complete (no last_request/last_started_at means the
        provider raised before setting them). Best-effort: a persistence
        failure is logged, not raised - it must never block the task itself.

        `step_id` (docs/UI_UX_AUDIT.md Phase 14e) is the same id stamped onto
        this step's operator_history entry and any ToolCallRequest.parent_step_id
        it leads to - the real parent-child link Graph v2 is built on.
        """
        if not self.persist_llm_calls:
            return
        messages = getattr(service, "last_request", None)
        started_at = getattr(service, "last_started_at", None)
        if not isinstance(messages, list) or not messages or started_at is None:
            return
        usage = getattr(service, "last_usage", None) or {}
        response_text = getattr(service, "last_response_text", None)
        if isinstance(response_text, str) and len(response_text) > self.llm_call_max_chars:
            response_text = f"{response_text[: self.llm_call_max_chars]}...[truncated]"
        try:
            record = LLMCallRecord(
                task_id=task_id,
                source=source,
                model=getattr(service, "last_model", None),
                step_index=step_index,
                step_id=step_id,
                messages=_cap_messages(redact_payload(messages, self.redact_patterns), self.llm_call_max_chars),
                response_text=response_text,
                prompt_tokens=usage.get("prompt_tokens"),
                completion_tokens=usage.get("completion_tokens"),
                total_tokens=usage.get("total_tokens"),
                latency_ms=getattr(service, "last_latency_ms", None),
                created_at=started_at,
            )
            self.repositories.llm_calls.create(record)
        except Exception as exc:
            self.audit.append(
                AuditEventType.ERROR, actor="worker", task_id=task_id,
                payload={"error": "llm_call_persist_failed", "reason": str(exc)},
            )

    def _record_tool_result(self, task_id: str, tool_name: str, result: ToolCallResult) -> TaskRecord:
        latest = self.repositories.tasks.get(task_id)
        if latest is None:
            raise KeyError(f"task not found: {task_id}")
        metadata = {
            **latest.metadata,
            "last_tool_name": tool_name,
            "last_tool_result": _trim_result(result),
        }
        output_text = _tool_output_text(result)
        if output_text:
            metadata["last_tool_output_text"] = output_text if len(output_text) <= 20000 else f"{output_text[:19997]}..."
            if AuditorService.is_content_tool(tool_name):
                metadata["last_content_tool_output_text"] = (
                    output_text if len(output_text) <= 20000 else f"{output_text[:19997]}..."
                )
                metadata["last_content_tool_request_id"] = result.request_id
        output = result.output if isinstance(result.output, dict) else {}
        usage = output.get("usage")
        if isinstance(usage, dict) and usage:
            metadata["last_tool_usage"] = usage
            if "copilot" in tool_name:
                metadata["last_copilot_usage"] = usage
        for metadata_key, output_key in (
            ("workspace_dir", "workspace_dir"),
            ("server_pid", "server_pid"),
            ("adapter_dir", "adapter_dir"),
            ("adapter_name", "adapter_name"),
            ("browser_state", "browser_state"),
            ("browser_url", "browser_url"),
            ("page_title", "page_title"),
            ("screenshot_path", "screenshot_path"),
            ("screenshot_uri", "screenshot_uri"),
            ("desktop_observation", "observation"),
            ("computer_use_actions", "actions_taken"),
            ("changed_paths", "changed_paths"),
            ("files_created", "files_created"),
            ("files_modified", "files_modified"),
            ("organized_paths", "changed_paths"),
            ("rename_manifest", "rename_manifest"),
            ("file_manifest", "manifest"),
            ("document_path", "path"),
            ("document_summary", "summary"),
            ("changed_files", "changed_files"),
            ("schedule_id", "schedule_id"),
            ("scheduled_task_id", "task_id"),
            ("schedule_next_run_at", "next_run_at"),
            ("mcp_catalog_path", "catalog_path"),
            ("mcp_catalog_updated_at", "catalog_updated_at"),
            ("mcp_selected_tool", "selected_tool"),
        ):
            if output.get(output_key):
                if metadata_key in {
                    "changed_paths",
                    "files_created",
                    "files_modified",
                    "organized_paths",
                    "changed_files",
                }:
                    metadata[metadata_key] = _merge_unique_list(
                        metadata.get(metadata_key), output[output_key]
                    )
                else:
                    metadata[metadata_key] = output[output_key]
        if tool_name == "coding.agent":
            for metadata_key, output_key in (
                ("coding_agent_workspace", "workspace_dir"),
                ("coding_agent_session_id", "session_id"),
                ("coding_agent_limit_state", "limit_state"),
            ):
                if output.get(output_key):
                    metadata[metadata_key] = output[output_key]
        if tool_name == "mcp.client":
            metadata["mcp_catalog"] = {
                "catalog_path": output.get("catalog_path"),
                "catalog_updated_at": output.get("catalog_updated_at"),
                "tool_count": len(output.get("tools") or []),
                "healthy": output.get("healthy"),
            }
        if result.artifact_ids:
            metadata["last_artifact_ids"] = result.artifact_ids
        elif output.get("artifact_ids"):
            metadata["last_artifact_ids"] = output["artifact_ids"]
        if output.get("preview_url"):
            metadata["preview_url"] = output["preview_url"]
        elif tool_name == "workspace.manage" and output.get("url"):
            metadata["preview_url"] = output["url"]
        if tool_name == "artifact.deliver":
            metadata["artifact_delivery"] = output
            if output.get("artifact_id"):
                metadata["last_delivered_artifact_id"] = output["artifact_id"]
        return self.repositories.tasks.update_metadata(task_id, metadata)


_OPERATOR_TOOL_ALIASES = {
    "filesystem": "filesystem.manage",
    "file_system": "filesystem.manage",
    "filesystem_manager": "filesystem.manage",
    "workspace": "workspace.manage",
    "workspace_manager": "workspace.manage",
    "coding_agent": "coding.agent",
    "coding-agent": "coding.agent",
    "codingagent": "coding.agent",
}

_OPERATOR_OPERATION_ALIASES = {
    "filesystem.manage": {
        "create_file": "write_text_file",
        "save_file": "write_text_file",
        "write_file": "write_text_file",
        "read_text_file": "read_file",
        "list_directory": "inspect_folder",
        "list_directory_with_sizes": "inspect_folder",
        "list_folder": "inspect_folder",
        "directory_tree": "inspect_folder",
        "tree": "inspect_folder",
        "search_files": "search",
        "find_files": "search",
        "search_file": "search",
    },
}


def _merge_unique_list(existing: Any, incoming: Any) -> list[Any]:
    result = list(existing) if isinstance(existing, list) else []
    values = incoming if isinstance(incoming, list) else [incoming]
    for value in values:
        if value not in result:
            result.append(value)
    return result


def _canonical_operator_tool_call(
    requested_name: str | None,
    requested_input: dict[str, Any] | None,
    definitions: dict[str, Any],
) -> tuple[str | None, dict[str, Any]]:
    """Recover common model dialects without weakening the tool catalog.

    Only aliases whose canonical target is actually registered are accepted,
    and operation aliases are applied only when that operation exists on the
    registered definition. Policy, schema validation, approval, and allowed
    roots therefore still run against the canonical request exactly as they
    do for a model that emitted the catalog spelling on its first try.
    """
    tool_name = requested_name.strip() if isinstance(requested_name, str) else requested_name
    tool_input = dict(requested_input or {})
    if tool_name not in definitions and isinstance(tool_name, str):
        alias = _OPERATOR_TOOL_ALIASES.get(tool_name.casefold())
        if alias in definitions:
            tool_name = alias

    definition = definitions.get(tool_name) if tool_name is not None else None
    operation = tool_input.get("operation")
    if definition is not None and isinstance(operation, str):
        operation_alias = _OPERATOR_OPERATION_ALIASES.get(str(tool_name), {}).get(operation.casefold())
        supported = set(getattr(definition, "operations", ()) or ())
        if operation_alias and operation_alias in supported:
            tool_input["operation"] = operation_alias
            if operation_alias == "inspect_folder" and "root" not in tool_input and "path" in tool_input:
                tool_input["root"] = tool_input.pop("path")
    if tool_name == "filesystem.manage":
        operation = tool_input.get("operation")
        if operation in {"inspect_folder", "search"} and "root" not in tool_input:
            for alias in ("folder_path", "root_folder", "path"):
                if tool_input.get(alias):
                    tool_input["root"] = tool_input.pop(alias)
                    break
        if operation == "search" and "query" not in tool_input:
            pattern = str(tool_input.pop("pattern", "") or "").strip()
            if pattern and pattern not in {"*", "**", "*.*", "**/*", "**\\*"}:
                tool_input["query"] = pattern
        if operation == "read_file" and "path" not in tool_input:
            file_path = tool_input.pop("file_path", None)
            if file_path:
                tool_input["path"] = file_path
            else:
                folder_path = tool_input.pop("folder_path", None)
                file_name = tool_input.pop("file_name", None)
                if folder_path and file_name:
                    tool_input["path"] = str(Path(str(folder_path)) / str(file_name))
    return tool_name, tool_input


def _normalization_history_fields(
    requested_name: str | None,
    requested_input: dict[str, Any],
    tool_name: str | None,
    tool_input: dict[str, Any],
) -> dict[str, Any]:
    if requested_name == tool_name and requested_input == tool_input:
        return {}
    return {
        "normalized_from": {
            "tool_name": requested_name,
            "operation": requested_input.get("operation"),
        }
    }


def _coding_agent_input_with_explicit_workspace(
    task: TaskRecord,
    tool_name: str | None,
    tool_input: dict[str, Any],
) -> dict[str, Any]:
    """Carry an explicitly requested folder into the coding-agent session.

    The classifier already extracts ``folder_path``. Using it is safe only
    when that exact path also appears in the user's original message; this
    prevents a model-invented path from expanding terminal scope. Without the
    handoff, the CLI writes in YBM's default task workspace while the prompt
    and later verification refer to a different directory.
    """
    if tool_name != "coding.agent":
        return tool_input
    intent = task.metadata.get("orchestration_intent")
    folder_path = None
    if isinstance(intent, dict):
        # Classifier schemas use ``folder_path`` for filesystem routes but
        # commonly use the generic ``path`` field for workspace.manage. Both
        # are bounded by the same exact-user-text check below.
        folder_path = intent.get("folder_path") or intent.get("path")
    original = str(task.metadata.get("original_message_text") or "")
    if isinstance(folder_path, str) and folder_path.strip():
        if folder_path.casefold() not in original.casefold():
            return tool_input
        if any(
            folder_path.casefold() in match.group(0).casefold()
            for match in re.finditer(r"\{\{.*?\}\}", original)
        ):
            return tool_input
        return {**tool_input, "workspace_dir": folder_path}

    # Classifiers sometimes recognize the coding route but omit the literal
    # path even though it appears in the message. Recover only when the user's
    # text contains exactly one existing absolute directory. Existence plus
    # exact source-text provenance avoids guessing where code may be written;
    # the coding adapter's own allowed-root and policy checks still apply.
    candidates = _existing_absolute_directories(original)
    if len(candidates) != 1:
        return tool_input
    return {**tool_input, "workspace_dir": candidates[0]}


def _coding_agent_input_with_task_defaults(
    task: TaskRecord,
    tool_name: str | None,
    tool_input: dict[str, Any],
) -> dict[str, Any]:
    """Fill schema-required coding fields already explicit in the task.

    This is recovery from an incomplete tool dialect, not model invention:
    a provider is filled only when exactly one supported provider is named in
    the user/task text, and the task's own normalized objective supplies a
    missing start prompt.
    """
    if tool_name != "coding.agent" or tool_input.get("operation") != "start":
        return tool_input
    enriched = dict(tool_input)
    if not enriched.get("provider"):
        providers = _named_coding_providers(task)
        if len(providers) == 1:
            enriched["provider"] = providers[0]
    if not enriched.get("prompt") and not enriched.get("objective"):
        enriched["objective"] = task.objective
    return enriched


def _named_coding_providers(task: TaskRecord) -> list[str]:
    request_text = " ".join(
        str(value or "")
        for value in (task.metadata.get("original_message_text"), task.objective)
    ).casefold()
    providers = []
    if re.search(r"\bcodex\b", request_text):
        providers.append("codex")
    if re.search(r"\bclaude(?:\s+code)?\b", request_text):
        providers.append("claude_code")
    if re.search(r"\b(?:github\s+)?copilot\b", request_text):
        providers.append("github_copilot")
    return providers


# Where an absolute path can begin. Matching only drive letters meant this
# recovery was dead on Linux and macOS - a user could name a workspace
# explicitly and the coding agent would still write to YBM's default task
# directory.
#
# The POSIX root alternative is enabled only off Windows, and deliberately so:
# on Windows a bare "/Users" resolves against the current drive, so scanning
# for it there would turn any forward-slash fragment in a message into a real
# existing directory and hand the coding agent a folder the user never named.
# The lookbehind keeps "https://", "and/or", "./foo" and "../bar" from starting
# a match.
_ABSOLUTE_PATH_START = re.compile(
    r"(?i)\b[a-z]:\\" if os.name == "nt" else r"(?i)(?:\b[a-z]:\\|(?<![\w:/.])/(?=[^\s/]))"
)
_ABSOLUTE_PATH_SHAPE = re.compile(r"(?i)(?:[a-z]:\\.+|/.+)")


def _existing_absolute_directories(text: str) -> list[str]:
    candidates: list[str] = []
    for match in _ABSOLUTE_PATH_START.finditer(text):
        segment = text[match.start() : match.start() + 600].splitlines()[0]
        resolved: str | None = None
        for end in range(len(segment), 1, -1):
            candidate = segment[:end].rstrip(" \t\r\n.,;:!?)]}\"'")
            if not _ABSOLUTE_PATH_SHAPE.fullmatch(candidate):
                continue
            try:
                path = Path(candidate)
                if path.is_absolute() and path.is_dir():
                    resolved = str(path.resolve())
                    break
            except OSError:
                continue
        if resolved and resolved.casefold() not in {item.casefold() for item in candidates}:
            candidates.append(resolved)
    return candidates


def _filesystem_search_input_with_content_intent(
    task: TaskRecord,
    tool_name: str | None,
    tool_input: dict[str, Any],
) -> dict[str, Any]:
    """Preserve an explicit request to search inside files.

    ``filesystem.manage:search`` supports content search, but an operator can
    omit the boolean and repeatedly search filenames for a marker the user
    explicitly said was in the file contents. Enrich only that declared
    intent; ordinary filename searches retain their cheaper default.
    """
    if tool_name != "filesystem.manage" or tool_input.get("operation") != "search":
        return tool_input
    request_text = " ".join(
        str(value or "")
        for value in (task.metadata.get("original_message_text"), task.objective)
    ).casefold()
    content_intent = bool(
        re.search(
            r"\b(?:contents?|text|body)\b[^.\n]{0,50}\b(?:contain|contains|include|includes|has|have|match|matches)\b",
            request_text,
        )
        or re.search(r"\b(?:search|find|look)\b[^.\n]{0,50}\b(?:inside|within|in)\s+(?:the\s+)?(?:file|files|contents?|text)\b", request_text)
    )
    if not content_intent:
        return tool_input
    enriched = {**tool_input, "include_content": True}
    if not enriched.get("query"):
        query = _explicit_content_query(request_text)
        if query:
            enriched["query"] = query
    return enriched


def _explicit_content_query(request_text: str) -> str | None:
    for pattern in (
        r"\bcontents?\s+(?:contain|contains|include|includes|match|matches)\s+[\"']?([a-z0-9][a-z0-9_.:-]{2,})",
        r"\bmarker\s+(?:is|named|called)\s+[\"']?([a-z0-9][a-z0-9_.:-]{2,})",
    ):
        match = re.search(pattern, request_text, flags=re.IGNORECASE)
        if match:
            return match.group(1).rstrip(".,;!?")
    return None


def _stale_read_recovery_call(
    task: TaskRecord,
    tool_name: str | None,
    tool_input: dict[str, Any],
    history: list[dict[str, Any]],
) -> tuple[str | None, dict[str, Any]]:
    """Switch an unchanged missing-file retry to a bounded parent search."""
    if tool_name != "filesystem.manage" or tool_input.get("operation") != "read_file":
        return tool_name, tool_input
    path = str(tool_input.get("path") or "").strip()
    if not path:
        return tool_name, tool_input
    repeated_failure = any(
        isinstance(entry, dict)
        and entry.get("tool_name") == "filesystem.manage"
        and entry.get("status") == ToolResultStatus.FAILED.value
        and isinstance(entry.get("input"), dict)
        and entry["input"].get("operation") == "read_file"
        and str(entry["input"].get("path") or "").casefold() == path.casefold()
        for entry in history
    )
    if not repeated_failure:
        return tool_name, tool_input
    request_text = str(task.metadata.get("original_message_text") or task.objective)
    if not re.search(r"\b(?:search|find|recover|renamed|moved|no longer)\b", request_text, re.IGNORECASE):
        return tool_name, tool_input
    if path.casefold() not in request_text.casefold():
        return tool_name, tool_input
    stale_path = Path(path).expanduser()
    recovery_root = next(
        (
            parent
            for parent in stale_path.parents
            if parent.is_dir() and str(parent) != stale_path.anchor
        ),
        None,
    )
    if recovery_root is None:
        return tool_name, tool_input
    query = Path(path).stem.strip()
    if not query:
        return tool_name, tool_input
    return "filesystem.manage", {
        "operation": "search",
        "root": str(recovery_root.resolve()),
        "query": query,
        "include_content": False,
    }


def _required_named_coding_agent_call(
    task: TaskRecord,
    tool_name: str | None,
    tool_input: dict[str, Any],
    definitions: dict[str, Any],
) -> tuple[str | None, dict[str, Any]]:
    """Honor an explicit named-provider delegation before fallback tools."""
    definition = definitions.get("coding.agent")
    if definition is None or not getattr(definition, "enabled", False):
        return tool_name, tool_input
    providers = _named_coding_providers(task)
    if len(providers) != 1:
        return tool_name, tool_input
    if PostconditionType.CODING_AGENT_STEP not in set(validate_fulfillment(task).missing):
        return tool_name, tool_input
    if tool_name == "coding.agent":
        return tool_name, tool_input
    return "coding.agent", {
        "operation": "start",
        "provider": providers[0],
        "objective": task.objective,
    }


def _ordered_artifact_delivery_call(
    task: TaskRecord,
    tool_name: str | None,
    tool_input: dict[str, Any],
    history: list[dict[str, Any]],
    definitions: dict[str, Any],
) -> tuple[str | None, dict[str, Any]]:
    """Honor an explicit delivery request before local/open follow-up work.

    A model repeatedly tried to create a schedule before sending the file in
    "only after the file is delivered, create a schedule", then fabricated
    that delivery and asked for confirmation. Once a successful write gives
    an exact path, no semantic choice remains: route the attempted schedule
    step through the registered delivery tool first. Its normal schema,
    policy, root, channel, and approval checks still apply; only ordering is
    made deterministic.
    """
    delivery_definition = definitions.get("artifact.deliver")
    delivery_operations = set(getattr(delivery_definition, "operations", ()) or ())
    delivery_missing = PostconditionType.ARTIFACT_DELIVERED in set(
        validate_fulfillment(task).missing
    )

    # `filesystem.manage:open_file` means open the file in a local desktop
    # application. Small models can mistake that for sending an already found
    # file to the user. When delivery is an explicit unmet postcondition and
    # the model already supplied the exact path, no semantic choice remains:
    # route the same path through the delivery adapter. Its normal root,
    # channel, schema, policy, and approval checks still apply.
    if (
        tool_name == "filesystem.manage"
        and tool_input.get("operation") == "open_file"
        and tool_input.get("path")
        and delivery_missing
        and "send_file" in delivery_operations
    ):
        return "artifact.deliver", {
            "operation": "send_file",
            "path": tool_input["path"],
            "caption": "Requested file",
        }

    if tool_name != "schedule.manage":
        return tool_name, tool_input
    request_text = str(task.metadata.get("original_message_text") or task.objective)
    ordered_request = re.search(
        r"\bonly\s+after\b[^.\n]{0,120}\b(?:deliver\w*|send|sent)\b"
        r"[^.\n]{0,120}\b(?:schedule\w*|scheduled)\b",
        request_text,
        flags=re.IGNORECASE,
    )
    if not ordered_request:
        return tool_name, tool_input
    if not delivery_missing:
        return tool_name, tool_input
    output_path = _known_written_file_path(task, history)
    if not output_path or delivery_definition is None:
        return tool_name, tool_input
    if "send_file" not in delivery_operations:
        return tool_name, tool_input
    return "artifact.deliver", {
        "operation": "send_file",
        "path": output_path,
        "caption": "Requested completed file",
    }


def _satisfied_task_does_not_need_clarification(
    task: TaskRecord,
    history: list[dict[str, Any]],
) -> bool:
    """Reject optional follow-up questions after typed work is complete.

    An empty expectation set cannot prove that the user's input is complete,
    so the guard deliberately does nothing for open-ended requests. At least
    one successful tool call and one satisfied inferred postcondition are
    required before asking the operator to finish instead of seek polish or
    confirmation that the user never made a prerequisite.
    """
    validation = validate_fulfillment(task)
    if not validation.expected or not validation.ok:
        return False
    return any(
        isinstance(entry, dict)
        and entry.get("status") == ToolResultStatus.SUCCEEDED.value
        for entry in history
    )


def _clarification_recovery_reason(
    task: TaskRecord,
    history: list[dict[str, Any]],
) -> str | None:
    if _satisfied_task_does_not_need_clarification(task, history):
        return (
            "the requested work's inferred postconditions are already satisfied; "
            "report optional review or verification gaps instead of asking the user"
        )

    validation = validate_fulfillment(task)
    missing = set(validation.missing)
    if PostconditionType.ARTIFACT_DELIVERED in missing:
        output_path = _known_written_file_path(task, history)
        if output_path:
            return (
                f"artifact delivery is still required and the successful write already provides "
                f"the exact path {output_path!r}; use the delivery tool"
            )
    if PostconditionType.ADAPTER_PROPOSAL in missing and _request_has_named_adapter(task):
        return (
            "the user supplied a concrete adapter name, generic contract, and safety boundary; "
            "scaffold the cache-only proposal with conservative defaults and report review gaps"
        )
    return None


def _known_written_file_path(task: TaskRecord, history: list[dict[str, Any]]) -> str | None:
    for entry in reversed(history):
        if not isinstance(entry, dict) or entry.get("status") != ToolResultStatus.SUCCEEDED.value:
            continue
        tool_input = entry.get("input") if isinstance(entry.get("input"), dict) else {}
        if (
            entry.get("tool_name") == "filesystem.manage"
            and tool_input.get("operation") == "write_text_file"
            and tool_input.get("path")
        ):
            return str(tool_input["path"])
    for key in ("changed_paths", "files_created", "files_modified", "changed_files"):
        values = task.metadata.get(key)
        if isinstance(values, list) and values:
            return str(values[-1])
    return None


def _request_has_named_adapter(task: TaskRecord) -> bool:
    request_text = " ".join(
        str(value or "")
        for value in (task.metadata.get("original_message_text"), task.objective)
    ).casefold()
    return bool(
        re.search(r"\b(?:adapter|proposal)\s+(?:named|called)\s+[a-z][a-z0-9_-]{2,}\b", request_text)
        or re.search(r"\bcreate\s+(?:a\s+)?[a-z][a-z0-9_-]{2,}\s+adapter\b", request_text)
    )


def _post_clarification_history_offset(task: TaskRecord, history: list[dict[str, Any]]) -> int:
    raw_offset = task.metadata.get("operator_history_offset_after_clarification")
    try:
        offset = int(raw_offset)
    except (TypeError, ValueError):
        # Conservative compatibility for a task resumed by an older process:
        # do not let any pre-answer content replace the current answer.
        return len(history) if task.metadata.get("clarification_answer") else 0
    return max(0, min(offset, len(history)))


def _auditor_response_context(task: TaskRecord, memory_context: str) -> str | None:
    parts: list[str] = []
    if memory_context.strip():
        parts.append(memory_context.strip())
    question = str(task.metadata.get("answered_clarifying_question") or "").strip()
    answer = str(task.metadata.get("clarification_answer") or "").strip()
    if answer:
        clarification = f"Latest user clarification: {answer}"
        if question:
            clarification = f"Latest clarifying question: {question}\n{clarification}"
        parts.append(clarification)
    return "\n\n".join(parts) or None


def _operator_blocked_reason(reason: str | None, history: list[dict[str, Any]]) -> str:
    stated = str(reason or "").strip()
    if stated and stated != "operator_blocked":
        return stated
    for entry in reversed(history):
        if entry.get("tool_name") in CHECK_ENTRY_NAMES:
            continue
        if entry.get("status") in {"failed", "denied", "rate_limited"}:
            tool_name = str(entry.get("tool_name") or "the latest capability")
            error = str(entry.get("error") or "it did not succeed").strip()
            return (
                "No available capability completed the request. "
                f"The latest attempt with {tool_name} did not succeed: {error}"
            )
    return "No available tool or capability can complete the request with the current configuration."


def _effective_operator_risk(tool_definition: Any, tool_input: dict[str, Any], declared: RiskLevel) -> RiskLevel:
    """Use the runtime-owned risk for a model-authored tool call.

    The executor still rejects understated requests from any other caller.
    Operator decisions are normalized first so a model cannot bypass policy
    by understating risk *or* derail a valid call by conservatively
    overstating it above the capability ceiling. Tool definitions and their
    operation-specific resolvers are the authoritative risk classifiers.
    Invalid inputs stay untouched and are rejected by the executor's normal
    schema validation path.
    """
    try:
        validated_input = tool_definition.validate_input(tool_input)
    except ValueError:
        return declared
    return tool_definition.required_risk(validated_input)

def _is_background_external_tool_result(tool_name: str | None, result: ToolCallResult) -> bool:
    if tool_name != "coding.agent" or result.status != ToolResultStatus.SUCCEEDED:
        return False
    output = result.output if isinstance(result.output, dict) else {}
    return output.get("status") == "running" and bool(output.get("session_id"))


def _last_content_tool_history_entry(history: list[dict[str, Any]]) -> dict[str, Any] | None:
    for entry in reversed(history):
        if entry.get("status") == "succeeded" and AuditorService.is_content_tool(entry.get("tool_name")):
            return entry
    return None


def _operator_audit_evidence(
    history: list[dict[str, Any]],
    last_content_entry: dict[str, Any],
    last_raw_output: str,
) -> str:
    """Build observable evidence for auditing a multi-tool objective.

    A one-tool task retains the full raw output byte-for-byte (important for
    long list/count audits). For a multi-tool task, include every recorded
    outcome so the auditor can see that earlier inspection, write, delivery,
    and scheduling steps happened instead of judging the entire objective
    from only the final tool's output.
    """
    real_entries = [entry for entry in history if entry.get("tool_name") not in CHECK_ENTRY_NAMES]
    if len(real_entries) <= 1:
        return last_raw_output
    parts: list[str] = []
    for index, entry in enumerate(real_entries, 1):
        tool_name = str(entry.get("tool_name") or "unknown")
        status = str(entry.get("status") or "unknown")
        if entry is last_content_entry:
            evidence = last_raw_output
        else:
            evidence = str(entry.get("output_summary") or entry.get("error") or "")
        tool_input = entry.get("input")
        input_text = json.dumps(tool_input, ensure_ascii=False, default=str)[:8000] if tool_input else ""
        details = []
        if input_text:
            details.append(f"Input: {input_text}")
        if evidence:
            details.append(f"Output: {evidence}")
        parts.append(f"{index}. {tool_name} ({status})\n" + "\n".join(details))
    return "\n\n".join(parts)


def _tool_call_count(history: list[dict[str, Any]]) -> int:
    """Real tool calls only, excluding the check pseudo-entries.

    The fulfillment/audit gap paths append CHECK_* rows to `history` so the
    next decide() call sees why `done` was rejected. Those are bookkeeping,
    not work: counting them against operator_max_steps meant every gap cycle
    stole a slot from the tool calls the model needs to actually close the
    gap - with the default budget of 8, two fulfillment plus two audit gaps
    burned half of it, and a task could exhaust its budget having called zero
    tools. See docs/HISTORY.md §3.1.
    """
    return len([entry for entry in history if entry.get("tool_name") not in CHECK_ENTRY_NAMES])


# A replay step gets this many attempts before a lingering TIMEOUT/
# RATE_LIMITED result gives up and blocks instead of reissuing the same
# call forever. Small and separate from RetryPolicy.max_retries (which
# governs the automatic RETRYING backoff a live task also gets) - this
# caps how many times _next_replay_decision itself will re-propose the
# identical step across those backoff cycles.
_MAX_REPLAY_STEP_ATTEMPTS = 3


def _next_replay_decision(
    replay_plan: list[dict[str, Any]],
    history: list[dict[str, Any]],
    metadata: dict[str, Any],
) -> tuple[OperatorDecision, dict[str, Any]]:
    """The replay counterpart to OperatorLoopService.decide() (docs/ROADMAP.md
    "reusable verified workflows"): walks a recorded plan deterministically
    instead of asking an LLM what to do next. Returns the same
    OperatorDecision shape a live decide() call would, so every downstream
    branch in _process_operator_loop - approval, retry, verification,
    fulfillment, DONE grounding - runs completely unchanged regardless of
    which produced it; only this one call site differs.

    FAILED/DENIED (never auto-retried - RetryPolicy.RETRYABLE_STATUSES is
    only TIMEOUT/RATE_LIMITED) stop the replay immediately: something about
    the target changed since the source run, and silently trying the next
    step anyway would mean skipping over a step nobody confirmed happened.

    TIMEOUT/RATE_LIMITED are genuinely ambiguous by the time this function
    ever sees one as the latest entry: reaching here at all means the task
    is back in a state ready for a fresh decision, which happens both right
    after RetryPolicy's own backoff cycle elapses (this step should simply
    be reissued, same as a live task's LLM would naturally do) and once
    RetryPolicy has separately exhausted itself on this step (reissuing
    would just fail the same way again). Nothing available here can tell
    those two apart, so this reissues up to _MAX_REPLAY_STEP_ATTEMPTS times
    before giving up - bounded retries that would otherwise risk running
    forever if the two cases can't be told apart.

    Returns (decision, updated_metadata) rather than mutating in place: the
    per-step attempt counter has to persist across ticks the same way
    operator_retry_count already does, and the caller is what actually owns
    writing task metadata.
    """
    real_entries = [entry for entry in history if entry.get("tool_name") not in CHECK_ENTRY_NAMES]
    completed = len(real_entries)
    last = real_entries[-1] if real_entries else None
    last_status = str(last.get("status") or "") if last else ""

    if last_status in {"failed", "denied"}:
        tool_name = str((last or {}).get("tool_name") or "a step")
        error = str((last or {}).get("error") or "it did not succeed")
        return (
            OperatorDecision(
                action=OperatorAction.BLOCKED,
                reason=f"Replay step {completed}/{len(replay_plan)} ({tool_name}) failed: {error}",
            ),
            metadata,
        )

    if last_status in {"timeout", "rate_limited"}:
        attempts = int(metadata.get("replay_step_attempts", 0)) + 1
        if attempts > _MAX_REPLAY_STEP_ATTEMPTS:
            tool_name = str((last or {}).get("tool_name") or "a step")
            error = str((last or {}).get("error") or last_status)
            return (
                OperatorDecision(
                    action=OperatorAction.BLOCKED,
                    reason=(
                        f"Replay step {completed}/{len(replay_plan)} ({tool_name}) did not succeed "
                        f"after {attempts - 1} attempt(s): {error}"
                    ),
                ),
                metadata,
            )
        # completed still counts this step (it produced a history entry) -
        # replay_plan[completed - 1] is the step that just came back
        # timed-out/rate-limited, reissued rather than advanced past.
        step = replay_plan[completed - 1]
        return _replay_call_decision(step, completed, len(replay_plan)), {
            **metadata,
            "replay_step_attempts": attempts,
        }

    if completed >= len(replay_plan):
        return (
            OperatorDecision(
                action=OperatorAction.DONE,
                final_answer=f"Replayed {len(replay_plan)}/{len(replay_plan)} step(s) successfully.",
            ),
            metadata,
        )

    step = replay_plan[completed]
    decision = _replay_call_decision(step, completed + 1, len(replay_plan))
    if metadata.get("replay_step_attempts"):
        return decision, {**metadata, "replay_step_attempts": 0}
    return decision, metadata


def build_replay_plan(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The ordered list of real, succeeded, single-tool calls from a
    completed task's operator_history (docs/ROADMAP.md "reusable verified
    workflows") - what admin.py's replay endpoint hands to a new task's
    replay_plan. Only succeeded entries: a step that failed, got retried,
    and then succeeded left more than one history entry behind for the
    same conceptual step, and only the one that actually worked should be
    reissued. risk_level is deliberately not carried over -
    _effective_operator_risk always re-derives it from the tool's own
    definition regardless of what a decision (live or replayed) declares,
    the same runtime-owned-risk guarantee a live task gets.

    Excludes "delegate" and anything with a non-empty `origin` (a
    parallel-batch or subagent entry, per ToolCallRequest.origin's own
    values) - both represent something other than one plain CALL_TOOL step
    (a whole sub-task, or one member of a batch dispatched together), and
    _next_replay_decision only ever reissues a single CALL_TOOL. Replaying
    those correctly would need its own handling, left out of this pass.
    """
    return [
        {"tool_name": entry["tool_name"], "tool_input": entry.get("input") or {}}
        for entry in history
        if entry.get("status") == "succeeded"
        and entry.get("tool_name") not in CHECK_ENTRY_NAMES
        and entry.get("tool_name") != "delegate"
        and not entry.get("origin")
    ]


def _replay_call_decision(step: dict[str, Any], step_number: int, total: int) -> OperatorDecision:
    return OperatorDecision(
        action=OperatorAction.CALL_TOOL,
        tool_name=str(step.get("tool_name") or ""),
        tool_input=dict(step.get("tool_input") or {}),
        risk_level=RiskLevel(str(step.get("risk_level") or "low")),
        reasoning=f"Replaying step {step_number}/{total} from the source task.",
    )


def _fulfilled_step_budget_answer(task: TaskRecord, history: list[dict[str, Any]]) -> str:
    """Ground a completion that reached the step cap after fulfillment."""
    adapter_dir = task.metadata.get("adapter_dir")
    if adapter_dir:
        return f"Created the adapter proposal in the cache at {adapter_dir}. It was not loaded automatically."
    workspace_dir = task.metadata.get("workspace_dir")
    if workspace_dir:
        return f"Completed the requested work in {workspace_dir}."
    schedule_id = task.metadata.get("schedule_id")
    if schedule_id:
        return f"Created the requested schedule ({schedule_id})."
    for entry in reversed(history):
        if entry.get("status") == "succeeded" and entry.get("output_summary"):
            return str(entry["output_summary"])
    return "Completed the requested work and recorded the required result."


def _repeated_no_progress_call(
    history: list[dict[str, Any]],
    *,
    threshold: int = 3,
) -> tuple[str, str] | None:
    """Detect a bounded run of identical observations or failures.

    An operator may vary the prose in a read-only request while continuing to
    receive exactly the same page or status. Treating those calls as progress
    burns the whole step budget and hides the actual limitation. Three
    consecutive identical outcomes are enough evidence to stop safely. This
    also catches a model retrying the same missing path despite receiving the
    same explicit error; writes with changing output do not match this guard.
    """
    real_entries = [
        entry
        for entry in history
        if isinstance(entry, dict) and entry.get("tool_name") not in CHECK_ENTRY_NAMES
    ]
    if len(real_entries) < threshold:
        return None
    recent = real_entries[-threshold:]
    statuses = {str(entry.get("status") or "") for entry in recent}
    if len(statuses) != 1 or next(iter(statuses)) not in {
        ToolResultStatus.SUCCEEDED.value,
        ToolResultStatus.FAILED.value,
        ToolResultStatus.DENIED.value,
    }:
        return None
    tool_names = {str(entry.get("tool_name") or "") for entry in recent}
    operations = {
        str((entry.get("input") or {}).get("operation") or "")
        for entry in recent
        if isinstance(entry.get("input"), dict)
    }
    observation_key = (
        "output_summary"
        if next(iter(statuses)) == ToolResultStatus.SUCCEEDED.value
        else "error"
    )
    outputs = {" ".join(str(entry.get(observation_key) or "").split()) for entry in recent}
    operation = next(iter(operations), "")
    if (
        len(tool_names) != 1
        or len(operations) != 1
        or not operation
        or len(outputs) != 1
        or not next(iter(outputs), "")
    ):
        return None
    return next(iter(tool_names)), operation


def _operator_transition_error_message(reason: str, max_steps: int) -> str:
    if reason == "operator_step_budget_exhausted":
        return (
            f"Blocked after the bounded {max_steps}-step operator budget was exhausted; "
            "stopped instead of continuing an unproductive loop."
        )
    return reason


# Operations and tools that actually put bytes on disk. Plan-producing
# operations (organize_plan, rename_plan) deliberately do not qualify - they
# describe a change without making it.
_WRITE_OPERATIONS = frozenset(
    {"write_text_file", "write_files", "materialize_static_app", "web_app_preview", "apply_manifest", "scaffold"}
)

_WRITE_CLAIM = re.compile(
    r"(?i)\b(?:created?|creating|wrote|written|writing|saved|saving|scaffolded|scaffolding|generated|"
    r"added|placed)\b[^.!?\n]{0,90}?\b(?:file|files|folder|directory|package\.json|readme|extension|script)\b"
    r"|\bfiles?\b[^.!?\n]{0,40}?\b(?:were|was|have been|has been|is|are)\s+(?:created|written|saved|added)\b"
)


_NEGATED_CLAIM = re.compile(
    r"(?i)\b(?:not|cannot|can't|can’t|could\s?n[o']?t|couldn’t|un(?:able|successful)|"
    r"fail(?:ed|s|ure)?|did\s?n[o']?t|was\s?n[o']?t|were\s?n[o']?t|no|never|without|skip(?:ped)?)\b"
)


def _claim_is_negated(answer: str, start: int) -> bool:
    """Whether a write claim sits inside a denial of that same write.

    "I could not create the files" states the truth this guard exists to
    protect; flagging it would push a correctly-behaving run into a pointless
    replan and teach the operator that honesty is penalized.
    """
    clause_start = max(
        answer.rfind(".", 0, start), answer.rfind("\n", 0, start), answer.rfind(";", 0, start)
    )
    window = answer[max(clause_start + 1, start - 60):start]
    return bool(_NEGATED_CLAIM.search(window))


# Metadata keys the tool layer promotes when something was actually produced.
# Same notion of evidence `fulfillment._postcondition_satisfied` checks, so the
# two guards cannot disagree about whether a write happened.
_WRITE_EVIDENCE_KEYS = (
    "changed_paths", "changed_files", "files_created", "files_modified",
    "organized_paths", "document_path", "adapter_dir",
)


def _unsupported_write_claim(
    answer: str | None,
    history: list[dict[str, Any]],
    metadata: dict[str, Any] | None = None,
) -> str | None:
    """Reject a final answer that claims files were written when none were.

    A single unsupported-operation failure was enough for the operator to stop
    and synthesize "The following files were created:" for an empty workspace,
    and the task completed (docs/E2E_FINDINGS.md P0-2). Objective-derived
    postconditions are the other guard, but they are inferred from wording; this
    one reads the claim the answer actually makes and demands matching evidence.

    Returns a gap reason for the caller's existing bounded-replan path, so a
    false positive costs two extra steps rather than blocking the task.
    """
    if not answer:
        return None
    claims = [match for match in _WRITE_CLAIM.finditer(answer) if not _claim_is_negated(answer, match.start())]
    if not claims:
        return None

    evidence = _recorded_write_evidence(history, metadata)
    if not evidence:
        return (
            "the answer states that files were created or written, but no successful write "
            "operation is recorded - either perform the write or say plainly that it did not happen"
        )

    # Evidence that *some* write happened is not evidence that the claimed one
    # did. P0-2 listed package.json and extension.ts for an empty workspace; a
    # task that had already written something unrelated would otherwise let the
    # same fabrication through.
    #
    # Only compared when both sides actually name files. A recorded write whose
    # path never surfaced (nothing in the tool input, nothing in the truncated
    # output summary) leaves nothing to compare against, and guessing wrong
    # there costs a correct task two replans and then blocks it - worse than
    # accepting a claim that is already backed by a real write.
    claimed = _claimed_filenames(answer)
    if claimed and _claimed_filenames(evidence) and not any(name in evidence for name in claimed):
        listed = ", ".join(sorted(claimed)[:5])
        return (
            f"the answer names files ({listed}) that no recorded write produced - write them, "
            "or name only what the tool history actually shows"
        )
    return None


# A filename as it appears in prose: a stem plus a short extension. Deliberately
# not matching bare words, so "created the extension" is not read as a file.
_CLAIMED_FILENAME = re.compile(r"\b[\w][\w.-]*\.[A-Za-z][A-Za-z0-9]{0,4}\b")


def _claimed_filenames(answer: str) -> set[str]:
    return {match.group(0).casefold() for match in _CLAIMED_FILENAME.finditer(answer)}


def _recorded_write_evidence(history: list[dict[str, Any]], metadata: dict[str, Any] | None) -> str:
    """Everything the run can show for a write, as one searchable blob."""
    parts: list[str] = []
    if isinstance(metadata, dict):
        for key in _WRITE_EVIDENCE_KEYS:
            value = metadata.get(key)
            if value:
                parts.append(json.dumps(value, default=str))
    for entry in history:
        if not isinstance(entry, dict) or entry.get("status") != ToolResultStatus.SUCCEEDED.value:
            continue
        tool_input = entry.get("input")
        if isinstance(tool_input, dict) and str(tool_input.get("operation") or "") in _WRITE_OPERATIONS:
            parts.append(json.dumps(tool_input, default=str))
            parts.append(str(entry.get("output_summary") or ""))
    return " ".join(parts).casefold()


def _ground_operator_final_answer(answer: str | None, history: list[dict[str, Any]]) -> str | None:
    """Keep the identity of a file-read result in the user-facing answer.

    Models occasionally return only the file contents even though the user
    asked which file was found and read. The tool history has the authoritative
    path, so append it when neither the full path nor basename survived the
    synthesis. This does not infer a path or alter non-file answers.
    """
    if not answer:
        return answer
    source_path = ""
    for entry in reversed(history):
        tool_input = entry.get("input") if isinstance(entry, dict) else None
        if (
            entry.get("status") == ToolResultStatus.SUCCEEDED.value
            and isinstance(tool_input, dict)
            and tool_input.get("operation") == "read_file"
            and tool_input.get("path")
        ):
            source_path = str(tool_input["path"])
            break
    if not source_path:
        return answer
    filename = source_path.replace("\\", "/").rsplit("/", 1)[-1]
    lowered = answer.lower()
    if source_path.lower() in lowered or (filename and filename.lower() in lowered):
        return answer
    return f"{answer.rstrip()}\n\nSource file: {source_path}"


def _completed_coding_agent_answer(tool_name: str, result: ToolCallResult) -> str | None:
    if tool_name != "coding.agent" or result.status != ToolResultStatus.SUCCEEDED:
        return None
    output = result.output if isinstance(result.output, dict) else {}
    if output.get("status") != "completed" or output.get("returncode") not in (None, 0):
        return None
    provider = str(output.get("provider") or "coding agent")
    lines = [f"{provider} completed successfully."]
    workspace = str(output.get("workspace_dir") or "").strip()
    if workspace:
        lines.append(f"Workspace: {workspace}")
    changed_files = [str(item) for item in output.get("changed_files") or [] if str(item).strip()]
    if changed_files:
        lines.append("Changed files:")
        lines.extend(f"- {path}" for path in changed_files[:40])
    summary = str(output.get("summary") or "").strip()
    if summary:
        lines.extend(("", summary[:2_000]))
    return "\n".join(lines)


def _tool_output_text(result: ToolCallResult) -> str:
    output = result.output
    if not isinstance(output, dict):
        return ""
    if _looks_like_mcp_output(output):
        return mcp_output_text(output)
    if _looks_like_http_output(output):
        if output.get("json") is not None:
            return json.dumps(output["json"], ensure_ascii=False, indent=2, default=str)
        if output.get("text"):
            return str(output["text"])
    terminal_output = output.get("terminal_output")
    if isinstance(terminal_output, list):
        chunks = []
        for item in terminal_output:
            if isinstance(item, dict) and item.get("content"):
                chunks.append(str(item["content"]))
        if chunks:
            return "\n\n".join(chunks)
    for key in ("final_summary", "summary", "text", "message", "content"):
        if output.get(key):
            return str(output[key])
    return ""

def _looks_like_mcp_output(output: dict[str, Any]) -> bool:
    if output.get("operation") in {"discover", "list_tools", "call_tool", "health"} and (
        "servers" in output or "tools" in output or "result" in output
    ):
        return True
    return bool(output.get("catalog_path") and ("servers" in output or "tools" in output))

def _looks_like_http_output(output: dict[str, Any]) -> bool:
    return output.get("operation") == "request" and "status_code" in output and (
        "json" in output or "text" in output
    )

def _cap_messages(messages: list[dict[str, Any]], max_chars: int) -> list[dict[str, Any]]:
    """Bounds one LLM call's persisted messages (docs/UI_UX_AUDIT.md Phase
    14d) - applied after redaction, per-message, so the cap can't truncate
    mid-redaction-pattern. A multimodal message's image_url parts are a
    base64 data URI, not prompt text; they're replaced with a placeholder
    rather than capped like text, since a screenshot belongs in the task's
    artifacts, not duplicated (and truncated into garbage) in a text field.
    """
    return [
        {**message, "content": _cap_message_content(message.get("content"), max_chars)}
        for message in messages
        if isinstance(message, dict)
    ]


def _cap_message_content(content: Any, max_chars: int) -> Any:
    if isinstance(content, str):
        return content if len(content) <= max_chars else f"{content[:max_chars]}...[truncated]"
    if isinstance(content, list):
        capped = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "image_url":
                capped.append({"type": "image_url", "image_url": {"url": "[image omitted from trace]"}})
            elif isinstance(part, dict) and isinstance(part.get("text"), str):
                capped.append({**part, "text": _cap_message_content(part["text"], max_chars)})
            else:
                capped.append(part)
        return capped
    return content


def _trim_result(result: ToolCallResult) -> dict:
    payload = result.model_dump(mode="json")
    return _trim_value(payload)

def _trim_value(value):
    if isinstance(value, str):
        return value if len(value) <= 4000 else f"{value[:3997]}..."
    if isinstance(value, list):
        return [_trim_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _trim_value(item) for key, item in value.items()}
    return value

def _task_memory_summary(task: TaskRecord) -> str:
    metadata = task.metadata
    parts = [
        f"Task {task.id} {task.status.value}: {task.objective}",
    ]
    for label, key in (
        ("tool", "last_tool_name"),
        ("output", "last_tool_output_text"),
        ("desktop_observation", "desktop_observation"),
        ("screenshot_path", "screenshot_path"),
        ("browser_url", "browser_url"),
        ("page_title", "page_title"),
        ("document_path", "document_path"),
        ("document_summary", "document_summary"),
        ("workspace_dir", "workspace_dir"),
        ("preview_url", "preview_url"),
        ("changed_paths", "changed_paths"),
        ("organized_paths", "organized_paths"),
        ("file_manifest", "file_manifest"),
        ("rename_manifest", "rename_manifest"),
        ("artifact_ids", "last_artifact_ids"),
    ):
        value = metadata.get(key)
        if value:
            parts.append(f"{label}: {_compact_jsonish(value)}")
    error = _last_error_from_task(task)
    if error:
        parts.append(f"error: {error}")
    return _trim_value(" | ".join(parts))  # type: ignore[return-value]

def _compact_jsonish(value: Any, limit: int = 900) -> str:
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False, default=str)
    text = " ".join(text.split())
    return text if len(text) <= limit else f"{text[: limit - 3]}..."

def _last_error_from_task(task: TaskRecord) -> str | None:
    result = task.metadata.get("last_tool_result")
    if isinstance(result, dict) and result.get("error_message"):
        return str(result["error_message"])
    if task.metadata.get("last_worker_error"):
        return str(task.metadata["last_worker_error"])
    if task.metadata.get("fulfillment_gap"):
        return str(task.metadata["fulfillment_gap"])
    return None

