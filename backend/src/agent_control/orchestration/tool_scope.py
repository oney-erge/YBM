"""Show the Operator only the tools that can matter for the kind of request it is handling.

The Operator picks one of about 25 tools (and one of their operations) at every step from a ~4,000-token catalog. A
small local model does that less reliably the more it has to choose from, and re-reads the whole catalog each time.
The Concierge has already said what kind of request this is (``orchestration_intent.route``), so the catalog can list the
handful of tools that route needs instead of all of them. This is a prompt change only: every registered tool still
runs if the model names it, and a task that is going badly gets the full catalog back.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

# Tools that stay in every scoped catalog: cheap, general, and the way out of a bad guess.
ALWAYS = frozenset({"skills.use", "knowledge.search", "task.status", "memory.manage", "persona.manage"})

#: route -> the tools its requests use, besides ALWAYS. A route absent here (conversation, status, unknown) gets the full
#: catalog. "configuration" is what "remember that my dog is called Biscuit" is classified as: its tools are all in ALWAYS
#: (memory.manage, persona.manage), and with the whole catalog in front of it the local model answered such a request by
#: reading a file named in an earlier turn of the conversation.
ROUTE_TOOLS: dict[str, frozenset[str]] = {
    "configuration": frozenset(),
    "filesystem.manage": frozenset({"filesystem.manage", "document.manage", "artifact.deliver", "code.interpreter"}),
    "document.manage": frozenset({"document.manage", "filesystem.manage", "artifact.deliver", "code.interpreter"}),
    "artifact.deliver": frozenset({"artifact.deliver", "filesystem.manage", "document.manage", "browser.open"}),
    "schedule.manage": frozenset({"schedule.manage", "artifact.deliver"}),
    "browser.open": frozenset({"browser.open", "browser.control", "web.search", "http.request", "artifact.deliver", "document.manage"}),
    "browser.control": frozenset({"browser.control", "browser.open", "web.search", "http.request", "artifact.deliver"}),
    "desktop.observe": frozenset({"computer.use", "browser.open", "artifact.deliver", "filesystem.manage"}),
    "computer.use": frozenset({"computer.use", "browser.open", "artifact.deliver", "filesystem.manage"}),
    "code.interpreter": frozenset({"code.interpreter", "filesystem.manage", "document.manage", "artifact.deliver", "dependencies.install", "workspace.manage"}),
    "coding.agent": frozenset({"coding.agent", "filesystem.manage", "workspace.manage", "artifact.deliver"}),
    "workspace.manage": frozenset({"workspace.manage", "filesystem.manage", "code.interpreter", "coding.agent", "browser.open"}),
    "adapter.factory": frozenset({"adapter.factory", "mcp.client", "http.request", "filesystem.manage", "code.interpreter"}),
}
# MCP servers' own tools are listed in the catalog too; only these routes can use them.
MCP_ROUTES = frozenset({"adapter.factory"})

_FAILED = {"failed", "denied", "already_done", "audit_gap", "fulfillment_gap", "clarification_gap"}
# After this many steps that went wrong the narrowed catalog is dropped: the route may have been the wrong guess.
FULL_CATALOG_AFTER_FAILURES = 2


def tools_for_route(route: str | None) -> frozenset[str] | None:
    """The tool names to list for a route, or None for the full catalog."""
    tools = ROUTE_TOOLS.get(str(route or ""))
    return None if tools is None else tools | ALWAYS


def scope_for_task(metadata: dict[str, Any]) -> frozenset[str] | None:
    """The tools to show for a task right now, or None for everything."""
    intent = metadata.get("orchestration_intent")
    route = intent.get("route") if isinstance(intent, dict) else None
    failures = sum(
        1 for entry in metadata.get("operator_history") or []
        if isinstance(entry, dict) and str(entry.get("status") or "") in _FAILED
    )
    if failures >= FULL_CATALOG_AFTER_FAILURES:
        return None
    return tools_for_route(route)


_TOOL_LINE = re.compile(r"^- ([a-z][\w.]*) \(")


def scope_catalog_text(config_context: str, allowed: Iterable[str] | None, route: str | None = None) -> str:
    """Narrow an already-rendered catalog to ``allowed`` tool names (the text form of ToolRegistry.context(only=...)).

    Keeps the heading, the blocks of allowed tools with their examples, the line naming disabled tools, and anything
    that is not a tool block. MCP server tool lines are kept only for routes in MCP_ROUTES.
    """
    if allowed is None:
        return config_context
    keep = set(allowed)
    out: list[str] = []
    skipping = False
    for line in config_context.splitlines():
        match = _TOOL_LINE.match(line)
        if match:
            skipping = match.group(1) not in keep
        elif line.startswith("- server="):
            skipping = str(route or "") not in MCP_ROUTES
        elif line and not line.startswith((" ", "\t")):
            skipping = False  # a heading, the disabled-tools line or the closing advice
        if not skipping:
            out.append(line)
    return "\n".join(out)
