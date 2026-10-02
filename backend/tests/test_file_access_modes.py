"""File access modes do what the Access page says they do.

Two bugs made the product's headline task impossible by following its own
instructions:

* Choosing "File system: Write with approval" did not make `filesystem.manage`
  available, because the tool also required an unrelated desktop-control flag
  (`adapters.computer_use.enabled`) that mode never sets. "Organize my Downloads"
  stayed blocked with the access switched on.
* "Read-only" enabled `filesystem.read`, a capability no tool used, so the agent
  got no file tool at all.

These tests drive the same code path the Access page does
(`apply_access_modes_to_config`) and then check what the agent can actually do.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_control.config import AppSettings, CapabilityPolicy
from agent_control.orchestration.executor import StaticToolAdapter, ToolExecutor
from agent_control.policy import PolicyEngine
from agent_control.policy.access_modes import apply_access_modes_to_config
from agent_control.schemas import (
    Capability,
    CapabilityAccessMode,
    ErrorClass,
    RiskLevel,
    ToolCallRequest,
    ToolResultStatus,
)
from agent_control.tools.filesystem_manage import READ_OPERATIONS
from agent_control.tools.registry import build_tool_registry
from helpers import make_repos


def _settings_for(mode: CapabilityAccessMode, **extra) -> AppSettings:
    config: dict = {}
    apply_access_modes_to_config(config, {"filesystem": mode})
    return AppSettings(_env_file=None, **config, **extra)


def _tools(settings: AppSettings) -> dict:
    registry = build_tool_registry(settings, "http://127.0.0.1:8765")
    return {definition.name: definition for definition in registry.definitions}, registry


def _call(operation: str, tool: str = "filesystem.manage", capability: Capability | None = None, **fields) -> ToolCallRequest:
    definition_capability = {
        "filesystem.manage": Capability.FILESYSTEM_WRITE,
        "document.manage": Capability.FILESYSTEM_WRITE,
    }[tool]
    return ToolCallRequest(
        task_id="task_1",
        tool_name=tool,
        capability=capability or definition_capability,
        risk_level=RiskLevel.LOW,
        input={"operation": operation, **fields},
    )


# ---- the original bug: turning file access on must make the tool available -----


@pytest.mark.parametrize(
    "mode",
    [CapabilityAccessMode.READ_ONLY, CapabilityAccessMode.WRITE_ACCESS, CapabilityAccessMode.FULL_ACCESS],
)
def test_switching_file_access_on_makes_the_file_tools_available(mode) -> None:
    settings = _settings_for(mode)

    definitions, registry = _tools(settings)

    assert definitions["filesystem.manage"].enabled is True
    assert definitions["document.manage"].enabled is True
    assert "filesystem.manage" in registry.adapters
    # ... without anyone touching the unrelated desktop-control flag.
    assert settings.adapters.computer_use.enabled is False


def test_switching_file_access_off_removes_the_file_tools() -> None:
    settings = _settings_for(CapabilityAccessMode.OFF)

    definitions, registry = _tools(settings)

    assert definitions["filesystem.manage"].enabled is False
    assert "filesystem.manage" not in registry.adapters


def test_a_fresh_default_config_has_no_file_tool() -> None:
    definitions, _ = _tools(AppSettings(_env_file=None))

    assert definitions["filesystem.manage"].enabled is False


# ---- Read-only means look, never touch --------------------------------------------


def test_read_operations_run_under_the_read_capability_and_writes_under_write() -> None:
    definition = _tools(_settings_for(CapabilityAccessMode.READ_ONLY))[0]["filesystem.manage"]

    for operation in READ_OPERATIONS:
        assert definition.capability_for({"operation": operation}) is Capability.FILESYSTEM_READ, operation
    for operation in ("write_text_file", "apply_manifest", "rename_plan", "open_file"):
        assert definition.capability_for({"operation": operation}) is Capability.FILESYSTEM_WRITE, operation
    # No operation given: the tool's default operation (a read) decides.
    assert definition.capability_for({}) is Capability.FILESYSTEM_READ


def test_read_only_mode_allows_reading_without_approval_and_refuses_writing() -> None:
    engine = PolicyEngine(_settings_for(CapabilityAccessMode.READ_ONLY))

    read = engine.evaluate(_call("inspect_folder", capability=Capability.FILESYSTEM_READ, root="C:/x"))
    write = engine.evaluate(
        _call("apply_manifest", capability=Capability.FILESYSTEM_WRITE).model_copy(update={"risk_level": RiskLevel.HIGH})
    )

    assert read.allowed is True and read.needs_approval is False
    assert write.allowed is False and write.reason == "capability_disabled"


def test_write_mode_reads_freely_but_asks_before_changing_anything() -> None:
    engine = PolicyEngine(_settings_for(CapabilityAccessMode.WRITE_ACCESS))

    read = engine.evaluate(_call("search", capability=Capability.FILESYSTEM_READ, root="desktop", query="x"))
    write = engine.evaluate(
        _call("apply_manifest", capability=Capability.FILESYSTEM_WRITE).model_copy(update={"risk_level": RiskLevel.HIGH})
    )

    assert read.allowed is True and read.needs_approval is False
    assert write.needs_approval is True and write.reason == "approval_required"


def test_document_reading_is_available_read_only_but_building_a_deck_is_not() -> None:
    definition = _tools(_settings_for(CapabilityAccessMode.READ_ONLY))[0]["document.manage"]
    engine = PolicyEngine(_settings_for(CapabilityAccessMode.READ_ONLY))

    assert definition.capability_for({"operation": "summarize_pdf"}) is Capability.FILESYSTEM_READ
    assert definition.capability_for({"operation": "create_presentation"}) is Capability.FILESYSTEM_WRITE
    assert engine.evaluate(
        _call("summarize_pdf", tool="document.manage", capability=Capability.FILESYSTEM_READ)
    ).allowed is True
    assert engine.evaluate(
        _call("create_presentation", tool="document.manage").model_copy(update={"risk_level": RiskLevel.HIGH})
    ).reason == "capability_disabled"


# ---- the model cannot pick a more convenient capability ------------------------------


@pytest.mark.asyncio
async def test_the_executor_refuses_a_write_dressed_up_as_a_read(tmp_path) -> None:
    """Capability is owned by the runtime definition. A model that names the read
    capability for a write operation (to slip past a Read-only policy) is refused
    before policy or the adapter ever sees it."""
    settings = _settings_for(CapabilityAccessMode.READ_ONLY)
    definitions, _ = _tools(settings)
    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("t")
    adapter = StaticToolAdapter(output={"operation": "apply_manifest"})
    executor = ToolExecutor(
        PolicyEngine(settings, audit),
        repos,
        audit,
        adapters={"filesystem.manage": adapter},
        tool_definitions={"filesystem.manage": definitions["filesystem.manage"]},
    )
    disguised = ToolCallRequest(
        task_id=task.id,
        tool_name="filesystem.manage",
        capability=Capability.FILESYSTEM_READ,  # the lie
        risk_level=RiskLevel.HIGH,
        input={"operation": "apply_manifest", "manifest": []},
    )

    result = await executor.execute(disguised)

    assert result.status != ToolResultStatus.SUCCEEDED
    assert adapter.requests == []  # never reached the tool
    assert "requires capability filesystem.write" in (result.error_message or "")


@pytest.mark.asyncio
async def test_the_executor_accepts_a_read_under_the_read_capability(tmp_path) -> None:
    settings = _settings_for(CapabilityAccessMode.READ_ONLY)
    definitions, _ = _tools(settings)
    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("t")
    adapter = StaticToolAdapter(output={"operation": "inspect_folder", "root": str(tmp_path), "entries": [], "summary": "ok"})
    executor = ToolExecutor(
        PolicyEngine(settings, audit),
        repos,
        audit,
        adapters={"filesystem.manage": adapter},
        tool_definitions={"filesystem.manage": definitions["filesystem.manage"]},
    )
    request = ToolCallRequest(
        task_id=task.id,
        tool_name="filesystem.manage",
        capability=Capability.FILESYSTEM_READ,
        risk_level=RiskLevel.LOW,
        input={"operation": "inspect_folder", "root": str(tmp_path)},
    )

    result = await executor.execute(request)

    assert result.error_class != ErrorClass.POLICY_DENIED, result.error_message
    assert len(adapter.requests) == 1


# ---- configs written before the read/write split keep working -------------------------


def _write_only_settings(**policy) -> AppSettings:
    settings = AppSettings(_env_file=None)
    settings.capabilities[Capability.FILESYSTEM_WRITE] = CapabilityPolicy(
        enabled=True, requires_approval=True, max_risk_level=RiskLevel.HIGH, **policy
    )
    settings.capabilities[Capability.FILESYSTEM_READ] = CapabilityPolicy(enabled=False)
    return settings


def test_a_config_that_enables_only_write_can_still_read() -> None:
    """Hand-edited configs (and anything from before reads had their own
    capability) enable only filesystem.write. Being allowed to change files
    implies being allowed to look at them, so nothing regresses."""
    settings = _write_only_settings()
    engine = PolicyEngine(settings)

    read = engine.evaluate(_call("inspect_folder", capability=Capability.FILESYSTEM_READ, root="C:/x"))

    assert read.allowed is True and read.needs_approval is False
    assert _tools(settings)[0]["filesystem.manage"].enabled is True


def test_the_implied_read_policy_never_shows_up_as_configured_access() -> None:
    settings = _write_only_settings()

    assert settings.capabilities[Capability.FILESYSTEM_READ].enabled is False  # as configured
    assert settings.capability_policy(Capability.FILESYSTEM_READ).enabled is True  # as enforced


def test_the_implied_read_policy_inherits_the_write_policys_scopes() -> None:
    settings = _write_only_settings(scopes=["C:/Users/me/Downloads"])
    engine = PolicyEngine(settings)

    inside = engine.evaluate(
        _call("inspect_folder", capability=Capability.FILESYSTEM_READ).model_copy(
            update={"scope_target": "C:/Users/me/Downloads/invoices"}
        )
    )
    outside = engine.evaluate(
        _call("inspect_folder", capability=Capability.FILESYSTEM_READ).model_copy(
            update={"scope_target": "C:/Users/me/Documents"}
        )
    )

    assert inside.allowed is True
    assert outside.allowed is False and outside.reason == "scope_not_allowed"


def test_an_explicitly_configured_read_policy_is_left_alone() -> None:
    settings = _write_only_settings()
    settings.capabilities[Capability.FILESYSTEM_READ] = CapabilityPolicy(
        enabled=True, requires_approval=True, max_risk_level=RiskLevel.LOW
    )

    assert settings.capability_policy(Capability.FILESYSTEM_READ).requires_approval is True


def test_write_does_not_imply_anything_for_other_capabilities() -> None:
    settings = _write_only_settings()

    assert settings.capability_policy(Capability.TERMINAL_RUN).enabled is False
    assert settings.capability_policy(Capability.BROWSER_OPEN).enabled is False


def test_the_repository_example_config_grants_no_file_access() -> None:
    import yaml

    example = Path(__file__).resolve().parents[2] / "config" / "config.example.yaml"
    shipped = yaml.safe_load(example.read_text(encoding="utf-8"))

    settings = AppSettings(_env_file=None, **{k: v for k, v in shipped.items() if k in {"capabilities", "adapters"}})

    assert settings.capability_policy(Capability.FILESYSTEM_READ).enabled is False
    assert settings.capability_policy(Capability.FILESYSTEM_WRITE).enabled is False
