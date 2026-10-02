"""Scenario: "use the fake MCP echo tool" through the real Operator loop ->
mcp.client call_tool -> a real fake MCP server subprocess over stdio. Ports
e2e/all_cases.json's `mcp_call_fake_echo` case down to the deterministic
tier (docs/archive/HISTORY.md P2) - the first MCP category case ported, and the
first where the pre-built tool catalog (not just capability config) has to
be part of the Operator's prompt for the LLM to know `fake.echo` exists at
all - mirrors test_mcp_client.py's `_fake_mcp_server` fixture and
`write_mcp_catalog` pre-seeding pattern rather than round-tripping a live
discovery call first.

Note: harness.build_scenario() snapshots config_context once at build time
(no config_context_factory, unlike production's cli.py wiring) - so the MCP
catalog must be written to disk *before* build_scenario() runs, or the
Operator's prompt won't mention `fake.echo` and the fixture recorded here
would key against the wrong prompt.

Two separate bugs this file used to document are now both fixed:

1. The adapter_proposal fulfillment-gap bug is structurally gone - see
   test_mcp_discover_tools.py's docstring (same root cause, fixed by the
   plan-based path's deletion).
2. A real prompt-format bug found 2026-07-29 re-recording this fixture and
   fixed the same day: `mcp_catalog_summary()` used to print each tool as a
   single dotted string (`"- fake.echo: Echo text; ..."`) while
   `MCPClientInput` requires `server` and `tool` as SEPARATE fields. The
   model copied the dotted string wholesale into one field - reproducibly,
   across two independent recording attempts with zero self-correction
   (`server="fake.echo"` one run, `tool="fake.echo"` with `server` missing
   the next). Fixed by printing the fields the way the schema names them:
   `- server="fake" tool="echo" - Echo text; ...`. Re-recording after that
   change, the model immediately produced the correct split input and the
   call succeeded first try. See docs/archive/HISTORY.md Part 2 §4 item 8.

The objective quotes the echoed text explicitly ("echo the exact text
\"hello from E2E\"") because the earlier unquoted phrasing ("echo hello
from E2E") is genuinely ambiguous English - the model reasonably read it as
echo "hello", attributed to E2E, and passed `text="hello"`. That is a test
clarity problem, not a model failure, and this test is about the MCP
round-trip, not about parsing ambiguous instructions.
"""

from __future__ import annotations

import sys

from agent_control.config import CapabilityPolicy, MCPConfig, MCPServerConfig, default_capability_policies
from agent_control.schemas import Capability, RiskLevel
from agent_control.tools.mcp_client import write_mcp_catalog
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


def _write_fake_mcp_server(path) -> None:
    path.write_text(
        """
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("fake")

@mcp.tool()
def echo(text: str) -> str:
    return text

if __name__ == "__main__":
    mcp.run()
""".strip()
        + "\n",
        encoding="utf-8",
    )




@pytest.mark.asyncio
async def test_mcp_call_fake_echo_tool_call_succeeds(tmp_path, monkeypatch) -> None:
    workspace = scenario_scratch_dir("mcp_call_fake_echo")
    server_path = workspace / "fake_mcp_server.py"
    _write_fake_mcp_server(server_path)
    catalog_path = workspace / "tool_catalog.json"

    settings = mcp_settings(monkeypatch, tmp_path, server_path, catalog_path)
    write_mcp_catalog(
        settings.mcp,
        servers=[{"name": "fake", "enabled": True, "healthy": True, "tool_count": 1}],
        tools=[
            {
                "server": "fake",
                "name": "echo",
                "tool": "echo",
                "description": "Echo text",
                "capability": Capability.TERMINAL_RUN.value,
                "risk_level": RiskLevel.LOW.value,
                "disabled": False,
            }
        ],
    )

    scenario = build_scenario(settings, tmp_path=tmp_path, fixture_name="mcp_call_fake_echo")

    task = await run_task_to_completion(scenario, 'Use the fake MCP echo tool to echo the exact text "hello from E2E".')

    tool_calls = scenario.repositories.tool_invocations.list_for_task(task.id)
    mcp_calls = [call for call in tool_calls if call["tool_name"] == "mcp.client"]
    assert mcp_calls
    for call in mcp_calls:
        call_input = call["request"].get("input", {})
        assert call_input.get("operation") == "call_tool"
        # Regression guard for the catalog-format bug (see module docstring):
        # server and tool must be split correctly, never a dotted
        # "fake.echo" crammed into one field.
        assert call_input.get("server") == "fake"
        assert call_input.get("tool") == "echo"
        assert call["result"]["status"] == "succeeded"
        assert "hello from E2E" in str(call["result"]["output"].get("result", ""))
    assert_completed(task)


@pytest.mark.asyncio
async def test_mcp_call_fake_echo_rejected_by_capability_scope(tmp_path, monkeypatch) -> None:
    workspace = scenario_scratch_dir("mcp_call_fake_echo")
    server_path = workspace / "fake_mcp_server.py"
    _write_fake_mcp_server(server_path)
    catalog_path = workspace / "tool_catalog.json"

    # Keep the tool visible to the Operator so this scenario exercises the
    # runtime policy gate instead of depending on a model to call a tool the
    # catalog explicitly labels disabled. The request carries no matching
    # scope target, so the configured capability scope must reject it before
    # the fake server runs.
    caps = default_capability_policies()
    caps[Capability.TERMINAL_RUN] = CapabilityPolicy(
        enabled=True,
        requires_approval=False,
        max_risk_level=RiskLevel.HIGH,
        scopes=["approved-server"],
    )
    settings = isolated_settings(
        monkeypatch, tmp_path,
        capabilities=caps,
        mcp=MCPConfig(
            enabled=True,
            catalog_path=str(catalog_path),
            servers={
                "fake": MCPServerConfig(
                    command=sys.executable, args=[str(server_path)],
                    timeout_seconds=MCP_HANDSHAKE_TIMEOUT_SECONDS,
                    # Matches this test's own catalog entry below (LOW) -
                    # see harness.mcp_settings()'s comment for why the
                    # default (HIGH) would be a self-inconsistent config.
                    risk_level=RiskLevel.LOW,
                )
            },
        ),
    )
    write_mcp_catalog(
        settings.mcp,
        servers=[{"name": "fake", "enabled": True, "healthy": True, "tool_count": 1}],
        tools=[
            {
                "server": "fake",
                "name": "echo",
                "tool": "echo",
                "description": "Echo text",
                "capability": Capability.TERMINAL_RUN.value,
                "risk_level": RiskLevel.LOW.value,
                "disabled": False,
            }
        ],
    )
    scenario = build_scenario(settings, tmp_path=tmp_path, fixture_name="mcp_call_fake_echo")

    task = await run_task_to_completion(scenario, 'Use the fake MCP echo tool to echo the exact text "hello from E2E".')

    # Distinct from the success case above: the Operator sees an enabled tool
    # and tries it, while the policy scope refuses the request before the MCP
    # adapter can dispatch it.
    assert_rejected(task)
    tool_calls = scenario.repositories.tool_invocations.list_for_task(task.id)
    mcp_calls = [call for call in tool_calls if call["tool_name"] == "mcp.client"]
    assert mcp_calls
    # Not necessarily every call: mcp.client's `arguments` field is easy to
    # confuse with the unrelated, list-typed `args` field on the same
    # contract (install_server's command-line args) - a first attempt using
    # the wrong one fails input validation before ever reaching the policy
    # engine's capability-scope check, and the model may retry several times
    # before it (or doesn't) get the shape right. Reproduced live: one
    # recording had the model repeat the `args` mistake for 7 consecutive
    # retries with zero self-correction, so every one of that run's calls
    # was "failed" (validation), never "denied" - see
    # test_code_interpreter_default_settings_need_approval_without_docker.py
    # for the same class of fragility already fixed the same way there. The
    # policy gate firing at least once is what this test is about.
    denied_calls = [call for call in mcp_calls if call["result"]["status"] == "denied"]
    assert denied_calls
    assert all(call["result"].get("error_message") == "scope_not_allowed" for call in denied_calls)
