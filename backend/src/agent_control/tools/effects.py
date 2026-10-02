"""Classifies what a tool call actually did, for receipts and evidence
(docs/archive/UI_UX_AUDIT.md Phase 14) - replacing Phase 8's "Touched during this
task" wording fix (itself a stopgap for the original, over-claiming
"Changed") with real per-item effect labels: read, created, modified,
moved, deleted, command_executed, website_visited, message_sent.

A mapping table from (tool_name, operation) to an effect kind - many
tools already distinguish this at the source (filesystem.manage's
read_file vs write_text_file vs open_file), this only surfaces it.
Anything not explicitly mapped falls back to "other" rather than
guessing - a new tool or operation added later degrades visibly instead
of being silently misclassified as something it isn't.
"""

from __future__ import annotations

EffectKind = str

_EFFECT_BY_TOOL_OPERATION: dict[tuple[str, str], EffectKind] = {
    # filesystem.manage - the tool this classification was designed
    # around: it already distinguishes read_file/open_file (read) from
    # write_text_file (modified) from apply_manifest (moved) at the source.
    ("filesystem.manage", "inspect_folder"): "read",
    ("filesystem.manage", "search"): "read",
    ("filesystem.manage", "resolve_desktop_item"): "read",
    ("filesystem.manage", "find_by_description"): "read",
    ("filesystem.manage", "read_file"): "read",
    ("filesystem.manage", "open_file"): "read",
    ("filesystem.manage", "collect_folder_snapshot"): "read",
    ("filesystem.manage", "describe_folder"): "read",
    ("filesystem.manage", "organize_plan"): "read",
    ("filesystem.manage", "rename_plan"): "read",
    ("filesystem.manage", "write_text_file"): "modified",
    ("filesystem.manage", "apply_manifest"): "moved",
    # document.manage
    ("document.manage", "inspect_document"): "read",
    ("document.manage", "extract_text"): "read",
    ("document.manage", "summarize_pdf"): "read",
    ("document.manage", "create_presentation"): "created",
    ("document.manage", "update_presentation"): "modified",
    # code.interpreter - every run operation executes generated code,
    # regardless of which of the near-identical run/repair/build/solve
    # variants requested it.
    ("code.interpreter", "run_python"): "command_executed",
    ("code.interpreter", "generate_and_run"): "command_executed",
    ("code.interpreter", "solve_once"): "command_executed",
    ("code.interpreter", "build_temp_helper"): "command_executed",
    ("code.interpreter", "repair_script"): "command_executed",
    ("code.interpreter", "inspect_state"): "read",
    ("code.interpreter", "health"): "read",
    # browser.open (navigation/read-oriented) and browser.control
    # (interaction-oriented) are two separate registered tools, not one -
    # confirmed against browser.py's own two ToolDefinitions rather than
    # assumed from the contracts.py class name prefixes, which don't
    # actually split the same way.
    ("browser.open", "open"): "website_visited",
    ("browser.open", "search"): "website_visited",
    ("browser.open", "research"): "website_visited",
    ("browser.open", "research_pages"): "website_visited",
    ("browser.open", "inspect_tabs"): "read",
    ("browser.open", "screenshot"): "read",
    ("browser.open", "summarize_page"): "read",
    ("browser.control", "navigate"): "website_visited",
    ("browser.control", "close_tab"): "other",
    ("browser.control", "click"): "other",
    ("browser.control", "fill_form"): "modified",
    ("browser.control", "fill_form_step"): "modified",
    ("browser.control", "check_page_update"): "read",
    ("browser.control", "extract_page_state"): "read",
    # computer.use's act/run_goal cover too wide a range of real-world
    # effects (a click, a form submit, a purchase) to classify as one kind
    # more specific than "other" without guessing.
    ("computer.use", "observe"): "read",
    ("computer.use", "act"): "other",
    ("computer.use", "run_goal"): "other",
    # artifact.deliver
    ("artifact.deliver", "send_file"): "message_sent",
    ("artifact.deliver", "send_latest"): "message_sent",
    ("artifact.deliver", "send_screenshot"): "message_sent",
    ("artifact.deliver", "list_artifacts"): "read",
    # schedule.manage
    ("schedule.manage", "create"): "created",
    ("schedule.manage", "list"): "read",
    ("schedule.manage", "pause"): "modified",
    ("schedule.manage", "resume"): "modified",
    ("schedule.manage", "delete"): "deleted",
    ("schedule.manage", "run_now"): "command_executed",
    # knowledge.search
    ("knowledge.search", "list_sources"): "read",
    ("knowledge.search", "search"): "read",
    # persona.manage
    ("persona.manage", "get"): "read",
    ("persona.manage", "update"): "modified",
    # skills.use
    ("skills.use", "list"): "read",
    ("skills.use", "read"): "read",
    # task.status
    ("task.status", "status"): "read",
    # mcp.client
    ("mcp.client", "discover"): "read",
    ("mcp.client", "list_tools"): "read",
    ("mcp.client", "health"): "read",
    ("mcp.client", "call_tool"): "other",
    ("mcp.client", "install_server"): "modified",
    # http.request - always an outbound network call.
    ("http.request", "request"): "website_visited",
    # memory.manage
    ("memory.manage", "remember"): "created",
    ("memory.manage", "list"): "read",
    ("memory.manage", "forget"): "deleted",
    # coding.agent - every operation but the read-only ones drives a real
    # coding session that can touch a workspace; "other" for those rather
    # than claiming a specific file effect this tool doesn't expose.
    ("coding.agent", "status"): "read",
    ("coding.agent", "limits"): "read",
    ("coding.agent", "get_latest_output"): "read",
    ("coding.agent", "start"): "other",
    ("coding.agent", "plan"): "other",
    ("coding.agent", "run_step"): "other",
    ("coding.agent", "run_goal"): "other",
    ("coding.agent", "resume"): "other",
    ("coding.agent", "stop"): "other",
    # tts.synthesize
    ("tts.synthesize", "synthesize"): "created",
    # adapter.factory
    ("adapter.factory", "assess"): "read",
    ("adapter.factory", "scaffold"): "created",
    ("adapter.factory", "sandbox_execute_once"): "command_executed",
    ("adapter.factory", "test_connector"): "read",
    ("adapter.factory", "promote_after_approval"): "modified",
    # workspace.manage
    ("workspace.manage", "prepare"): "created",
    ("workspace.manage", "write_files"): "modified",
    ("workspace.manage", "materialize_static_app"): "created",
    ("workspace.manage", "launch_static"): "command_executed",
    ("workspace.manage", "web_app_preview"): "read",
}

# Tools whose input has no `operation` field at all - confirmed against
# their contracts (CodingAssistantInput, VSCodeCopilotTerminalInput,
# VSCodeTerminalCommandInput carry no such field), not assumed - so there
# is nothing to look up in the table above; every call to these always
# has the one effect listed here.
_EFFECT_BY_TOOL: dict[str, EffectKind] = {
    "coding_assistant": "command_executed",
    "vscode.terminal_command": "command_executed",
    "vscode.copilot_terminal": "command_executed",
}


def classify_effect(tool_name: str, operation: str | None) -> EffectKind:
    if operation:
        effect = _EFFECT_BY_TOOL_OPERATION.get((tool_name, operation))
        if effect is not None:
            return effect
    return _EFFECT_BY_TOOL.get(tool_name, "other")
