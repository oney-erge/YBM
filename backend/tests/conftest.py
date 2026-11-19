"""Shared test isolation.

The suite must not read the developer's real `.env`. `ybm test` already tries
to arrange that by deleting AGENT_ADMIN_TOKEN from the environment before
invoking pytest, but that is only half the lookup: `read_env_value` is
`os.getenv(key) or read_env_file(env_path).get(key)`, so a token that is
absent from the environment is still found in the *file*.

The effect was 11 admin tests failing with 401 instead of their expected
status on any checkout where `ybm setup` had been run - which is every real
developer machine, since setup generates AGENT_ADMIN_TOKEN. The tests build
their own TestClient instances that deliberately send no admin token, so a
token appearing from the ambient .env turns "assert 404" into "assert 401".

Patching `read_env_file` (rather than ENV_FILE_PATH) is what actually works
here: both `read_env_value` and `ConfigManager` bind ENV_FILE_PATH as a
*default argument*, evaluated once at import, so reassigning the module
constant afterwards changes nothing. `read_env_file` is resolved from module
globals on every call, including from the many modules that imported
`read_env_value` by name.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
from pathlib import Path

import pytest

from agent_control import config_sync
from agent_control.llm import hardware


# Each pytest-xdist worker gets a temp folder of its own. The scenario scratch root (`<temp>/ybm_scenario_scratch`) is one fixed path per machine, named in
# the recorded prompts and replaced there by a placeholder, so two workers that wrote the same scratch name at once deleted each other's files. This must
# run before anything asks for the temp directory (agent_control.testing.scripted_llm reads it when it is imported).
_WORKER = os.environ.get("PYTEST_XDIST_WORKER")
_WORKER_TEMP = Path(tempfile.gettempdir()) / f"ybm_xdist_{_WORKER}" if _WORKER else None
if _WORKER_TEMP is not None:
    _WORKER_TEMP.mkdir(parents=True, exist_ok=True)
    for _name in ("TMPDIR", "TEMP", "TMP"):
        os.environ[_name] = str(_WORKER_TEMP)
    tempfile.tempdir = str(_WORKER_TEMP)

# The e2e fixtures folder (e2e/fixtures.py) is one folder under the repository that the runner's own tests delete and write again, and a live e2e run uses it
# too. Under pytest it is a folder of this process's own.
_E2E_FIXTURES = Path(tempfile.gettempdir()) / f"ybm_e2e_fixtures_{os.getpid()}"
os.environ["YBM_E2E_FIXTURE_ROOT"] = str(_E2E_FIXTURES)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    shutil.rmtree(_E2E_FIXTURES, ignore_errors=True)
    if _WORKER_TEMP is not None:
        shutil.rmtree(_WORKER_TEMP, ignore_errors=True)


@pytest.fixture(autouse=True)
def isolate_repo_dotenv(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the repository's own .env invisible to the suite.

    Scoped to that one path on purpose: tests that exercise env reading and
    writing against their own tmp_path files are untouched, and keep testing
    the real parser.
    """
    real_read_env_file = config_sync.read_env_file
    repo_env = config_sync.ENV_FILE_PATH.resolve()

    def read_env_file_without_repo_dotenv(
        env_path: Path = config_sync.ENV_FILE_PATH,
    ) -> dict[str, str]:
        try:
            if env_path.resolve() == repo_env:
                return {}
        except OSError:
            pass
        return real_read_env_file(env_path)

    monkeypatch.setattr(config_sync, "read_env_file", read_env_file_without_repo_dotenv)


@pytest.fixture(autouse=True)
def fast_throwaway_databases(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test databases are thrown away, so nothing waits for the disk.

    Every repository call opens its own connection and commits, and each commit flushes the file to disk. That is what a product database must do and it
    costs nothing in one test, but sixteen parallel workers on Windows queued behind the disk and ordinary 0.5 second tests took 25 seconds. The product
    code is untouched: only connections made while a test runs skip the flush.
    """
    real_connect = sqlite3.connect

    def connect(*args, **kwargs):
        connection = real_connect(*args, **kwargs)
        connection.execute("PRAGMA synchronous = OFF")
        connection.execute("PRAGMA journal_mode = MEMORY")
        return connection

    monkeypatch.setattr(sqlite3, "connect", connect)


@pytest.fixture(autouse=True)
def isolate_localdeploy_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Do not probe a developer's running LocalDeploy during unrelated tests.

    Tests of the HTTP probe can explicitly provide their own addresses and
    mock transport; the production defaults remain unchanged.
    """
    monkeypatch.setattr(hardware, "_LOCALDEPLOY_BASE_URLS", ())
