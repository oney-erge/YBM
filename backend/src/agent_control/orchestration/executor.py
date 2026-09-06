from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Protocol

from agent_control.egress import extract_egress_hosts, record_egress
from agent_control.policy import PolicyEngine, normalize_scope_target, scope_contains
from agent_control.schemas import (
    ApprovalGrant,
    AuditEventType,
    ErrorClass,
    RiskLevel,
    ToolCallRequest,
    ToolCallResult,
    ToolResultStatus,
)
from agent_control.storage.audit import AuditLogger
from agent_control.storage.redaction import redact_payload
from agent_control.storage.repositories import Repositories


class ToolAdapter(Protocol):
    async def execute(self, request: ToolCallRequest) -> ToolCallResult:
        ...


class StaticToolAdapter:
    def __init__(self, output: dict | None = None) -> None:
        self.output = output or {"ok": True}
        self.requests: list[ToolCallRequest] = []

    async def execute(self, request: ToolCallRequest) -> ToolCallResult:
        self.requests.append(request)
        return ToolCallResult(
            request_id=request.id,
            status=ToolResultStatus.SUCCEEDED,
            output=self.output,
        )


class ToolExecutor:
    def __init__(
        self,
        policy: PolicyEngine,
        repositories: Repositories,
        audit: AuditLogger,
        adapters: dict[str, ToolAdapter] | None = None,
        tool_definitions: Iterable[Any] | None = None,
    ) -> None:
        self.policy = policy
        self.repositories = repositories
        self.audit = audit
        self.adapters = adapters or {}
        if isinstance(tool_definitions, dict):
            self.tool_definitions = tool_definitions
        else:
            self.tool_definitions = {definition.name: definition for definition in (tool_definitions or [])}

    async def execute(self, request: ToolCallRequest, approval_id: str | None = None) -> ToolCallResult:
        request, validation_error = self._validated_request(request)
        # Persist only a sanitized copy. The live request still reaches the
        # adapter unchanged (a credential may be required to perform the
        # operation), but task traces and the admin API must never become a
        # second secret store.
        self.repositories.tool_invocations.create(_redacted_request(request))
        self.audit.append(
            AuditEventType.TOOL_REQUESTED,
            actor="orchestrator",
            task_id=request.task_id,
            payload=request.model_dump(mode="json"),
        )

        if validation_error:
            return self._complete(
                request,
                ToolCallResult(
                    request_id=request.id,
                    status=ToolResultStatus.FAILED,
                    error_class=ErrorClass.VALIDATION_FAILED,
                    error_message=validation_error,
                ),
            )

        approval = self.repositories.approvals.get(approval_id) if approval_id else None
        if approval_id and approval is None:
            return self._complete(
                request,
                ToolCallResult(
                    request_id=request.id,
                    status=ToolResultStatus.DENIED,
                    error_class=ErrorClass.POLICY_DENIED,
                    error_message="approval_not_found",
                ),
            )
        grant = (
            self.repositories.approval_grants.find_matching(request.task_id, request.tool_name, request.capability)
            if approval is None
            else None
        )
        has_grant = grant is not None and self._grant_covers(grant, request)
        decision = self.policy.evaluate(request, approval=approval, has_grant=has_grant)
        if decision.needs_approval:
            definition = self.tool_definitions.get(request.tool_name)
            reason = definition.approval_reason(request.input) if definition else None
            summary = f"Approve {request.tool_name} using {request.capability.value}: {reason}" if reason else None
            approval = self.policy.approval_request(request, summary=summary)
            self.repositories.approvals.create(approval)
            self.audit.append(
                AuditEventType.APPROVAL_REQUESTED,
                actor="policy",
                task_id=request.task_id,
                payload={"approval_id": approval.id, "tool_request_id": request.id},
            )
            return self._complete(
                request,
                ToolCallResult(
                    request_id=request.id,
                    status=ToolResultStatus.NEEDS_APPROVAL,
                    output={"approval_id": approval.id},
                )
            )

        if not decision.allowed:
            return self._complete(
                request,
                ToolCallResult(
                    request_id=request.id,
                    status=ToolResultStatus.DENIED,
                    error_class=ErrorClass.POLICY_DENIED,
                    error_message=decision.reason,
                )
            )

        adapter = self.adapters.get(request.tool_name)
        if adapter is None:
            return self._complete(
                request,
                ToolCallResult(
                    request_id=request.id,
                    status=ToolResultStatus.FAILED,
                    error_class=ErrorClass.ADAPTER_FAILED,
                    error_message=f"tool adapter not registered: {request.tool_name}",
                )
            )

        if approval is not None and not self.repositories.approvals.consume_approved(approval.id):
            return self._complete(
                request,
                ToolCallResult(
                    request_id=request.id,
                    status=ToolResultStatus.DENIED,
                    error_class=ErrorClass.POLICY_DENIED,
                    error_message="approval_not_consumable",
                ),
            )
        if approval is None and has_grant and grant is not None and not self.repositories.approval_grants.record_usage(grant.id):
            # The grant hit its cap (or was revoked/expired) between the
            # check above and here - deny rather than let the call through
            # uncounted. Same race-safety reasoning as consume_approved.
            return self._complete(
                request,
                ToolCallResult(
                    request_id=request.id,
                    status=ToolResultStatus.DENIED,
                    error_class=ErrorClass.POLICY_DENIED,
                    error_message="grant_not_consumable",
                ),
            )

        try:
            dispatch_request = request
            if "approved" in request.input:
                # Set unconditionally, not just when authorized: a model
                # supplying input.approved=true on its own must never reach
                # the adapter as-is, even for an operation whose adapter-level
                # gate isn't (or stops being) backed by an
                # approval_required_operations entry. This is the actual
                # bypass-proofing; the policy-level gate above is the primary
                # control.
                dispatch_request = request.model_copy(
                    update={"input": {**request.input, "approved": bool(approval is not None or has_grant)}}
                )
            result = await adapter.execute(dispatch_request)
            result, output_validation_error = self._validated_result(dispatch_request, result)
            if output_validation_error:
                return self._complete(
                    request,
                    ToolCallResult(
                        request_id=request.id,
                        status=ToolResultStatus.FAILED,
                        output={"invalid_output": result.output},
                        error_class=ErrorClass.VALIDATION_FAILED,
                        error_message=output_validation_error,
                    ),
                )
            return self._complete(request, result)
        except Exception as exc:
            return self._complete(
                request,
                ToolCallResult(
                    request_id=request.id,
                    status=ToolResultStatus.FAILED,
                    error_class=ErrorClass.ADAPTER_FAILED,
                    error_message=str(exc),
                )
            )

    @staticmethod
    def _grant_covers(grant: ApprovalGrant, request: ToolCallRequest) -> bool:
        """Whether a fetched ApprovalGrant actually covers this call.

        find_matching already filtered on task/tool/capability plus every
        unconditional criterion (not expired, not revoked, not at its
        operation cap - see its own docstring); scope is the one condition
        left, because it needs this specific request's scope_target, which
        the repository layer does not have. No scope on the grant means no
        narrowing beyond what find_matching already checked.
        """
        if grant.scope is None:
            return True
        if not request.scope_target:
            return False
        return scope_contains(normalize_scope_target(request.scope_target), grant.scope)

    def _validated_request(self, request: ToolCallRequest) -> tuple[ToolCallRequest, str | None]:
        definition = self.tool_definitions.get(request.tool_name)
        if definition is None:
            return request, None
        if request.capability != definition.capability:
            return (
                request,
                (
                    f"tool {request.tool_name} requires capability "
                    f"{definition.capability.value}, not {request.capability.value}"
                ),
            )
        try:
            validated_input = definition.validate_input(request.input)
        except ValueError as exc:
            return request, str(exc)
        required_risk = definition.required_risk(validated_input)
        if _RISK_ORDER[request.risk_level] < _RISK_ORDER[required_risk]:
            return (
                request,
                (
                    f"risk level {request.risk_level.value} understates "
                    f"{request.tool_name} operation "
                    f"{validated_input.get('operation') or definition.default_operation or '<default>'}; "
                    f"minimum is {required_risk.value}"
                ),
            )
        return (
            request.model_copy(
                update={
                    "input": validated_input,
                    "scope_target": request.scope_target or validated_input.get("scope_target"),
                    "timeout_seconds": int(validated_input.get("timeout_seconds") or request.timeout_seconds),
                    "requires_approval": (
                        request.requires_approval
                        or definition.requires_approval(validated_input)
                    ),
                }
            ),
            None,
        )

    def _validated_result(
        self,
        request: ToolCallRequest,
        result: ToolCallResult,
    ) -> tuple[ToolCallResult, str | None]:
        if result.status != ToolResultStatus.SUCCEEDED:
            return result, None
        definition = self.tool_definitions.get(request.tool_name)
        if definition is None:
            return result, None
        output = dict(result.output or {})
        if "operation" not in output and request.input.get("operation"):
            output["operation"] = request.input["operation"]
        try:
            validated_output = definition.validate_output(output)
        except ValueError as exc:
            return result, str(exc)
        return result.model_copy(update={"output": validated_output}), None

    def _record_egress_and_verify(self, request: ToolCallRequest, result: ToolCallResult) -> ToolCallResult:
        """Runs once, on the shared success path, for every tool call - the
        things docs/ROADMAP.md's "Proof" item and the untrusted-content
        boundary (docs/THREAT_MODEL.md) ask for, all driven by the
        ToolDefinition instead of a manual call site inside an adapter (see
        spec.py's operation_egress/verify/operation_content_trust field
        comments).
        """
        definition = self.tool_definitions.get(request.tool_name)
        if definition is None:
            return result
        operation = str(request.input.get("operation") or definition.default_operation or "")
        if operation in definition.operation_egress:
            for host in extract_egress_hosts(request.input, result.output or {}):
                record_egress(self.audit, request.task_id, host, request.tool_name)
        if definition.verify is not None:
            verification = definition.verify(request, result)
            if verification is not None:
                result = result.model_copy(update={"verification": verification})
        content_trust = definition.operation_content_trust.get(operation)
        if content_trust is not None:
            result = result.model_copy(update={"content_trust": content_trust})
        return result

    def _complete(self, request: ToolCallRequest, result: ToolCallResult) -> ToolCallResult:
        # This is the shared ingestion boundary for every adapter result. A
        # secret read from a local file otherwise flows into tool_invocations,
        # task metadata, the next LLM prompt, memory summaries, notifications,
        # and E2E dumps. Sanitize once here and return the same safe result to
        # the worker so all downstream consumers agree.
        if result.status == ToolResultStatus.SUCCEEDED:
            result = self._record_egress_and_verify(request, result)
        result = _redacted_result(result)
        self.repositories.tool_invocations.complete(result)
        self.audit.append(
            AuditEventType.TOOL_COMPLETED,
            actor="orchestrator",
            task_id=request.task_id,
            payload=result.model_dump(mode="json"),
        )
        return result


def _redacted_request(request: ToolCallRequest) -> ToolCallRequest:
    return ToolCallRequest.model_validate(redact_payload(request.model_dump(mode="python")))


def _redacted_result(result: ToolCallResult) -> ToolCallResult:
    return ToolCallResult.model_validate(redact_payload(result.model_dump(mode="python")))


_RISK_ORDER = {
    RiskLevel.LOW: 1,
    RiskLevel.MEDIUM: 2,
    RiskLevel.HIGH: 3,
    RiskLevel.CRITICAL: 4,
}
