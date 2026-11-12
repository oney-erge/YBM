"""Deciding an approval from a chat button, including "Allow for this task".

The dashboard has had "Allow for this task" for a while (`admin.py`, `ApprovalGrant`): approve one call and skip the question for
the same kind of call for the rest of that task. A web form sign-up asks three times (go to the page, fill the form, click the
button), which is three taps on a phone for one decision. This is the same grant, reachable from the Telegram button, and it never
widens what policy allows: the risk ceiling, scope and patterns still run before a grant is consulted, and a grant ends with its
task and after a fixed number of uses.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from agent_control.schemas import ApprovalGrant, ApprovalRequest, ApprovalStatus, AuditEventType, utc_now
from agent_control.storage.audit import AuditLogger
from agent_control.storage.repositories import Repositories

#: How many calls one "Allow for this task" covers. The same cap the dashboard applies: generous enough that a real job never
#: hits it, not "always allow".
DEFAULT_GRANT_MAX_OPERATIONS = 200


def _children(repositories: Repositories, approval: ApprovalRequest) -> list[ApprovalRequest]:
    payload = approval.action_payload if isinstance(approval.action_payload, dict) else {}
    found: list[ApprovalRequest] = []
    for entry in payload.get("batch") or []:
        child_id = str(entry.get("approval_id") or "") if isinstance(entry, dict) else ""
        child = repositories.approvals.get(child_id) if child_id else None
        if child is not None:
            found.append(child)
    return found


def decide_with_children(repositories: Repositories, approval: ApprovalRequest, status: ApprovalStatus) -> bool:
    """Decide an approval and, for a batch, every call it stands for (so none is left pending or approved by accident)."""
    if not repositories.approvals.decide_pending(approval.id, status):
        return False
    for child in _children(repositories, approval):
        repositories.approvals.decide_pending(child.id, status)
    return True


def _grant_targets(repositories: Repositories, approval: ApprovalRequest) -> list[tuple[str, ApprovalRequest]]:
    """One (tool name, approval) per distinct tool the decision covers."""
    children = _children(repositories, approval)
    sources = children or [approval]
    targets: dict[str, ApprovalRequest] = {}
    for source in sources:
        payload = source.action_payload if isinstance(source.action_payload, dict) else {}
        name = str(payload.get("tool_name") or "")
        if name:
            targets.setdefault(name, source)
    return list(targets.items())


def grant_for_task(
    repositories: Repositories, audit: AuditLogger, approval: ApprovalRequest, *, ttl_seconds: int, actor: str
) -> list[ApprovalGrant]:
    """Allow the kind of call just approved for the rest of its task. Returns the grants made (one per tool in a batch)."""
    grants: list[ApprovalGrant] = []
    for tool_name, source in _grant_targets(repositories, approval):
        payload: dict[str, Any] = source.action_payload if isinstance(source.action_payload, dict) else {}
        scope = str(payload.get("scope_target") or "").strip() or None
        grant = ApprovalGrant(
            task_id=approval.task_id,
            tool_name=tool_name,
            capability=source.capability,
            granted_from_approval_id=approval.id,
            expires_at=utc_now() + timedelta(seconds=ttl_seconds),
            scope=scope,
            max_operations=DEFAULT_GRANT_MAX_OPERATIONS,
        )
        repositories.approval_grants.create(grant)
        audit.append(
            AuditEventType.CONFIG_UPDATED,
            actor=actor,
            task_id=approval.task_id,
            payload={
                "section": "approval_grant", "action": "create", "grant_id": grant.id, "tool_name": tool_name,
                "capability": source.capability.value, "scope": scope, "max_operations": grant.max_operations,
                "source": "telegram_button",
            },
        )
        grants.append(grant)
    return grants
