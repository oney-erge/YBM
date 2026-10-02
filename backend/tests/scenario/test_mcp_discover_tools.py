"""Scenario: "check what MCP tools are available" through the real Operator
loop -> mcp.client list_tools -> a real fake MCP server subprocess over
stdio. Ports e2e/all_cases.json's `mcp_discover_tools` case down to the
deterministic tier (docs/archive/HISTORY.md P2) - the list_tools counterpart to
test_mcp_call_fake_echo.py's call_tool case.

Originally documented a real fulfillment-gap bug: the deleted plan-based
path's self-declared `plan.postconditions` reached for `type:
"adapter_proposal"` for `mcp.client` generally (not call_tool-specific),
which `fulfillment.py`'s old `_postcondition_satisfied()` could never mark
satisfied for a real mcp.client result - list_tools calls succeeded
perfectly (verified against a real fake MCP server subprocess) but the task
still ended in CLARIFYING. Re-recording against the Operator loop confirms
this bug is now structurally gone, not just re-recorded around: the
Operator loop's `fulfillment.py` infers postconditions deterministically
from objective text + tool names (see docs/ARCHITECTURE.md), with no
self-declared `plan.postconditions` mechanism left to reach for the wrong
type. The task now completes correctly.
"""

from __future__ import annotations

import sys

from agent_control.config import MCPConfig, MCPServerConfig
import pytest

from .harness import (
    assert_completed,
    MCP_HANDSHAKE_TIMEOUT_SECONDS,
    assert_rejected,
    build_scenario,
    mcp_settings,
    isolated_settings,
    run_task_to_completion,
    scenario_scratch_dir,
)
from .test_mcp_call_fake_echo import _write_fake_mcp_server




@pytest.mark.asyncio
async def test_mcp_discover_tools_lists_configured_tools(tmp_path, monkeypatch) -> None:
    workspace = scenario_scratch_dir("mcp_discover_tools")
    server_path = workspace / "fake_mcp_server.py"
    _write_fake_mcp_server(server_path)
    catalog_path = workspace / "tool_catalog.json"

    settings = mcp_settings(monkeypatch, tmp_path, server_path, catalog_path)
    scenario = build_scenario(settings, tmp_path=tmp_path, fixture_name="mcp_discover_tools")

    task = await run_task_to_completion(scenario, "Check what MCP tools are available.")

    # See module docstring: this used to end in CLARIFYING despite every
    # list_tools call succeeding, because of the deleted plan-based path's
    # adapter_proposal fulfillment-gap bug. The Operator loop has no
    # self-declared plan.postconditions left to reach for the wrong type,
    # so this now correctly completes.
    tool_calls = scenario.repositories.tool_invocations.list_for_task(task.id)
    mcp_calls = [call for call in tool_calls if call["tool_name"] == "mcp.client"]
    assert mcp_calls
    for call in mcp_calls:
        assert call["request"].get("input", {}).get("operation") == "list_tools"
        assert call["result"]["status"] == "succeeded"
        assert any(tool.get("name") == "echo" for tool in call["result"]["output"].get("tools", []))
    assert_completed(task)


@pytest.mark.asyncio
async def test_mcp_discover_tools_disabled_by_capability_policy(tmp_path, monkeypatch) -> None:
    workspace = scenario_scratch_dir("mcp_discover_tools")
    server_path = workspace / "fake_mcp_server.py"
    _write_fake_mcp_server(server_path)
    catalog_path = workspace / "tool_catalog.json"

    # TERMINAL_RUN left at its secure-by-default disabled state.
    settings = isolated_settings(
        monkeypatch, tmp_path,
        mcp=MCPConfig(
            enabled=True,
            catalog_path=str(catalog_path),
            servers={"fake": MCPServerConfig(command=sys.executable, args=[str(server_path)], timeout_seconds=MCP_HANDSHAKE_TIMEOUT_SECONDS)},
        ),
    )
    scenario = build_scenario(settings, tmp_path=tmp_path, fixture_name="mcp_discover_tools")

    task = await run_task_to_completion(scenario, "Check what MCP tools are available.")

    assert_rejected(task)
    # The Operator still decides to try mcp.client (a reasonable read of the
    # objective) - the policy engine is what blocks it, recorded as a denied
    # attempt in the audit trail, not as the call never being attempted at
    # all. Matches the pattern used elsewhere for gated tools (e.g.
    # test_code_interpreter_default_settings_need_approval_without_docker.py).
    tool_calls = scenario.repositories.tool_invocations.list_for_task(task.id)
    mcp_calls = [call for call in tool_calls if call["tool_name"] == "mcp.client"]
    assert mcp_calls
    assert all(call["result"]["status"] == "denied" for call in mcp_calls)
    assert all(call["result"].get("error_message") == "capability_disabled" for call in mcp_calls)
