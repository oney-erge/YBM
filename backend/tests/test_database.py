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
