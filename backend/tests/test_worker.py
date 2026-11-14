from __future__ import annotations

import pytest

from agent_control.config import AppSettings, CapabilityPolicy
from agent_control.orchestration import StaticToolAdapter, TaskWorker, ToolExecutor, reconcile_orphaned_tasks
from agent_control.policy import PolicyEngine
from agent_control.schemas import (
    AuditEventType,
    Capability,
    OperatorAction,
    OperatorDecision,
    RiskLevel,
    TaskStatus,
)
from helpers import make_repos




class QueueOperator:
    """Fake OperatorLoopService - returns decisions in order, one per decide() call."""

    def __init__(self, decisions: list[OperatorDecision]) -> None:
        self.decisions = list(decisions)

    async def decide(self, objective, config_context, history, *, memory_context="", prefer_major=False):
        return self.decisions.pop(0)


class RecordingNotifier:
    def __init__(self) -> None:
        self.tasks = []

    async def notify(self, task) -> None:
        self.tasks.append(task)


@pytest.mark.asyncio
async def test_worker_claim_covers_task_specific_budget_before_processing(tmp_path, monkeypatch) -> None:
    from datetime import datetime

    from agent_control.schemas import utc_now

    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("long task", metadata={"task_budget_seconds": 3600})
    worker = TaskWorker(repos, audit, task_budget_seconds=60)

    async def observe_claim(task_id):
        with repos.tasks.database.connect() as connection:
            row = connection.execute(
                "SELECT claimed_by, claim_expires_at FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
        assert row["claimed_by"] == worker.worker_id
        assert (datetime.fromisoformat(row["claim_expires_at"]) - utc_now()).total_seconds() >= 3700
        return repos.tasks.update_status(task_id, TaskStatus.COMPLETED)

    monkeypatch.setattr(worker, "process_task", observe_claim)
    result = await worker.process_next()
    assert result.id == task.id
    assert result.status == TaskStatus.COMPLETED, result.metadata.get("last_worker_error")


@pytest.mark.asyncio
async def test_worker_does_not_process_after_losing_its_claim(tmp_path, monkeypatch) -> None:
    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("do not execute after ownership changes")
    worker = TaskWorker(repos, audit)
    extend = repos.tasks.extend_claim

    def replace_owner(task_id, **kwargs):
        with repos.tasks.database.connect() as connection:
            connection.execute("UPDATE tasks SET claimed_by = ? WHERE id = ?", ("other-worker", task_id))
        return extend(task_id, **kwargs)

    async def must_not_process(_task_id):
        pytest.fail("a worker that lost its claim must not begin processing")

    monkeypatch.setattr(repos.tasks, "extend_claim", replace_owner)
    monkeypatch.setattr(worker, "process_task", must_not_process)
    assert await worker.process_next() is None
    with repos.tasks.database.connect() as connection:
        row = connection.execute("SELECT claimed_by FROM tasks WHERE id = ?", (task.id,)).fetchone()
    assert row["claimed_by"] == "other-worker"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["new_owner", "expired", "missing_expiry"])
async def test_worker_cannot_publish_progress_after_ownership_changes(tmp_path, monkeypatch, failure) -> None:
    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("work whose owner changes")
    worker = TaskWorker(repos, audit)

    async def stale_process(task_id):
        owner = "new-worker" if failure == "new_owner" else worker.worker_id
        expiry = "2999-01-01T00:00:00+00:00" if failure == "new_owner" else (
            "2000-01-01T00:00:00+00:00" if failure == "expired" else None
        )
        with repos.tasks.database.connect() as connection:
            connection.execute(
                "UPDATE tasks SET claimed_by = ?, claim_expires_at = ?, status = ? WHERE id = ?",
                (owner, expiry, TaskStatus.RUNNING.value, task_id),
            )
        return repos.tasks.update_metadata(task_id, {"old_worker_result": "stale"}, TaskStatus.COMPLETED)

    monkeypatch.setattr(worker, "process_task", stale_process)
    result = await worker.process_next()
    assert result.status == TaskStatus.RUNNING
    assert "old_worker_result" not in repos.tasks.get(task.id).metadata
    with repos.tasks.database.connect() as connection:
        row = connection.execute("SELECT claimed_by FROM tasks WHERE id = ?", (task.id,)).fetchone()
    assert row["claimed_by"] == ("new-worker" if failure == "new_owner" else worker.worker_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [
    TaskStatus.RECEIVED, TaskStatus.INTERPRETING, TaskStatus.PLANNED, TaskStatus.RUNNING, TaskStatus.RETRYING,
])
@pytest.mark.parametrize("has_in_flight", [False, True])
async def test_missing_claim_expiry_requires_review_before_reclaim(tmp_path, monkeypatch, status, has_in_flight) -> None:
    from agent_control.storage.repositories import WorkerClaimLost, worker_claim_context

    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("work that may still be running")
    marker = {"tool_name": "filesystem.manage", "capability": "filesystem.write",
              "risk_level": "high", "input": {"operation": "write_text_file"}}
    repos.tasks.update_metadata(task.id, {"operator_in_flight": marker} if has_in_flight else {}, status)
    with repos.tasks.database.connect() as connection:
        connection.execute("UPDATE tasks SET claimed_by = ?, claim_expires_at = NULL WHERE id = ?",
                           ("old-worker", task.id))

    assert repos.tasks.claim_next([status], worker_id="old-worker") is None
    assert repos.tasks.claim_next([status], worker_id="new-worker") is None
    worker = TaskWorker(repos, audit)
    dispatched = []

    async def unexpected_dispatch(task_id):
        dispatched.append(task_id)
        return repos.tasks.get(task_id)

    monkeypatch.setattr(worker, "process_task", unexpected_dispatch)
    assert await worker.process_next() is None
    recovered = repos.tasks.get(task.id)
    assert recovered.status == TaskStatus.CLARIFYING
    assert recovered.metadata["claim_recovery"]["reason"] == "missing_expiry"
    assert "previous worker" in recovered.metadata["clarifying_question"]
    assert "check" in recovered.metadata["clarifying_question"].lower()
    if has_in_flight:
        assert recovered.metadata["interrupted_step"] == marker
    with repos.tasks.database.connect() as connection:
        row = connection.execute("SELECT claimed_by, claim_expires_at FROM tasks WHERE id = ?", (task.id,)).fetchone()
    assert row["claimed_by"] is None and row["claim_expires_at"] is None
    with worker_claim_context(repos.tasks.database, task.id, "old-worker"), pytest.raises(WorkerClaimLost):
        repos.tasks.update_metadata(task.id, {"stale_result": True}, TaskStatus.RUNNING)
    assert await worker.process_next() is None
    assert dispatched == []


@pytest.mark.parametrize("changed", ["renewed", "cancelled"])
def test_missing_expiry_recovery_rechecks_a_changed_claim_or_control(tmp_path, changed) -> None:
    repos, _ = make_repos(tmp_path)
    task = repos.tasks.create("check an uncertain worker")
    repos.tasks.update_status(task.id, TaskStatus.RUNNING)
    with repos.tasks.database.connect() as connection:
        connection.execute("UPDATE tasks SET claimed_by = ?, claim_expires_at = NULL WHERE id = ?",
                           ("old-worker", task.id))
    assert repos.tasks.recovery_candidates(after_rowid=0, expired_only=True)
    with repos.tasks.database.connect() as connection:
        if changed == "renewed":
            connection.execute("UPDATE tasks SET claim_expires_at = ? WHERE id = ?", ("2999-01-01T00:00:00+00:00", task.id))
        else:
            connection.execute("UPDATE tasks SET status = ? WHERE id = ?", (TaskStatus.CANCELLED.value, task.id))

    def must_not_recover(_task):
        pytest.fail("a renewed claim or cancelled task must not be recovered")

    assert repos.tasks.reconcile_claim(task.id, must_not_recover, expired_only=True,
                                       decide_missing_expiry=must_not_recover) is None
    assert repos.tasks.get(task.id).status == (TaskStatus.RUNNING if changed == "renewed" else TaskStatus.CANCELLED)


@pytest.mark.asyncio
async def test_missing_expiry_recovery_question_is_visible_to_agent_status(tmp_path) -> None:
    from agent_control.schemas import ToolCallRequest
    from agent_control.tools.task_status import TaskStatusAdapter

    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("recover an unknown claim")
    with repos.tasks.database.connect() as connection:
        connection.execute("UPDATE tasks SET claimed_by = ?, claim_expires_at = NULL WHERE id = ?",
                           ("old-worker", task.id))
    assert reconcile_orphaned_tasks(repos, audit) == 1
    result = await TaskStatusAdapter(repos).execute(ToolCallRequest(
        task_id="status", tool_name="task.status", capability=Capability.AGENT_CORE, input={"operation": "status"},
    ))
    row = next(item for item in result.output["task_status"]["recent_tasks"] if item["id"] == task.id)
    assert row["status"] == TaskStatus.CLARIFYING.value
    assert "previous worker" in row["clarifying_question"]
    assert "previous worker" in result.output["summary"]


@pytest.mark.asyncio
async def test_missing_expiry_review_can_resume_after_user_reply(tmp_path, monkeypatch) -> None:
    from agent_control.clarification import resume_clarifying_task
    from agent_control.schemas import utc_now

    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("recover an unknown claim")
    with repos.tasks.database.connect() as connection:
        connection.execute("UPDATE tasks SET claimed_by = ?, claim_expires_at = NULL WHERE id = ?",
                           ("old-worker", task.id))
    assert reconcile_orphaned_tasks(repos, audit) == 1
    awaiting = repos.tasks.get(task.id)
    result = resume_clarifying_task(
        repos, audit, awaiting, text="I checked the prior work; continue", actor="test",
        message_id="reply", received_at=utc_now(),
    )
    assert result.task.status == TaskStatus.RECEIVED
    assert result.task.metadata["claim_recovery"]["reason"] == "missing_expiry"
    seen = []
    worker = TaskWorker(repos, audit)

    async def complete_once(task_id):
        seen.append(task_id)
        return repos.tasks.update_metadata(task_id, repos.tasks.get(task_id).metadata, TaskStatus.COMPLETED)

    monkeypatch.setattr(worker, "process_task", complete_once)
    completed = await worker.process_next()
    assert completed.status == TaskStatus.COMPLETED
    assert seen == [task.id]


@pytest.mark.asyncio
async def test_worker_notifies_once_on_completion(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("Run safe step", metadata={"source_chat_id": "100"})
    settings = AppSettings(
        _env_file=None,
        capabilities={
            Capability.LLM_GENERATE: CapabilityPolicy(
                enabled=True,
                requires_approval=False,
                max_risk_level=RiskLevel.LOW,
            )
        },
    )
    notifier = RecordingNotifier()
    executor = ToolExecutor(
        PolicyEngine(settings, audit),
        repos,
        audit,
        adapters={"llm": StaticToolAdapter({"text": "done"})},
    )
    operator = QueueOperator([
        OperatorDecision(action=OperatorAction.CALL_TOOL, tool_name="llm", tool_input={}, risk_level=RiskLevel.LOW),
        OperatorDecision(action=OperatorAction.DONE, final_answer="done"),
    ])
    worker = TaskWorker(repos, audit, executor=executor, operator=operator, notification_sink=notifier)

    await worker.process_next()
    await worker.process_next()

    assert [item.status for item in notifier.tasks] == [TaskStatus.RUNNING, TaskStatus.COMPLETED]
    # RUNNING is deduped per completed step (docs/archive/HISTORY.md §3.3), so its key
    # carries the step count; COMPLETED is once-per-task and stays bare.
    assert repos.tasks.get(task.id).metadata["notified_statuses"] == [
        TaskStatus.COMPLETED.value,
        "running:steps:1",
    ]


def test_reconcile_preserves_an_unexpired_worker_claim(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("a worker is still performing an effect")
    repos.tasks.update_metadata(task.id, {
        "operator_in_flight": {
            "tool_name": "filesystem.manage",
            "capability": "filesystem.write",
            "risk_level": "high",
            "input": {"operation": "apply_manifest"},
        },
    }, TaskStatus.RUNNING)
    repos.tasks.claim_next([TaskStatus.RUNNING], worker_id="live-worker", claim_expiry_seconds=1200)

    assert reconcile_orphaned_tasks(repos, audit) == 0
    assert repos.tasks.get(task.id).status == TaskStatus.RUNNING
    assert "operator_in_flight" in repos.tasks.get(task.id).metadata
    with repos.tasks.database.connect() as connection:
        row = connection.execute("SELECT claimed_by FROM tasks WHERE id = ?", (task.id,)).fetchone()
    assert row["claimed_by"] == "live-worker"
    assert repos.tasks.claim_next([TaskStatus.RUNNING], worker_id="second-worker") is None


def test_reconcile_orphaned_tasks_fails_running_and_interpreting_tasks(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    running = repos.tasks.create("was mid-execution when the worker died")
    repos.tasks.update_status(running.id, TaskStatus.RUNNING)
    interpreting = repos.tasks.create("was mid-planning when the worker died")
    repos.tasks.update_status(interpreting.id, TaskStatus.INTERPRETING)
    # Untouched statuses should survive reconciliation unchanged.
    received = repos.tasks.create("freshly queued, no worker has touched it yet")
    clarifying = repos.tasks.create("waiting on the user, not a dead worker")
    repos.tasks.update_status(clarifying.id, TaskStatus.CLARIFYING)

    count = reconcile_orphaned_tasks(repos, audit)

    # Nothing was in flight for either, so both resume rather than being lost.
    assert count == 2
    assert repos.tasks.get(running.id).status == TaskStatus.RUNNING
    assert repos.tasks.get(interpreting.id).status == TaskStatus.RUNNING
    assert repos.tasks.get(received.id).status == TaskStatus.RECEIVED
    assert repos.tasks.get(clarifying.id).status == TaskStatus.CLARIFYING


def test_reconcile_asks_the_user_when_a_write_was_interrupted(tmp_path) -> None:
    """A write caught mid-dispatch may have half-happened. Retrying could do it
    twice and skipping could leave the job undone, so the user is asked."""
    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("move the files")
    repos.tasks.update_metadata(task.id, {
        "operator_in_flight": {
            "tool_name": "filesystem.manage",
            "capability": "filesystem.write",
            "risk_level": "high",
            "input": {"operation": "apply_manifest"},
        },
    }, TaskStatus.RUNNING)

    assert reconcile_orphaned_tasks(repos, audit) == 1

    reloaded = repos.tasks.get(task.id)
    assert reloaded.status == TaskStatus.CLARIFYING
    assert "filesystem.manage" in reloaded.metadata["clarifying_question"]
    assert "operator_in_flight" not in reloaded.metadata


def test_reconcile_asks_before_repeating_a_low_risk_message_send(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("deliver a message")
    repos.tasks.update_metadata(task.id, {
        "operator_in_flight": {
            "tool_name": "artifact.deliver", "capability": "telegram.send",
            "risk_level": "low", "input": {"operation": "deliver"},
        },
    }, TaskStatus.RUNNING)

    assert reconcile_orphaned_tasks(repos, audit) == 1
    assert repos.tasks.get(task.id).status == TaskStatus.CLARIFYING


def test_reconcile_resumes_when_only_a_read_was_interrupted(tmp_path) -> None:
    """Re-running a read is harmless, so it does not need a human."""
    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("read the file")
    repos.tasks.update_metadata(task.id, {
        "operator_in_flight": {
            "tool_name": "knowledge.search",
            "capability": "telegram.receive",
            "risk_level": "low",
            "input": {"operation": "search"},
        },
    }, TaskStatus.RUNNING)

    assert reconcile_orphaned_tasks(repos, audit) == 1

    reloaded = repos.tasks.get(task.id)
    assert reloaded.status == TaskStatus.RUNNING
    assert reloaded.metadata["resumed_after_interrupt"] == "knowledge.search"


def test_reconcile_treats_a_malformed_parallel_record_as_uncertain(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("run a batch")
    repos.tasks.update_metadata(task.id, {
        "operator_in_flight": {"tool_name": "call_tools_parallel", "parallel_calls": ["unreadable"]}
    }, TaskStatus.RUNNING)

    assert reconcile_orphaned_tasks(repos, audit) == 1
    assert repos.tasks.get(task.id).status == TaskStatus.CLARIFYING


@pytest.mark.parametrize("marker", [
    None, "damaged", {}, {"tool_name": "unknown"},
    {"tool_name": "x", "risk_level": "low", "capability": ""},
    {"tool_name": "x", "risk_level": "low", "capability": "filesystem.read"},
    {"tool_name": "x", "risk_level": [], "capability": "filesystem.read"},
    {"tool_name": "x", "risk_level": "low", "capability": []},
])
def test_reconcile_does_not_retry_an_unreadable_inflight_record(tmp_path, marker) -> None:
    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("recover with incomplete operation evidence")
    repos.tasks.update_metadata(task.id, {"operator_in_flight": marker}, TaskStatus.RUNNING)

    assert reconcile_orphaned_tasks(repos, audit) == 1

    recovered = repos.tasks.get(task.id)
    assert recovered.status == TaskStatus.CLARIFYING
    assert "cannot tell" in recovered.metadata["clarifying_question"]
    assert "interrupted_step" in recovered.metadata


def test_reconcile_orphaned_tasks_releases_claim_and_writes_audit_trail(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("claimed by a worker that then crashed")
    repos.tasks.update_status(task.id, TaskStatus.RUNNING)
    repos.tasks.claim_next([TaskStatus.RUNNING], worker_id="worker-that-died", claim_expiry_seconds=0)

    reconcile_orphaned_tasks(repos, audit)

    reloaded = repos.tasks.get(task.id)
    assert reloaded.status == TaskStatus.RUNNING
    events = repos.audit.list_for_task(task.id)
    # A resume is a state change, not an error - nothing went wrong, the task
    # just outlived the process that was running it.
    assert any(event.type == AuditEventType.TASK_STATE_CHANGED for event in events)
    assert not any(event.type == AuditEventType.ERROR for event in events)
    with repos.tasks.database.connect() as connection:
        row = connection.execute(
            "SELECT claimed_by, claim_expires_at FROM tasks WHERE id = ?", (task.id,)
        ).fetchone()
    assert row["claimed_by"] is None
    assert row["claim_expires_at"] is None


@pytest.mark.asyncio
async def test_expired_inflight_write_is_reconciled_before_reclaim(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("an interrupted external write")
    repos.tasks.update_metadata(task.id, {
        "operator_in_flight": {
            "tool_name": "filesystem.manage", "capability": "filesystem.write",
            "risk_level": "high", "input": {"operation": "apply_manifest"},
        },
    }, TaskStatus.RUNNING)
    repos.tasks.claim_next([TaskStatus.RUNNING], worker_id="old-worker", claim_expiry_seconds=0)
    assert repos.tasks.claim_next([TaskStatus.RUNNING], worker_id="new-worker") is None

    assert await TaskWorker(repos, audit).process_next() is None
    recovered = repos.tasks.get(task.id)
    assert recovered.status == TaskStatus.CLARIFYING
    assert recovered.metadata["interrupted_step"]["tool_name"] == "filesystem.manage"


def test_reconciliation_rechecks_a_claim_acquired_after_candidate_scan(tmp_path) -> None:
    repos, _ = make_repos(tmp_path)
    task = repos.tasks.create("queued recovery")
    repos.tasks.update_status(task.id, TaskStatus.RUNNING)
    assert repos.tasks.recovery_candidates(after_rowid=0, expired_only=False)
    assert repos.tasks.claim_next([TaskStatus.RUNNING], worker_id="live-worker") is not None

    def must_not_run(_task):
        pytest.fail("a newly claimed task must not be reconciled")

    assert repos.tasks.reconcile_claim(task.id, must_not_run, expired_only=False) is None


def test_reconcile_orphaned_tasks_returns_zero_when_nothing_to_do(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    repos.tasks.create("normal queued task")

    assert reconcile_orphaned_tasks(repos, audit) == 0


def test_tool_output_text_grounds_auditor_in_code_interpreter_file_content() -> None:
    """Regression guard (docs/archive/HISTORY.md Part 3 W1): before code.interpreter
    added file_previews to its terminal_output, `tool_output_text()` - the
    function that feeds `last_tool_output_text`, which is exactly what the
    Auditor's raw_output argument is built from (see worker.py's audit gate
    right before a `done` decision is accepted) - only ever saw file NAMES
    and stdout for a code.interpreter result, never what was actually inside
    a created file. A script that silently wrote the wrong answer, or an
    empty file, passed the audit because nothing downstream ever looked. This
    proves the wiring end-to-end without a live LLM call: build a
    ToolCallResult shaped exactly like code.interpreter's real output and
    confirm the file's content text makes it into what the Auditor sees.
    """
    from agent_control.orchestration.task_record import tool_output_text
    from agent_control.schemas import ToolCallResult, ToolResultStatus

    result = ToolCallResult(
        request_id="req_1",
        status=ToolResultStatus.SUCCEEDED,
        output={
            "operation": "run_python",
            "workspace_dir": "/tmp/workspace",
            "files_created": ["answer.json"],
            "file_previews": [{"path": "answer.json", "content": '{"total": 16, "count": 3}'}],
            "stdout": "wrote answer.json",
            "terminal_output": [
                {
                    "instance_id": "local-worker",
                    "terminal_id": "code-interpreter",
                    "content": (
                        "Code interpreter operation completed: run_python\n"
                        "Created files:\n- answer.json\n"
                        'Content of answer.json:\n{"total": 16, "count": 3}\n'
                        "Stdout:\nwrote answer.json"
                    ),
                    "is_final": True,
                    "exit_code": 0,
                    "source": "code_interpreter",
                }
            ],
        },
    )

    raw_output = tool_output_text(result)

    assert "Content of answer.json:" in raw_output
    assert '"total": 16' in raw_output
