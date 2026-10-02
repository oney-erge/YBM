"""A task that cannot proceed because access is off says which access.

On a fresh install every high-impact capability is off, so the first real request
("organize my Downloads") used to end as "No available tool or capability can
complete the request with the current configuration." That names nothing. These
pin the replacement: evidence-based wording, and a structured record the console
turns into a one-click "turn it on".
"""

from __future__ import annotations

import pytest

from agent_control.config import AppSettings, CapabilityPolicy
from agent_control.orchestration import StaticToolAdapter, TaskWorker, ToolExecutor
from agent_control.policy import PolicyEngine
from agent_control.policy.access_hints import blocked_access_hint, group_for_capability
from agent_control.schemas import (
    Capability,
    OperatorAction,
    OperatorDecision,
    RiskLevel,
    TaskStatus,
)
from agent_control.tools.registry import build_tool_registry
from helpers import make_repos


class _Defn:
    """Just enough of a ToolDefinition for the pure function."""

    def __init__(self, capability: Capability, *, enabled: bool = False, ops: dict | None = None) -> None:
        self.capability = capability
        self.enabled = enabled
        self._ops = ops or {}

    def capability_for(self, value) -> Capability:
        return self._ops.get(str((value or {}).get("operation") or ""), self.capability)


def _denied(tool: str, operation: str = "inspect_folder") -> dict:
    return {"tool_name": tool, "status": "denied", "error": "capability_disabled", "input": {"operation": operation}}


# ---- the pure function --------------------------------------------------------------


def test_a_refused_call_names_the_exact_access_that_is_off() -> None:
    defs = {"filesystem.manage": _Defn(Capability.FILESYSTEM_WRITE)}

    hint = blocked_access_hint(defs, [_denied("filesystem.manage")])

    assert hint is not None
    assert hint["evidence"] == "denied"
    assert [g["group"] for g in hint["groups"]] == ["filesystem"]
    assert hint["groups"][0]["label"] == "File system"
    assert hint["groups"][0]["recommended_mode"] == "write_access"  # ask before changing things
    assert "File system" in hint["summary"] and "Access" in hint["summary"]


def test_a_refused_read_is_attributed_to_the_read_capabilitys_group() -> None:
    defs = {"filesystem.manage": _Defn(Capability.FILESYSTEM_WRITE, ops={"inspect_folder": Capability.FILESYSTEM_READ})}

    hint = blocked_access_hint(defs, [_denied("filesystem.manage", "inspect_folder")])

    assert hint is not None and hint["groups"][0]["group"] == "filesystem"


def test_with_nothing_attempted_it_lists_what_is_off_and_says_probably() -> None:
    defs = {
        "filesystem.manage": _Defn(Capability.FILESYSTEM_WRITE),
        "browser.open": _Defn(Capability.BROWSER_OPEN),
        "code.interpreter": _Defn(Capability.TERMINAL_RUN),
        "task.status": _Defn(Capability.TELEGRAM_RECEIVE, enabled=True),
    }

    hint = blocked_access_hint(defs, [])

    assert hint is not None and hint["evidence"] == "off"
    # What a request to "do something on this computer" most plausibly needs, first.
    assert [g["group"] for g in hint["groups"]][:3] == ["filesystem", "browser", "terminal"]
    assert "Access is off for File system, Browser and Terminal" in hint["summary"]


def test_a_tool_that_is_on_is_not_blamed() -> None:
    defs = {"filesystem.manage": _Defn(Capability.FILESYSTEM_WRITE, enabled=True)}

    assert blocked_access_hint(defs, []) is None


def test_switches_that_are_not_about_doing_work_on_the_machine_are_never_blamed() -> None:
    defs = {
        "schedule.manage": _Defn(Capability.SCHEDULE_MANAGE),
        "vscode.terminal_command": _Defn(Capability.VSCODE_WRITE_FILES),
        "github.push": _Defn(Capability.GITHUB_PUSH),
    }

    assert blocked_access_hint(defs, []) is None


def test_denials_for_unrelated_reasons_are_not_treated_as_missing_access() -> None:
    defs = {"filesystem.manage": _Defn(Capability.FILESYSTEM_WRITE, enabled=True)}
    history = [{"tool_name": "filesystem.manage", "status": "denied", "error": "scope_not_allowed", "input": {}}]

    assert blocked_access_hint(defs, history) is None


def test_several_refused_areas_are_all_named() -> None:
    defs = {"filesystem.manage": _Defn(Capability.FILESYSTEM_WRITE), "code.interpreter": _Defn(Capability.TERMINAL_RUN)}

    hint = blocked_access_hint(defs, [_denied("filesystem.manage"), _denied("code.interpreter", "run")])

    assert hint is not None
    assert [g["group"] for g in hint["groups"]] == ["filesystem", "terminal"]
    assert "File system and Terminal" in hint["summary"]


def test_every_recommended_mode_is_a_real_option_of_its_group() -> None:
    from agent_control.policy.access_modes import ACCESS_GROUPS

    defs = {name: _Defn(cap) for name, cap in {
        "a": Capability.FILESYSTEM_WRITE, "b": Capability.BROWSER_OPEN, "c": Capability.TERMINAL_RUN,
        "d": Capability.DESKTOP_CONTROL,
    }.items()}

    hint = blocked_access_hint(defs, [])

    assert hint is not None
    for record in hint["groups"]:
        assert record["recommended_mode"] in {option.value for option in ACCESS_GROUPS[record["group"]].options}


def test_group_for_capability_covers_both_halves_of_a_group() -> None:
    assert group_for_capability(Capability.FILESYSTEM_READ).name == "filesystem"
    assert group_for_capability(Capability.FILESYSTEM_WRITE).name == "filesystem"
    assert group_for_capability(Capability.LLM_GENERATE) is None


# ---- through the worker ------------------------------------------------------------------


class _Operator:
    def __init__(self, decisions) -> None:
        self.decisions = list(decisions)

    async def decide(self, objective, config_context, history, *, memory_context="", prefer_major=False):
        return self.decisions.pop(0)


def _worker_on_a_fresh_install(tmp_path, decisions):
    settings = AppSettings(_env_file=None)  # every high-impact capability off
    repos, audit = make_repos(tmp_path)
    registry = build_tool_registry(settings, "http://127.0.0.1:8765")
    executor = ToolExecutor(
        PolicyEngine(settings, audit), repos, audit, adapters=registry.adapters,
        tool_definitions=registry.definition_index,
    )
    return repos, TaskWorker(repos, audit, executor=executor, operator=_Operator(decisions))


@pytest.mark.asyncio
async def test_giving_up_with_access_off_explains_which_access_instead_of_a_generic_message(tmp_path) -> None:
    repos, worker = _worker_on_a_fresh_install(tmp_path, [OperatorDecision(action=OperatorAction.BLOCKED)])
    task = repos.tasks.create("Organize my Downloads folder by file type")

    result = await worker.process_task(task.id)

    assert result.status == TaskStatus.BLOCKED
    message = result.metadata["last_worker_error"]
    assert "No available tool or capability" not in message
    assert "File system" in message and "Access" in message
    record = result.metadata["blocked_access"]
    assert record["evidence"] == "off"
    assert record["groups"][0]["group"] == "filesystem"


@pytest.mark.asyncio
async def test_a_refused_file_call_names_the_file_system_and_records_it(tmp_path) -> None:
    repos, worker = _worker_on_a_fresh_install(tmp_path, [
        OperatorDecision(action=OperatorAction.CALL_TOOL, tool_name="filesystem.manage",
                         tool_input={"operation": "inspect_folder", "root": "downloads"}, risk_level=RiskLevel.LOW),
        OperatorDecision(action=OperatorAction.BLOCKED),
    ])
    task = repos.tasks.create("Organize my Downloads folder by file type")

    running = await worker.process_task(task.id)
    result = await worker.process_task(running.id)

    assert result.status == TaskStatus.BLOCKED
    assert result.metadata["blocked_access"]["evidence"] == "denied"
    assert "YBM tried to use File system" in result.metadata["last_worker_error"]
    assert "capability_disabled" not in result.metadata["last_worker_error"]  # no internal jargon


@pytest.mark.asyncio
async def test_a_reason_the_operator_gave_is_kept_when_the_cause_is_not_access(tmp_path) -> None:
    repos, worker = _worker_on_a_fresh_install(tmp_path, [
        OperatorDecision(action=OperatorAction.BLOCKED, reason="The folder you named does not exist."),
    ])
    task = repos.tasks.create("Organize my Downloads folder")

    result = await worker.process_task(task.id)

    # Nothing was attempted, so access is only a probable cause: the structured
    # record is kept for the console, but the operator's own wording is not replaced.
    assert result.metadata["last_worker_error"] == "The folder you named does not exist."
    assert result.metadata["blocked_access"]["evidence"] == "off"


@pytest.mark.asyncio
async def test_when_everything_is_on_a_block_gets_no_access_hint(tmp_path) -> None:
    settings = AppSettings(
        _env_file=None,
        capabilities={
            cap: CapabilityPolicy(enabled=True, requires_approval=False, max_risk_level=RiskLevel.CRITICAL)
            for cap in Capability
        },
    )
    repos, audit = make_repos(tmp_path)
    executor = ToolExecutor(
        PolicyEngine(settings, audit), repos, audit,
        adapters={"x": StaticToolAdapter({})}, tool_definitions={},
    )
    worker = TaskWorker(repos, audit, executor=executor, operator=_Operator([OperatorDecision(action=OperatorAction.BLOCKED)]))
    task = repos.tasks.create("impossible")

    result = await worker.process_task(task.id)

    assert "blocked_access" not in result.metadata
    assert "No available tool or capability" in result.metadata["last_worker_error"]
