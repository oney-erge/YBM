from __future__ import annotations

from datetime import timedelta
import json

from agent_control.config import AppSettings, CapabilityPolicy
from agent_control.schemas import (
    ApprovalRequest,
    AuditEventType,
    Capability,
    RiskLevel,
    StrictBaseModel,
    ToolCallRequest,
    utc_now,
)
from agent_control.storage.audit import AuditLogger


RISK_ORDER = {
    RiskLevel.LOW: 1,
    RiskLevel.MEDIUM: 2,
    RiskLevel.HIGH: 3,
    RiskLevel.CRITICAL: 4,
}


def normalize_scope_target(scope_target: str) -> str:
    return scope_target.replace("\\", "/").lower()


def scope_contains(normalized_target: str, scope: str) -> bool:
    """Prefix containment for a scope string, shared by CapabilityPolicy's
    configured scopes (_scope_allowed) and an ApprovalGrant's own `scope`
    (docs/ROADMAP.md "scoped temporary authority") - the same matching rule
    both places, not two copies that could drift apart.
    """
    raw_scope = scope.replace("\\", "/").lower()
    if raw_scope == "/":
        return normalized_target.startswith("/")
    normalized_scope = raw_scope.rstrip("/")
    if not normalized_scope:
        return False
    return normalized_target == normalized_scope or normalized_target.startswith(f"{normalized_scope}/")


class PolicyDecision(StrictBaseModel):
    allowed: bool
    needs_approval: bool = False
    reason: str
    capability: Capability
    risk_level: RiskLevel


class PolicyEngine:
    def __init__(self, settings: AppSettings, audit: AuditLogger | None = None) -> None:
        self.settings = settings
        self.audit = audit

    def evaluate(
        self,
        request: ToolCallRequest,
        approval: ApprovalRequest | None = None,
        has_grant: bool = False,
    ) -> PolicyDecision:
        """``has_grant`` (docs/UI_UX_AUDIT.md Phase 1's "Allow for this
        task"): the caller has already confirmed an ApprovalGrant matches
        (task_id, tool_name, capability) - see ApprovalGrantRepository.
        find_matching. It only skips the "ask a human" step below; every
        check above it (capability enabled, risk ceiling, scope, patterns)
        still runs unconditionally, so a grant can never let through a call
        policy would otherwise reject.
        """
        policy = self.settings.capabilities.get(request.capability)
        if policy is None or not policy.enabled:
            return self._decision(False, False, "capability_disabled", request)

        if RISK_ORDER[request.risk_level] > RISK_ORDER[policy.max_risk_level]:
            return self._decision(False, False, "risk_exceeds_capability_policy", request)

        if not self._scope_allowed(request, policy):
            return self._decision(False, False, "scope_not_allowed", request)

        if not self._patterns_allowed(request, policy):
            return self._decision(False, False, "pattern_denied", request)

        if approval is not None and not self._approval_matches(request, approval):
            return self._decision(False, False, "approval_invalid_or_mismatched", request)

        if approval is None and not has_grant and self._requires_approval(request, policy):
            return self._decision(False, True, "approval_required", request)

        if approval is None and has_grant:
            return self._decision(True, False, "granted_for_task", request)

        return self._decision(True, False, "allowed", request)

    def approval_request(self, request: ToolCallRequest, summary: str | None = None) -> ApprovalRequest:
        return ApprovalRequest(
            task_id=request.task_id,
            capability=request.capability,
            risk_level=request.risk_level,
            summary=summary or f"Approve {request.tool_name} using {request.capability.value}",
            action_payload=request.model_dump(mode="json"),
            expires_at=utc_now() + timedelta(seconds=self.settings.approval_policy.default_timeout_seconds),
        )

    def _requires_approval(self, request: ToolCallRequest, policy: CapabilityPolicy) -> bool:
        return (
            request.requires_approval
            or policy.requires_approval
            or RISK_ORDER[request.risk_level] >= RISK_ORDER[self.settings.approval_policy.require_approval_at_or_above]
        )

    @staticmethod
    def _approval_matches(request: ToolCallRequest, approval: ApprovalRequest) -> bool:
        if approval.status.value != "approved" or approval.expires_at <= utc_now():
            return False
        if approval.task_id != request.task_id:
            return False
        try:
            approved_request = ToolCallRequest.model_validate(approval.action_payload)
        except (TypeError, ValueError):
            return False
        return PolicyEngine._approval_binding(approved_request) == PolicyEngine._approval_binding(request)

    @staticmethod
    def _approval_binding(request: ToolCallRequest) -> dict:
        """Security-relevant fields bound by a one-shot approval.

        Request ids, timestamps, and idempotency keys are intentionally
        excluded because the worker reconstructs the call after a human
        decision. Tool identity, authority, and every validated parameter
        must remain byte-for-byte equivalent after model normalization.
        """
        return {
            "task_id": request.task_id,
            "tool_name": request.tool_name,
            "capability": request.capability.value,
            "risk_level": request.risk_level.value,
            "scope_target": request.scope_target,
            "input": request.input,
            "timeout_seconds": request.timeout_seconds,
            "requires_approval": request.requires_approval,
        }

    @staticmethod
    def _scope_allowed(request: ToolCallRequest, policy: CapabilityPolicy) -> bool:
        if not policy.scopes:
            return True
        if not request.scope_target:
            return False
        normalized = normalize_scope_target(request.scope_target)
        # Scope matching is a string prefix test, so "allowed/../../elsewhere"
        # would satisfy an "allowed" scope. The filesystem adapter resolves
        # paths before its own allowed-roots check, which is what actually
        # stops traversal today; refusing a traversal segment here keeps this
        # layer from silently depending on that. Fail closed rather than
        # resolving, because scope_target is not necessarily a filesystem path.
        if any(segment == ".." for segment in normalized.split("/")):
            return False
        return any(scope_contains(normalized, scope) for scope in policy.scopes)

    @staticmethod
    def _matches_scope(normalized_target: str, scope: str) -> bool:
        return scope_contains(normalized_target, scope)

    @staticmethod
    def _patterns_allowed(request: ToolCallRequest, policy: CapabilityPolicy) -> bool:
        haystack = json.dumps(
            {"scope_target": request.scope_target, "input": request.input},
            default=str,
            sort_keys=True,
        ).lower()
        if any(pattern.lower() in haystack for pattern in policy.deny_patterns):
            return False
        if policy.allow_patterns and not any(pattern.lower() in haystack for pattern in policy.allow_patterns):
            return False
        return True

    def _decision(
        self,
        allowed: bool,
        needs_approval: bool,
        reason: str,
        request: ToolCallRequest,
    ) -> PolicyDecision:
        decision = PolicyDecision(
            allowed=allowed,
            needs_approval=needs_approval,
            reason=reason,
            capability=request.capability,
            risk_level=request.risk_level,
        )
        if self.audit:
            self.audit.append(
                AuditEventType.POLICY_DECISION,
                actor="policy",
                task_id=request.task_id,
                payload={
                    "tool_request_id": request.id,
                    "allowed": allowed,
                    "needs_approval": needs_approval,
                    "reason": reason,
                    "capability": request.capability.value,
                    "risk_level": request.risk_level.value,
                },
            )
        return decision
