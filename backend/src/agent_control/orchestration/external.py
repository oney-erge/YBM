"""Work that finishes somewhere else or later: a coding agent, a usage limit, a call whose result is uncertain."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from agent_control.orchestration.claims import unsupported_write_claim
from agent_control.orchestration.finishing import completed_coding_agent_answer
from agent_control.orchestration.fulfillment import (
    validate_fulfillment,
)
from agent_control.orchestration.recovery import (
    interrupted_task_recovery,
    retry_error_text,
    uncertain_consequential_result,
)
from agent_control.orchestration.task_record import tool_output_text
from agent_control.recovery.usage_limits import describe_wait, next_attempt_at
from agent_control.schemas import (
    AuditEventType,
    ErrorClass,
    OperatorDecision,
    TaskRecord,
    TaskStatus,
    ToolCallRequest,
    ToolCallResult,
    ToolResultStatus,
    utc_now,
)


class ExternalStage:
    def _review_uncertain_result(
        self,
        task: TaskRecord,
        history: list[dict[str, Any]],
        request: ToolCallRequest,
        result: ToolCallResult,
        *,
        step_id: str,
    ) -> TaskRecord | None:
        """Do not let a model retry a consequential call with an unknown outcome."""
        if not uncertain_consequential_result(request, result):
            return None
        marker = task.metadata.get("operator_in_flight")
        if not isinstance(marker, dict) or marker.get("step_id") != step_id:
            return task
        history.append({
            "tool_name": request.tool_name, "input": request.input,
            "status": result.status.value, "output_summary": None,
            "error": retry_error_text(result, request), "request_id": result.request_id, "step_id": step_id,
        })
        metadata = {**task.metadata, "operator_history": history}
        metadata.pop("operator_pending_call", None)
        metadata.pop("pending_approval_preview", None)
        metadata, status = interrupted_task_recovery(task.model_copy(update={"metadata": metadata}))
        return self._transition_operator(task, metadata, status, "operator_uncertain_effect_review")

    def _review_uncertain_batch(
        self, task: TaskRecord, history: list[dict[str, Any]], entries: list[dict[str, Any]], *, step_id: str
    ) -> TaskRecord | None:
        if not any(entry.get("outcome_uncertain") for entry in entries):
            return None
        marker = task.metadata.get("operator_in_flight")
        if not isinstance(marker, dict) or marker.get("step_id") != step_id:
            return task
        metadata = {**task.metadata, "operator_history": history}
        metadata.pop("operator_pending_batch", None)
        metadata.pop("pending_approval_preview", None)
        metadata, status = interrupted_task_recovery(task.model_copy(update={"metadata": metadata}), force_review=True)
        return self._transition_operator(task, metadata, status, "operator_uncertain_batch_review")

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
        (docs/archive/HISTORY.md P3 §2.2); this is narrowly about pacing, not
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
            "error": retry_error_text(result, request),
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
        background rather than a finished result. The session completion
        handler uses the task repository to bind a matching session and save
        metadata["pending_tool_result"]. _resume_operator_pending_external
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
            # above (docs/archive/UI_UX_AUDIT.md Phase 14e).
            "operator_pending_call": {"tool_name": decision.tool_name, "tool_input": decision.tool_input, "step_id": step_id},
            "awaiting_external": {
                "tool_name": decision.tool_name,
                "session_id": str(output.get("session_id") or ""),
                "step_id": step_id,
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
        output_text = tool_output_text(result) if result.status == ToolResultStatus.SUCCEEDED else None
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
        answer = completed_coding_agent_answer(tool_name, result)
        if not answer:
            return None
        completed_metadata = {**metadata, "operator_history": history, "synthesized_answer": answer}
        candidate = task.model_copy(update={"metadata": completed_metadata})
        validation = validate_fulfillment(candidate)
        # An empty expectation set is not proof of completion for an open-ended
        # request; let the operator interpret the result in that case.
        if not validation.expected:
            return None
        gap = validation.first_gap or unsupported_write_claim(
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
