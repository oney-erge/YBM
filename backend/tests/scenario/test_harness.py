"""Unit coverage for run_task_to_completion's own auto_approve mechanism
(harness.py), not for any adapter or the Operator loop. auto_approve exists
to unstick tests whose subject is *execution correctness* of an operation
that is unconditionally approval-gated by design (ToolDefinition's
approval_required_operations - see code_interpreter.py, schedule_manage.py),
as opposed to tests whose subject *is* the approval gate itself. Every other
scenario test exercises auto_approve only indirectly, through a real
Operator loop and a recorded fixture; this file drives it directly with a
scripted fake worker so the mechanism's own two behaviors - approve-and-
continue vs. stay-terminal - are pinned independently of any model output.
"""

from __future__ import annotations

from datetime import timedelta

from agent_control.schemas import (
    ApprovalRequest,
    ApprovalStatus,
    Capability,
    RiskLevel,
    TaskRecord,
    TaskStatus,
    utc_now,
)
from agent_control.storage import AuditLogger, Database, Repositories
import pytest

from .harness import Scenario, assert_completed, isolated_settings, run_task_to_completion
from . import harness


def test_scenario_scratch_refuses_escape_and_cleans_its_own_directory(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(harness, "SCENARIO_SCRATCH_ROOT", tmp_path / "scenario-scratch")
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")
    with pytest.raises(ValueError, match="one directory name"):
        harness.scenario_scratch_dir("../outside")
    assert sentinel.read_text(encoding="utf-8") == "keep"

    scratch = harness.scenario_scratch_dir("fixture")
    (scratch / "old.txt").write_text("old", encoding="utf-8")
    assert harness.scenario_scratch_dir("fixture") == scratch
    assert list(scratch.iterdir()) == []


class _ApprovalThenCompleteWorker:
    """Fake TaskWorker.process_task: files a pending approval and returns
    AWAITING_APPROVAL on the first call, COMPLETED on every call after -
    the exact shape auto_approve is meant to unstick, without needing a real
    Operator loop or LLM fixture to produce it."""

    def __init__(self, repositories: Repositories) -> None:
        self.repositories = repositories
        self.calls = 0

    async def process_task(self, task_id: str) -> TaskRecord:
        self.calls += 1
        if self.calls == 1:
            self.repositories.approvals.create(
                ApprovalRequest(
                    task_id=task_id,
                    capability=Capability.TERMINAL_RUN,
                    risk_level=RiskLevel.HIGH,
                    summary="Approve run_python using code.interpreter",
                    expires_at=utc_now() + timedelta(minutes=5),
                )
            )
            return self.repositories.tasks.update_status(task_id, TaskStatus.AWAITING_APPROVAL)
        return self.repositories.tasks.update_status(task_id, TaskStatus.COMPLETED)


class _AlwaysAwaitingApprovalWorker:
    """Fake TaskWorker.process_task: files a pending approval and returns
    AWAITING_APPROVAL on every call, forever - stands in for a real gate
    that a human has deliberately not approved yet."""

    def __init__(self, repositories: Repositories) -> None:
        self.repositories = repositories
        self.calls = 0

    async def process_task(self, task_id: str) -> TaskRecord:
        self.calls += 1
        if self.calls == 1:
            self.repositories.approvals.create(
                ApprovalRequest(
                    task_id=task_id,
                    capability=Capability.TERMINAL_RUN,
                    risk_level=RiskLevel.HIGH,
                    summary="Approve run_python using code.interpreter",
                    expires_at=utc_now() + timedelta(minutes=5),
                )
            )
        return self.repositories.tasks.update_status(task_id, TaskStatus.AWAITING_APPROVAL)


def _bare_scenario(monkeypatch, tmp_path) -> Scenario:
    """A Scenario with only the pieces run_task_to_completion actually
    touches (repositories, worker) populated for real; settings/audit/
    provider/telegram are unused by the code path under test here. worker
    is filled in by each test after construction, since the fake workers
    above need scenario.repositories to already exist."""
    settings = isolated_settings(monkeypatch, tmp_path)
    database = Database(f"sqlite:///{tmp_path / 'agent.db'}")
    database.initialize()
    repositories = Repositories.for_database(database)
    audit = AuditLogger(repositories.audit)
    return Scenario(
        settings=settings, repositories=repositories, audit=audit, worker=None,
        provider=None, telegram=None,
    )


@pytest.mark.asyncio
async def test_auto_approve_true_approves_pending_and_continues_to_completion(tmp_path, monkeypatch) -> None:
    scenario = _bare_scenario(monkeypatch, tmp_path)
    worker = _ApprovalThenCompleteWorker(scenario.repositories)
    scenario.worker = worker

    task = await run_task_to_completion(scenario, "do a gated thing", auto_approve=True)

    assert_completed(task)
    # First call filed the approval and returned AWAITING_APPROVAL; the
    # mechanism approved it and ticked the worker again for COMPLETED.
    assert worker.calls == 2
    approvals = scenario.repositories.approvals.list_for_task(task.id)
    assert len(approvals) == 1
    assert approvals[0].status == ApprovalStatus.APPROVED


@pytest.mark.asyncio
async def test_auto_approve_false_leaves_awaiting_approval_terminal(tmp_path, monkeypatch) -> None:
    scenario = _bare_scenario(monkeypatch, tmp_path)
    worker = _AlwaysAwaitingApprovalWorker(scenario.repositories)
    scenario.worker = worker

    task = await run_task_to_completion(scenario, "do a gated thing")

    assert task.status == TaskStatus.AWAITING_APPROVAL
    # Default auto_approve=False must treat AWAITING_APPROVAL as terminal
    # immediately - no retry loop, no approval decision made on the human's
    # behalf.
    assert worker.calls == 1
    approvals = scenario.repositories.approvals.list_for_task(task.id)
    assert len(approvals) == 1
    assert approvals[0].status == ApprovalStatus.PENDING
