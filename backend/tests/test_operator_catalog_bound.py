"""The tool catalog is bounded without losing a tool (orchestration/operator.py, _format_config_context)."""

from __future__ import annotations

import re

from agent_control.orchestration.operator import _MAX_CONFIG_PROMPT_CHARS, _format_config_context

TOOLS = [
    "workspace.manage", "web.search", "filesystem.manage", "adapter.factory", "code.interpreter", "http.request", "mcp.client",
    "coding.agent", "knowledge.search", "persona.manage", "memory.manage", "schedule.manage", "skills.use", "task.status",
    "system.inventory", "artifact.deliver", "document.manage", "computer.use", "browser.open", "browser.control",
]


def _catalog(*, mcp_tools: int, description_chars: int = 90, example_chars: int = 200) -> str:
    lines = ["Available worker tools:"]
    for name in TOOLS:
        lines.append(
            f"- {name} (some.capability): does the thing this tool does, in a sentence that is about eighty characters long; "
            f"lifecycle=ready; operations=one,two,three"
        )
        lines.append("    example tool_input: " + '{"operation": "one", "x": "' + "y" * example_chars + '"}')
    lines.append("")
    lines.append("Configured MCP tools from .agent_control/mcp/tool_catalog.json")
    lines.append('(call with operation="call_tool" and BOTH the server and tool values shown below):')
    for index in range(mcp_tools):
        lines.append(f'- server="ybm" tool="tool_{index}" - {"d" * description_chars}; capability=terminal.run; risk=high')
    lines.append("")
    lines.append("Prefer conservative plans. Use registered tool names exactly.")
    return "\n".join(lines)


def _tool_names(text: str) -> list[str]:
    return re.findall(r"(?m)^- ([a-z_]+\.[a-z_]+) ", text)


def test_a_catalog_that_fits_is_returned_unchanged():
    catalog = _catalog(mcp_tools=2, example_chars=5)
    assert len(catalog) <= _MAX_CONFIG_PROMPT_CHARS
    assert _format_config_context(catalog, _MAX_CONFIG_PROMPT_CHARS) == catalog


def test_the_last_tools_survive_when_the_catalog_is_only_a_little_over_the_limit():
    # the live failure: after the examples were dropped the catalog was 136 characters over, and the last three tools were cut
    catalog = _catalog(mcp_tools=20, example_chars=150)
    without_examples = "\n".join(line for line in catalog.splitlines() if "example tool_input" not in line)
    limit = len(without_examples) - 136
    assert len(without_examples) > limit
    bounded = _format_config_context(catalog, limit)
    assert len(bounded) <= limit
    assert _tool_names(bounded) == TOOLS
    assert "browser.control" in bounded and "computer.use" in bounded


def test_mcp_tools_give_up_detail_before_any_native_tool_does():
    catalog = _catalog(mcp_tools=20, example_chars=150)
    without_examples = "\n".join(line for line in catalog.splitlines() if "example tool_input" not in line)
    bounded = _format_config_context(catalog, len(without_examples) - 600)
    assert _tool_names(bounded) == TOOLS
    assert "capability=terminal.run" not in bounded  # the suffix on MCP lines goes first
    assert 'server="ybm" tool="tool_0"' in bounded and 'tool="tool_19"' in bounded  # every MCP tool is still named


def test_when_even_that_is_not_enough_mcp_tools_are_left_out_last_first_and_the_native_tools_stay():
    catalog = _catalog(mcp_tools=60, example_chars=300)
    bounded = _format_config_context(catalog, 4_000)
    assert _tool_names(bounded) == TOOLS
    assert "more MCP tool(s) left out here" in bounded
    assert 'tool="tool_0"' in bounded and 'tool="tool_59"' not in bounded


def test_the_real_catalog_shape_fits_with_every_tool(tmp_path):
    # 22 native tools, 20 MCP tools whose descriptions end in a capability and risk suffix, as a machine with two servers prints it
    catalog = _catalog(mcp_tools=20, example_chars=190)
    assert len(catalog) > _MAX_CONFIG_PROMPT_CHARS
    bounded = _format_config_context(catalog, _MAX_CONFIG_PROMPT_CHARS)
    assert len(bounded) <= _MAX_CONFIG_PROMPT_CHARS
    assert _tool_names(bounded) == TOOLS
