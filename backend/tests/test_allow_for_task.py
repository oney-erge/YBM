"""One tap to allow the same kind of step for the rest of a task (a form sign-up asks three times)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from agent_control.approval_actions import (
    DEFAULT_GRANT_MAX_OPERATIONS, decide_with_children, grant_for_task,
)
from agent_control.channels.telegram_notifications import _approval_inline_keyboard
from agent_control.schemas import (
    ApprovalRequest, ApprovalStatus, Capability, RiskLevel, ToolCallRequest, utc_now,
)
from helpers import make_repos


def _call(task_id: str, **inp) -> ToolCallRequest:
    return ToolCallRequest(
        task_id=task_id, tool_name="browser.control", capability=Capability.BROWSER_CONTROL,
        risk_level=RiskLevel.CRITICAL, input={"operation": "click", "text": "Sign me up", **inp},
    )


def _single(repos, task_id: str) -> ApprovalRequest:
    request = _call(task_id)
    approval = ApprovalRequest(
        task_id=task_id, capability=request.capability, risk_level=request.risk_level, summary="x",
        action_payload=request.model_dump(mode="json"), expires_at=utc_now() + timedelta(minutes=15),
    )
    repos.approvals.create(approval)
    return approval


def _batch(repos, task_id: str, tools: list[str]) -> tuple[ApprovalRequest, list[ApprovalRequest]]:
    children = []
    for name in tools:
        request = _call(task_id).model_copy(update={"tool_name": name})
        child = ApprovalRequest(
            task_id=task_id, capability=request.capability, risk_level=request.risk_level, summary="child",
            action_payload=request.model_dump(mode="json"), expires_at=utc_now() + timedelta(minutes=15),
        )
        repos.approvals.create(child)
        children.append(child)
    parent = ApprovalRequest(
        task_id=task_id, capability=Capability.BROWSER_CONTROL, risk_level=RiskLevel.CRITICAL, summary="batch",
        action_payload={"tool_name": tools[0], "batch": [
            {"tool_name": n, "risk_level": "critical", "input": {}, "approval_id": c.id} for n, c in zip(tools, children)
        ]},
        expires_at=utc_now() + timedelta(minutes=15),
    )
    repos.approvals.create(parent)
    return parent, children


@pytest.fixture
def repos_audit(tmp_path):
    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("sign me up")
    return repos, audit, task


def test_the_keyboard_offers_allow_for_this_task():
    keyboard = _approval_inline_keyboard("appr_1")["inline_keyboard"]
    assert [b["text"] for b in keyboard[0]] == ["Approve", "Reject"]
    assert keyboard[1] == [{"text": "Allow for this task", "callback_data": "approval:appr_1:approve_task"}]


def test_a_grant_covers_the_same_kind_of_call_for_the_rest_of_the_task(repos_audit):
    repos, audit, task = repos_audit
    approval = _single(repos, task.id)
    assert decide_with_children(repos, approval, ApprovalStatus.APPROVED)
    grants = grant_for_task(repos, audit, approval, ttl_seconds=600, actor="telegram:user:1")
    assert len(grants) == 1
    found = repos.approval_grants.find_matching(task.id, "browser.control", Capability.BROWSER_CONTROL)
    assert found is not None and found.max_operations == DEFAULT_GRANT_MAX_OPERATIONS
    assert found.expires_at <= utc_now() + timedelta(seconds=601)


def test_a_grant_belongs_to_one_task_only(repos_audit):
    repos, audit, task = repos_audit
    approval = _single(repos, task.id)
    decide_with_children(repos, approval, ApprovalStatus.APPROVED)
    grant_for_task(repos, audit, approval, ttl_seconds=600, actor="x")
    other = repos.tasks.create("a different job")
    assert repos.approval_grants.find_matching(other.id, "browser.control", Capability.BROWSER_CONTROL) is None
    assert repos.approval_grants.find_matching(task.id, "browser.control", Capability.NETWORK_HTTP) is None


def test_a_batch_grants_each_tool_in_it_and_decides_every_child(repos_audit):
    repos, audit, task = repos_audit
    parent, children = _batch(repos, task.id, ["browser.control", "browser.open"])
    assert decide_with_children(repos, parent, ApprovalStatus.APPROVED)
    assert all(repos.approvals.get(c.id).status == ApprovalStatus.APPROVED for c in children)
    grants = grant_for_task(repos, audit, parent, ttl_seconds=600, actor="x")
    assert sorted(g.tool_name for g in grants) == ["browser.control", "browser.open"]


def test_rejecting_a_batch_rejects_its_children_too(repos_audit):
    repos, _, task = repos_audit
    parent, children = _batch(repos, task.id, ["browser.control", "browser.open"])
    assert decide_with_children(repos, parent, ApprovalStatus.REJECTED)
    assert all(repos.approvals.get(c.id).status == ApprovalStatus.REJECTED for c in children)


def test_deciding_an_already_decided_approval_does_nothing(repos_audit):
    repos, audit, task = repos_audit
    approval = _single(repos, task.id)
    assert decide_with_children(repos, approval, ApprovalStatus.APPROVED)
    assert not decide_with_children(repos, approval, ApprovalStatus.APPROVED)
