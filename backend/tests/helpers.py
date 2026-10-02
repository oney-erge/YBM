"""Shared test helpers.

Eight test files each carried a byte-identical private `_repos(tmp_path)`
before 2026-07-29 - flagged as friction in the original audit
(docs/archive/HISTORY.md Part 2 §2.5) and left open until now. One definition here
means a change to how a test database is built (a new repository, a
migration, different redaction settings) is a one-line edit rather than
eight, with no risk of the copies drifting apart.

A plain function rather than a pytest fixture deliberately: the call sites
already destructure a tuple inside the test body, so a function keeps them
unchanged, whereas a fixture would require editing every test's signature.
"""

from __future__ import annotations

from agent_control.storage import AuditLogger, Database, Repositories


def make_repos(tmp_path) -> tuple[Repositories, AuditLogger]:
    """A real, initialized SQLite database in a temp dir, plus its audit logger.

    Real storage rather than a mock on purpose: these tests exercise the
    worker/policy/executor stack against actual persistence.
    """
    database = Database(f"sqlite:///{tmp_path / 'agent.db'}")
    database.initialize()
    repositories = Repositories.for_database(database)
    return repositories, AuditLogger(repositories.audit)
