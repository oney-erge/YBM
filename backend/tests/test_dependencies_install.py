"""dependencies.install - the tool that can change the machine permanently.

The capability existed with no implementation: the enum, the HIGH risk score,
and an access-mode switch all referred to something no tool declared, so
"install what you need" was impossible and the console offered a switch that
did nothing.

These tests are mostly about the guards, because this is the operation a
hijacked prompt would reach for first.
"""

from __future__ import annotations

import pytest

from agent_control.config import AppSettings, CapabilityPolicy, DependenciesAdapterConfig
from agent_control.schemas import Capability, RiskLevel, ToolCallRequest, ToolResultStatus
from agent_control.tools.dependencies import DependenciesAdapter
from agent_control.tools.registry import build_tool_registry


def _request(**payload) -> ToolCallRequest:
    return ToolCallRequest(
        task_id="task_1",
        tool_name="dependencies.install",
        capability=Capability.DEPENDENCIES_INSTALL,
        risk_level=RiskLevel.HIGH,
        input=payload,
    )


def _adapter(tmp_path, allowed: list[str]) -> DependenciesAdapter:
    return DependenciesAdapter(
        DependenciesAdapterConfig(
            enabled=True, allowed_packages=allowed, target_root=str(tmp_path / "deps")
        )
    )


@pytest.mark.asyncio
async def test_package_outside_the_allowlist_is_refused(tmp_path) -> None:
    result = await _adapter(tmp_path, ["requests"]).execute(
        _request(operation="install", packages=["cryptominer"])
    )

    assert result.status == ToolResultStatus.FAILED
    assert "allowed_packages" in (result.error_message or "")


@pytest.mark.asyncio
async def test_empty_allowlist_installs_nothing(tmp_path) -> None:
    """Enabling the capability must not by itself permit arbitrary installs -
    someone has to say what is acceptable first."""
    result = await _adapter(tmp_path, []).execute(_request(operation="install", packages=["requests"]))

    assert result.status == ToolResultStatus.FAILED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "requirement",
    [
        "https://evil.example/pkg.tar.gz",
        "git+https://github.com/someone/pkg",
        "./local-package",
        "requests --index-url https://evil.example/simple",
    ],
)
async def test_non_allowlistable_requirement_forms_are_refused(tmp_path, requirement) -> None:
    """URLs, VCS refs, local paths and index switches all fetch code from
    somewhere the allowlist never described, so the name check alone is not
    enough to make an allowlist meaningful."""
    result = await _adapter(tmp_path, ["requests"]).execute(
        _request(operation="install", packages=[requirement])
    )

    assert result.status == ToolResultStatus.FAILED


@pytest.mark.asyncio
async def test_list_allowed_needs_no_packages_and_reports_the_allowlist(tmp_path) -> None:
    result = await _adapter(tmp_path, ["requests", "httpx"]).execute(_request(operation="list_allowed"))

    assert result.status == ToolResultStatus.SUCCEEDED
    assert result.output["allowed_packages"] == ["httpx", "requests"]


def test_install_always_requires_approval_even_under_a_raised_floor() -> None:
    """Bound to the operation, not left to the risk threshold, so an
    access-mode preset cannot quietly remove the prompt."""
    settings = AppSettings(
        _env_file=None,
        capabilities={
            Capability.DEPENDENCIES_INSTALL: CapabilityPolicy(
                enabled=True, requires_approval=False, max_risk_level=RiskLevel.CRITICAL
            )
        },
    )
    settings.adapters.dependencies = DependenciesAdapterConfig(enabled=True, allowed_packages=["requests"])

    registry = build_tool_registry(settings, backend_base_url="http://127.0.0.1:8765")
    definition = next(d for d in registry.definitions if d.name == "dependencies.install")

    assert definition.enabled is True
    assert "install" in definition.approval_required_operations


def test_tool_is_absent_until_the_capability_is_enabled() -> None:
    settings = AppSettings(_env_file=None)
    registry = build_tool_registry(settings, backend_base_url="http://127.0.0.1:8765")
    definition = next(d for d in registry.definitions if d.name == "dependencies.install")

    assert definition.enabled is False


def test_the_install_command_is_pip_when_pip_is_there_else_uv_else_a_clear_refusal(tmp_path, monkeypatch) -> None:
    """Found live 11/15: this install's Python is uv's and has no pip, so every install failed ("No module named pip") and no spreadsheet, Word file or chart could be made."""
    import sys
    from pathlib import Path

    from agent_control.tools import dependencies as deps

    target = tmp_path / "t"
    monkeypatch.setattr(deps.importlib.util, "find_spec", lambda name: object() if name == "pip" else None)
    assert deps.installer_command(target, ["openpyxl"])[:4] == [sys.executable, "-m", "pip", "install"]

    monkeypatch.setattr(deps.importlib.util, "find_spec", lambda name: None)
    monkeypatch.setattr(deps.shutil, "which", lambda name: "/bin/uv" if name == "uv" else None)
    command = deps.installer_command(target, ["openpyxl", "python-docx"])
    assert command[:3] == ["/bin/uv", "pip", "install"] and command[-2:] == ["openpyxl", "python-docx"]
    assert command[command.index("--python") + 1] == sys.executable and command[command.index("--target") + 1] == str(Path(target))

    monkeypatch.setattr(deps.shutil, "which", lambda name: None)
    with pytest.raises(ValueError, match="neither pip nor uv"):
        deps.installer_command(target, ["openpyxl"])
