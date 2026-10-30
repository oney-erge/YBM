"""Unit tests for the tool registry.

Focuses on the registry-side contract: per-operation schema validation, and
the context/vault summary strings shown to the Operator's decide() call.

We use ad-hoc toy ToolDefinitions instead of pulling the full
build_tool_registry - that path is covered indirectly by every other test
that builds a real worker.
"""
from __future__ import annotations

import pytest
from pydantic import BaseModel, Field

from agent_control.schemas import Capability
from agent_control.config import AppSettings, CapabilityPolicy
from agent_control.tools.registry import ToolDefinition, ToolRegistry, build_tool_registry


class _ToyInput(BaseModel):
    operation: str
    path: str = Field(min_length=1)


class _ToyOutput(BaseModel):
    operation: str
    status: str = "ok"


def _toy_definition(*, enabled: bool = True) -> ToolDefinition:
    return ToolDefinition(
        name="toy.tool",
        capability=Capability.FILESYSTEM_READ,
        enabled=enabled,
        description="toy tool for tests",
        operations=("read",),
        operation_schemas={"read": _ToyInput},
        operation_output_schemas={"read": _ToyOutput},
        default_operation="read",
        examples=({"operation": "read", "path": "/tmp/a.txt"},),
    )


def test_validate_input_succeeds_for_valid_payload() -> None:
    definition = _toy_definition()
    out = definition.validate_input({"operation": "read", "path": "/tmp/x.txt"})
    assert out == {"operation": "read", "path": "/tmp/x.txt"}


def test_validate_input_raises_value_error_on_invalid_payload() -> None:
    definition = _toy_definition()
    with pytest.raises(ValueError) as excinfo:
        definition.validate_input({"operation": "read", "path": ""})
    assert "invalid input for toy.tool" in str(excinfo.value)


def test_validate_input_rejects_unknown_operation() -> None:
    definition = _toy_definition()
    with pytest.raises(ValueError) as excinfo:
        definition.validate_input({"operation": "delete", "path": "/x"})
    assert "unsupported operation" in str(excinfo.value)


def test_validate_input_defaults_missing_operation_to_default() -> None:
    definition = _toy_definition()
    out = definition.validate_input({"path": "/tmp/x.txt"})
    assert out["operation"] == "read"


def test_a_single_operation_tool_defaults_to_that_operation() -> None:
    definition = build_tool_registry(AppSettings(), "http://127.0.0.1:8765").definition_index["web.search"]

    assert definition.validate_input({"query": "new episodes"})["operation"] == "search"
    assert _toy_definition().default_operation == "read"  # an explicit default is kept


def test_validate_output_routes_by_operation() -> None:
    definition = _toy_definition()
    out = definition.validate_output({"operation": "read"})
    # status defaults to "ok"; output schema fills it in
    assert out == {"operation": "read", "status": "ok"}


def test_registry_context_lists_enabled_and_disabled_tools() -> None:
    enabled = _toy_definition(enabled=True)
    disabled = ToolDefinition(
        name="disabled.tool",
        capability=Capability.FILESYSTEM_READ,
        enabled=False,
        description="not currently available",
    )
    registry = ToolRegistry(adapters={}, definitions=(enabled, disabled))
    ctx = registry.context()
    # Enabled tools carry their full description and operations; disabled ones
    # are named only. The catalog is the largest item in every Operator prompt
    # and the loop is prefill-bound, so detail the model cannot act on (it
    # cannot call a disabled tool) is not worth re-sending every step - the
    # name alone still lets it say "that capability is turned off".
    assert "toy.tool" in ctx
    assert "not currently available" not in ctx
    assert "Disabled (cannot be called" in ctx
    assert "disabled.tool" in ctx
    # Examples are shown for enabled tools only.
    assert "/tmp/a.txt" in ctx


def test_registry_context_omits_examples_for_disabled_tool() -> None:
    disabled = ToolDefinition(
        name="toy.tool",
        capability=Capability.FILESYSTEM_READ,
        enabled=False,
        description="disabled",
        examples=({"operation": "read", "path": "/should-not-appear.txt"},),
    )
    registry = ToolRegistry(adapters={}, definitions=(disabled,))
    assert "/should-not-appear.txt" not in registry.context()


def test_vault_summary_marks_disabled_as_known_gap() -> None:
    disabled = ToolDefinition(
        name="missing.tool",
        capability=Capability.FILESYSTEM_READ,
        enabled=False,
        description="not wired",
    )
    summary = ToolRegistry(adapters={}, definitions=(disabled,)).vault_summary()
    assert "missing.tool: known_gap" in summary


def test_build_registry_exposes_http_request_only_with_allowlist() -> None:
    settings = AppSettings(
        _env_file=None,
        capabilities={Capability.NETWORK_HTTP: CapabilityPolicy(enabled=True, requires_approval=False)},
        adapters={"http_request": {"enabled": True, "allowed_hosts": ["api.example.com"]}},
    )

    registry = build_tool_registry(settings, "http://127.0.0.1:8765")
    definitions = {definition.name: definition for definition in registry.definitions}

    assert definitions["http.request"].enabled is True
    assert "http.request" in registry.adapters
    assert "api.example.com" in registry.context()
