"""A running worker follows config.yaml and .env instead of a startup snapshot.

Turning on file access, or choosing a model, in the console only edits those
files. The worker used to build its policy, tools and model clients once, so none
of it took effect until the whole stack was restarted. These pin that it now does.
"""

from __future__ import annotations

import os

import pytest
import yaml

from agent_control import cli
from agent_control.orchestration import TaskWorker
from agent_control.policy.access_modes import apply_access_modes_to_config
from agent_control.schemas import CapabilityAccessMode
from helpers import make_repos


def _write_config(path, modes: dict) -> None:
    config: dict = {}
    apply_access_modes_to_config(config, modes)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")


def _bump_mtime(path, seconds: int = 5) -> None:
    """Make a rewrite visible even on a filesystem with coarse timestamps."""
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + seconds * 1_000_000_000))


def _clean_env(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "AGENT_CAPABILITIES"):
        monkeypatch.delenv(name, raising=False)


# ---- the real thing: access switched on while the worker is running ------------------


@pytest.mark.asyncio
async def test_switching_file_access_on_reaches_a_worker_that_is_already_running(monkeypatch, tmp_path) -> None:
    _clean_env(monkeypatch, tmp_path)
    config_path = tmp_path / "config" / "config.yaml"
    _write_config(config_path, {"filesystem": CapabilityAccessMode.OFF})
    repos, audit = make_repos(tmp_path)
    reloader = cli.WorkerRuntimeReloader(lambda: cli._worker_kwargs(cli.load_settings(), repos, audit), audit)
    worker = TaskWorker(repos, audit, **reloader.kwargs, runtime_refresh=reloader)
    assert worker.executor.tool_definitions["filesystem.manage"].enabled is False

    # What the Access page does, in another process, while the worker idles.
    _write_config(config_path, {"filesystem": CapabilityAccessMode.WRITE_ACCESS})
    _bump_mtime(config_path)
    await worker.process_next()  # no task queued; the refresh runs before the claim

    assert worker.executor.tool_definitions["filesystem.manage"].enabled is True
    assert worker.runtime_version == 1


@pytest.mark.asyncio
async def test_switching_access_back_off_also_takes_effect(monkeypatch, tmp_path) -> None:
    """Revoking access must be as immediate as granting it."""
    _clean_env(monkeypatch, tmp_path)
    config_path = tmp_path / "config" / "config.yaml"
    _write_config(config_path, {"filesystem": CapabilityAccessMode.WRITE_ACCESS})
    repos, audit = make_repos(tmp_path)
    reloader = cli.WorkerRuntimeReloader(lambda: cli._worker_kwargs(cli.load_settings(), repos, audit), audit)
    worker = TaskWorker(repos, audit, **reloader.kwargs, runtime_refresh=reloader)
    assert worker.executor.tool_definitions["filesystem.manage"].enabled is True

    _write_config(config_path, {"filesystem": CapabilityAccessMode.OFF})
    _bump_mtime(config_path)
    await worker.process_next()

    assert worker.executor.tool_definitions["filesystem.manage"].enabled is False


@pytest.mark.asyncio
async def test_a_key_saved_to_dotenv_while_running_gives_the_worker_a_model(monkeypatch, tmp_path) -> None:
    _clean_env(monkeypatch, tmp_path)
    config_path = tmp_path / "config" / "config.yaml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        yaml.safe_dump({
            "llm": {
                "default_profile": "onboard",
                "profiles": {"onboard": {
                    "provider": "openai_compatible", "model": "gpt-4.1",
                    "base_url": "https://api.openai.com/v1", "api_key_env": "OPENAI_API_KEY",
                }},
            }
        }),
        encoding="utf-8",
    )
    repos, audit = make_repos(tmp_path)
    reloader = cli.WorkerRuntimeReloader(lambda: cli._worker_kwargs(cli.load_settings(), repos, audit), audit)
    worker = TaskWorker(repos, audit, **reloader.kwargs, runtime_refresh=reloader)
    # The profile names a key variable that is not set yet, so nothing can answer.
    before = worker.operator

    env_path = tmp_path / ".env"
    env_path.write_text("OPENAI_API_KEY=sk-saved-through-the-console\n", encoding="utf-8")
    await worker.process_next()

    assert worker.runtime_version == 1
    assert worker.operator is not before


# ---- the reloader itself ----------------------------------------------------------------


class _Audit:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def append(self, event_type, actor=None, task_id=None, payload=None) -> None:
        self.events.append({"actor": actor, "payload": payload})


class _Worker:
    def __init__(self) -> None:
        self.runtime_version = 0
        self.applied: list[dict] = []

    def reconfigure(self, **kwargs) -> None:
        self.applied.append(kwargs)


def _reloader(tmp_path, build):
    watched = (tmp_path / "config.yaml", tmp_path / ".env")
    return cli.WorkerRuntimeReloader(build, _Audit(), watched=watched), watched


def test_nothing_is_rebuilt_while_the_files_are_unchanged(tmp_path) -> None:
    builds: list[int] = []
    reloader, _ = _reloader(tmp_path, lambda: builds.append(1) or {"generation": len(builds)})
    worker = _Worker()

    reloader(worker)
    reloader(worker)

    assert builds == [1]  # only the initial build
    assert worker.applied == []


def test_a_change_rebuilds_once_and_every_worker_adopts_it(tmp_path) -> None:
    builds: list[int] = []
    reloader, (config, _) = _reloader(tmp_path, lambda: builds.append(1) or {"generation": len(builds)})
    first, second = _Worker(), _Worker()

    config.write_text("changed", encoding="utf-8")
    reloader(first)
    reloader(second)
    reloader(first)  # already up to date: no second application

    assert builds == [1, 1]  # initial + exactly one rebuild, shared by both workers
    assert first.applied == [{"generation": 2}]
    assert second.applied == [{"generation": 2}]
    assert first.runtime_version == second.runtime_version == 1


def test_creating_the_env_file_counts_as_a_change(tmp_path) -> None:
    reloader, (_, env) = _reloader(tmp_path, lambda: {"k": 1})
    worker = _Worker()

    env.write_text("OPENAI_API_KEY=x\n", encoding="utf-8")
    reloader(worker)

    assert worker.runtime_version == 1


def test_a_broken_edit_is_reported_and_the_previous_runtime_keeps_working(tmp_path) -> None:
    state = {"fail": False, "n": 0}

    def build():
        state["n"] += 1
        if state["fail"]:
            raise ValueError("invalid config: unknown field")
        return {"generation": state["n"]}

    reloader, (config, _) = _reloader(tmp_path, build)
    worker = _Worker()

    state["fail"] = True
    config.write_text("garbage: [", encoding="utf-8")
    reloader(worker)

    assert worker.applied == []  # nothing adopted
    assert reloader.kwargs == {"generation": 1}  # still the last good one
    assert reloader._audit.events[-1]["payload"]["error"] == "config_reload_failed"
    assert "unknown field" in reloader._audit.events[-1]["payload"]["reason"]

    # Fixing the file recovers without a restart.
    state["fail"] = False
    config.write_text("ok: true", encoding="utf-8")
    _bump_mtime(config)
    reloader(worker)

    assert worker.applied == [{"generation": 3}]


def test_a_failed_reload_is_not_retried_every_poll_until_the_file_changes_again(tmp_path) -> None:
    builds = {"n": 0}

    def build():
        builds["n"] += 1
        if builds["n"] > 1:
            raise ValueError("bad")
        return {}

    reloader, (config, _) = _reloader(tmp_path, build)
    worker = _Worker()
    config.write_text("x", encoding="utf-8")

    reloader(worker)
    reloader(worker)
    reloader(worker)

    assert builds["n"] == 2  # initial + one failed attempt, not one per poll


# ---- TaskWorker.reconfigure -----------------------------------------------------------------


def test_reconfigure_replaces_configuration_but_not_identity(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    worker = TaskWorker(repos, audit, operator_max_steps=5, task_budget_seconds=100)
    original_id = worker.worker_id

    worker.reconfigure(operator_max_steps=9, task_budget_seconds=250, audit_min_tool_calls=4)

    assert worker.operator_max_steps == 9
    assert worker.task_budget_seconds == 250
    assert worker.audit_min_tool_calls == 4
    assert worker.worker_id == original_id  # claims keep their owner
    assert worker.repositories is repos and worker.audit is audit


def test_reconfigure_keeps_the_refresh_hook_and_version(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    hook = lambda w: None  # noqa: E731
    worker = TaskWorker(repos, audit, runtime_refresh=hook)
    worker.runtime_version = 3

    worker.reconfigure(operator_max_steps=7)

    assert worker.runtime_refresh is hook
    assert worker.runtime_version == 3


def test_a_refresh_hook_that_raises_never_stops_the_worker(tmp_path) -> None:
    import asyncio

    repos, audit = make_repos(tmp_path)

    def broken(worker) -> None:
        raise RuntimeError("boom")

    worker = TaskWorker(repos, audit, runtime_refresh=broken)

    assert asyncio.run(worker.process_next()) is None  # nothing queued, and no exception
