from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ToolInputModel(BaseModel):
    model_config = ConfigDict(
        extra="allow",
        str_strip_whitespace=True,
        validate_assignment=True,
    )

    scope_target: str | None = None
    timeout_seconds: int | None = Field(default=None, ge=1)


class ToolOutputModel(BaseModel):
    model_config = ConfigDict(
        extra="allow",
        str_strip_whitespace=True,
        validate_assignment=True,
    )

    operation: str | None = None
    terminal_output: list[dict[str, Any]] = Field(default_factory=list)


class WorkspaceFileInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    path: str = Field(min_length=1)
    content: str = ""


class WorkspacePrepareInput(ToolInputModel):
    operation: Literal["prepare"] = "prepare"
    objective: str | None = None
    refresh_task_file: bool = True


class WorkspaceWriteFilesInput(ToolInputModel):
    operation: Literal["write_files"] = "write_files"
    objective: str | None = None
    files: list[WorkspaceFileInput] = Field(min_length=1)


class WorkspaceMaterializeStaticAppInput(ToolInputModel):
    operation: Literal["materialize_static_app"] = "materialize_static_app"
    objective: str | None = None
    prompt: str | None = None
    source_text: str | None = None
    assistant_output: str | None = None
    allow_fallback_template: bool = True
    require_index: bool = False
    overwrite: bool = False


class WorkspaceLaunchStaticInput(ToolInputModel):
    operation: Literal["launch_static"] = "launch_static"
    objective: str | None = None
    prompt: str | None = None
    web_port_start: int | None = Field(default=None, ge=1, le=65535)
    open_browser: bool | None = None
    ensure_index: bool = True


class WorkspaceWebAppPreviewInput(ToolInputModel):
    operation: Literal["web_app_preview"] = "web_app_preview"
    objective: str | None = None
    prompt: str | None = None
    web_port_start: int | None = Field(default=None, ge=1, le=65535)
    open_browser: bool | None = None


class AdapterFactoryAssessInput(ToolInputModel):
    operation: Literal["assess"] = "assess"
    objective: str | None = None
    prompt: str | None = None
    adapter_name: str | None = None


class AdapterFactoryScaffoldInput(ToolInputModel):
    operation: Literal["scaffold"] = "scaffold"
    objective: str | None = None
    prompt: str | None = None
    adapter_name: str | None = None
    name: str | None = None
    description: str | None = None
    tool_name: str | None = None
    capability: str | None = None
    operations: list[str] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)
    api_endpoint: str | None = None
    auto_load: bool = False


class AdapterFactorySandboxExecuteInput(ToolInputModel):
    operation: Literal["sandbox_execute_once"] = "sandbox_execute_once"
    adapter_dir: str | None = None
    adapter_id: str | None = None
    objective: str | None = None
    code: str | None = None
    dry_run: bool = True


class AdapterFactoryTestConnectorInput(ToolInputModel):
    operation: Literal["test_connector"] = "test_connector"
    adapter_dir: str = Field(min_length=1)


class AdapterFactoryPromoteInput(ToolInputModel):
    operation: Literal["promote_after_approval"] = "promote_after_approval"
    adapter_dir: str = Field(min_length=1)
    approved: bool = False


class VSCodeCopilotTerminalInput(ToolInputModel):
    prompt: str | None = None
    command: str | None = None
    terminal_id: str = "agent-control-copilot"
    instance_id: str | None = None
    cwd: str | None = None
    capture_output: bool = True
    allow_local_fallback: bool = True

    @model_validator(mode="after")
    def prompt_or_command_required(self) -> "VSCodeCopilotTerminalInput":
        if not (self.prompt or self.command):
            raise ValueError("prompt or command is required")
        return self


class VSCodeTerminalCommandInput(ToolInputModel):
    command: str = Field(min_length=1)
    terminal_id: str = "agent-control"
    instance_id: str | None = None
    cwd: str | None = None
    capture_output: bool = True


class CodingAssistantInput(ToolInputModel):
    prompt: str = Field(min_length=1)


class TTSSynthesizeInput(ToolInputModel):
    operation: Literal["synthesize"] = "synthesize"
    text: str = Field(min_length=1)
    voice: str | None = None
    output_name: str | None = None


class ArtifactDeliverInput(ToolInputModel):
    operation: Literal["send_file", "send_latest", "send_screenshot", "list_artifacts"] = "send_latest"
    artifact_id: str | None = None
    path: str | None = None
    artifact_type: str | None = None
    chat_id: str | None = None
    caption: str | None = None
    mime_type: str | None = None

    @model_validator(mode="after")
    def _require_target_for_send_file(self) -> "ArtifactDeliverInput":
        # send_file needs SOMETHING pointing at the file. Without this, the
        # adapter raises ValueError at execute time and the planner has to
        # replan from scratch. Encoding the constraint here makes the planner
        # see a precise schema error instead.
        if self.operation == "send_file" and not (self.artifact_id or self.path):
            raise ValueError(
                "send_file requires 'path' (file path or basename of a prior step's artifact) "
                "or 'artifact_id'"
            )
        return self


class DocumentManageInput(ToolInputModel):
    operation: Literal["inspect_document", "extract_text", "summarize_pdf", "create_presentation", "update_presentation"] = "inspect_document"
    path: str | None = None
    artifact_id: str | None = None
    title: str | None = None
    content: str | None = None
    instructions: str | None = None
    output_name: str | None = None

    @model_validator(mode="after")
    def _require_inputs_per_operation(self) -> "DocumentManageInput":
        # Operations that read an existing document need to know which one.
        # Operations that create one need at least a title or content to work with.
        op = self.operation
        if op in {"inspect_document", "extract_text", "summarize_pdf", "update_presentation"} and not (
            self.path or self.artifact_id
        ):
            raise ValueError(
                f"{op} requires 'path' or 'artifact_id' to identify the document"
            )
        if op == "create_presentation" and not (self.title or self.content or self.instructions):
            raise ValueError(
                "create_presentation requires at least one of 'title', 'content', or 'instructions'"
            )
        return self


class CodingAgentInput(ToolInputModel):
    operation: Literal[
        "start", "plan", "run_step", "run_goal", "status", "limits", "resume", "stop", "get_latest_output"
    ] = "run_goal"
    # Provider is required to start a run; status/stop/get_latest_output can
    # omit it and resolve the latest session.
    provider: Literal["codex", "github_copilot", "claude_code"] | None = None
    prompt: str | None = None
    objective: str | None = None
    workspace_dir: str | None = None
    session_id: str | None = None
    step_index: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _require_input_for_run_ops(self) -> "CodingAgentInput":
        if self.operation in {"start", "plan", "run_step", "run_goal", "resume"}:
            if not self.provider:
                raise ValueError(f"coding.agent {self.operation} requires 'provider'")
            if not (self.prompt or self.objective):
                raise ValueError(f"coding.agent {self.operation} requires 'prompt' or 'objective'")
        return self


class ScheduleManageInput(ToolInputModel):
    operation: Literal["create", "list", "pause", "resume", "delete", "run_now"] = "create"
    schedule_id: str | None = None
    objective: str | None = None
    cadence: str | None = None
    timezone: str | None = None
    source_chat_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _require_inputs_per_operation(self) -> "ScheduleManageInput":
        if self.operation == "create" and not self.objective:
            raise ValueError("schedule.manage create requires 'objective'")
        if self.operation in {"pause", "resume", "delete", "run_now"} and not self.schedule_id:
            raise ValueError(f"schedule.manage {self.operation} requires 'schedule_id'")
        return self


class KnowledgeBaseInput(ToolInputModel):
    operation: Literal["list_sources", "search"] = "search"
    # Required for operation=search - the search query.
    query: str | None = None

    @model_validator(mode="after")
    def search_requires_query(self) -> "KnowledgeBaseInput":
        if self.operation == "search" and not (self.query or "").strip():
            raise ValueError("operation=search requires 'query'")
        return self


class PersonaInput(ToolInputModel):
    operation: Literal["get", "update"] = "get"
    # Required for operation=update - the complete new persona content,
    # replacing what's there now (read it first via operation=get).
    content: str | None = None

    @model_validator(mode="after")
    def update_requires_content(self) -> "PersonaInput":
        if self.operation == "update" and not (self.content or "").strip():
            raise ValueError("operation=update requires 'content'")
        return self


class WebSearchInput(ToolInputModel):
    operation: Literal["search"] = "search"
    query: str = Field(min_length=1, max_length=500)
    max_results: int = Field(default=5, ge=1, le=25)


class DependenciesInstallInput(ToolInputModel):
    operation: Literal["install", "list_allowed"] = "install"
    # Plain `name` or `name==version` only. URLs, local paths and VCS refs are
    # rejected by the adapter - each is a way to fetch code the allowlist never
    # described.
    packages: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def install_requires_packages(self) -> "DependenciesInstallInput":
        if self.operation == "install" and not self.packages:
            raise ValueError("dependencies.install install requires a non-empty 'packages' list")
        return self


class SkillsInput(ToolInputModel):
    operation: Literal["list", "read"] = "list"
    # Required for operation=read - which skill's full body to load. Optional
    # for operation=list, which never needs one (docs/archive/HISTORY.md Part 4 T1.3).
    name: str | None = None

    @model_validator(mode="after")
    def read_requires_name(self) -> "SkillsInput":
        if self.operation == "read" and not (self.name or "").strip():
            raise ValueError("operation=read requires 'name'")
        return self


class TaskStatusInput(ToolInputModel):
    operation: Literal["status"] = "status"
    limit: int = Field(default=10, ge=1, le=50)


class BrowserOpenInput(ToolInputModel):
    operation: Literal["open"] = "open"
    url: str | None = None
    query: str | None = None
    objective: str | None = None
    new_tab: bool = True
    wait_seconds: float | None = Field(default=None, ge=0.0, le=30.0)


class BrowserSearchInput(ToolInputModel):
    operation: Literal["search"] = "search"
    query: str | None = None
    objective: str | None = None
    open_first_result: bool = False
    wait_seconds: float | None = Field(default=None, ge=0.0, le=30.0)

    @model_validator(mode="after")
    def _require_query_or_objective(self) -> "BrowserSearchInput":
        if not (self.query or self.objective):
            raise ValueError("browser.search requires 'query' or 'objective'")
        return self


class BrowserResearchInput(ToolInputModel):
    operation: Literal["research"] = "research"
    objective: str = Field(min_length=1)
    url: str | None = None
    query: str | None = None
    open_first_result: bool | None = None
    screenshot: bool = False
    wait_seconds: float | None = Field(default=None, ge=0.0, le=30.0)


class BrowserInspectTabsInput(ToolInputModel):
    operation: Literal["inspect_tabs"] = "inspect_tabs"
    include_text: bool = False
    max_tabs: int = Field(default=8, ge=1, le=30)


class BrowserScreenshotInput(ToolInputModel):
    operation: Literal["screenshot"] = "screenshot"
    url: str | None = None
    tab_id: str | None = None
    filename: str | None = None
    full_page: bool = True
    wait_seconds: float | None = Field(default=None, ge=0.0, le=30.0)


class BrowserNavigateInput(ToolInputModel):
    operation: Literal["navigate"] = "navigate"
    url: str = Field(min_length=1)
    tab_id: str | None = None
    wait_seconds: float | None = Field(default=None, ge=0.0, le=30.0)


class BrowserCloseTabInput(ToolInputModel):
    operation: Literal["close_tab"] = "close_tab"
    tab_id: str | None = None
    url_contains: str | None = None
    title_contains: str | None = None


class BrowserClickInput(ToolInputModel):
    operation: Literal["click"] = "click"
    selector: str | None = None
    text: str | None = None
    tab_id: str | None = None
    wait_seconds: float | None = Field(default=None, ge=0.0, le=30.0)

    @model_validator(mode="after")
    def _require_selector_or_text(self) -> "BrowserClickInput":
        if not (self.selector or self.text):
            raise ValueError("browser.click requires 'selector' (CSS) or 'text' (visible text to match)")
        return self


class BrowserFillFormInput(ToolInputModel):
    operation: Literal["fill_form"] = "fill_form"
    # fields is logically required for fill_form - encoding that here instead
    # of in the browser adapter's runtime check eliminates a class of replans.
    fields: dict[str, str] = Field(..., min_length=1)
    submit: bool = False
    submit_selector: str | None = None
    tab_id: str | None = None
    wait_seconds: float | None = Field(default=None, ge=0.0, le=30.0)


class BrowserSummarizePageInput(ToolInputModel):
    operation: Literal["summarize_page"] = "summarize_page"
    tab_id: str | None = None
    url: str | None = None
    url_contains: str | None = None
    title_contains: str | None = None
    wait_seconds: float | None = Field(default=None, ge=0.0, le=30.0)


class BrowserCheckPageUpdateInput(ToolInputModel):
    operation: Literal["check_page_update"] = "check_page_update"
    url: str | None = None
    objective: str | None = None
    previous_observation: str | None = None
    wait_seconds: float | None = Field(default=None, ge=0.0, le=30.0)

    @model_validator(mode="after")
    def _require_target(self) -> "BrowserCheckPageUpdateInput":
        if not (self.url or self.objective):
            raise ValueError("browser.check_page_update requires 'url' or 'objective' to identify the page")
        return self


class BrowserResearchPagesInput(ToolInputModel):
    operation: Literal["research_pages"] = "research_pages"
    query: str | None = None
    objective: str | None = None
    page_limit: int = Field(default=10, ge=1, le=50)
    wait_seconds: float | None = Field(default=None, ge=0.0, le=30.0)

    @model_validator(mode="after")
    def _require_query_or_objective(self) -> "BrowserResearchPagesInput":
        if not (self.query or self.objective):
            raise ValueError("browser.research_pages requires 'query' or 'objective'")
        return self


class BrowserExtractPageStateInput(ToolInputModel):
    operation: Literal["extract_page_state"] = "extract_page_state"
    tab_id: str | None = None
    url: str | None = None
    url_contains: str | None = None
    title_contains: str | None = None
    wait_seconds: float | None = Field(default=None, ge=0.0, le=30.0)


class BrowserFillFormStepInput(ToolInputModel):
    operation: Literal["fill_form_step"] = "fill_form_step"
    fields: dict[str, str] = Field(default_factory=dict)
    submit: bool = False
    submit_selector: str | None = None
    tab_id: str | None = None
    url_contains: str | None = None
    title_contains: str | None = None
    wait_seconds: float | None = Field(default=None, ge=0.0, le=30.0)


class ComputerObserveInput(ToolInputModel):
    operation: Literal["observe"] = "observe"
    objective: str | None = None
    include_screenshot: bool = True
    include_ui_tree: bool = True
    summarize: bool = True


class ComputerActInput(ToolInputModel):
    operation: Literal["act"] = "act"
    objective: str | None = None
    action: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _require_action(self) -> "ComputerActInput":
        if not self.action:
            raise ValueError("computer.use act requires an 'action' dict describing the UI action")
        return self


class ComputerRunGoalInput(ToolInputModel):
    operation: Literal["run_goal"] = "run_goal"
    objective: str = Field(min_length=1)
    max_steps: int | None = Field(default=None, ge=1, le=50)
    include_ui_tree: bool = True
    require_vision: bool = True


class FilesystemInspectInput(ToolInputModel):
    operation: Literal["inspect_folder"] = "inspect_folder"
    root: str = Field(min_length=1)
    max_depth: int = Field(default=2, ge=0, le=10)
    max_entries: int = Field(default=200, ge=1, le=5000)

    @model_validator(mode="before")
    @classmethod
    def _normalize_folder_path(cls, value: Any) -> Any:
        # Some clients split a target into an allowed ``root`` plus a relative
        # ``folder_path``. The adapter historically ignored the latter and
        # inspected the entire allowed root, which can look successful while
        # making no progress toward the requested folder.
        if not isinstance(value, dict) or not value.get("folder_path"):
            return value
        normalized = dict(value)
        folder = Path(str(normalized.pop("folder_path")))
        parent = normalized.get("root")
        normalized["root"] = str(folder if folder.is_absolute() or not parent else Path(str(parent)) / folder)
        return normalized


class FilesystemSearchInput(ToolInputModel):
    operation: Literal["search"] = "search"
    root: str = Field(min_length=1)
    query: str = Field(min_length=1)
    include_content: bool = False
    max_results: int = Field(default=100, ge=1, le=1000)


class FilesystemResolveDesktopItemInput(ToolInputModel):
    operation: Literal["resolve_desktop_item"] = "resolve_desktop_item"
    name: str | None = None
    query: str | None = None
    item_type: Literal["file", "folder", "any"] = "any"


class FilesystemFindByDescriptionInput(ToolInputModel):
    operation: Literal["find_by_description"] = "find_by_description"
    root: str | None = None
    description: str = Field(min_length=1)
    max_results: int = Field(default=20, ge=1, le=200)


class FilesystemOpenFileInput(ToolInputModel):
    operation: Literal["open_file"] = "open_file"
    path: str = Field(min_length=1)


class FilesystemReadFileInput(ToolInputModel):
    operation: Literal["read_file"] = "read_file"
    path: str = Field(min_length=1)
    max_chars: int = Field(default=12000, ge=100, le=100000)


class FilesystemWriteTextFileInput(ToolInputModel):
    operation: Literal["write_text_file"] = "write_text_file"
    path: str = Field(min_length=1)
    content: str = ""
    overwrite: bool = False

    @model_validator(mode="before")
    @classmethod
    def _accept_text_alias(cls, value: Any) -> Any:
        # ``text`` is the natural field name models and API clients use for a
        # text-writing operation. Keep ``content`` canonical while accepting
        # that equivalent spelling; without normalization the permissive base
        # schema retained ``text`` as an extra and silently wrote an empty file.
        if isinstance(value, dict) and "content" not in value and "text" in value:
            value = dict(value)
            value["content"] = value.pop("text")
        return value


class FilesystemCollectFolderSnapshotInput(ToolInputModel):
    operation: Literal["collect_folder_snapshot"] = "collect_folder_snapshot"
    root: str = Field(min_length=1)
    max_depth: int = Field(default=2, ge=0, le=10)
    max_entries: int = Field(default=200, ge=1, le=5000)


class FilesystemDescribeFolderInput(ToolInputModel):
    operation: Literal["describe_folder"] = "describe_folder"
    root: str = Field(min_length=1)
    recursive: bool = False
    include_ocr: bool = True
    max_files: int = Field(default=50, ge=1, le=500)
    max_chars_per_file: int = Field(default=4000, ge=100, le=50000)


class FilesystemOrganizePlanInput(ToolInputModel):
    operation: Literal["organize_plan"] = "organize_plan"
    root: str = Field(min_length=1)
    strategy: Literal["by_type", "by_extension"] = "by_type"
    recursive: bool = False
    max_files: int = Field(default=1000, ge=1, le=10000)


class FilesystemRenamePlanInput(ToolInputModel):
    operation: Literal["rename_plan"] = "rename_plan"
    root: str = Field(min_length=1)
    strategy: Literal["by_content", "by_name"] = "by_content"
    recursive: bool = False
    max_files: int = Field(default=1000, ge=1, le=10000)


class FilesystemManifestItem(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    operation: Literal["move", "copy", "rename"] = "move"
    source: str = Field(min_length=1)
    destination: str = Field(min_length=1)
    reason: str | None = None
    before_name: str | None = None
    after_name: str | None = None


class FilesystemApplyManifestInput(ToolInputModel):
    operation: Literal["apply_manifest"] = "apply_manifest"
    root: str = Field(min_length=1)
    manifest: list[FilesystemManifestItem] = Field(min_length=1)
    dry_run: bool = False
    overwrite: bool = False


class WorkspacePrepareOutput(ToolOutputModel):
    workspace_dir: str = Field(min_length=1)
    files: list[str] = Field(default_factory=list)


class WorkspaceWriteFilesOutput(WorkspacePrepareOutput):
    files: list[str] = Field(min_length=1)


class WorkspaceMaterializeStaticAppOutput(WorkspacePrepareOutput):
    materialized_from: Literal["assistant_output", "fallback_template", "existing_files"]


class WorkspaceLaunchStaticOutput(WorkspacePrepareOutput):
    url: str = Field(pattern=r"^https?://")
    server_pid: int = Field(ge=1)


class WorkspaceWebAppPreviewOutput(WorkspaceLaunchStaticOutput):
    pass


class CodeInterpreterRunPythonInput(ToolInputModel):
    operation: Literal["run_python"] = "run_python"
    code: str = Field(min_length=1)
    objective: str | None = None
    workspace_dir: str | None = None
    script_name: str = "script.py"
    backend: str | None = None
    execution_profile: str | None = None
    session_id: str | None = None
    allow_network: bool | None = None
    allowed_packages: list[str] = Field(default_factory=list)
    requirements: list[str] = Field(default_factory=list)
    files: list[str] = Field(default_factory=list)
    persist_session: bool = False
    approved: bool = False


class CodeInterpreterGenerateAndRunInput(ToolInputModel):
    operation: Literal["generate_and_run"] = "generate_and_run"
    objective: str = Field(min_length=1)
    context: str | None = None
    workspace_dir: str | None = None
    script_name: str = "script.py"
    backend: str | None = None
    execution_profile: str | None = None
    session_id: str | None = None
    allow_network: bool | None = None
    allowed_packages: list[str] = Field(default_factory=list)
    requirements: list[str] = Field(default_factory=list)
    files: list[str] = Field(default_factory=list)
    persist_session: bool = False
    # Runtime-only approval marker. The model cannot bypass policy by setting
    # this: ToolDefinition.approval_required_operations gates the request
    # before adapter dispatch, and ToolExecutor overwrites this to True only
    # when a human already authorized this call - either by approving this
    # exact request (approval consumed) or via an active ApprovalGrant
    # ("Allow for this task", docs/archive/UI_UX_AUDIT.md Phase 1). Adapter-level
    # approval gates (e.g. code.interpreter's unsandboxed-fallback check)
    # must see the same signal a policy-level bypass already granted, or a
    # grant silently stops working the moment an adapter has its own gate.
    approved: bool = False


class CodeInterpreterSolveOnceInput(CodeInterpreterGenerateAndRunInput):
    operation: Literal["solve_once"] = "solve_once"


class CodeInterpreterBuildTempHelperInput(CodeInterpreterGenerateAndRunInput):
    operation: Literal["build_temp_helper"] = "build_temp_helper"


class CodeInterpreterRepairScriptInput(CodeInterpreterGenerateAndRunInput):
    operation: Literal["repair_script"] = "repair_script"
    failing_code: str | None = None
    error_text: str | None = None


class CodeInterpreterInspectStateInput(ToolInputModel):
    operation: Literal["inspect_state"] = "inspect_state"
    workspace_dir: str | None = None
    max_files: int = Field(default=200, ge=1, le=5000)


class CodeInterpreterHealthInput(ToolInputModel):
    operation: Literal["health"] = "health"


class MCPClientInput(ToolInputModel):
    operation: Literal["discover", "list_tools", "call_tool", "health", "install_server"] = "list_tools"
    server: str | None = None
    tool: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)
    name: str | None = None
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str | None = None
    disabled_tools: list[str] = Field(default_factory=list)
    capability: str | None = None
    risk_level: str | None = None
    max_output_chars: int | None = Field(default=None, ge=100, le=200000)

    @field_validator("args", mode="before")
    @classmethod
    def _reject_dict_args(cls, value: Any) -> Any:
        # 'args' (install_server's command-line arguments, a list of
        # strings) and 'arguments' (call_tool's key-value tool arguments,
        # a dict) are easy to conflate - the local 8B model reliably does,
        # reproduced across three independent live recordings with zero
        # self-correction against the raw Pydantic "should be a valid
        # list" message (docs/archive/HISTORY.md Part 4's re-recording note).
        # Naming the correct field explicitly here gives it something
        # actionable to react to instead.
        if isinstance(value, dict):
            raise ValueError(
                "'args' must be a list of command-line strings (install_server only) - "
                "key-value tool arguments for call_tool belong in 'arguments' instead"
            )
        return value

    @model_validator(mode="after")
    def _require_inputs_per_operation(self) -> "MCPClientInput":
        if self.operation == "call_tool":
            if not self.server:
                raise ValueError("mcp.client call_tool requires 'server'")
            if not self.tool:
                raise ValueError("mcp.client call_tool requires 'tool'")
        if self.operation == "install_server":
            if not self.name:
                raise ValueError("mcp.client install_server requires 'name'")
            if not self.command:
                raise ValueError("mcp.client install_server requires 'command'")
        return self


class HttpRequestInput(ToolInputModel):
    operation: Literal["request"] = "request"
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"] = "GET"
    url: str = Field(min_length=1)
    headers: dict[str, str] = Field(default_factory=dict)
    query: dict[str, Any] = Field(default_factory=dict)
    json_body: Any | None = None
    body: str | None = None
    secret_refs: dict[str, Any] = Field(default_factory=dict)
    parse_json: bool = True
    max_response_chars: int | None = Field(default=None, ge=100, le=1000000)

    @field_validator("method", mode="before")
    @classmethod
    def _normalize_method(cls, value: Any) -> Any:
        return str(value).upper() if value is not None else value

    @model_validator(mode="after")
    def _body_is_bounded_to_one_field(self) -> "HttpRequestInput":
        if self.json_body is not None and self.body is not None:
            raise ValueError("http.request accepts only one of 'json_body' or 'body'")
        return self


class CodeInterpreterOutput(ToolOutputModel):
    operation: str = Field(min_length=1)
    workspace_dir: str = Field(min_length=1)
    script_path: str | None = None
    files_before: list[str] = Field(default_factory=list)
    files_after: list[str] = Field(default_factory=list)
    files_created: list[str] = Field(default_factory=list)
    files_modified: list[str] = Field(default_factory=list)
    files_deleted: list[str] = Field(default_factory=list)
    file_previews: list[dict[str, str]] = Field(default_factory=list)
    stdout: str = ""
    stderr: str = ""
    returncode: int | None = None
    summary: str | None = None
    generated: bool = False
    backend: str | None = None
    execution_profile: str | None = None
    sandboxed: bool = False
    resource_usage: dict[str, Any] = Field(default_factory=dict)
    resource_limits: dict[str, Any] = Field(default_factory=dict)
    network_enabled: bool = False
    session_id: str | None = None
    rich_outputs: list[dict[str, Any]] = Field(default_factory=list)
    backend_fallback_warning: str | None = None


class AdapterFactoryAssessOutput(ToolOutputModel):
    adapter_name: str = Field(min_length=1)
    assessment: str = Field(min_length=1)
    cacheable: bool = True
    execution_policy: str = Field(min_length=1)


class AdapterFactoryScaffoldOutput(ToolOutputModel):
    adapter_dir: str = Field(min_length=1)
    adapter_name: str = Field(min_length=1)
    files: list[str] = Field(min_length=1)
    cacheable: bool = True
    execution_policy: str = Field(min_length=1)


class AdapterFactorySandboxOutput(ToolOutputModel):
    adapter_dir: str | None = None
    result: str = Field(min_length=1)
    execution_policy: str = Field(min_length=1)
    returncode: int = 0
    promoted: bool = False


class MCPClientOutput(ToolOutputModel):
    operation: str = Field(min_length=1)
    servers: list[dict[str, Any]] = Field(default_factory=list)
    tools: list[dict[str, Any]] = Field(default_factory=list)
    result: dict[str, Any] | None = None
    healthy: bool | None = None
    installed: bool = False
    summary: str | None = None
    catalog_path: str | None = None
    catalog_updated_at: str | None = None
    catalog_entries: list[dict[str, Any]] = Field(default_factory=list)
    selected_tool: dict[str, Any] | None = None


class HttpRequestOutput(ToolOutputModel):
    operation: Literal["request"] = "request"
    url: str = Field(min_length=1)
    method: str = Field(min_length=1)
    status_code: int = Field(ge=100, le=599)
    ok: bool
    headers: dict[str, str] = Field(default_factory=dict)
    response_json: Any | None = Field(default=None, alias="json")
    text: str = ""
    truncated: bool = False
    elapsed_ms: int | None = Field(default=None, ge=0)
    summary: str | None = None


class VSCodeTerminalToolOutput(ToolOutputModel):
    command_id: str | None = None
    queued: dict[str, Any] | None = None
    usage: dict[str, str] = Field(default_factory=dict)
    retried: bool | None = None
    terminal_output: list[dict[str, Any]] = Field(default_factory=list)


class CodingAssistantOutput(ToolOutputModel):
    stdout: str = ""
    stderr: str = ""
    returncode: int


class TTSSynthesizeOutput(ToolOutputModel):
    operation: Literal["synthesize"] = "synthesize"
    path: str = Field(min_length=1)
    voice: str | None = None
    provider: str = Field(min_length=1)
    sample_rate: int | None = Field(default=None, ge=1)
    summary: str | None = None


class ArtifactDeliveryOutput(ToolOutputModel):
    operation: str = Field(min_length=1)
    delivered: bool = False
    delivery_method: str | None = None
    artifact_id: str | None = None
    artifact_ids: list[str] = Field(default_factory=list)
    path: str | None = None
    chat_id: str | None = None
    summary: str | None = None
    telegram_result: dict[str, Any] | None = None
    artifacts: list[dict[str, Any]] = Field(default_factory=list)


class DocumentManageOutput(ToolOutputModel):
    operation: str = Field(min_length=1)
    path: str | None = None
    artifact_id: str | None = None
    artifact_ids: list[str] = Field(default_factory=list)
    text: str | None = None
    summary: str | None = None
    slide_count: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class CodingAgentOutput(ToolOutputModel):
    operation: str = Field(min_length=1)
    provider: str = ""
    workspace_dir: str | None = None
    session_id: str | None = None
    status: str | None = None
    pid: int | None = None
    returncode: int | None = None
    started_at: str | None = None
    ended_at: str | None = None
    limit_state: dict[str, Any] = Field(default_factory=dict)
    changed_files: list[str] = Field(default_factory=list)
    log_path: str | None = None
    log_tail: str = ""
    summary: str | None = None


class ScheduleManageOutput(ToolOutputModel):
    operation: str = Field(min_length=1)
    schedule_id: str | None = None
    schedules: list[dict[str, Any]] = Field(default_factory=list)
    task_id: str | None = None
    next_run_at: str | None = None
    summary: str | None = None


class MemoryManageInput(ToolInputModel):
    operation: Literal["remember", "list", "forget"] = "remember"
    category: str | None = None
    content: str | None = None
    fact_id: str | None = None
    query: str | None = None

    @model_validator(mode="after")
    def _require_inputs_per_operation(self) -> "MemoryManageInput":
        if self.operation == "remember" and not (self.category and self.content):
            raise ValueError("memory.manage remember requires 'category' and 'content'")
        if self.operation == "forget" and not self.fact_id:
            raise ValueError("memory.manage forget requires 'fact_id'")
        return self


class MemoryManageOutput(ToolOutputModel):
    operation: str = Field(min_length=1)
    fact_id: str | None = None
    facts: list[dict[str, Any]] = Field(default_factory=list)
    summary: str | None = None


class TaskStatusOutput(ToolOutputModel):
    operation: Literal["status"] = "status"
    summary: str = Field(min_length=1)
    task_status: dict[str, Any] = Field(default_factory=dict)
    plan: dict[str, Any] | None = None


class KnowledgeBaseOutput(ToolOutputModel):
    operation: Literal["list_sources", "search"] = "search"
    summary: str = Field(min_length=1)
    sources: list[str] = Field(default_factory=list)
    results: list[dict[str, Any]] = Field(default_factory=list)


class PersonaOutput(ToolOutputModel):
    operation: Literal["get", "update"] = "get"
    summary: str = Field(min_length=1)
    content: str = ""


class WebSearchOutput(ToolOutputModel):
    operation: Literal["search"] = "search"
    summary: str = Field(min_length=1)
    query: str = ""
    provider: str = ""
    # title / url / snippet per entry.
    results: list[dict[str, Any]] = Field(default_factory=list)


class DependenciesOutput(ToolOutputModel):
    operation: Literal["install", "list_allowed"] = "install"
    summary: str = Field(min_length=1)
    installed: list[str] = Field(default_factory=list)
    allowed_packages: list[str] = Field(default_factory=list)
    target_dir: str | None = None


class SkillsOutput(ToolOutputModel):
    operation: Literal["list", "read"] = "list"
    summary: str = Field(min_length=1)
    # operation=list: name+description only (progressive disclosure - the
    # full body is never in this response). operation=read: exactly one
    # entry, with "body" populated.
    skills: list[dict[str, Any]] = Field(default_factory=list)


class BrowserToolOutput(ToolOutputModel):
    operation: str = Field(min_length=1)
    browser_state: dict[str, Any] | None = None
    browser_url: str | None = None
    url: str | None = None
    page_title: str | None = None
    summary: str | None = None
    tabs: list[dict[str, Any]] = Field(default_factory=list)
    links: list[dict[str, str]] = Field(default_factory=list)
    screenshot_path: str | None = None
    screenshot_uri: str | None = None
    visited_urls: list[str] = Field(default_factory=list)
    page_summaries: list[dict[str, Any]] = Field(default_factory=list)
    forms: list[dict[str, Any]] = Field(default_factory=list)


class ComputerUseOutput(ToolOutputModel):
    operation: str = Field(min_length=1)
    observation: dict[str, Any] | None = None
    actions_taken: list[dict[str, Any]] = Field(default_factory=list)
    screenshots: list[str] = Field(default_factory=list)
    screenshot_path: str | None = None
    screenshot_uri: str | None = None
    final_summary: str | None = None
    completed: bool = False


class FilesystemManageOutput(ToolOutputModel):
    operation: str = Field(min_length=1)
    root: str | None = None
    path: str | None = None
    entries: list[dict[str, Any]] = Field(default_factory=list)
    text: str | None = None
    content_preview: str | None = None
    content_summary: str | None = None
    manifest: list[dict[str, Any]] = Field(default_factory=list)
    changed_paths: list[str] = Field(default_factory=list)
    dry_run: bool = False
    summary: str | None = None
