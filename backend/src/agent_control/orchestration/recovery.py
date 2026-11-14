"""What happens to a task that was running when the worker stopped: which in-flight calls are ambiguous, what an interrupted task
is asked, and how orphaned tasks are reconciled on start."""
from __future__ import annotations

from typing import Any

from agent_control.schemas import (
    AuditEventType,
    Capability,
    ErrorClass,
    RiskLevel,
    TaskRecord,
    TaskStatus,
    ToolCallRequest,
    ToolCallResult,
    ToolResultStatus,
)
from agent_control.storage.audit import AuditLogger
from agent_control.storage.repositories import (
    Repositories,
)


def in_flight_is_ambiguous(in_flight: dict[str, Any]) -> bool:
    """Whether a call caught mid-dispatch might have changed something.

    A read that never returned can simply be run again. A write might have
    half-happened, and nobody can tell from here - not the worker, and not the
    user unless they are told. Risk informs authorization, but a low-risk
    mixed or outbound capability can still have consequential effects.
    """
    risk = in_flight.get("risk_level")
    if not isinstance(risk, str) or risk not in {level.value for level in RiskLevel}:
        return True
    if risk in {RiskLevel.HIGH.value, RiskLevel.CRITICAL.value}:
        return True
    if "parallel_calls" in in_flight:
        parallel_calls = in_flight["parallel_calls"]
        if not isinstance(parallel_calls, list) or not parallel_calls or not all(
            isinstance(call, dict) for call in parallel_calls
        ):
            return True
        return any(in_flight_is_ambiguous(call) for call in parallel_calls)
    capability = in_flight.get("capability")
    # Risk is an authorization classification, not proof of a read-only call.
    # Mixed and outbound capabilities are uncertain even at low risk.
    return not isinstance(capability, str) or capability not in {
        Capability.TELEGRAM_RECEIVE.value, Capability.LLM_GENERATE.value,
        Capability.STT_TRANSCRIBE.value, Capability.VSCODE_READ_STATE.value,
        Capability.FILESYSTEM_READ.value, Capability.DESKTOP_SCREENSHOT.value,
        Capability.GITHUB_READ.value,
    }


def _in_flight_record_incomplete(record: Any) -> bool:
    """A persisted reservation must retain its operation identity and input."""
    if not isinstance(record, dict) or not isinstance(record.get("tool_name"), str) or not record["tool_name"]:
        return True
    if "parallel_calls" in record:
        calls = record["parallel_calls"]
        return not isinstance(calls, list) or not calls or any(_in_flight_record_incomplete(call) for call in calls)
    return not isinstance(record.get("input"), dict)


# A clean rejection (the request never actually ran) versus an unknown
# outcome (the request may have reached the far end before the connection
# was lost). Only the second kind is worth warning the model about before
# it retries a write - a 429/quota response means nothing happened yet.
AMBIGUOUS_COMPLETION_ERRORS = {ErrorClass.TRANSIENT}


def uncertain_consequential_result(request: ToolCallRequest, result: ToolCallResult) -> bool:
    return (
        result.status != ToolResultStatus.SUCCEEDED
        and (result.status == ToolResultStatus.TIMEOUT or result.error_class in AMBIGUOUS_COMPLETION_ERRORS)
        and in_flight_is_ambiguous({"risk_level": request.risk_level.value, "capability": request.capability.value})
    )


def retry_error_text(result: ToolCallResult, request: ToolCallRequest | None) -> str | None:
    """The error text recorded in history for an automatically-retried call.

    Reuses in_flight_is_ambiguous's own "is this a risky write" judgment
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
    ambiguous_outcome = result.status == ToolResultStatus.TIMEOUT or result.error_class in AMBIGUOUS_COMPLETION_ERRORS
    if not ambiguous_outcome:
        return text
    risky = in_flight_is_ambiguous({"risk_level": request.risk_level.value, "capability": request.capability.value})
    if not risky:
        return text
    base = text or "the call did not return in time"
    return f"{base} - this may have already taken effect before the connection was lost; verify before repeating it."


def interrupted_task_recovery(task: TaskRecord, *, force_review: bool = False) -> tuple[dict[str, Any], TaskStatus]:
    has_in_flight = "operator_in_flight" in task.metadata
    recorded = task.metadata.get("operator_in_flight")
    in_flight = recorded if isinstance(recorded, dict) else {"unreadable_record": recorded}
    if has_in_flight and (force_review or _in_flight_record_incomplete(recorded) or in_flight_is_ambiguous(in_flight)):
        tool_name = in_flight.get("tool_name") or "an unidentified operation"
        question = (
            f"I was interrupted part-way through `{tool_name}` "
            f"and cannot tell whether it finished. First make sure the previous worker and its work have stopped; "
            f"check whether it took effect, "
            f"then tell me to continue or to redo that step."
        )
        return {
            **{k: v for k, v in task.metadata.items() if k != "operator_in_flight"},
            "clarifying_question": question,
            "interrupted_step": in_flight,
        }, TaskStatus.CLARIFYING
    metadata = {k: v for k, v in task.metadata.items() if k not in {"operator_in_flight", "last_worker_error"}}
    if has_in_flight:
        metadata["resumed_after_interrupt"] = in_flight.get("tool_name")
    return metadata, TaskStatus.RUNNING


_MISSING_CLAIM_EXPIRY_QUESTION = (
    "The previous worker's claim has no expiry, so I cannot tell whether its work has stopped. "
    "First make sure the previous worker and its work have stopped; check what took effect, "
    "then tell me how to continue."
)


def _missing_claim_expiry_recovery(task: TaskRecord) -> tuple[dict[str, Any], TaskStatus]:
    metadata = dict(task.metadata)
    if "operator_in_flight" in metadata:
        recorded = metadata.pop("operator_in_flight")
        metadata["interrupted_step"] = recorded if isinstance(recorded, dict) else {"unreadable_record": recorded}
    metadata["claim_recovery"] = {"reason": "missing_expiry", "previous_status": task.status.value}
    metadata["clarifying_question"] = _MISSING_CLAIM_EXPIRY_QUESTION
    return metadata, TaskStatus.CLARIFYING


def reconcile_orphaned_tasks(
    repositories: Repositories, audit: AuditLogger, *, expired_claims_only: bool = False
) -> int:
    """Reconcile unclaimed or expired active tasks from their saved evidence.

    Unexpired claims are left alone and eligibility is rechecked in the write
    transaction. Expiry alone does not establish that the old owner stopped.
    """

    total = 0
    cursor = 0
    while True:
        candidates = repositories.tasks.recovery_candidates(
            after_rowid=cursor, expired_only=expired_claims_only
        )
        if not candidates:
            break
        for cursor, task_id in candidates:
            result = repositories.tasks.reconcile_claim(
                task_id, interrupted_task_recovery, expired_only=expired_claims_only,
                decide_missing_expiry=_missing_claim_expiry_recovery,
            )
            if result is None:
                continue
            task, updated = result
            if (updated.status == TaskStatus.CLARIFYING
                    and updated.metadata.get("clarifying_question") == _MISSING_CLAIM_EXPIRY_QUESTION):
                reason = "worker claim expiry missing; asking the user before continuing"
            else:
                reason = (
                    "interrupted mid-write; asking the user before continuing"
                    if updated.status == TaskStatus.CLARIFYING else "resumed after an interrupted run"
                )
            audit.task_state_changed("worker", task.id, task.status, updated.status)
            audit.append(
                AuditEventType.ERROR if updated.status == TaskStatus.CLARIFYING else AuditEventType.TASK_STATE_CHANGED,
                actor="worker",
                task_id=task.id,
                payload={"error": reason, "status": updated.status.value},
            )
            total += 1
    return total
