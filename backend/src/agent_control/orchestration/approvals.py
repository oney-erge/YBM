"""Asking the person: one approval for a call or a batch, and what happens when they answer."""
from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from agent_control.orchestration.call_rewrites import (
    _canonical_operator_tool_call,
)
from agent_control.orchestration.task_record import (
    effective_operator_risk,
    tool_output_text,
)
from agent_control.schemas import (
    ApprovalRequest,
    ApprovalStatus,
    AuditEventType,
    OperatorDecision,
    ParallelToolCall,
    RiskLevel,
    TaskRecord,
    TaskStatus,
    ToolCallRequest,
    ToolResultStatus,
    utc_now,
)


class ApprovalStage:
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
            request = ToolCallRequest(
                task_id=task_id, tool_name=tool_name, capability=tool_def.capability_for(tool_input),
                risk_level=effective_operator_risk(tool_def, tool_input, call.risk_level),
                input=tool_input, parent_step_id=step_id,
            )
            # Bind the call as it will run (schema defaults filled in), not the model's raw input.
            built.append((call, self.executor.normalized_request(request)))
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
                # link to the same step that requested it (docs/archive/UI_UX_AUDIT.md
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
        step_id = str(pending.get("step_id") or f"step_{uuid4().hex}")
        admitted_task, admitted = self._mark_parallel_in_flight(
            task.id, calls, step_id=step_id, allow_awaiting_approval=True
        )
        if not admitted:
            return admitted_task
        entries = await self._run_parallel_calls(
            task.id, calls, step_id=step_id, approvals_by_call=approvals_by_call
        )
        history.extend(entries)
        recorded = self.repositories.tasks.get(task.id) or task
        uncertain = self._review_uncertain_batch(recorded, history, entries, step_id=step_id)
        if uncertain is not None:
            return uncertain
        metadata = {k: v for k, v in recorded.metadata.items() if k not in {"operator_pending_batch", "pending_approval_preview"}}
        in_flight = metadata.get("operator_in_flight")
        if isinstance(in_flight, dict) and in_flight.get("step_id") == step_id:
            metadata.pop("operator_in_flight", None)
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
        step_id = str(pending_call.get("step_id") or f"step_{uuid4().hex}")
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
            capability=tool_def.capability_for(tool_input),
            risk_level=RiskLevel(pending_call["risk_level"]) if pending_call.get("risk_level") else RiskLevel.LOW,
            input=tool_input,
            parent_step_id=step_id,
        )
        admitted_task, admitted = self._mark_in_flight(
            task.id, request, step_id=step_id, allow_awaiting_approval=True
        )
        if not admitted:
            return admitted_task
        result = await self.executor.execute(request, approval_id=approval.id)
        recorded = self._record_tool_result(task.id, tool_name, result)
        uncertain = self._review_uncertain_result(recorded, history, request, result, step_id=step_id)
        if uncertain is not None:
            return uncertain
        output_text = tool_output_text(result) if result.status == ToolResultStatus.SUCCEEDED else None
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
        in_flight = final_metadata.get("operator_in_flight")
        if isinstance(in_flight, dict) and in_flight.get("step_id") == step_id:
            final_metadata.pop("operator_in_flight", None)
        return self.repositories.tasks.update_metadata(task.id, final_metadata, TaskStatus.RUNNING)
