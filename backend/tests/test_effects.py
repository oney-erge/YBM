"""docs/archive/UI_UX_AUDIT.md Phase 14: real per-item effect classification,
replacing "touched" with what actually happened - a tool's own operation
name already distinguishes this at the source in most cases.
"""

from __future__ import annotations

from agent_control.tools.effects import classify_effect


def test_filesystem_read_file_classifies_as_read() -> None:
    assert classify_effect("filesystem.manage", "read_file") == "read"


def test_filesystem_write_text_file_classifies_as_modified() -> None:
    assert classify_effect("filesystem.manage", "write_text_file") == "modified"


def test_filesystem_apply_manifest_classifies_as_moved() -> None:
    assert classify_effect("filesystem.manage", "apply_manifest") == "moved"


def test_memory_forget_classifies_as_deleted() -> None:
    assert classify_effect("memory.manage", "forget") == "deleted"


def test_artifact_send_file_classifies_as_message_sent() -> None:
    assert classify_effect("artifact.deliver", "send_file") == "message_sent"


def test_http_request_classifies_as_website_visited() -> None:
    assert classify_effect("http.request", "request") == "website_visited"


def test_code_interpreter_run_python_classifies_as_command_executed() -> None:
    assert classify_effect("code.interpreter", "run_python") == "command_executed"


def test_unmapped_tool_falls_back_to_other_not_a_guess() -> None:
    assert classify_effect("some.future_tool", "does_something_new") == "other"


def test_missing_operation_falls_back_to_other_for_a_tool_with_no_default() -> None:
    assert classify_effect("filesystem.manage", None) == "other"


def test_browser_open_and_browser_control_are_separate_tools_not_one() -> None:
    """Regression test: these were originally conflated into one
    "browser.control" entry, assumed from the contracts.py class name
    prefixes rather than verified against browser.py's own two
    ToolDefinitions, which split operations differently than the class
    names suggest."""
    assert classify_effect("browser.open", "search") == "website_visited"
    assert classify_effect("browser.control", "fill_form") == "modified"
    assert classify_effect("browser.control", "search") == "other"


def test_a_tool_with_no_operation_field_still_classifies_by_tool_name() -> None:
    """coding_assistant, vscode.terminal_command, and vscode.copilot_terminal
    have no `operation` field on their input at all - every call to them
    has the same one effect, so they classify from the tool name alone."""
    assert classify_effect("coding_assistant", None) == "command_executed"
    assert classify_effect("vscode.terminal_command", None) == "command_executed"
