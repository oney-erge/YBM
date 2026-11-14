from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any, Protocol
from uuid import uuid4

from agent_control.error_text import describe_exception
from agent_control.llm.cloud_gate import CloudFallbackNeeded, hosted_allowed, is_yes
from agent_control.logging_setup import bind_task_context
from agent_control.orchestration import answer_checks, call_policy, ladder, voice
from agent_control.orchestration.approvals import ApprovalStage
from agent_control.orchestration.asking import (
    clarification_recovery_reason,
    named_target_not_found,
    post_clarification_history_offset,
    still_asking_after_the_work_is_done,
    users_own_words,
)
from agent_control.orchestration.auditor import AuditorService
from agent_control.orchestration.call_rewrites import (
    ALREADY_DONE_STATUS,
)
from agent_control.orchestration.check_entries import (
    CHECK_ENTRY_AUDIT,
    CHECK_ENTRY_CLARIFICATION,
    CHECK_ENTRY_FULFILLMENT,
    CHECK_ENTRY_NAMES,
    CHECK_ENTRY_STRATEGY,
    CHECK_ENTRY_STUCK,
)
from agent_control.orchestration.claims import unsupported_write_claim
from agent_control.orchestration.delegation import DelegationStage
from agent_control.orchestration.executor import ToolExecutor
from agent_control.orchestration.external import ExternalStage
from agent_control.orchestration.finishing import (
    finish_named_as_a_tool,
    finish_offer_pending,
    fulfilled_step_budget_answer,
    operator_blocked_reason,
)
from agent_control.orchestration.fulfillment import (
    deliverable_evidence,
    delivery_gap,
    fulfillment_guidance,
    validate_fulfillment,
)
from agent_control.orchestration.grounding import ground_operator_final_answer
from agent_control.orchestration.guards import GuardStage
from agent_control.orchestration.learning import LearningStage
from agent_control.orchestration.operator import OperatorLoopService
from agent_control.orchestration.planning import PlanningStage
from agent_control.orchestration.records import RecordsStage
from agent_control.orchestration.recovery import (
    interrupted_task_recovery,
    reconcile_orphaned_tasks,
)
from agent_control.orchestration.repeats import (
    already_done_summary,
    redundant_read,
    repeat_block_message,
    repeat_stop_message,
    repeated_no_progress_call,
    succeeded_side_effect,
    with_what_is_missing,
)
from agent_control.orchestration.replay import next_replay_decision
from agent_control.orchestration.reply import ReplyStage
from agent_control.orchestration.signals import (
    STALE_SWEEP_INTERVAL_SECONDS,
    sweep_expired_approvals,
    sweep_stale_blocked,
)
from agent_control.orchestration.task_record import (
    TOOL_RUNNING_ACTIONS,
    answer_from_results,
    auditor_response_context,
    effective_operator_risk,
    is_background_external_tool_result,
    last_content_tool_history_entry,
    normalization_history_fields,
    only_receipted_writes,
    operator_audit_evidence,
    steps_used,
    tool_output_text,
)
from agent_control.plans.runner import PlanRunner
from agent_control.plans.stage import StageOutcome
from agent_control.policy.access_hints import blocked_access_hint
from agent_control.recovery import RetryPolicy
from agent_control.schemas import (
    AuditEventType,
    Capability,
    OperatorAction,
    OperatorDecision,
    RiskLevel,
    TaskRecord,
    TaskStatus,
    ToolCallRequest,
    ToolResultStatus,
    new_id,
    utc_now,
)
from agent_control.storage.audit import AuditLogger
from agent_control.storage.repositories import (
    WORKER_CLAIMABLE_STATUSES,
    Repositories,
    WorkerClaimLost,
    worker_claim_context,
)

logger = logging.getLogger(__name__)

class TaskNotificationSink(Protocol):
    async def notify(self, task: TaskRecord) -> None:
        ...

WORKABLE_STATUSES = list(WORKER_CLAIMABLE_STATUSES)


# Active statuses that need recovery after their claim expires or is absent.
# Starting another worker does not prove the current owner stopped. An expired
# lease is also not process fencing; consequential in-flight work needs review.
ORPHANABLE_STATUSES = (TaskStatus.RUNNING, TaskStatus.INTERPRETING)



















class TaskWorker(ApprovalStage, DelegationStage, ExternalStage, LearningStage, ReplyStage, PlanningStage, RecordsStage, GuardStage):
    # Default per-task wall-clock budget. A single task that exceeds this is
    # forcibly transitioned to FAILED so the worker can move on to the queue.
    # Override globally via ``settings.limits.task_budget_seconds`` (config) or
    # per-task via ``task.metadata["task_budget_seconds"]``.
    DEFAULT_TASK_BUDGET_SECONDS: float = 600.0
    # A delegated sub-task (docs/archive/HISTORY.md Part 4 T1.2) gets its own small,
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
        auto_close_blocked_hours: int = 0,
        auditor: AuditorService | None = None,
        persist_llm_calls: bool = True,
        llm_call_max_chars: int = 8000,
        redact_patterns: list[str] | tuple[str, ...] | None = None,
        fulfillment_mode: str = "auditor",
        audit_min_tool_calls: int = 2,
        scope_tools: bool = False,
        verify_numbers: bool = True,
        plan_parts: bool = False,
        voice_pass: bool = False,
        heartbeat_seconds: float = 0.0,
        persona_config: object | None = None,
        skills_config: object | None = None,
        runtime_refresh: Callable[["TaskWorker"], None] | None = None,
        ability_notes: Callable[[str, str | None], Awaitable[tuple[str, str]]] | None = None,
        plan_runner: PlanRunner | None = None,
        plan_stage: Callable[[TaskRecord], Awaitable[StageOutcome]] | None = None,
    ) -> None:
        # Moves started plans forward on every pass (plans/runner.py); None when plans are off.
        self.plan_runner = plan_runner
        # Plans a request that a skill routes to the planner (plans/stage.py); None when plans are off.
        self.plan_stage = plan_stage
        # What bears on a request that the Operator cannot already see (abilities/context.py, plans/context.py), as prompt text, given
        # the request and the chat; "" for most.
        self.ability_notes = ability_notes
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
        # The observe/decide/act loop (docs/archive/HISTORY.md P3 §2.2) - the sole
        # execution path. See orchestration/operator.py.
        self.operator = operator
        self.operator_max_steps = operator_max_steps
        self.auto_close_blocked_hours = auto_close_blocked_hours
        self._last_stale_sweep: float | None = None  # the first poll sweeps (monotonic time can be small on a fresh boot)
        # The Auditor (docs/archive/HISTORY.md P3 §2.1) - grounds a `done` decision
        # against raw tool output before letting it complete. Optional: a
        # worker with no auditor configured skips straight to the
        # fulfillment-gap check, same as before this existed.
        self.auditor = auditor
        # Only the Auditor can own fulfillment, so a worker built without one
        # keeps the legacy gate rather than silently losing the check entirely.
        self._auditor_owns_fulfillment = fulfillment_mode == "auditor" and auditor is not None
        self.audit_min_tool_calls = max(1, int(audit_min_tool_calls))
        self.scope_tools = scope_tools
        self.verify_numbers = verify_numbers
        self.plan_parts = plan_parts
        self.voice_pass = voice_pass
        self.heartbeat_seconds = float(heartbeat_seconds)
        # None disables the persona learning pass entirely (tests, and any
        # embedder that never configured a persona file).
        self.persona_config = persona_config
        self.skills_config = skills_config
        # LLM-call persistence (docs/archive/UI_UX_AUDIT.md Phase 14d) - the receipts
        # behind the Duration view's real (non-inferred) segments. See
        # _record_llm_call below.
        self.persist_llm_calls = persist_llm_calls
        self.llm_call_max_chars = llm_call_max_chars
        self.redact_patterns = redact_patterns
        # Stable id per worker process; written into tasks.claimed_by so
        # concurrent workers can't race on the same task.
        self.worker_id = f"worker-{uuid4().hex[:12]}"
        # Called before each claim so a worker started long ago still honors the
        # access and model the person has since chosen (see reconfigure()).
        self.runtime_refresh = runtime_refresh
        self.runtime_version = 0
        # task id -> the progress line still being sent for it (see _send_notification)
        self._progress_sends: dict[str, asyncio.Future[None]] = {}

    # Attributes reconfigure() leaves alone: who this worker is and what it is
    # connected to. Everything else on the instance is derived from settings.
    _IDENTITY_ATTRIBUTES = frozenset({"repositories", "audit", "worker_id", "runtime_refresh", "runtime_version", "_progress_sends"})

    def reconfigure(self, **kwargs: Any) -> None:
        """Adopt freshly built settings-derived state (executor and its policy and
        tools, the model clients, budgets) without restarting the process.

        The worker used to build all of that once at startup, so turning on file
        access or choosing a model in the console had no effect on it until the
        whole stack was restarted - the console even told people to "restart
        long-running processes". Building a throwaway worker from the new keyword
        arguments and copying its configuration across keeps this in step with
        __init__ automatically, instead of a second list of fields to maintain. A
        task already running keeps the objects it started with.
        """
        fresh = TaskWorker(self.repositories, self.audit, **kwargs)
        for name, value in vars(fresh).items():
            if name not in self._IDENTITY_ATTRIBUTES:
                setattr(self, name, value)

    async def process_next(self) -> TaskRecord | None:
        # Atomically claim a task. Two workers running this call simultaneously
        # will produce at most one successful claim - the other returns None
        # and tries again on the next poll. Claim expires after budget+buffer
        # so a crashed worker doesn't strand the task.
        if self.runtime_refresh is not None:
            try:
                self.runtime_refresh(self)
            except Exception:  # noqa: BLE001 - a bad config edit must never stop the worker
                logger.exception("worker_runtime_refresh_failed")
        if self.plan_runner is not None:
            try:
                await self.plan_runner.tick()
            except Exception:  # noqa: BLE001 - a plan's trouble must never stop the worker taking tasks
                logger.warning("plan_runner_tick_failed", exc_info=True)
        # A claim can expire after startup. Reconcile it before another worker
        # can claim a running task with an uncertain in-flight operation.
        reconcile_orphaned_tasks(self.repositories, self.audit, expired_claims_only=True)
        claim_expiry = int(self.task_budget_seconds) + 120
        task = self.repositories.tasks.claim_next(
            WORKABLE_STATUSES,
            worker_id=self.worker_id,
            claim_expiry_seconds=claim_expiry,
        )
        if task is None:
            return None
        budget = float(task.metadata.get("task_budget_seconds") or self.task_budget_seconds)
        # Per-task overrides may exceed the default used for initial admission.
        # Extend before processing, but do not revive an expired or stolen claim.
        if not self.repositories.tasks.extend_claim(
            task.id, worker_id=self.worker_id, claim_expiry_seconds=budget + 120
        ):
            logger.warning("worker_claim_lost_before_processing", extra={"task_id": task.id})
            return None
        with worker_claim_context(self.repositories.tasks.database, task.id, self.worker_id):
            try:
                return await self._process_claimed(task, budget)
            except WorkerClaimLost:
                logger.warning("worker_claim_lost_during_processing", extra={"task_id": task.id})
                return self.repositories.tasks.get(task.id)

    async def _process_claimed(self, task: TaskRecord, budget: float) -> TaskRecord:
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
                TaskStatus.CLARIFYING,
            }:
                self.repositories.tasks.release_claim(processed.id, worker_id=self.worker_id)
            return processed
        except asyncio.TimeoutError:
            reason = f"task budget {budget:.0f}s exceeded"
            return await self._record_claimed_failure(task.id, reason, budget_seconds=budget)
        except WorkerClaimLost:
            raise
        except Exception as exc:
            return await self._record_claimed_failure(task.id, str(exc))

    async def _record_claimed_failure(
        self, task_id: str, reason: str, *, budget_seconds: float | None = None
    ) -> TaskRecord:
        latest = self.repositories.tasks.get(task_id)
        if latest is None:
            raise KeyError(f"task not found: {task_id}")
        metadata, recovered_status = interrupted_task_recovery(latest)
        if recovered_status != TaskStatus.CLARIFYING:
            metadata, recovered_status = dict(latest.metadata), TaskStatus.FAILED
        metadata["last_worker_error"] = reason
        failed = self.repositories.tasks.update_metadata(task_id, metadata, recovered_status)
        try:
            self.audit.task_state_changed("worker", latest.id, latest.status, failed.status)
            payload: dict[str, Any] = {"error": reason, "status": failed.status.value}
            if budget_seconds is not None:
                payload["budget_seconds"] = budget_seconds
            self.audit.append(AuditEventType.ERROR, actor="worker", task_id=task_id, payload=payload)
            await self._notify_if_needed(failed)
        finally:
            self.repositories.tasks.release_claim(task_id, worker_id=self.worker_id)
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
            # (docs/archive/UI_UX_AUDIT.md Phase 8, second pass) - claim_next never
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
            self._close_stale_tasks()
            if processed is None:
                await asyncio.sleep(poll_interval_seconds)

    def _close_stale_tasks(self) -> list[str]:
        """Close blocked or unanswered tasks that stopped long ago (signals.sweep_stale_blocked), at most every ten minutes."""
        if self.auto_close_blocked_hours <= 0:
            return []
        now = time.monotonic()
        if self._last_stale_sweep is not None and now - self._last_stale_sweep < STALE_SWEEP_INTERVAL_SECONDS:
            return []
        self._last_stale_sweep = now
        try:
            return sweep_stale_blocked(self.repositories, self.audit, max_age=timedelta(hours=self.auto_close_blocked_hours))
        except Exception:  # noqa: BLE001 - housekeeping must never stop the worker
            logger.warning("closing stale tasks failed", exc_info=True)
            return []

    async def process_task(self, task_id: str) -> TaskRecord:
        # Rebind (not merge) so every log line for this tick is greppable by
        # task_id alone - one grep, the whole story, instead of correlating
        # timestamps across an unstructured stdout capture (docs/archive/HISTORY.md §2.1).
        # Rebinding fresh each call matters: this worker's asyncio Task is
        # long-lived (run_forever() polls it repeatedly), so a stale task_id
        # from a previous tick would otherwise leak into this one's logs.
        bind_task_context(task_id=task_id, worker_id=self.worker_id)
        task = self.repositories.tasks.get(task_id)
        if task is None:
            raise KeyError(f"task not found: {task_id}")
        beat = (
            asyncio.create_task(self._heartbeat(task_id))
            if self.heartbeat_seconds > 0 and self.notification_sink is not None else None
        )
        try:
            return await self._process_operator_loop(task)
        finally:
            if beat is not None:
                beat.cancel()

    async def _heartbeat(self, task_id: str) -> None:
        """While one tick runs long (a search of every drive, a script, a slow model call) say so now and then.

        The progress line is only rewritten when something changes, so a single step that takes a minute left the person looking
        at the same words for that minute. This re-sends the line with how long it has been.
        """
        started = time.monotonic()
        while True:
            await asyncio.sleep(self.heartbeat_seconds)
            try:
                task = self.repositories.tasks.get(task_id)
                if task is None or task.status not in {TaskStatus.RUNNING, TaskStatus.RETRYING} or self.notification_sink is None:
                    return
                waited = {**task.metadata, "progress_waiting_seconds": int(time.monotonic() - started)}
                await self.notification_sink.notify(task.model_copy(update={"metadata": waited}))
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a missed heartbeat must never fail a task
                logger.debug("heartbeat notify failed", exc_info=True)

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
        # can still run one (see next_replay_decision's own docstring).
        if self.executor is None:
            return self._transition_operator(task, task.metadata, TaskStatus.BLOCKED, "operator_loop_not_configured")

        latest = self.repositories.tasks.get(task.id) or task
        if latest.status in {TaskStatus.PAUSED, TaskStatus.CANCELLED, TaskStatus.CLARIFYING}:
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
        # A task is RUNNING while this loop works on it. It is not yet when the loop starts, and it is not again after a question was
        # answered (clarification.resume_clarifying_task puts it back to RECEIVED, with the loop flag still set): a call is only
        # admitted for a RUNNING task, so the first call after an answer was never run and the model was asked again, every poll.
        if not latest.metadata.get("operator_loop") or latest.status in {
            TaskStatus.RECEIVED, TaskStatus.INTERPRETING, TaskStatus.PLANNED,
        }:
            latest = self.repositories.tasks.update_metadata(
                latest.id, {**latest.metadata, "operator_loop": True}, TaskStatus.RUNNING
            )
            self.audit.task_state_changed("worker", task.id, task.status, TaskStatus.RUNNING)

        tool_call_count = steps_used(history)
        fulfillment_repair_count = sum(
            1 for entry in history if entry.get("tool_name") == CHECK_ENTRY_FULFILLMENT
        )
        budget_reached = tool_call_count >= self.operator_max_steps + fulfillment_repair_count
        offer_pending = finish_offer_pending(history)
        if budget_reached and not offer_pending:
            fulfillment = validate_fulfillment(latest)
            repair_possible = bool(fulfillment.first_gap) and fulfillment_repair_count < 2
            if not repair_possible and not any(
                isinstance(entry, dict) and entry.get("tool_name") == CHECK_ENTRY_STUCK for entry in history
            ):
                # Out of steps with nothing left to repair. Ending now gave the user a canned line ("Completed the requested
                # work in <folder>") when the work was done and "Blocked" when it was not, although the model usually knows
                # what it found or why it could not finish. One last step, held to answering or asking, lets it say so;
                # what it answers goes through the same checks.
                history.append({
                    "tool_name": CHECK_ENTRY_STUCK,
                    "input": None,
                    "status": "stuck",
                    "error": (
                        "The step budget is used up. Answer now with what you have: state what you found, and say plainly "
                        "anything you could not do (action done), or ask the user one question (action ask_user). "
                        "Do not call another tool."
                    ),
                })
                return self.repositories.tasks.update_metadata(latest.id, {**latest.metadata, "operator_history": history})
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
                    "synthesized_answer": fulfilled_step_budget_answer(latest, history),
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
            if latest.metadata.get("claude_code_offer") is None and self._claude_code_ready():
                return self._offer_claude_code(latest, history)
            return self._transition_operator(
                latest, {**latest.metadata, "operator_history": history},
                TaskStatus.BLOCKED, "operator_step_budget_exhausted",
            )

        pending_hosted = latest.metadata.get("cloud_fallback_pending")
        if isinstance(pending_hosted, dict) and latest.metadata.get("answered_clarifying_question") == pending_hosted.get("question"):
            if is_yes(latest.metadata.get("clarification_answer")):
                approved = sorted({*(latest.metadata.get("cloud_allowed_profiles") or []), str(pending_hosted.get("profile"))})
                latest = self.repositories.tasks.update_metadata(
                    latest.id, {**latest.metadata, "cloud_allowed_profiles": approved, "cloud_fallback_pending": None}
                )
                self.audit.append(
                    AuditEventType.TASK_STATE_CHANGED, actor="user", task_id=latest.id,
                    payload={"reason": "hosted_model_approved", "profile": pending_hosted.get("profile"), "model": pending_hosted.get("model")},
                )
            else:
                return self._transition_operator(
                    latest,
                    {
                        **latest.metadata, "cloud_fallback_pending": None,
                        "last_worker_error": "Stopped: the hosted model was not approved, so nothing was sent outside this machine.",
                    },
                    TaskStatus.BLOCKED,
                    "hosted_model_declined",
                )

        memory_context = str(latest.metadata.get("memory_context") or "")
        # docs/archive/HISTORY.md Part 4 T2.6: a done that was already rejected once
        # (audit-gap or fulfillment-gap retry marker in history) is a
        # concrete, local, zero-cost-to-check sign the default model is
        # struggling on this task - worth the stronger model if one is
        # configured, same reasoning as the existing parse-failure escalation
        # in operator.py, just reacting to an earlier signal.
        prefer_major = any(entry.get("tool_name") in CHECK_ENTRY_NAMES for entry in history)
        # A stable id for this one observe/decide/act tick (docs/archive/UI_UX_AUDIT.md
        # Phase 14e) - stamped onto the operator's own LLM call, the resulting
        # operator_history entry, and any ToolCallRequest.parent_step_id it
        # leads to, so the graph can be built from a real parent-child link
        # instead of inferring structure from `origin` tags alone. Survives an
        # approval or background-external wait via operator_pending_call/
        # awaiting_external, which stash it the same way they already stash
        # tool_name/tool_input.
        step_id = new_id("step")
        replay_plan = latest.metadata.get("replay_plan")
        forced_decision: OperatorDecision | None = None
        if not (isinstance(replay_plan, list) and replay_plan) and self.operator is not None:
            steered = self._ladder_step(latest, history, step_id)
            if isinstance(steered, TaskRecord):
                return steered
            forced_decision = steered
        if forced_decision is None and self.plan_parts and not (isinstance(replay_plan, list) and replay_plan) and self.operator is not None:
            planned = await self._plan_step(latest, history, step_id)
            if isinstance(planned, TaskRecord):
                return planned
            forced_decision = planned
        if forced_decision is not None:
            decision = forced_decision
        elif isinstance(replay_plan, list) and replay_plan:
            # docs/ROADMAP.md "reusable verified workflows": walk a recorded
            # plan instead of asking an LLM what to do next. Everything past
            # this point - approval, retry, verification, fulfillment - is
            # the exact same code a live decision goes through; only how
            # `decision` gets produced differs. See next_replay_decision's
            # own docstring for why retry/failure handling needs its own
            # per-step attempt cap here.
            decision, metadata_updates = next_replay_decision(
                replay_plan, history, latest.metadata, self.executor.tool_definitions
            )
            if metadata_updates is not latest.metadata:
                latest = self.repositories.tasks.update_metadata(latest.id, metadata_updates)
        elif self.operator is None:
            return self._transition_operator(latest, latest.metadata, TaskStatus.BLOCKED, "operator_loop_not_configured")
        else:
            latest = await self._with_ability_notes(latest)
            staged = await self._planned(latest)
            if staged is not None:
                return staged
            try:
                with hosted_allowed(latest.metadata.get("cloud_allowed_profiles") or ()):
                    decision = await self.operator.decide(
                        latest.objective,
                        self._planner_context(
                            self._project_planner_context(latest.metadata) + str(latest.metadata.get("ability_notes") or ""),
                            metadata=latest.metadata,
                        ),
                        history,
                        memory_context=memory_context, prefer_major=prefer_major,
                        # Only passed when it applies, so an Operator without the option is called exactly as before.
                        **({"finish_only": True} if offer_pending else {}),
                    )
                latest = self._record_llm_usage(
                    latest, "operator", getattr(self.operator, "last_usage", None),
                    fallback_used=getattr(self.operator, "last_fallback_used", False),
                )
                self._record_llm_call(latest.id, "operator", len(history), self.operator, step_id=step_id)
            except CloudFallbackNeeded as exc:
                return self._ask_hosted_permission(latest, history, exc)
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

        # A pause or cancel can arrive while the model is deciding. Do not
        # turn the returned decision into a new tool dispatch in that case.
        latest = self.repositories.tasks.get(latest.id) or latest
        if latest.status in {TaskStatus.PAUSED, TaskStatus.CANCELLED}:
            return latest

        if budget_reached and offer_pending and decision.action in TOOL_RUNNING_ACTIONS:
            history.append({
                "tool_name": CHECK_ENTRY_STUCK,
                "input": None,
                "status": "stuck_refused",
                "error": "The model asked for another tool after the step budget was used up; nothing was run.",
            })
            return self.repositories.tasks.update_metadata(latest.id, {**latest.metadata, "operator_history": history})

        self.audit.append(
            AuditEventType.TASK_STATE_CHANGED, actor="operator", task_id=latest.id,
            payload={
                "action": "operator_decision", "decision": decision.model_dump(mode="json"),
                "step_index": len(history), "step_id": step_id,
            },
        )

        decision = finish_named_as_a_tool(decision)
        answer_copied_from_results = False
        if decision.action == OperatorAction.ASK_USER and still_asking_after_the_work_is_done(latest, history):
            # Twice told that the work is done and to report the gap instead of asking, a live run
            # put its question to the user anyway, got "go ahead", and asked it again. The request's
            # work is done, so the task finishes with what was done and the question as a note.
            decision = decision.model_copy(update={
                "action": OperatorAction.DONE,
                "final_answer": answer_from_results(history, decision.question),
            })
            answer_copied_from_results = True

        if decision.action == OperatorAction.DONE:
            final_answer = ground_operator_final_answer(decision.final_answer, history)
            # Skip the grounding pass on a task that made a single tool call:
            # the Operator has just read that one result directly and written
            # final_answer from it, and the Auditor costs ~11s against a whole
            # request that is often ~12s. Governed by
            # operator.audit_min_tool_calls (1 restores auditing everything),
            # because it is a real trade - see that setting's comment. An answer
            # copied line by line from the results has nothing synthesized to audit.
            real_steps = [e for e in history if isinstance(e, dict) and e.get("tool_name") not in CHECK_ENTRY_NAMES]
            request_text = users_own_words(latest)
            # Something the request named that the history proves is not there: "it does not exist" is the whole answer, and a
            # model asked whether that answer is "sufficient" has no content to judge it by.
            absence = ladder.proven_absent(request_text, real_steps)
            worth_auditing = (
                steps_used(history) >= self.audit_min_tool_calls and not answer_copied_from_results and absence is None
                and not only_receipted_writes(real_steps)
            )
            unseen_addresses, unseen = self._values_no_tool_produced(latest, history, final_answer, answer_copied_from_results)
            # What a script can tell is wrong with the answer (answer_checks.py): the model is sent back with the reason, or the answer goes on.
            pushback = answer_checks.check_answer(answer_checks.Answer(
                latest, history, final_answer, request_text, real_steps, step_id, tuple(unseen_addresses), tuple(unseen),
            ))
            if pushback is not None:
                history.append(pushback.entry)
                return self.repositories.tasks.update_metadata(
                    latest.id, {**latest.metadata, "operator_history": history, **pushback.metadata}, TaskStatus.RUNNING,
                )
            evidence_offset = post_clarification_history_offset(latest, history)
            content_entry = (
                last_content_tool_history_entry(history[evidence_offset:])
                if self.auditor is not None and worth_auditing
                else None
            )
            audit_unconfirmed = False
            if content_entry is not None:
                audit_gap_count = int(latest.metadata.get("operator_audit_gap_count", 0))
                if audit_gap_count < 2:
                    # Audit the FULL recorded output, not the history entry's
                    # 2000-char display summary. The Auditor's whole job is
                    # count/section sufficiency ("are all 5 episodes here?") -
                    # judging a truncation produces false INSUFFICIENT verdicts
                    # on exactly the long-content objectives it exists for.
                    # See docs/archive/HISTORY.md §3.2.
                    full_content_request_id = latest.metadata.get("last_content_tool_request_id")
                    raw_output = str(
                        latest.metadata.get("last_content_tool_output_text")
                        if full_content_request_id
                        and full_content_request_id == content_entry.get("request_id")
                        else content_entry.get("output_summary") or ""
                    )
                    audit_evidence = operator_audit_evidence(
                        history[evidence_offset:], content_entry, raw_output
                    )
                    try:
                        with hosted_allowed(latest.metadata.get("cloud_allowed_profiles") or ()):
                            audit_result = await self.auditor.audit(
                                latest.objective,
                                audit_evidence,
                                original_message=str(latest.metadata.get("original_message_text") or "") or None,
                                deliverable_evidence=deliverable_evidence(latest) if self._auditor_owns_fulfillment else "",
                                response_context=auditor_response_context(latest, memory_context),
                            )
                    except CloudFallbackNeeded as exc:
                        return self._ask_hosted_permission(latest, history, exc)
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
                        reworded = ground_operator_final_answer(audit_result.answer, history)
                        if self._audit_rewrite_adds_no_wrong_value(latest, history, final_answer, reworded):
                            final_answer = reworded
                elif self._answer_is_grounded_though_unconfirmed(latest, history, final_answer, memory_context):
                    # The Auditor turned the answer down twice, and the answer holds no number or address that nothing read
                    # produced. A small Auditor rejected a correct two-sentence summary of a 159-character document for being
                    # short, and the task ended "Blocked". The answer is delivered, with a plain note that it was not confirmed.
                    audit_unconfirmed = True
                    final_answer = (
                        f"{final_answer}\n\n(Not fully confirmed: an automatic check could not verify this from what I read, "
                        "so please double-check it.)"
                    )
                else:
                    if latest.metadata.get("claude_code_offer") is None and self._claude_code_ready():
                        return self._offer_claude_code(latest, history)
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

            if self.voice_pass and not audit_unconfirmed and voice.needs_voice(final_answer):
                # A bare value or a tool's own phrasing, said the way a person would; any number it adds is refused.
                spoken = await voice.speak(getattr(self.operator, "provider", None), request_text, final_answer)
                if spoken:
                    final_answer = spoken
            if (
                absence and "looked through" not in final_answer and absence.casefold() not in final_answer.casefold()
                and not re.search(r"\d[\d,]{3,}", final_answer)  # an answer that already quotes how many folders needs no receipt
            ):
                final_answer = f"{final_answer}\n\n{absence}"
            metadata = {
                **latest.metadata, "operator_history": history, "synthesized_answer": final_answer,
                **({"operator_audit_unconfirmed": True} if audit_unconfirmed else {}),
            }
            # With the Auditor owning fulfillment, its INSUFFICIENT verdict
            # above is already the gap check - it saw the user's own words and
            # the factual deliverable list. Re-running the word-set gate here
            # would let hardcoded intent inference overrule the judgment we
            # just paid an LLM call for. unsupported_write_claim is a
            # different kind of check - a narrow, deterministic claim-vs-
            # evidence regex, not an intent-inference heuristic - so it still
            # runs in both modes; it is what caught "The following files were
            # created:" against an empty workspace (docs/archive/E2E_FINDINGS.md P0-2).
            candidate_task = latest.model_copy(update={"metadata": metadata})
            gap = (
                # The Auditor judges the rest, but whether a file or screenshot the user asked for was actually sent is a fact.
                delivery_gap(candidate_task)
                if self._auditor_owns_fulfillment
                else validate_fulfillment(candidate_task).first_gap
            ) or unsupported_write_claim(final_answer, history, metadata)
            if gap and named_target_not_found(candidate_task, history):
                # The request named something that is not there. "It does not exist" is the answer, and it cannot also be
                # required to hold the contents the request expected: the check sent the honest answer back and ended in "Blocked".
                gap = None
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
            intent = latest.metadata.get("orchestration_intent")
            if isinstance(intent, dict) and ladder.needs_feasibility_check(
                {str(e.get("hint_id")) for e in history if isinstance(e, dict) and e.get("tool_name") == CHECK_ENTRY_STRATEGY},
                route=intent.get("route"), questions_asked=int(latest.metadata.get("clarify_count", 0)),
            ):
                history.append({
                    "tool_name": CHECK_ENTRY_STRATEGY, "input": None, "status": "strategy", "hint_id": "feasibility",
                    "error": ladder.FEASIBILITY_TEXT, "step_id": step_id,
                })
                return self.repositories.tasks.update_metadata(latest.id, {**latest.metadata, "operator_history": history}, TaskStatus.RUNNING)
            candidate = latest.model_copy(
                update={"metadata": {**latest.metadata, "operator_history": history}}
            )
            clarification_recovery = clarification_recovery_reason(candidate, history, decision.question)
            if clarification_recovery:
                recovery_count = int(
                    latest.metadata.get("operator_optional_question_recovery_count", 0)
                )
                if offer_pending and sum(
                    1 for entry in history if isinstance(entry, dict) and entry.get("tool_name") == CHECK_ENTRY_STUCK
                ) < 2:
                    # The model used its one "answer now" on a question the user had already answered. Without another
                    # chance the task ended "blocked" although the model only needed to say it could not do the thing.
                    history.append({
                        "tool_name": CHECK_ENTRY_STUCK,
                        "input": None,
                        "status": "stuck",
                        "error": (
                            "That question was already answered, so it cannot be asked again. Answer now with what you "
                            "have, in plain words (action done), or say what blocks you (action blocked)."
                        ),
                    })
                    return self.repositories.tasks.update_metadata(
                        latest.id, {**latest.metadata, "operator_history": history}, TaskStatus.RUNNING
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
            reason = operator_blocked_reason(decision.reason, history)
            metadata = {**latest.metadata, "operator_history": history}
            # A task that cannot proceed because access is off should say which
            # access, and let the console offer to turn it on, rather than leave a
            # first-time user with "no tool can do this" and a dozen settings to
            # guess between. Evidence-based (tools that are off, calls refused for
            # it), never a match on the words of the request.
            hint = blocked_access_hint(self.executor.tool_definitions, history) if self.executor is not None else None
            if hint is not None:
                stated = str(decision.reason or "").strip()
                if not stated or stated == "operator_blocked":
                    reason = hint["summary"]
                elif hint["evidence"] == "denied":
                    reason = f"{stated} {hint['summary']}"
                metadata["blocked_access"] = {"evidence": hint["evidence"], "groups": hint["groups"]}
            metadata["last_worker_error"] = reason
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
            admitted_task, admitted = self._mark_parallel_in_flight(
                latest.id, decision.parallel_calls, step_id=step_id
            )
            if not admitted:
                return admitted_task
            entries = await self._run_parallel_calls(latest.id, decision.parallel_calls, step_id=step_id)
            history.extend(entries)
            # Each call promotes its result into task metadata. Reload after
            # the batch before adding history; writing the pre-batch `latest`
            # snapshot here erased changed_paths/workspace evidence produced
            # by every successful parallel call.
            recorded = self.repositories.tasks.get(latest.id) or latest
            uncertain = self._review_uncertain_batch(recorded, history, entries, step_id=step_id)
            if uncertain is not None:
                return uncertain
            metadata = dict(recorded.metadata)
            in_flight = metadata.get("operator_in_flight")
            if isinstance(in_flight, dict) and in_flight.get("step_id") == step_id:
                metadata.pop("operator_in_flight", None)
            return self.repositories.tasks.update_metadata(
                recorded.id,
                {**metadata, "operator_history": history},
            )

        if decision.action == OperatorAction.DELEGATE:
            admitted_task, admitted = self.repositories.tasks.admit_operator_call(latest.id, {
                "tool_name": "delegate", "capability": Capability.AGENT_CORE.value,
                "risk_level": RiskLevel.HIGH.value,
                "input": {"objective": decision.delegate_objective, "delegate_tools": decision.delegate_tools},
                "step_id": step_id, "dispatched_at": utc_now().isoformat(),
            })
            if not admitted:
                return admitted_task
            latest, entry = await self._run_delegate(admitted_task, decision, step_id=step_id)
            history.append(entry)
            recorded = self.repositories.tasks.get(latest.id) or latest
            marker = recorded.metadata.get("operator_in_flight")
            if not isinstance(marker, dict) or marker.get("step_id") != step_id:
                return recorded
            metadata = {k: v for k, v in recorded.metadata.items() if k != "operator_in_flight"}
            metadata["operator_history"] = history
            if entry.get("status") == "interrupted":
                metadata["interrupted_step"] = marker
                metadata["clarifying_question"] = (
                    "Delegated work was interrupted and may have made changes. Make sure its work has stopped, "
                    "check what took effect, then tell me to continue or redo the step."
                )
                return self.repositories.tasks.update_metadata(recorded.id, metadata, TaskStatus.CLARIFYING)
            return self.repositories.tasks.update_metadata(recorded.id, metadata)

        # CALL_TOOL
        requested_tool_name = decision.tool_name
        requested_tool_input = decision.tool_input
        # What happens to the call before it runs (a refusal, or the model's known habits corrected) is one ordered list, call_policy.py.
        vetted = call_policy.vet_call(latest, requested_tool_name, requested_tool_input, history, self.executor.tool_definitions)
        if vetted.refusal is not None:
            return self._refuse_call(latest, history, vetted, step_id)
        tool_name, tool_input = vetted.tool_name, vetted.tool_input
        decision = decision.model_copy(update={"tool_name": tool_name, "tool_input": tool_input})
        normalization = normalization_history_fields(
            requested_tool_name, requested_tool_input, tool_name, tool_input
        )
        earlier = succeeded_side_effect(history, tool_name, tool_input)
        read_again = False
        if earlier is None:
            earlier = redundant_read(history, tool_name, tool_input)
            read_again = earlier is not None
        if earlier is not None:
            # The call would create, change, move, delete or send something a second time. A model that
            # does not recognise its own earlier success repeats it (one live run created the same
            # schedule nine times, each through a separate approval). The earlier result is returned
            # instead; if the model still insists, repeated_no_progress_call stops the loop.
            # The same goes for a read whose answer cannot have changed: the model already has what it asked for.
            history.append({
                "tool_name": tool_name,
                "input": tool_input,
                "status": ALREADY_DONE_STATUS,
                # A repeated write is also told what is still missing, so a model that has not finished (a form
                # filled and never submitted) has something else to do.
                "output_summary": (
                    already_done_summary(*earlier, read=True) if read_again
                    else with_what_is_missing(latest, history, already_done_summary(*earlier))
                ),
                "step_id": step_id,
                **normalization,
            })
            if read_again and sum(
                1 for item in history if isinstance(item, dict) and item.get("status") == ALREADY_DONE_STATUS
                and item.get("tool_name") == tool_name and item.get("input") == tool_input
            ) >= 2:
                if not any(isinstance(item, dict) and item.get("tool_name") == CHECK_ENTRY_STUCK for item in history):
                    # The model has been shown the result twice and still asks for it. Let it answer in its own words
                    # (held to answering when the strict schema is on) before the raw result is given to the user: a
                    # question the file does not answer ("what is the total?") got the whole file back as its answer.
                    return self._offer_finish(
                        latest, history, called=f"{tool_name}:{tool_input.get('operation') or ''}".rstrip(":")
                    )
                finished = self._finish_with_read_result(latest, history, earlier[1])
                if finished is not None:
                    return finished
            repeated_call = repeated_no_progress_call(history)
            if repeated_call is not None:
                return self._offer_finish_or_block(
                    latest, history,
                    called=f"{tool_name}:{tool_input.get('operation') or ''}".rstrip(":"),
                    blocked_message=repeat_stop_message(latest, history, tool_name, tool_input),
                )
            return self.repositories.tasks.update_metadata(latest.id, {**latest.metadata, "operator_history": history})
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
            capability=tool_def.capability_for(tool_input),
            risk_level=effective_operator_risk(tool_def, decision.tool_input, decision.risk_level),
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
        admitted_task, admitted = self._mark_in_flight(latest.id, request, step_id=step_id)
        if not admitted:
            return admitted_task
        result = await self.executor.execute(request)

        if result.status == ToolResultStatus.NEEDS_APPROVAL:
            return self._await_operator_approval(
                latest,
                decision,
                history,
                approval_id=str(result.output.get("approval_id") or ""),
                step_id=step_id,
            )

        recorded = self._record_tool_result(latest.id, tool_name, result)
        uncertain = self._review_uncertain_result(recorded, history, request, result, step_id=step_id)
        if uncertain is not None:
            return uncertain
        # Keep the durable marker until a result/history or pending-external
        # transition is written. Removing it from this in-memory copy makes the
        # eventual metadata update clear it atomically with that transition.
        in_flight = recorded.metadata.get("operator_in_flight")
        if isinstance(in_flight, dict) and in_flight.get("step_id") == step_id:
            recorded = recorded.model_copy(update={
                "metadata": {k: v for k, v in recorded.metadata.items() if k != "operator_in_flight"}
            })
        if is_background_external_tool_result(tool_name, result):
            return self._await_operator_external(recorded, decision, result, history, step_id=step_id)
        if result.status != ToolResultStatus.SUCCEEDED:
            retry_outcome = self._operator_retry_or_ask(recorded, decision, result, history, step_id=step_id, request=request)
            if retry_outcome is not None:
                return retry_outcome
        output_text = tool_output_text(result) if result.status == ToolResultStatus.SUCCEEDED else None
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
            # "untrusted_external" when this call's ToolDefinition marked the
            # operation as returning content this machine does not control
            # (docs/THREAT_MODEL.md), stamped by ToolExecutor onto `result`
            # itself. operator.py's _format_history reads this straight off
            # the stored entry to fence that content in the very next
            # prompt - previously only reconstructed at trace-read time
            # (admin.py's _enrich_operator_history), invisible to the model.
            "content_trust": result.content_trust,
            # The folder a listing or search was taken from (an alias such as "desktop" is already resolved): its entries are
            # shown relative to it, and a later read of one of them needs it to find the file.
            "output_root": result.output.get("root") if tool_name == "filesystem.manage" else None,
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
        repeated_call = repeated_no_progress_call(history)
        if repeated_call is not None:
            tool_name, operation = repeated_call
            called = f"{tool_name}:{operation}" if operation else tool_name
            return self._offer_finish_or_block(
                recorded.model_copy(update={"metadata": {**recorded.metadata, "operator_history": history}}),
                history,
                called=called,
                blocked_message=repeat_block_message(history, called),
            )
        return self.repositories.tasks.update_metadata(recorded.id, {**recorded.metadata, "operator_history": history})
























































































































































































































