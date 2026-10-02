"""Cancellation must clean up after itself (docs/archive/UI_UX_AUDIT.md Phase 8) -
previously only the task's own status flipped to cancelled; a pending
approval stayed pending forever, a task-scoped grant stayed valid until its
own unrelated expiry, and a stuck-at-needs_approval tool invocation stayed
stuck. None of that is cosmetic: a stale pending approval keeps showing up
in the console as if it still needs a decision, on a task that is already
dead.
"""

from __future__ import annotations

import json
from datetime import timedelta

from agent_control.config import AppSettings
from agent_control.orchestration.signals import (
    apply_task_signal,
    requeue_after_approval_decision,
    sweep_expired_approvals,
)
from agent_control.schemas import (
    ApprovalGrant,
    ApprovalRequest,
    ApprovalStatus,
    Capability,
    RiskLevel,
    TaskStatus,
    ToolCallRequest,
    ToolResultStatus,
    utc_now,
)
from helpers import make_repos


def _pending_approval(repositories, task_id: str) -> ApprovalRequest:
    return repositories.approvals.create(
        ApprovalRequest(
            task_id=task_id,
            capability=Capability.FILESYSTEM_WRITE,
            risk_level=RiskLevel.HIGH,
            summary="write a file",
            expires_at=utc_now() + timedelta(minutes=15),
        )
    )


def test_cancelling_a_task_rejects_its_pending_approval(tmp_path) -> None:
    repositories, audit = make_repos(tmp_path)
    task = repositories.tasks.create("write a report")
    approval = _pending_approval(repositories, task.id)

    apply_task_signal(repositories, audit, task.id, "cancel", "operator", {})

    updated = repositories.approvals.get(approval.id)
    assert updated is not None
    assert updated.status == ApprovalStatus.CANCELLED
    # And it must actually disappear from what the console calls "pending" -
    # the whole point is that it stops looking actionable.
    assert approval.id not in {a.id for a in repositories.approvals.list_pending()}


def test_cancelling_a_task_does_not_touch_another_tasks_pending_approval(tmp_path) -> None:
    repositories, audit = make_repos(tmp_path)
    cancelled_task = repositories.tasks.create("cancel me")
    other_task = repositories.tasks.create("leave me alone")
    _pending_approval(repositories, cancelled_task.id)
    other_approval = _pending_approval(repositories, other_task.id)

    apply_task_signal(repositories, audit, cancelled_task.id, "cancel", "operator", {})

    still_pending = repositories.approvals.get(other_approval.id)
    assert still_pending is not None
    assert still_pending.status == ApprovalStatus.PENDING


def test_cancelling_a_task_revokes_its_approval_grants(tmp_path) -> None:
    repositories, audit = make_repos(tmp_path)
    task = repositories.tasks.create("write a report")
    grant = repositories.approval_grants.create(
        ApprovalGrant(
            task_id=task.id,
            tool_name="filesystem.manage",
            capability=Capability.FILESYSTEM_WRITE,
            granted_from_approval_id="approval_x",
            expires_at=utc_now() + timedelta(minutes=30),
        )
    )

    apply_task_signal(repositories, audit, task.id, "cancel", "operator", {})

    assert repositories.approval_grants.find_matching(task.id, "filesystem.manage", Capability.FILESYSTEM_WRITE) is None
    # The row survives (audit history, not deleted) - only its validity window closes.
    remaining = repositories.approval_grants.list_for_task(task.id)
    assert len(remaining) == 1
    assert remaining[0].id == grant.id
    assert remaining[0].expires_at <= utc_now()


def test_cancelling_a_task_cancels_stuck_tool_invocations(tmp_path) -> None:
    repositories, audit = make_repos(tmp_path)
    task = repositories.tasks.create("write a report")
    request = ToolCallRequest(
        task_id=task.id, tool_name="filesystem.manage", capability=Capability.FILESYSTEM_WRITE,
        input={"operation": "write_text_file"},
    )
    repositories.tool_invocations.create(request, status=ToolResultStatus.NEEDS_APPROVAL)

    apply_task_signal(repositories, audit, task.id, "cancel", "operator", {})

    invocations = repositories.tool_invocations.list_for_task(task.id)
    assert len(invocations) == 1
    assert invocations[0]["status"] == "cancelled"
    assert invocations[0]["result"]["error_message"]


def test_cancelling_a_task_with_nothing_pending_is_a_clean_no_op(tmp_path) -> None:
    """No approval, no grant, no stuck invocation - cancel must not raise
    just because there was nothing to clean up."""
    repositories, audit = make_repos(tmp_path)
    task = repositories.tasks.create("nothing pending here")

    signal, old_status, new_status = apply_task_signal(repositories, audit, task.id, "cancel", "operator", {})

    assert new_status == TaskStatus.CANCELLED
    assert signal.signal == "cancel"


def test_cancelling_a_task_awaiting_a_coding_session_stops_the_process(tmp_path, monkeypatch) -> None:
    """AWAITING_EXTERNAL is the one status the worker doesn't busy-poll - it
    only resumes via the session watcher noticing completion. A cancel while
    a session is genuinely running must stop that process itself, not just
    leave it running unsupervised in the background.
    """
    repositories, audit = make_repos(tmp_path)
    session_root = tmp_path / "coding_sessions"
    session_root.mkdir()
    (session_root / "sess_abc.json").write_text(json.dumps({"session_id": "sess_abc", "pid": 999999}), encoding="utf-8")
    monkeypatch.setattr("agent_control.tools.coding_agent.stop_session_process", lambda _session: True)
    settings = AppSettings(_env_file=None, adapters={"coding_agent": {"session_root": str(session_root)}})
    task = repositories.tasks.create("run some coding session")
    repositories.tasks.update_metadata(
        task.id,
        {"awaiting_external": {"tool_name": "coding.agent", "session_id": "sess_abc"}},
        TaskStatus.AWAITING_EXTERNAL,
    )
    task = repositories.tasks.get(task.id)

    apply_task_signal(repositories, audit, task.id, "cancel", "operator", {}, settings=settings)

    events = repositories.audit.list_for_task(task.id)
    stop_events = [e for e in events if e.payload.get("action") == "cancellation_stop_external_session"]
    assert len(stop_events) == 1
    assert stop_events[0].payload["session_id"] == "sess_abc"
    assert stop_events[0].payload["stopped"] is True


def test_cancelling_a_task_with_no_settings_skips_the_session_stop_without_raising(tmp_path) -> None:
    """admin.py and telegram.py both pass settings; a hypothetical caller
    that doesn't must not crash - it just can't stop a live session."""
    repositories, audit = make_repos(tmp_path)
    task = repositories.tasks.create("run some coding session")
    repositories.tasks.update_metadata(
        task.id,
        {"awaiting_external": {"tool_name": "coding.agent", "session_id": "sess_abc"}},
        TaskStatus.AWAITING_EXTERNAL,
    )

    _signal, _old, new_status = apply_task_signal(repositories, audit, task.id, "cancel", "operator", {})

    assert new_status == TaskStatus.CANCELLED


def test_cancelling_an_already_completed_tool_invocation_leaves_it_alone(tmp_path) -> None:
    """Only needs_approval is "stuck" - a succeeded call must keep its real
    result, not get overwritten just because the task was later cancelled."""
    repositories, audit = make_repos(tmp_path)
    task = repositories.tasks.create("write a report")
    request = ToolCallRequest(
        task_id=task.id, tool_name="filesystem.manage", capability=Capability.FILESYSTEM_READ,
        input={"operation": "read_file"},
    )
    repositories.tool_invocations.create(request, status=ToolResultStatus.SUCCEEDED)

    apply_task_signal(repositories, audit, task.id, "cancel", "operator", {})

    invocations = repositories.tool_invocations.list_for_task(task.id)
    assert invocations[0]["status"] == "succeeded"


# ---- requeue_after_approval_decision (docs/archive/UI_UX_AUDIT.md Phase 8, second review) ----


def test_requeue_after_approval_decision_makes_the_task_claimable_again(tmp_path) -> None:
    """The other half of the worker-blocking fix: AWAITING_APPROVAL was
    removed from WORKABLE_STATUSES so claim_next stops re-selecting a task
    stuck waiting on a human, but something still has to notice a decision
    landing and put the task back where the worker's poll will find it."""
    repositories, audit = make_repos(tmp_path)
    task = repositories.tasks.create("write a report")
    repositories.tasks.update_metadata(task.id, task.metadata, TaskStatus.AWAITING_APPROVAL)

    requeue_after_approval_decision(repositories, task.id)

    assert repositories.tasks.get(task.id).status == TaskStatus.RUNNING


def test_requeue_after_approval_decision_is_a_noop_for_a_task_not_awaiting_approval(tmp_path) -> None:
    """Every caller (admin API, Telegram inline buttons, Telegram plain-text
    approve) invokes this after a decide_pending call succeeds - it must not
    blindly stamp RUNNING over a task that moved on for some other reason
    (cancelled, already resumed by a concurrent decision) in the meantime."""
    repositories, audit = make_repos(tmp_path)
    task = repositories.tasks.create("write a report")
    repositories.tasks.update_metadata(task.id, task.metadata, TaskStatus.CANCELLED)

    requeue_after_approval_decision(repositories, task.id)

    assert repositories.tasks.get(task.id).status == TaskStatus.CANCELLED


def test_requeue_after_approval_decision_is_a_noop_for_an_unknown_task(tmp_path) -> None:
    repositories, audit = make_repos(tmp_path)

    requeue_after_approval_decision(repositories, "task_does_not_exist")  # must not raise


def _awaiting_task_with_approval(repositories, *, expires_at) -> tuple[str, str]:
    """A task parked in AWAITING_APPROVAL the way the real worker leaves one:
    metadata carries operator_pending_call.approval_id pointing at a real,
    still-PENDING ApprovalRequest row."""
    task = repositories.tasks.create("write a report")
    approval = repositories.approvals.create(
        ApprovalRequest(
            task_id=task.id,
            capability=Capability.FILESYSTEM_WRITE,
            risk_level=RiskLevel.HIGH,
            summary="write a file",
            expires_at=expires_at,
        )
    )
    metadata = {
        **task.metadata,
        "operator_pending_call": {"tool_name": "filesystem.manage", "approval_id": approval.id, "tool_input": {}},
    }
    repositories.tasks.update_metadata(task.id, metadata, TaskStatus.AWAITING_APPROVAL)
    return task.id, approval.id


def test_sweep_expired_approvals_requeues_a_task_nobody_decided_on(tmp_path) -> None:
    """The timeout side of requeue_after_approval_decision's gap: nothing
    decided this approval before its deadline, and nothing else would ever
    notice for an operator who only uses Telegram/WhatsApp and never opens
    the admin console (whose list_pending() is the only other caller of
    approvals.expire_stale())."""
    repositories, audit = make_repos(tmp_path)
    task_id, approval_id = _awaiting_task_with_approval(repositories, expires_at=utc_now() - timedelta(minutes=1))

    requeued = sweep_expired_approvals(repositories)

    assert requeued == [task_id]
    assert repositories.tasks.get(task_id).status == TaskStatus.RUNNING
    assert repositories.approvals.get(approval_id).status == ApprovalStatus.EXPIRED


def test_sweep_expired_approvals_leaves_a_live_pending_approval_alone(tmp_path) -> None:
    repositories, audit = make_repos(tmp_path)
    task_id, approval_id = _awaiting_task_with_approval(repositories, expires_at=utc_now() + timedelta(minutes=30))

    requeued = sweep_expired_approvals(repositories)

    assert requeued == []
    assert repositories.tasks.get(task_id).status == TaskStatus.AWAITING_APPROVAL
    assert repositories.approvals.get(approval_id).status == ApprovalStatus.PENDING


def test_sweep_expired_approvals_requeues_a_task_whose_approval_was_decided_by_a_missed_race(tmp_path) -> None:
    """Belt-and-suspenders: if a decision path somehow updated the approval
    row without also calling requeue_after_approval_decision, this sweep
    still catches it on the very next tick instead of leaving the task
    stranded in AWAITING_APPROVAL indefinitely."""
    repositories, audit = make_repos(tmp_path)
    task_id, approval_id = _awaiting_task_with_approval(repositories, expires_at=utc_now() + timedelta(minutes=30))
    repositories.approvals.decide_pending(approval_id, ApprovalStatus.REJECTED)

    requeued = sweep_expired_approvals(repositories)

    assert requeued == [task_id]
    assert repositories.tasks.get(task_id).status == TaskStatus.RUNNING


def test_sweep_expired_approvals_ignores_a_task_with_no_pending_call_on_record(tmp_path) -> None:
    """A task could reach AWAITING_APPROVAL through some other path without
    operator_pending_call set - must skip it rather than raise or requeue
    something with nothing to resume."""
    repositories, audit = make_repos(tmp_path)
    task = repositories.tasks.create("write a report")
    repositories.tasks.update_metadata(task.id, task.metadata, TaskStatus.AWAITING_APPROVAL)

    requeued = sweep_expired_approvals(repositories)

    assert requeued == []
    assert repositories.tasks.get(task.id).status == TaskStatus.AWAITING_APPROVAL
