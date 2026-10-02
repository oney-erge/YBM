from __future__ import annotations

from datetime import timedelta

from agent_control import db_tools
from agent_control.storage.audit import AuditLogger
from agent_control.storage.database import Database
from agent_control.storage.repositories import Repositories
from agent_control.schemas import (
    ApprovalRequest,
    AuditEventType,
    Capability,
    ChannelType,
    LLMCallRecord,
    RiskLevel,
    TaskRecord,
    utc_now,
)


def _repositories(tmp_path, monkeypatch) -> tuple[Repositories, Database]:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "config.yaml").write_text("storage:\n  database_url: sqlite:///./test.db\n", encoding="utf-8")
    database = Database("sqlite:///./test.db")
    database.initialize()
    return Repositories.for_database(database), database


def _backdate_task(database: Database, task: TaskRecord, *, days_old: int) -> None:
    created_at = (utc_now() - timedelta(days=days_old)).isoformat()
    with database.connect() as connection:
        connection.execute("UPDATE tasks SET created_at = ? WHERE id = ?", (created_at, task.id))


def test_db_inspect_runs_without_error(tmp_path, monkeypatch, capsys) -> None:
    _repositories(tmp_path, monkeypatch)

    exit_code = db_tools.db_inspect()

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "tasks" in out


def test_db_clean_removes_old_tasks_keeps_recent(tmp_path, monkeypatch) -> None:
    repositories, database = _repositories(tmp_path, monkeypatch)
    old_task = repositories.tasks.create(objective="old task")
    recent_task = repositories.tasks.create(objective="recent task")
    _backdate_task(database, old_task, days_old=60)
    _backdate_task(database, recent_task, days_old=1)

    exit_code = db_tools.db_clean(days=30)

    assert exit_code == 0
    assert repositories.tasks.get(old_task.id) is None
    assert repositories.tasks.get(recent_task.id) is not None


def _backdate_audit_event(database: Database, event_id: str, *, days_old: int) -> None:
    created_at = (utc_now() - timedelta(days=days_old)).isoformat()
    with database.connect() as connection:
        connection.execute("UPDATE audit_events SET created_at = ? WHERE id = ?", (created_at, event_id))


def test_db_clean_removes_old_orphaned_audit_events_keeps_recent(tmp_path, monkeypatch) -> None:
    """audit_events.task_id is nullable (config changes, Telegram access
    decisions, pre-task messages) - the task-anchored cascade in db_clean
    never reaches these, so without an explicit pass they accumulate forever
    regardless of --days (docs/archive/HISTORY.md N5)."""
    repositories, database = _repositories(tmp_path, monkeypatch)
    audit = AuditLogger(repositories.audit)
    old_event = audit.append(AuditEventType.CONFIG_UPDATED, actor="admin", payload={"section": "llm"})
    recent_event = audit.append(AuditEventType.CONFIG_UPDATED, actor="admin", payload={"section": "telegram"})
    _backdate_audit_event(database, old_event.id, days_old=60)
    _backdate_audit_event(database, recent_event.id, days_old=1)

    exit_code = db_tools.db_clean(days=30)

    assert exit_code == 0
    remaining_ids = {event.id for event in repositories.audit.list_recent(100)}
    assert old_event.id not in remaining_ids
    assert recent_event.id in remaining_ids


def test_db_clean_does_not_delete_orphaned_audit_events_attached_to_a_kept_task(tmp_path, monkeypatch) -> None:
    repositories, database = _repositories(tmp_path, monkeypatch)
    audit = AuditLogger(repositories.audit)
    recent_task = repositories.tasks.create(objective="recent task")
    _backdate_task(database, recent_task, days_old=1)
    event = audit.append(AuditEventType.TASK_CREATED, actor="test", task_id=recent_task.id)
    _backdate_audit_event(database, event.id, days_old=60)

    exit_code = db_tools.db_clean(days=30)

    assert exit_code == 0
    assert repositories.tasks.get(recent_task.id) is not None
    remaining_ids = {e.id for e in repositories.audit.list_for_task(recent_task.id)}
    assert event.id in remaining_ids


def test_db_clean_rejects_invalid_days(tmp_path, monkeypatch) -> None:
    _repositories(tmp_path, monkeypatch)

    exit_code = db_tools.db_clean(days=0)

    assert exit_code == 1


def test_db_reset_requires_confirmation(tmp_path, monkeypatch) -> None:
    _repositories(tmp_path, monkeypatch)

    exit_code = db_tools.db_reset(yes=False)

    assert exit_code == 1


def test_db_reset_clears_all_tasks(tmp_path, monkeypatch) -> None:
    repositories, _database = _repositories(tmp_path, monkeypatch)
    repositories.tasks.create(objective="some task")

    exit_code = db_tools.db_reset(yes=True)

    assert exit_code == 0
    assert repositories.tasks.list_recent(10) == []


def _full_schema_fixture(repositories: Repositories) -> TaskRecord:
    """A task wired to every FK-referencing table, to catch deletion-order bugs.

    tasks.conversation_id -> conversations, approvals.task_id -> tasks: a
    delete order that clears `conversations` before `tasks`, or skips
    `approvals` in the per-task cascade, trips a real FOREIGN KEY violation
    here exactly like it did against the live database during manual testing.
    """
    conversation_id = repositories.conversations.get_or_create(ChannelType.TELEGRAM, "chat-1")
    task = repositories.tasks.create(objective="full schema task", conversation_id=conversation_id)
    repositories.approvals.create(ApprovalRequest(
        task_id=task.id,
        capability=Capability.FILESYSTEM_WRITE,
        risk_level=RiskLevel.HIGH,
        summary="test approval",
        expires_at=utc_now() + timedelta(hours=1),
    ))
    repositories.llm_calls.create(LLMCallRecord(task_id=task.id, source="operator", messages=[]))
    return task


def test_db_reset_handles_full_foreign_key_graph(tmp_path, monkeypatch) -> None:
    repositories, _database = _repositories(tmp_path, monkeypatch)
    _full_schema_fixture(repositories)

    exit_code = db_tools.db_reset(yes=True)

    assert exit_code == 0


def test_db_clean_handles_full_foreign_key_graph(tmp_path, monkeypatch) -> None:
    repositories, database = _repositories(tmp_path, monkeypatch)
    task = _full_schema_fixture(repositories)
    _backdate_task(database, task, days_old=60)

    exit_code = db_tools.db_clean(days=30)

    assert exit_code == 0
    assert repositories.tasks.get(task.id) is None


def test_retention_sweep_removes_old_tasks_keeps_recent_and_reports_counts(tmp_path, monkeypatch) -> None:
    """The print-free, programmatic sibling of db_clean() that
    scheduler.py's automatic sweep calls - same deletion behavior, but
    returns counts instead of printing, since it runs unattended."""
    repositories, database = _repositories(tmp_path, monkeypatch)
    old_task = repositories.tasks.create(objective="old task")
    recent_task = repositories.tasks.create(objective="recent task")
    _backdate_task(database, old_task, days_old=60)
    _backdate_task(database, recent_task, days_old=1)

    deleted_tasks, deleted_orphan_audit = db_tools.retention_sweep(database, days=30)

    assert deleted_tasks == 1
    assert deleted_orphan_audit == 0
    assert repositories.tasks.get(old_task.id) is None
    assert repositories.tasks.get(recent_task.id) is not None


def test_retention_sweep_removes_old_orphaned_audit_events(tmp_path, monkeypatch) -> None:
    repositories, database = _repositories(tmp_path, monkeypatch)
    audit = AuditLogger(repositories.audit)
    old_event = audit.append(AuditEventType.CONFIG_UPDATED, actor="admin", payload={"section": "llm"})
    _backdate_audit_event(database, old_event.id, days_old=60)

    deleted_tasks, deleted_orphan_audit = db_tools.retention_sweep(database, days=30)

    assert deleted_tasks == 0
    assert deleted_orphan_audit == 1
    remaining_ids = {event.id for event in repositories.audit.list_recent(100)}
    assert old_event.id not in remaining_ids


def test_retention_sweep_is_a_noop_when_nothing_is_old_enough(tmp_path, monkeypatch) -> None:
    repositories, database = _repositories(tmp_path, monkeypatch)
    task = repositories.tasks.create(objective="recent task")
    _backdate_task(database, task, days_old=1)

    deleted_tasks, deleted_orphan_audit = db_tools.retention_sweep(database, days=30)

    assert (deleted_tasks, deleted_orphan_audit) == (0, 0)
    assert repositories.tasks.get(task.id) is not None


def test_retention_sweep_handles_full_foreign_key_graph(tmp_path, monkeypatch) -> None:
    repositories, database = _repositories(tmp_path, monkeypatch)
    task = _full_schema_fixture(repositories)
    _backdate_task(database, task, days_old=60)

    deleted_tasks, _deleted_orphan_audit = db_tools.retention_sweep(database, days=30)

    assert deleted_tasks == 1
    assert repositories.tasks.get(task.id) is None
