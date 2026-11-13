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

from pathlib import Path

import pytest

from agent_control import config_sync
from agent_control.llm import hardware


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
def isolate_localdeploy_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Do not probe a developer's running LocalDeploy during unrelated tests.

    Tests of the HTTP probe can explicitly provide their own addresses and
    mock transport; the production defaults remain unchanged.
    """
    monkeypatch.setattr(hardware, "_LOCALDEPLOY_BASE_URLS", ())
