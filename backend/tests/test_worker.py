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


def test_reconcile_orphaned_tasks_releases_claim_and_writes_audit_trail(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("claimed by a worker that then crashed")
    repos.tasks.update_status(task.id, TaskStatus.RUNNING)
    repos.tasks.claim_next([TaskStatus.RUNNING], worker_id="worker-that-died", claim_expiry_seconds=1200)

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


def test_reconcile_orphaned_tasks_returns_zero_when_nothing_to_do(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    repos.tasks.create("normal queued task")

    assert reconcile_orphaned_tasks(repos, audit) == 0


def test_tool_output_text_grounds_auditor_in_code_interpreter_file_content() -> None:
    """Regression guard (docs/archive/HISTORY.md Part 3 W1): before code.interpreter
    added file_previews to its terminal_output, `_tool_output_text()` - the
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
    from agent_control.orchestration.worker import _tool_output_text
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

    raw_output = _tool_output_text(result)

    assert "Content of answer.json:" in raw_output
    assert '"total": 16' in raw_output
