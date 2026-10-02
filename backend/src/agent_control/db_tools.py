"""`ybm db inspect|clean|reset` implementation.

Exists because the DB accumulates task/audit history with no retention and
no visibility short of opening it in a SQLite browser (see docs/archive/HISTORY.md P6).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import sqlite3

from agent_control.config import load_settings
from agent_control.storage.database import Database


# Deletion order matters: children before parents, respecting FOREIGN KEY = ON.
# task_signals/tool_invocations/artifacts/approvals/llm_calls reference tasks;
# tasks/messages/conversation_memory reference conversations. audit_events and
# schedules carry no FK. See backend/src/agent_control/storage/migrations.py.
# "plans"/"subtasks" are kept in the lists below only so a pre-existing
# database that still has those (now-schema-dropped, unused since P3) tables
# gets them swept by `ybm db reset`/`db clean` too, not left behind forever.
TABLES_CHILD_FIRST = (
    "task_signals", "subtasks", "tool_invocations", "plans", "artifacts", "approvals", "llm_calls",
    "tasks",
    "messages", "conversation_memory",
    "conversations",
    "audit_events", "schedules",
)


def _database() -> Database:
    settings = load_settings()
    database = Database(settings.storage.database_url)
    database.initialize()
    return database


def db_inspect() -> int:
    database = _database()
    print(f"database: {database.database_url}")
    print()
    with database.connect() as connection:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        table_names = [row["name"] for row in rows]
        width = max((len(name) for name in table_names), default=10)
        print("table" + " " * (width - 5) + "  rows")
        for name in table_names:
            count = connection.execute(f'SELECT COUNT(*) AS n FROM "{name}"').fetchone()["n"]
            print(f"{name.ljust(width)}  {count}")

        if "tasks" in table_names:
            print()
            print("task status breakdown:")
            for row in connection.execute(
                "SELECT status, COUNT(*) AS n FROM tasks GROUP BY status ORDER BY n DESC"
            ):
                print(f"  {row['status']:22} {row['n']}")

        if "schedules" in table_names:
            schedule_count = connection.execute("SELECT COUNT(*) AS n FROM schedules").fetchone()["n"]
            if schedule_count:
                print()
                print(f"schedules: {schedule_count} active (run `ybm db inspect` again after "
                      f"changes; stale schedules keep spawning tasks - see docs/archive/HISTORY.md P6)")
    return 0


def _clean_tables(connection: sqlite3.Connection, tables: set[str], cutoff: str) -> tuple[int, int]:
    """Deletes rows older than ``cutoff``. Shared by the manual `ybm db-clean`
    command and scheduler.py's automatic retention_sweep() so the two never
    drift on which tables to touch or in what order - only db_clean() prints;
    this returns counts so a caller can decide how to report them.

    Returns (deleted_tasks, deleted_orphan_audit_events).
    """
    deleted_tasks = 0
    # Tasks are the retention anchor: delete tasks older than the cutoff,
    # then cascade-delete their children by task_id so we never orphan
    # audit/tool-call history for a task that's kept.
    if "tasks" in tables:
        old_task_ids = [
            row["id"]
            for row in connection.execute("SELECT id FROM tasks WHERE created_at < ?", (cutoff,))
        ]
        if old_task_ids:
            placeholders = ",".join("?" for _ in old_task_ids)
            for table in ("task_signals", "subtasks", "tool_invocations", "plans", "artifacts", "approvals", "llm_calls", "audit_events"):
                if table in tables:
                    connection.execute(f'DELETE FROM "{table}" WHERE task_id IN ({placeholders})', old_task_ids)
            connection.execute(f"DELETE FROM tasks WHERE id IN ({placeholders})", old_task_ids)
            deleted_tasks = len(old_task_ids)

    # audit_events.task_id is nullable (config changes, Telegram access
    # decisions, and any message logged before a task existed carry no
    # task_id) - the task-anchored cascade above never reaches these, so
    # without this they accumulate forever regardless of retention settings
    # (docs/archive/HISTORY.md N5's retention gap).
    deleted_orphan_audit = 0
    if "audit_events" in tables:
        deleted_orphan_audit = connection.execute(
            "DELETE FROM audit_events WHERE task_id IS NULL AND created_at < ?", (cutoff,)
        ).rowcount

    return deleted_tasks, deleted_orphan_audit


def db_clean(days: int) -> int:
    if days < 1:
        print("FAIL: --days must be >= 1")
        return 1
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    database = _database()
    print(f"deleting rows older than {days} day(s) (before {cutoff})")
    with database.connect() as connection:
        tables = {
            row["name"]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        deleted_tasks, deleted_orphan_audit = _clean_tables(connection, tables, cutoff)
        if deleted_tasks:
            print(f"deleted {deleted_tasks} task(s) and their child records")
        if deleted_orphan_audit:
            print(f"deleted {deleted_orphan_audit} orphaned audit event(s) (no task_id)")
        if not deleted_tasks and not deleted_orphan_audit:
            print("nothing to clean")
    return 0


def retention_sweep(database: Database, days: int) -> tuple[int, int]:
    """Programmatic, print-free equivalent of `ybm db-clean --days N` for
    scheduler.py's automatic sweep (config.py's storage.retention_days).
    Same deletion logic as db_clean via _clean_tables - see that function's
    docstring. Returns (deleted_tasks, deleted_orphan_audit_events).
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with database.connect() as connection:
        tables = {
            row["name"]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        return _clean_tables(connection, tables, cutoff)


def db_reset(*, yes: bool) -> int:
    if not yes:
        print("FAIL: db reset deletes ALL tasks, plans, audit events, schedules, and conversation "
              "history. Re-run with --yes to confirm.")
        return 1
    database = _database()
    with database.connect() as connection:
        tables = {
            row["name"]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        for table in TABLES_CHILD_FIRST:
            if table in tables:
                n = connection.execute(f'SELECT COUNT(*) AS n FROM "{table}"').fetchone()["n"]
                connection.execute(f'DELETE FROM "{table}"')
                print(f"cleared {table} ({n} rows)")
        # VACUUM cannot run inside a transaction; the deletes above opened one implicitly.
        connection.commit()
        connection.execute("VACUUM")
    print("database reset")
    return 0
