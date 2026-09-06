from __future__ import annotations

import sys
from pathlib import Path

import pytest

from agent_control.config import AppSettings, CapabilityPolicy, MCPConfig, MCPServerConfig
from agent_control.config_sync import ConfigManager
from agent_control.schemas import Capability, RiskLevel, ToolCallRequest, ToolResultStatus
from agent_control.tools.mcp_client import MCPClientAdapter, load_mcp_catalog, mcp_catalog_summary, write_mcp_catalog
from agent_control.tools.registry import build_tool_registry

# Spawning a Python subprocess and completing the MCP initialize() round
# trip measured 11.5s on a loaded Windows machine, so the previous 10s was
# marginal and failed as an opaque anyio WouldBlock rather than a timeout.
# Mirrors tests/scenario/harness.py's constant of the same name.
MCP_HANDSHAKE_TIMEOUT_SECONDS = 30


def test_mcp_config_and_registry_expose_client_when_enabled(tmp_path) -> None:
    settings = AppSettings(
        _env_file=None,
        capabilities={Capability.TERMINAL_RUN: CapabilityPolicy(enabled=True, requires_approval=False, max_risk_level=RiskLevel.HIGH)},
        mcp={
            "enabled": True,
            "catalog_path": str(tmp_path / "tool_catalog.json"),
            "servers": {
                "fake": {
                    "command": sys.executable,
                    "args": ["fake_server.py"],
                }
            },
        },
    )

    registry = build_tool_registry(settings, "http://127.0.0.1:8765")
    definitions = {definition.name: definition for definition in registry.definitions}

    assert definitions["mcp.client"].enabled is True
    assert "mcp.client" in registry.adapters
    assert "call_tool" in definitions["mcp.client"].operations
    # docs/THREAT_MODEL.md: a configured server's own reply is untrusted
    # content, distinct from the discover/list_tools/health catalog calls.
    assert definitions["mcp.client"].operation_content_trust == {"call_tool": "untrusted_external"}


def test_registry_context_reads_mcp_catalog_dynamically(tmp_path) -> None:
    config = MCPConfig(
        enabled=True,
        catalog_path=str(tmp_path / "tool_catalog.json"),
        servers={"fake": MCPServerConfig(command=sys.executable, args=["fake_server.py"])},
    )
    settings = AppSettings(
        _env_file=None,
        capabilities={Capability.TERMINAL_RUN: CapabilityPolicy(enabled=True, requires_approval=False, max_risk_level=RiskLevel.HIGH)},
        mcp=config,
    )
    registry = build_tool_registry(settings, "http://127.0.0.1:8765")

    assert "catalog not built yet" in registry.context()
    write_mcp_catalog(
        config,
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

    context = registry.context()
    # server and tool are advertised as separate labelled fields, never a
    # dotted "fake.echo" - see mcp_catalog_summary() for why.
    assert 'server="fake" tool="echo"' in context
    assert "fake.echo" not in context
    assert "catalog not built yet" not in context


@pytest.mark.asyncio
async def test_mcp_client_discovers_and_calls_fake_stdio_server(tmp_path) -> None:
    server_path = _fake_mcp_server(tmp_path)
    catalog_path = tmp_path / "tool_catalog.json"
    adapter = MCPClientAdapter(
        MCPConfig(
            enabled=True,
            catalog_path=str(catalog_path),
            servers={
                "fake": MCPServerConfig(
                    command=sys.executable,
                    args=[str(server_path)],
                    timeout_seconds=MCP_HANDSHAKE_TIMEOUT_SECONDS,
                )
            },
        )
    )

    listed = await adapter.execute(
        ToolCallRequest(
            task_id="task_mcp",
            tool_name="mcp.client",
            capability=Capability.TERMINAL_RUN,
            input={"operation": "list_tools"},
        )
    )
    called = await adapter.execute(
        ToolCallRequest(
            task_id="task_mcp",
            tool_name="mcp.client",
            capability=Capability.TERMINAL_RUN,
            input={"operation": "call_tool", "server": "fake", "tool": "echo", "arguments": {"text": "hello"}},
        )
    )

    assert listed.status == ToolResultStatus.SUCCEEDED
    assert any(tool["name"] == "echo" for tool in listed.output["tools"])
    assert listed.output["catalog_path"] == str(catalog_path)
    assert catalog_path.exists()
    catalog = load_mcp_catalog(adapter.config)
    assert any(tool["tool"] == "echo" and tool["input_schema"] for tool in catalog["tools"])
    assert called.status == ToolResultStatus.SUCCEEDED
    assert "hello" in str(called.output["result"])
    assert "hello" in called.output["terminal_output"][0]["content"]


@pytest.mark.asyncio
async def test_mcp_catalog_records_but_output_hides_disabled_tools(tmp_path) -> None:
    server_path = _fake_mcp_server(tmp_path)
    config = MCPConfig(
        enabled=True,
        catalog_path=str(tmp_path / "tool_catalog.json"),
        servers={
            "fake": MCPServerConfig(
                command=sys.executable,
                args=[str(server_path)],
                timeout_seconds=MCP_HANDSHAKE_TIMEOUT_SECONDS,
                disabled_tools=["hidden"],
            )
        },
    )
    adapter = MCPClientAdapter(config)

    listed = await adapter.execute(
        ToolCallRequest(
            task_id="task_mcp",
            tool_name="mcp.client",
            capability=Capability.TERMINAL_RUN,
            input={"operation": "list_tools"},
        )
    )

    assert listed.status == ToolResultStatus.SUCCEEDED
    assert {tool["name"] for tool in listed.output["tools"]} == {"echo", "large"}
    catalog = load_mcp_catalog(config)
    hidden = [tool for tool in catalog["tools"] if tool["tool"] == "hidden"]
    assert hidden and hidden[0]["disabled"] is True
    summary = mcp_catalog_summary(config)
    assert 'server="fake" tool="echo"' in summary
    assert 'tool="hidden"' not in summary
    settings = AppSettings(
        _env_file=None,
        capabilities={Capability.TERMINAL_RUN: CapabilityPolicy(enabled=True, requires_approval=False, max_risk_level=RiskLevel.HIGH)},
        mcp=config,
    )
    context = build_tool_registry(settings, "http://127.0.0.1:8765").context()
    assert "Configured MCP tools" in context
    assert 'server="fake" tool="echo"' in context
    assert 'tool="hidden"' not in context


@pytest.mark.asyncio
async def test_mcp_selected_server_refresh_preserves_other_catalog_entries(tmp_path) -> None:
    server_path = _fake_mcp_server(tmp_path)
    config = MCPConfig(
        enabled=True,
        catalog_path=str(tmp_path / "tool_catalog.json"),
        servers={
            "alpha": MCPServerConfig(command=sys.executable, args=[str(server_path)], timeout_seconds=MCP_HANDSHAKE_TIMEOUT_SECONDS),
            "beta": MCPServerConfig(command=sys.executable, args=[str(server_path)], timeout_seconds=MCP_HANDSHAKE_TIMEOUT_SECONDS),
        },
    )
    adapter = MCPClientAdapter(config)

    initial = await adapter.execute(
        ToolCallRequest(
            task_id="task_mcp",
            tool_name="mcp.client",
            capability=Capability.TERMINAL_RUN,
            input={"operation": "list_tools"},
        )
    )
    assert initial.status == ToolResultStatus.SUCCEEDED
    refreshed = await adapter.execute(
        ToolCallRequest(
            task_id="task_mcp",
            tool_name="mcp.client",
            capability=Capability.TERMINAL_RUN,
            input={"operation": "list_tools", "server": "alpha"},
        )
    )

    assert refreshed.status == ToolResultStatus.SUCCEEDED
    catalog = load_mcp_catalog(config)
    servers = {server["name"] for server in catalog["servers"]}
    tool_servers = {tool["server"] for tool in catalog["tools"]}
    assert servers == {"alpha", "beta"}
    assert tool_servers == {"alpha", "beta"}


@pytest.mark.asyncio
async def test_mcp_call_tool_truncates_large_output(tmp_path) -> None:
    server_path = _fake_mcp_server(tmp_path)
    adapter = MCPClientAdapter(
        MCPConfig(
            enabled=True,
            catalog_path=str(tmp_path / "tool_catalog.json"),
            servers={
                "fake": MCPServerConfig(
                    command=sys.executable,
                    args=[str(server_path)],
                    timeout_seconds=MCP_HANDSHAKE_TIMEOUT_SECONDS,
                    max_output_chars=300,
                )
            },
        )
    )

    called = await adapter.execute(
        ToolCallRequest(
            task_id="task_mcp",
            tool_name="mcp.client",
            capability=Capability.TERMINAL_RUN,
            input={"operation": "call_tool", "server": "fake", "tool": "large", "arguments": {}},
        )
    )

    assert called.status == ToolResultStatus.SUCCEEDED
    assert called.output["result"]["truncated"] is True
    assert len(called.output["result"]["preview"]) <= 300


@pytest.mark.asyncio
async def test_mcp_health_reports_unreachable_server_and_writes_catalog(tmp_path) -> None:
    config = MCPConfig(
        enabled=True,
        catalog_path=str(tmp_path / "tool_catalog.json"),
        servers={
            "broken": MCPServerConfig(
                command=sys.executable,
                args=[str(tmp_path / "missing_server.py")],
                timeout_seconds=1,
            )
        },
    )
    adapter = MCPClientAdapter(config)

    result = await adapter.execute(
        ToolCallRequest(
            task_id="task_mcp",
            tool_name="mcp.client",
            capability=Capability.TERMINAL_RUN,
            input={"operation": "health"},
        )
    )

    assert result.status == ToolResultStatus.SUCCEEDED
    assert result.output["healthy"] is False
    assert result.output["servers"][0]["healthy"] is False
    assert (tmp_path / "tool_catalog.json").exists()


def test_mcp_client_rejects_disabled_tool() -> None:
    adapter = MCPClientAdapter(
        MCPConfig(
            enabled=True,
            servers={
                "fake": MCPServerConfig(
                    command=sys.executable,
                    args=["server.py"],
                    disabled_tools=["echo"],
                )
            },
        )
    )

    # The adapter catches the disabled-tool error and returns a failed tool result.
    import asyncio

    result = asyncio.run(
        adapter.execute(
            ToolCallRequest(
                task_id="task_mcp",
                tool_name="mcp.client",
                capability=Capability.TERMINAL_RUN,
                input={"operation": "call_tool", "server": "fake", "tool": "echo", "arguments": {}},
            )
        )
    )

    assert result.status == ToolResultStatus.FAILED
    assert "disabled" in (result.error_message or "").lower()


@pytest.mark.asyncio
async def test_mcp_client_installs_server_into_config(tmp_path) -> None:
    config_path = tmp_path / "config.yaml"
    adapter = MCPClientAdapter(
        MCPConfig(enabled=False, catalog_path=str(tmp_path / "tool_catalog.json")),
        config_manager=ConfigManager(config_path=config_path, env_path=tmp_path / ".env"),
    )

    result = await adapter.execute(
        ToolCallRequest(
            task_id="task_mcp",
            tool_name="mcp.client",
            capability=Capability.TERMINAL_RUN,
            input={
                "operation": "install_server",
                "name": "fake",
                "command": sys.executable,
                "args": ["server.py"],
                "risk_level": "low",
            },
        )
    )

    assert result.status == ToolResultStatus.SUCCEEDED
    assert result.output["installed"] is True
    assert adapter.config.enabled is True
    assert "fake" in adapter.config.servers
    saved = config_path.read_text(encoding="utf-8")
    assert "fake:" in saved
    assert "server.py" in saved


def _fake_mcp_server(tmp_path: Path) -> Path:
    server_path = tmp_path / "fake_mcp_server.py"
    server_path.write_text(
        """
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("fake")

@mcp.tool()
def echo(text: str) -> str:
    return text

@mcp.tool()
def hidden(text: str = "secret") -> str:
    return text

@mcp.tool()
def large() -> str:
    return "x" * 2000

if __name__ == "__main__":
    mcp.run()
""".strip()
        + "\n",
        encoding="utf-8",
    )
    return server_path


@pytest.mark.asyncio
async def test_mcp_session_closes_subprocess_when_handshake_fails(monkeypatch, tmp_path) -> None:
    """A failed handshake must not strand the spawned MCP server.

    `_session.__aenter__` enters stdio_client (spawning the subprocess) and
    only then calls `initialize()`. When initialize raised - a handshake
    timeout is the easy way to hit it - the exception propagated out of
    __aenter__, so the caller's `async with` never ran __aexit__ and the
    already-entered stdio context was never closed. The orphaned async
    generator was eventually finalized by the event loop at shutdown, in a
    different task than had entered it, producing an unrelated-looking
    "Attempted to exit cancel scope in a different task" RuntimeError.
    """
    from contextlib import asynccontextmanager

    from agent_control.tools import mcp_client as mcp_client_module

    closed: list[str] = []

    @asynccontextmanager
    async def fake_stdio_client(_params):
        try:
            yield ("read-stream", "write-stream")
        finally:
            closed.append("stdio")

    class FakeSession:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc) -> None:
            closed.append("session")

        async def initialize(self):
            raise TimeoutError("handshake timed out")

    monkeypatch.setattr(mcp_client_module, "stdio_client", fake_stdio_client)
    monkeypatch.setattr(mcp_client_module, "ClientSession", FakeSession)

    server = MCPServerConfig(command=sys.executable, args=["-c", "pass"], timeout_seconds=5)

    with pytest.raises(TimeoutError):
        async with mcp_client_module._session(server):
            pass  # pragma: no cover - __aenter__ raises before the body runs

    # Both layers unwound despite __aenter__ failing partway through.
    assert closed == ["session", "stdio"]
