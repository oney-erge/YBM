"""Tests for Database's one-time legacy-location migration (docs/archive/HISTORY.md
P6): the default database_url moved from the repo root to .agent_control/,
matching where every other piece of runtime state already lives.

Every test here chdir's into an isolated tmp_path first - the migration
logic works off *relative* paths ("./agent_control.db",
".agent_control/agent_control.db") resolved against the process CWD, and
this repo's real root has an actual agent_control.db on disk. Without
isolation these tests would read from and could move a real file.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta

from agent_control.schemas import Capability, utc_now
from agent_control.storage import Repositories
from agent_control.storage.database import Database, _CURRENT_DEFAULT_PATH, _LEGACY_DEFAULT_PATH


def test_migrates_legacy_database_file_to_new_default_location(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    legacy = tmp_path / _LEGACY_DEFAULT_PATH
    legacy.write_bytes(b"sqlite file bytes")

    Database(f"sqlite:///{_CURRENT_DEFAULT_PATH}")

    target = tmp_path / _CURRENT_DEFAULT_PATH
    assert target.exists()
    assert target.read_bytes() == b"sqlite file bytes"
    assert not legacy.exists()


def test_does_not_migrate_when_no_legacy_file_exists(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    Database(f"sqlite:///{_CURRENT_DEFAULT_PATH}")

    assert not (tmp_path / _CURRENT_DEFAULT_PATH).exists()
    assert not (tmp_path / ".agent_control").exists()


def test_does_not_overwrite_an_existing_file_at_the_new_location(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    legacy = tmp_path / _LEGACY_DEFAULT_PATH
    legacy.write_bytes(b"old repo-root file")
    target = tmp_path / _CURRENT_DEFAULT_PATH
    target.parent.mkdir(parents=True)
    target.write_bytes(b"already-migrated file, do not touch")

    Database(f"sqlite:///{_CURRENT_DEFAULT_PATH}")

    assert target.read_bytes() == b"already-migrated file, do not touch"
    assert legacy.exists()
    assert legacy.read_bytes() == b"old repo-root file"


def test_does_not_migrate_a_caller_customized_database_url(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    legacy = tmp_path / _LEGACY_DEFAULT_PATH
    legacy.write_bytes(b"sqlite file bytes")

    Database(f"sqlite:///{tmp_path / 'custom' / 'wherever.db'}")

    assert legacy.exists()  # untouched - this database_url isn't the default


def test_migrated_database_is_immediately_usable(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    # A real sqlite file with its own table (not the placeholder bytes the
    # other tests use) - confirms the migrated file's actual content is what
    # gets opened at the new path afterward, not a fresh empty database.
    legacy_path = tmp_path / _LEGACY_DEFAULT_PATH
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(legacy_path)
    conn.execute("CREATE TABLE marker (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()

    database = Database(f"sqlite:///{_CURRENT_DEFAULT_PATH}")
    database.initialize()

    with database.connect() as connection:
        tables = {row["name"] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "marker" in tables  # the migrated file's own content survived, not a fresh empty db
    assert (tmp_path / _CURRENT_DEFAULT_PATH).exists()
    assert not legacy_path.exists()


_LEGACY_APPROVAL_GRANTS = """
CREATE TABLE approval_grants (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    capability TEXT NOT NULL,
    granted_from_approval_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    FOREIGN KEY(task_id) REFERENCES tasks(id)
)
"""


def test_upgrades_an_approval_grants_table_from_before_scope_and_revocation(tmp_path) -> None:
    """A database created before approval_grants gained scope/max_operations/
    operations_used/revoked kept the seven-column table, because CREATE TABLE IF
    NOT EXISTS never alters it. Every tool call then died in find_matching with
    "no such column: revoked"."""
    url = f"sqlite:///{tmp_path / 'old.db'}"
    database = Database(url)
    database.initialize()
    repositories = Repositories.for_database(database)
    task = repositories.tasks.create("read the notes")
    expires = (utc_now() + timedelta(minutes=10)).isoformat()
    with database.connect() as connection:
        connection.execute("DROP TABLE approval_grants")
        connection.execute(_LEGACY_APPROVAL_GRANTS)
        connection.execute(
            "INSERT INTO approval_grants VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("grant_old", task.id, "filesystem.manage", Capability.FILESYSTEM_READ.value, "seed", utc_now().isoformat(), expires),
        )

    database.initialize()

    with database.connect() as connection:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(approval_grants)")}
    assert {"scope", "max_operations", "operations_used", "revoked"} <= columns
    grant = Repositories.for_database(database).approval_grants.find_matching(
        task.id, "filesystem.manage", Capability.FILESYSTEM_READ
    )
    assert grant is not None and grant.id == "grant_old"
    assert grant.revoked is False and grant.operations_used == 0 and grant.max_operations is None

    database.initialize()  # idempotent: a second start must not fail on the now-present columns
