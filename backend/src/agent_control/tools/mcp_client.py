from __future__ import annotations

import json
import re
from contextlib import AsyncExitStack
from datetime import timedelta
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from agent_control.config import MCPConfig, MCPServerConfig
from agent_control.config_sync import ConfigManager
from agent_control.schemas import Capability, RiskLevel, ToolCallRequest, ToolCallResult, ToolResultStatus, utc_now
from agent_control.tools.contracts import MCPClientInput, MCPClientOutput
from agent_control.tools.spec import (
    UNTRUSTED_EXTERNAL,
    Adapters,
    Definitions,
    RegistryDeps,
    ToolDefinition,
    capability_enabled,
    failed_result,
    same_output_schema,
)


class MCPClientAdapter:
    """Call tools from configured external MCP servers.

    YBM's existing ``mcp_server.py`` exposes YBM to other clients. This adapter
    is the opposite direction: it lets YBM discover and call configured MCP
    servers as worker tools.
    """

    def __init__(self, config: MCPConfig, config_manager: ConfigManager | None = None) -> None:
        self.config = config
        self.config_manager = config_manager or ConfigManager()

    async def execute(self, request: ToolCallRequest) -> ToolCallResult:
        operation = str(request.input.get("operation") or "list_tools")
        if not self.config.enabled and operation != "install_server":
            return failed_result(request, "MCP client is disabled")
        try:
            if operation in {"discover", "list_tools"}:
                output = await self._list_tools(request)
            elif operation == "health":
                output = await self._health(request)
            elif operation == "call_tool":
                output = await self._call_tool(request)
            elif operation == "install_server":
                output = self._install_server(request)
            else:
                return failed_result(request, f"unsupported MCP operation: {operation}")
        except Exception as exc:
            return failed_result(request, f"MCP operation failed: {exc}")

        output["operation"] = operation
        output["terminal_output"] = [_terminal_output(operation, output)]
        return ToolCallResult(request_id=request.id, status=ToolResultStatus.SUCCEEDED, output=output)

    async def _list_tools(self, request: ToolCallRequest) -> dict[str, Any]:
        selected = str(request.input.get("server") or "")
        tools: list[dict[str, Any]] = []
        catalog_entries: list[dict[str, Any]] = []
        servers: list[dict[str, Any]] = []
        for name, server in self._servers(selected).items():
            try:
                server_tools = await _list_server_tools(name, server)
                catalog_entries.extend(server_tools)
                tools.extend([tool for tool in server_tools if not tool.get("disabled")])
                servers.append(
                    {"name": name, "enabled": server.enabled, "healthy": True, "tool_count": len(server_tools)}
                )
            except Exception as exc:
                servers.append({"name": name, "enabled": server.enabled, "healthy": False, "error": str(exc)})
        catalog = write_mcp_catalog(self.config, servers=servers, tools=catalog_entries, replace_all=not bool(selected))
        return {
            "servers": servers,
            "tools": tools,
            "healthy": all(item.get("healthy") for item in servers) if servers else False,
            "summary": f"Discovered {len(tools)} MCP tool(s) across {len(servers)} server(s).",
            "catalog_path": catalog["catalog_path"],
            "catalog_updated_at": catalog["catalog_updated_at"],
            "catalog_entries": catalog_entries,
        }

    async def _health(self, request: ToolCallRequest) -> dict[str, Any]:
        output = await self._list_tools(request)
        output["summary"] = _health_summary(output["servers"])
        return output

    async def _call_tool(self, request: ToolCallRequest) -> dict[str, Any]:
        server_name = str(request.input["server"])
        tool_name = str(request.input["tool"])
        server = self.config.servers.get(server_name)
        if server is None or not server.enabled:
            raise ValueError(f"MCP server is not configured or enabled: {server_name}")
        if tool_name in set(server.disabled_tools):
            raise ValueError(f"MCP tool is disabled by configuration: {server_name}.{tool_name}")
        result = await _call_server_tool(
            server_name,
            server,
            tool_name,
            dict(request.input.get("arguments") or {}),
        )
        catalog = load_mcp_catalog(self.config)
        return {
            "servers": [{"name": server_name, "enabled": True, "healthy": True}],
            "tools": [{"server": server_name, "name": tool_name}],
            "result": result,
            "healthy": True,
            "summary": f"Called MCP tool {server_name}.{tool_name}.",
            "selected_tool": {"server": server_name, "tool": tool_name},
            "catalog_path": str(_catalog_path(self.config)),
            "catalog_updated_at": catalog.get("updated_at"),
        }

    def _install_server(self, request: ToolCallRequest) -> dict[str, Any]:
        name = str(request.input["name"]).strip()
        if not re.match(r"^[A-Za-z0-9_.-]{1,80}$", name):
            raise ValueError("MCP server name must contain only letters, numbers, underscore, dot, or dash")
        command = str(request.input["command"]).strip()
        if not command:
            raise ValueError("MCP server command is required")
        args = [str(item) for item in request.input.get("args") or []]
        env = {str(key): str(value) for key, value in dict(request.input.get("env") or {}).items()}
        capability = Capability(str(request.input.get("capability") or Capability.TERMINAL_RUN.value))
        risk_level = RiskLevel(str(request.input.get("risk_level") or RiskLevel.HIGH.value))
        server = MCPServerConfig(
            enabled=True,
            command=command,
            args=args,
            env=env,
            cwd=request.input.get("cwd"),
            timeout_seconds=int(request.input.get("timeout_seconds") or 30),
            capability=capability,
            risk_level=risk_level,
            disabled_tools=[str(item) for item in request.input.get("disabled_tools") or []],
            max_output_chars=int(request.input.get("max_output_chars") or 20000),
        )

        config = self.config_manager.read_config()
        mcp_config = config.setdefault("mcp", {})
        if not isinstance(mcp_config, dict):
            raise ValueError("config mcp section must be an object")
        mcp_config["enabled"] = True
        mcp_config.setdefault("cache_ttl_seconds", self.config.cache_ttl_seconds)
        mcp_config.setdefault("catalog_path", self.config.catalog_path)
        servers = mcp_config.setdefault("servers", {})
        if not isinstance(servers, dict):
            raise ValueError("config mcp.servers section must be an object")
        servers[name] = server.model_dump(mode="json")
        self.config_manager.write_config(config)

        self.config.enabled = True
        self.config.servers[name] = server
        return {
            "installed": True,
            "servers": [{"name": name, **server.model_dump(mode="json")}],
            "tools": [],
            "healthy": None,
            "summary": f"Installed MCP server configuration: {name}. Run mcp.client list_tools to refresh the catalog.",
            "catalog_path": str(_catalog_path(self.config)),
        }

    def _servers(self, selected: str = "") -> dict[str, MCPServerConfig]:
        if selected:
            server = self.config.servers.get(selected)
            return {selected: server} if server and server.enabled else {}
        return {name: server for name, server in self.config.servers.items() if server.enabled}


async def _list_server_tools(name: str, server: MCPServerConfig) -> list[dict[str, Any]]:
    async with _session(server) as session:
        result = await session.list_tools()
        disabled = set(server.disabled_tools)
        tools = []
        for tool in result.tools:
            payload = tool.model_dump(mode="json") if hasattr(tool, "model_dump") else dict(tool)
            tool_name = str(payload.get("name") or getattr(tool, "name", ""))
            payload["server"] = name
            payload["tool"] = tool_name
            payload["disabled"] = tool_name in disabled
            payload["capability"] = server.capability.value
            payload["risk_level"] = server.risk_level.value
            payload["healthy"] = True
            payload["last_seen"] = utc_now().isoformat()
            payload["last_error"] = None
            payload["input_schema"] = payload.get("inputSchema") or payload.get("input_schema") or {}
            tools.append(payload)
        return tools


async def _call_server_tool(name: str, server: MCPServerConfig, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    async with _session(server) as session:
        result = await session.call_tool(tool_name, arguments)
        payload = result.model_dump(mode="json") if hasattr(result, "model_dump") else {"result": str(result)}
        text = json.dumps(payload, ensure_ascii=False, default=str)
        if len(text) > server.max_output_chars:
            payload = {"truncated": True, "preview": text[: server.max_output_chars]}
        payload["server"] = name
        payload["tool"] = tool_name
        return payload


class _session:
    """One MCP stdio session (subprocess + ClientSession), opened and closed
    as a unit.

    Uses AsyncExitStack rather than driving each context manager's
    __aenter__/__aexit__ by hand. The hand-rolled version leaked on any
    partial-enter failure: `stdio_client` was already entered when
    `initialize()` raised (a handshake timeout is the easy way to hit this),
    and because `__aenter__` propagated that exception the surrounding
    `async with` never called `__aexit__` - so the spawned MCP server
    subprocess and its streams were never closed. It surfaced as an
    "Attempted to exit cancel scope in a different task than it was entered
    in" RuntimeError once the event loop finally finalized the orphaned async
    generator at shutdown, in a different task than had entered it.

    AsyncExitStack unwinds whatever was entered, in reverse order, in this
    same task - both on the failure path below and on normal exit.
    """

    def __init__(self, server: MCPServerConfig) -> None:
        self.server = server
        self._stack: AsyncExitStack | None = None
        self.session: ClientSession | None = None

    async def __aenter__(self) -> ClientSession:
        params = StdioServerParameters(
            command=self.server.command,
            args=list(self.server.args),
            env=dict(self.server.env) or None,
            cwd=self.server.cwd,
        )
        stack = AsyncExitStack()
        try:
            read, write = await stack.enter_async_context(stdio_client(params))
            self.session = await stack.enter_async_context(
                ClientSession(
                    read,
                    write,
                    read_timeout_seconds=timedelta(seconds=self.server.timeout_seconds),
                )
            )
            await self.session.initialize()
        except BaseException:
            # Partial enter: close whatever did open before re-raising, so a
            # handshake failure doesn't strand the server subprocess.
            await stack.aclose()
            self.session = None
            raise
        self._stack = stack
        return self.session

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._stack is not None:
            await self._stack.aclose()
            self._stack = None
        self.session = None


def _health_summary(servers: list[dict[str, Any]]) -> str:
    if not servers:
        return "No enabled MCP servers are configured."
    healthy = sum(1 for item in servers if item.get("healthy"))
    return f"MCP health: {healthy}/{len(servers)} server(s) reachable."


def _catalog_path(config: MCPConfig) -> Path:
    return Path(config.catalog_path).expanduser()


def write_mcp_catalog(
    config: MCPConfig,
    *,
    servers: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    replace_all: bool = True,
) -> dict[str, Any]:
    updated_at = utc_now().isoformat()
    path = _catalog_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    if replace_all:
        catalog_servers = servers
        catalog_tools = tools
    else:
        existing = load_mcp_catalog(config)
        refreshed_servers = {str(server.get("name")) for server in servers if server.get("name")}
        catalog_servers = [
            server
            for server in existing.get("servers", [])
            if isinstance(server, dict) and str(server.get("name")) not in refreshed_servers
        ]
        catalog_servers.extend(servers)
        catalog_tools = [
            tool
            for tool in existing.get("tools", [])
            if isinstance(tool, dict) and str(tool.get("server")) not in refreshed_servers
        ]
        catalog_tools.extend(tools)
    payload = {
        "updated_at": updated_at,
        "servers": catalog_servers,
        "tools": catalog_tools,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    return {"catalog_path": str(path), "catalog_updated_at": updated_at}


def load_mcp_catalog(config: MCPConfig) -> dict[str, Any]:
    path = _catalog_path(config)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def mcp_catalog_summary(config: MCPConfig, *, max_tools: int = 20) -> str:
    if not config.enabled:
        return ""
    catalog = load_mcp_catalog(config)
    tools = [tool for tool in catalog.get("tools", []) if isinstance(tool, dict) and not tool.get("disabled")]
    servers = [server for server in catalog.get("servers", []) if isinstance(server, dict)]
    if not catalog:
        if not config.servers:
            return "Configured MCP tools: MCP is enabled but no servers are configured."
        return "Configured MCP tools: catalog not built yet. Use mcp.client list_tools before choosing an MCP tool."
    if not tools:
        server_bits = ", ".join(
            f"{server.get('name')}({'healthy' if server.get('healthy') else 'unreachable'})"
            for server in servers[:8]
        )
        return f"Configured MCP tools: none currently available. Servers: {server_bits or 'none'}."
    # Each tool is printed as two explicitly labelled fields, NOT as a dotted
    # "server.tool" string. The dotted form reads naturally but is actively
    # misleading here: MCPClientInput requires `server` and `tool` as separate
    # fields, and a model shown "- fake.echo: ..." copies that whole string
    # into one of them - observed reproducibly (docs/HISTORY.md Part 2 §4
    # item 8), landing on `server="fake.echo"` one run and `tool="fake.echo"`
    # with `server` missing the next. Labelling the fields the same way the
    # schema names them removes the ambiguity at the source.
    lines = [
        f"Configured MCP tools from {config.catalog_path}",
        '(call with operation="call_tool" and BOTH the server and tool values shown below):',
    ]
    for tool in tools[:max_tools]:
        name = tool.get("name") or tool.get("tool")
        server = tool.get("server")
        description = str(tool.get("description") or "").strip()
        capability = tool.get("capability") or "terminal.run"
        risk_level = tool.get("risk_level") or "high"
        desc = f" - {description[:160]}" if description else ""
        lines.append(
            f'- server="{server}" tool="{name}"{desc}; capability={capability}; risk={risk_level}'
        )
    if len(tools) > max_tools:
        lines.append(f"- ... {len(tools) - max_tools} more MCP tool(s) omitted")
    return "\n".join(lines)


def mcp_output_text(output: dict[str, Any]) -> str:
    if output.get("result") is not None:
        return json.dumps(output["result"], ensure_ascii=False, indent=2, default=str)
    lines: list[str] = []
    if output.get("summary"):
        lines.append(str(output["summary"]))
    servers = output.get("servers")
    if isinstance(servers, list) and servers:
        lines.append("Servers:")
        for server in servers[:20]:
            if not isinstance(server, dict):
                continue
            state = "healthy" if server.get("healthy") else "unreachable"
            suffix = f" ({server.get('error')})" if server.get("error") else ""
            lines.append(f"- {server.get('name')}: {state}{suffix}")
    tools = output.get("tools")
    if isinstance(tools, list) and tools:
        lines.append("Tools:")
        for tool in tools[:50]:
            if not isinstance(tool, dict):
                continue
            name = tool.get("name") or tool.get("tool")
            desc = str(tool.get("description") or "").strip()
            suffix = f": {desc[:180]}" if desc else ""
            lines.append(f"- {tool.get('server')}.{name}{suffix}")
    return "\n".join(lines)


def _terminal_output(operation: str, output: dict[str, Any]) -> dict[str, Any]:
    return {
        "instance_id": "local-worker",
        "terminal_id": "mcp-client",
        "content": mcp_output_text(output) or output.get("summary") or f"MCP operation completed: {operation}",
        "is_final": True,
        "exit_code": 0,
        "source": "mcp_client",
    }




def register(deps: RegistryDeps, definitions: Definitions, adapters: Adapters) -> None:
    settings = deps.settings
    enabled = (
        settings.mcp.enabled
        and capability_enabled(settings, Capability.TERMINAL_RUN)
    )
    definitions.append(
        ToolDefinition(
            name="mcp.client",
            capability=Capability.TERMINAL_RUN,
            enabled=enabled,
            description="discover and call configured external MCP server tools through stdio",
            operations=("discover", "list_tools", "call_tool", "health", "install_server"),
            input_schema=MCPClientInput,
            output_schema=MCPClientOutput,
            operation_output_schemas=same_output_schema(
                ("discover", "list_tools", "call_tool", "health", "install_server"),
                MCPClientOutput,
            ),
            default_operation="list_tools",
            # A configured MCP server still runs code YBM does not author -
            # its tool's return value is not this machine's own content
            # (docs/THREAT_MODEL.md). discover/list_tools/health describe the
            # protocol/catalog itself, not a server's task-specific reply.
            operation_content_trust={"call_tool": UNTRUSTED_EXTERNAL},
            risk_resolver=lambda value: _mcp_required_risk(settings.mcp, value),
            approval_required_operations=("install_server",),
            approval_reasons={
                "install_server": "persists a new MCP server config that will run its own command/process on future calls",
            },
            examples=(
                {"operation": "list_tools"},
                # `server` and `tool` are separate fields - never a single
                # dotted "server.tool" string. The catalog summary prints them
                # as server="..." tool="..." for the same reason; see
                # mcp_catalog_summary() and docs/HISTORY.md Part 2 §4 item 8.
                {"operation": "call_tool", "server": "filesystem", "tool": "read_file", "arguments": {"path": "notes.txt"}},
                {
                    "operation": "install_server",
                    "name": "filesystem",
                    "command": "npx",
                    # A placeholder, not a real home directory: this example is
                    # shown to the model in the tool catalog and ships in a
                    # public repository, so it should not name anyone's account.
                    "args": ["-y", "@modelcontextprotocol/server-filesystem", "<a folder you allow>"],
                },
            ),
        )
    )
    if settings.mcp.enabled:
        adapters["mcp.client"] = MCPClientAdapter(settings.mcp)


def _mcp_required_risk(config: MCPConfig, value: dict[str, Any]) -> RiskLevel:
    operation = str(value.get("operation") or "list_tools")
    if operation in {"discover", "list_tools", "health"}:
        return RiskLevel.LOW
    if operation == "install_server":
        return RiskLevel.CRITICAL
    server = config.servers.get(str(value.get("server") or ""))
    return server.risk_level if server is not None else RiskLevel.HIGH
