from __future__ import annotations

from contextlib import contextmanager
import logging
from pathlib import Path
import sqlite3
from collections.abc import Iterator

from agent_control.storage.migrations import SCHEMA_STATEMENTS, apply_additive_migrations

logger = logging.getLogger(__name__)

# Pre-P6 default location (repo root) and current default (nested under
# .agent_control/ with every other piece of runtime state - artifacts, the
# secret vault, workspaces). Only used to detect "did this install predate
# the P6 move" - never compared against a caller-customized database_url.
_LEGACY_DEFAULT_PATH = "./agent_control.db"
_CURRENT_DEFAULT_PATH = ".agent_control/agent_control.db"


def _migrate_legacy_database_file(current_path: str) -> None:
    """One-time move of a pre-P6 repo-root database into .agent_control/.

    Only acts when ``current_path`` is exactly today's default location, the
    legacy file exists, and nothing already sits at the new location -
    never touches a database_url a caller explicitly customized, and never
    overwrites an existing file at the destination (docs/archive/HISTORY.md P6)."""
    if Path(current_path) != Path(_CURRENT_DEFAULT_PATH):
        return
    legacy = Path(_LEGACY_DEFAULT_PATH)
    target = Path(current_path)
    if target.exists() or not legacy.exists():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    legacy.replace(target)
    logger.info("moved legacy database %s -> %s (now lives under .agent_control/)", legacy, target)


class Database:
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url
        self.path = self._sqlite_path(database_url)
        if self.path != ":memory:":
            _migrate_legacy_database_file(self.path)

    @staticmethod
    def _sqlite_path(database_url: str) -> str:
        if database_url == "sqlite:///:memory:":
            return ":memory:"
        if database_url.startswith("sqlite:///"):
            return database_url.removeprefix("sqlite:///")
        raise ValueError("only sqlite:/// database URLs are supported in the MVP")

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connect() as connection:
            for statement in SCHEMA_STATEMENTS:
                connection.execute(statement)
            apply_additive_migrations(connection)
