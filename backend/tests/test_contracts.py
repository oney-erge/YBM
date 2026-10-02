"""Unit tests for tool input/output contracts.

The contracts file holds the per-operation Pydantic schemas that the registry
validates plans and tool outputs against. Most schemas are pure shape
definitions covered transitively by tool tests; this file targets the
cross-cutting model_validators that encode operation-specific constraints,
because those are the ones whose failure modes the planner sees as
"validation error" strings.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from agent_control.tools.contracts import (
    ArtifactDeliverInput,
    BrowserCheckPageUpdateInput,
    BrowserClickInput,
    BrowserFillFormInput,
    BrowserResearchPagesInput,
    CodeInterpreterGenerateAndRunInput,
    CodeInterpreterRunPythonInput,
    CodingAgentInput,
    ComputerActInput,
    ComputerRunGoalInput,
    DocumentManageInput,
    FilesystemInspectInput,
    FilesystemReadFileInput,
    FilesystemSearchInput,
    FilesystemWriteTextFileInput,
    HttpRequestInput,
    MCPClientInput,
    ScheduleManageInput,
    VSCodeCopilotTerminalInput,
    VSCodeTerminalCommandInput,
)


# ----- Shared base behavior (ToolInputModel) --------------------------------


def test_scope_target_and_timeout_seconds_are_optional() -> None:
    # Defaults are None; concrete inputs only require their own required fields.
    model = FilesystemReadFileInput(path="/x")
    assert model.scope_target is None
    assert model.timeout_seconds is None


def test_unknown_operation_literal_rejected() -> None:
    with pytest.raises(ValidationError):
        ArtifactDeliverInput(operation="upload_to_drive")


def test_filesystem_write_text_accepts_text_alias_without_losing_content() -> None:
    model = FilesystemWriteTextFileInput(
        operation="write_text_file",
        path="report.md",
        text="Evidence-backed report",
    )

    assert model.content == "Evidence-backed report"


def test_filesystem_inspect_combines_allowed_root_and_relative_folder_path(tmp_path) -> None:
    model = FilesystemInspectInput(
        operation="inspect_folder",
        root=str(tmp_path),
        folder_path="nested/evidence",
    )

    assert Path(model.root) == tmp_path / "nested" / "evidence"


# ----- artifact.deliver -----------------------------------------------------


def test_artifact_deliver_send_file_requires_path_or_id() -> None:
    with pytest.raises(ValidationError) as excinfo:
        ArtifactDeliverInput(operation="send_file")
    assert "requires" in str(excinfo.value).lower()


def test_artifact_deliver_send_latest_does_not_require_target() -> None:
    # Default operation; no fields needed.
    model = ArtifactDeliverInput()
    assert model.operation == "send_latest"


def test_artifact_deliver_send_file_accepts_path() -> None:
    model = ArtifactDeliverInput(operation="send_file", path="report.xlsx")
    assert model.path == "report.xlsx"


# ----- document.manage ------------------------------------------------------


def test_document_manage_read_ops_require_path_or_id() -> None:
    for op in ("inspect_document", "extract_text", "summarize_pdf", "update_presentation"):
        with pytest.raises(ValidationError) as excinfo:
            DocumentManageInput(operation=op)
        assert "identify the document" in str(excinfo.value)


def test_document_manage_create_presentation_requires_at_least_one_field() -> None:
    with pytest.raises(ValidationError):
        DocumentManageInput(operation="create_presentation")
    # any one is enough:
    DocumentManageInput(operation="create_presentation", title="t")
    DocumentManageInput(operation="create_presentation", content="c")
    DocumentManageInput(operation="create_presentation", instructions="i")


# ----- coding.agent ---------------------------------------------------------


def test_coding_agent_run_ops_require_prompt_or_objective() -> None:
    for op in ("plan", "run_step", "run_goal"):
        with pytest.raises(ValidationError) as excinfo:
            CodingAgentInput(operation=op, provider="codex")
        assert "prompt" in str(excinfo.value)


def test_coding_agent_status_does_not_require_objective() -> None:
    CodingAgentInput(operation="status", provider="codex")


def test_coding_agent_unknown_provider_rejected() -> None:
    with pytest.raises(ValidationError):
        CodingAgentInput(operation="status", provider="anthropic-cli")


# ----- schedule.manage ------------------------------------------------------


def test_schedule_create_requires_objective() -> None:
    with pytest.raises(ValidationError):
        ScheduleManageInput(operation="create")
    ScheduleManageInput(operation="create", objective="daily news")


def test_schedule_pause_resume_delete_require_schedule_id() -> None:
    for op in ("pause", "resume", "delete", "run_now"):
        with pytest.raises(ValidationError):
            ScheduleManageInput(operation=op)


def test_schedule_list_has_no_required_args() -> None:
    ScheduleManageInput(operation="list")


# ----- browser.* validators -------------------------------------------------


def test_browser_click_requires_selector_or_text() -> None:
    with pytest.raises(ValidationError):
        BrowserClickInput()
    BrowserClickInput(selector="button.submit")
    BrowserClickInput(text="Submit")


def test_browser_fill_form_rejects_empty_fields() -> None:
    with pytest.raises(ValidationError):
        BrowserFillFormInput(fields={})
    BrowserFillFormInput(fields={"name": "x"})


def test_browser_check_page_update_requires_url_or_objective() -> None:
    with pytest.raises(ValidationError):
        BrowserCheckPageUpdateInput()
    BrowserCheckPageUpdateInput(url="https://example.com")


def test_browser_research_pages_requires_query_or_objective() -> None:
    with pytest.raises(ValidationError):
        BrowserResearchPagesInput()
    BrowserResearchPagesInput(query="python release notes")


def test_browser_research_pages_page_limit_bounded() -> None:
    with pytest.raises(ValidationError):
        BrowserResearchPagesInput(query="x", page_limit=0)
    with pytest.raises(ValidationError):
        BrowserResearchPagesInput(query="x", page_limit=51)
    BrowserResearchPagesInput(query="x", page_limit=10)


# ----- http.request ---------------------------------------------------------


def test_http_request_requires_one_body_shape() -> None:
    HttpRequestInput(url="https://api.example.com/status")
    with pytest.raises(ValidationError):
        HttpRequestInput(url="https://api.example.com/status", json_body={}, body="x")


# ----- computer.use ---------------------------------------------------------


def test_computer_act_requires_non_empty_action() -> None:
    with pytest.raises(ValidationError):
        ComputerActInput(action={})
    ComputerActInput(action={"type": "click", "x": 10, "y": 20})


def test_computer_run_goal_requires_objective() -> None:
    with pytest.raises(ValidationError):
        ComputerRunGoalInput(objective="")
    ComputerRunGoalInput(objective="open notepad")


# ----- code interpreter & filesystem ---------------------------------------


def test_code_interpreter_run_python_requires_code() -> None:
    with pytest.raises(ValidationError):
        CodeInterpreterRunPythonInput()
    CodeInterpreterRunPythonInput(code="print(1)")


def test_code_interpreter_generate_and_run_requires_objective() -> None:
    with pytest.raises(ValidationError):
        CodeInterpreterGenerateAndRunInput()
    CodeInterpreterGenerateAndRunInput(objective="compute fib(20)")


def test_filesystem_search_requires_root_and_query() -> None:
    with pytest.raises(ValidationError):
        FilesystemSearchInput(query="x")
    with pytest.raises(ValidationError):
        FilesystemSearchInput(root="/tmp")
    FilesystemSearchInput(root="/tmp", query="x")


# ----- vscode copilot validator --------------------------------------------


def test_vscode_copilot_requires_prompt_or_command() -> None:
    with pytest.raises(ValidationError):
        VSCodeCopilotTerminalInput()
    VSCodeCopilotTerminalInput(prompt="explain this")
    VSCodeCopilotTerminalInput(command="git status")


def test_vscode_terminal_command_requires_non_empty_command() -> None:
    with pytest.raises(ValidationError):
        VSCodeTerminalCommandInput(command="")
    VSCodeTerminalCommandInput(command="ls")


# ----- mcp.client -------------------------------------------------------


def test_mcp_client_call_tool_requires_server_and_tool() -> None:
    with pytest.raises(ValidationError):
        MCPClientInput(operation="call_tool")
    MCPClientInput(operation="call_tool", server="fake", tool="echo")


def test_mcp_client_dict_args_rejected_with_actionable_message() -> None:
    # docs/archive/HISTORY.md Part 4's re-recording note: 'args' (install_server's
    # list[str] of command-line arguments) and 'arguments' (call_tool's
    # dict of tool arguments) are easy to conflate - reproduced live across
    # three independent recordings of a local 8B model repeating the
    # mistake with zero self-correction against Pydantic's raw "should be a
    # valid list" message. This pins the clearer message in its place.
    with pytest.raises(ValidationError) as excinfo:
        MCPClientInput(operation="call_tool", server="fake", tool="echo", args={"text": "hi"})
    assert "arguments" in str(excinfo.value)
    # A real list (install_server's actual shape) is untouched.
    MCPClientInput(operation="install_server", name="fake", command="python", args=["server.py"])
