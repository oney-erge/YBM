from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


class StrictBaseModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
        use_enum_values=False,
    )


class ChannelType(StrEnum):
    TELEGRAM = "telegram"
    WHATSAPP = "whatsapp"
    SLACK = "slack"
    DISCORD = "discord"
    WEB = "web"
    CLI = "cli"


class MessageKind(StrEnum):
    TEXT = "text"
    VOICE = "voice"
    IMAGE = "image"
    DOCUMENT = "document"
    CALLBACK = "callback"
    SYSTEM = "system"


class TaskStatus(StrEnum):
    RECEIVED = "received"
    INTERPRETING = "interpreting"
    CLARIFYING = "clarifying"
    PLANNED = "planned"
    AWAITING_APPROVAL = "awaiting_approval"
    AWAITING_EXTERNAL = "awaiting_external"
    RUNNING = "running"
    PAUSED = "paused"
    RETRYING = "retrying"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


class ScheduleStatus(StrEnum):
    ENABLED = "enabled"
    PAUSED = "paused"
    DELETED = "deleted"


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class Capability(StrEnum):
    TELEGRAM_RECEIVE = "telegram.receive"
    TELEGRAM_SEND = "telegram.send"
    LLM_GENERATE = "llm.generate"
    STT_TRANSCRIBE = "stt.transcribe"
    TTS_SYNTHESIZE = "tts.synthesize"
    VSCODE_READ_STATE = "vscode.read_state"
    VSCODE_WRITE_FILES = "vscode.write_files"
    TERMINAL_RUN = "terminal.run"
    FILESYSTEM_READ = "filesystem.read"
    FILESYSTEM_WRITE = "filesystem.write"
    DESKTOP_SCREENSHOT = "desktop.screenshot"
    DESKTOP_CONTROL = "desktop.control"
    BROWSER_OPEN = "browser.open"
    BROWSER_CONTROL = "browser.control"
    NETWORK_HTTP = "network.http"
    SCHEDULE_MANAGE = "schedule.manage"
    GITHUB_READ = "github.read"
    GITHUB_PUSH = "github.push"
    DEPENDENCIES_INSTALL = "dependencies.install"
    MEMORY_MANAGE = "memory.manage"


class TaskType(StrEnum):
    DEVELOPMENT = "development"
    CONFIGURATION = "configuration"
    ADMIN_CONTROL = "admin_control"
    DESKTOP_OBSERVATION = "desktop_observation"
    QUESTION = "question"
    STATUS_REQUEST = "status_request"
    OTHER = "other"


class IntentRoute(StrEnum):
    CONVERSATION = "conversation"
    STATUS = "status"
    DESKTOP_OBSERVE = "desktop.observe"
    COMPUTER_USE = "computer.use"
    BROWSER_OPEN = "browser.open"
    BROWSER_CONTROL = "browser.control"
    FILESYSTEM_MANAGE = "filesystem.manage"
    DOCUMENT_MANAGE = "document.manage"
    ARTIFACT_DELIVERY = "artifact.deliver"
    CODE_INTERPRETER = "code.interpreter"
    CODING_AGENT = "coding.agent"
    SCHEDULE_MANAGE = "schedule.manage"
    ADAPTER_FACTORY = "adapter.factory"
    WORKSPACE_MANAGE = "workspace.manage"
    CONFIGURATION = "configuration"
    UNKNOWN = "unknown"


class DeliveryKind(StrEnum):
    NONE = "none"
    LATEST = "latest"
    FILE = "file"
    SCREENSHOT = "screenshot"


class CapabilityAccessMode(StrEnum):
    OFF = "off"
    READ_ONLY = "read_only"
    WRITE_ACCESS = "write_access"
    FULL_ACCESS = "full_access"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    CONSUMED = "consumed"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class ToolResultStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    DENIED = "denied"
    NEEDS_APPROVAL = "needs_approval"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"


class ArtifactType(StrEnum):
    TEXT_LOG = "text_log"
    JSON = "json"
    SCREENSHOT = "screenshot"
    VOICE = "voice"
    TRANSCRIPT = "transcript"
    GENERATED_FILE = "generated_file"
    DOCUMENT = "document"
    EXTERNAL_LINK = "external_link"


class PostconditionType(StrEnum):
    WORKSPACE_DIR = "workspace_dir"
    WORKSPACE_FILES = "workspace_files"
    SOURCE_CONTENT = "source_content"
    PREVIEW_URL = "preview_url"
    ADAPTER_PROPOSAL = "adapter_proposal"
    ARTIFACT_DELIVERED = "artifact_delivered"
    SCREENSHOT_DELIVERED = "screenshot_delivered"
    DOCUMENT_SUMMARY = "document_summary"
    PRESENTATION_FILE = "presentation_file"
    CODING_AGENT_STEP = "coding_agent_step"
    SCHEDULE_CREATED = "schedule_created"
    BROWSER_STATE = "browser_state"
    DESKTOP_OBSERVATION = "desktop_observation"
    FILE_ORGANIZATION = "file_organization"
    TASK_STATUS = "task_status"
    GITHUB_PR = "github_pr"
    EXTERNAL_COMMAND = "external_command"


class AuditEventType(StrEnum):
    MESSAGE_RECEIVED = "message_received"
    MESSAGE_SENT = "message_sent"
    CONFIG_UPDATED = "config_updated"
    TELEGRAM_ACCESS_DECISION = "telegram_access_decision"
    # Generic form of the above (docs/UI_UX_AUDIT.md Phase 16) - Telegram
    # keeps its own named event (existing historical rows, existing code),
    # WhatsApp and any future channel use this one rather than borrowing
    # Telegram's name for an event that isn't about Telegram.
    CHANNEL_ACCESS_DECISION = "channel_access_decision"
    MESSAGE_CLASSIFIED = "message_classified"
    TASK_SPAWN_FAILED = "task_spawn_failed"
    TASK_CREATED = "task_created"
    TASK_STATE_CHANGED = "task_state_changed"
    PLAN_CREATED = "plan_created"
    POLICY_DECISION = "policy_decision"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_DECIDED = "approval_decided"
    TOOL_REQUESTED = "tool_requested"
    TOOL_COMPLETED = "tool_completed"
    ARTIFACT_CREATED = "artifact_created"
    # A durable fact was stored outside a tool call - the chat route persisting
    # a standing instruction the user stated. Tool-driven memory writes are
    # already covered by TOOL_COMPLETED; this one has no tool invocation to
    # attach to, and an unaudited memory write is not acceptable.
    MEMORY_UPDATED = "memory_updated"
    EGRESS_CONTACTED = "egress_contacted"
    ERROR = "error"
    # What a task cancellation cleaned up - pending approvals rejected,
    # task-scoped grants revoked, stuck tool invocations closed out, an
    # awaiting-external session stopped (docs/UI_UX_AUDIT.md Phase 8).
    TASK_CANCELLED = "task_cancelled"


class ErrorClass(StrEnum):
    TRANSIENT = "transient"
    POLICY_DENIED = "policy_denied"
    APPROVAL_TIMEOUT = "approval_timeout"
    VALIDATION_FAILED = "validation_failed"
    ADAPTER_FAILED = "adapter_failed"
    RATE_LIMITED = "rate_limited"
    USAGE_LIMITED = "usage_limited"
    FATAL = "fatal"


class Attachment(StrictBaseModel):
    id: str = Field(default_factory=lambda: new_id("att"))
    kind: MessageKind
    file_id: str | None = None
    file_name: str | None = None
    mime_type: str | None = None
    size_bytes: int | None = Field(default=None, ge=0)
    metadata: dict[str, Any] = Field(default_factory=dict)


class VoiceAttachment(Attachment):
    kind: MessageKind = MessageKind.VOICE
    duration_seconds: int | None = Field(default=None, ge=0)
    transcript: str | None = None


class InboundMessage(StrictBaseModel):
    id: str = Field(default_factory=lambda: new_id("msg"))
    channel: ChannelType
    kind: MessageKind
    sender_id: str
    chat_id: str
    text: str | None = None
    attachments: list[Attachment] = Field(default_factory=list)
    received_at: datetime = Field(default_factory=utc_now)
    correlation_id: str = Field(default_factory=lambda: new_id("corr"))
    raw: dict[str, Any] | None = None


class OutboundMessage(StrictBaseModel):
    id: str = Field(default_factory=lambda: new_id("out"))
    channel: ChannelType
    chat_id: str
    text: str | None = None
    artifact_ids: list[str] = Field(default_factory=list)
    reply_to_message_id: str | None = None
    correlation_id: str = Field(default_factory=lambda: new_id("corr"))


class CommandEnvelope(StrictBaseModel):
    id: str = Field(default_factory=lambda: new_id("cmd"))
    type: str
    source: str
    payload: dict[str, Any]
    created_at: datetime = Field(default_factory=utc_now)
    correlation_id: str = Field(default_factory=lambda: new_id("corr"))


class OrchestrationIntent(StrictBaseModel):
    model_config = ConfigDict(
        extra="ignore",
        str_strip_whitespace=True,
        validate_assignment=True,
        use_enum_values=False,
    )

    route: IntentRoute
    operation: str | None = None
    objective: str | None = None
    reasoning: str
    tool_name: str | None = None
    url: str | None = None
    path: str | None = None
    folder_path: str | None = None
    file_path: str | None = None
    query: str | None = None
    provider: str | None = None
    cadence: str | None = None
    schedule_id: str | None = None
    scheduled_objective: str | None = None
    delivery: DeliveryKind = DeliveryKind.NONE
    artifact_type: str | None = None
    page_limit: int | None = Field(default=None, ge=1, le=50)
    form_fields: dict[str, str] = Field(default_factory=dict)
    submit: bool = False
    open_first_result: bool = False
    needs_plan_first: bool = False
    use_external_agent: bool = False
    allow_deletion: bool = False
    allow_overwrite: bool = False

    @field_validator("route", mode="before")
    @classmethod
    def route_accepts_aliases(cls, value: Any) -> Any:
        if isinstance(value, IntentRoute):
            return value
        if value is None:
            return value
        aliases = {
            "browser.research": IntentRoute.BROWSER_OPEN,
            "browser.search": IntentRoute.BROWSER_OPEN,
            "browser.screenshot": IntentRoute.BROWSER_OPEN,
            "browser.summarize": IntentRoute.BROWSER_OPEN,
            "browser.navigate": IntentRoute.BROWSER_CONTROL,
            "browser.fill_form": IntentRoute.BROWSER_CONTROL,
            "browser.form": IntentRoute.BROWSER_CONTROL,
            "filesystem.inspect": IntentRoute.FILESYSTEM_MANAGE,
            "filesystem.search": IntentRoute.FILESYSTEM_MANAGE,
            "filesystem.organize": IntentRoute.FILESYSTEM_MANAGE,
            "filesystem.rename": IntentRoute.FILESYSTEM_MANAGE,
            "filesystem.describe": IntentRoute.FILESYSTEM_MANAGE,
            "document.pdf": IntentRoute.DOCUMENT_MANAGE,
            "document.presentation": IntentRoute.DOCUMENT_MANAGE,
            "desktop.screenshot": IntentRoute.DESKTOP_OBSERVE,
            "python": IntentRoute.CODE_INTERPRETER,
            "python.run": IntentRoute.CODE_INTERPRETER,
            "code.run": IntentRoute.CODE_INTERPRETER,
            "code_interpreter": IntentRoute.CODE_INTERPRETER,
            "coding.codex": IntentRoute.CODING_AGENT,
            "coding.copilot": IntentRoute.CODING_AGENT,
        }
        return aliases.get(str(value).strip().lower(), value)

    @field_validator("operation")
    @classmethod
    def operation_is_simple(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            return None
        if not cleaned.replace("_", "").replace("-", "").replace(".", "").isalnum():
            raise ValueError("operation must be a simple operation id")
        return cleaned


class MessageClassification(StrictBaseModel):
    is_task: bool
    task_type: TaskType = TaskType.OTHER
    normalized_objective: str | None = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    reason: str
    intent: OrchestrationIntent | None = None
    # True when this message is a correction or addition to work already
    # running, rather than a new job. Set only when the context shows an
    # active task - see prompts/base/concierge_system.md. The alternative was
    # spawning a second task for "make it five, not three", which is how a
    # running task came to be unsteerable in the first place.
    steers_active_task: bool = False
    # Populated only when is_task=False (see prompts/base/concierge_system.md) -
    # the Concierge composes the chat reply in the same call it classifies, so
    # a non-task message doesn't need a second LLM round trip. None/empty falls
    # back to TelegramIntakeService's separate `responder` if one is configured
    # (see channels/responder.py) - kept for classifiers that don't populate this.
    reply: str | None = None
    # Optional: which fixed outcome type(s) (docs/ROADMAP.md "Finish the
    # Proof") this specific request objectively requires to count as done,
    # when that's plain from the request itself - e.g. "make me a slide
    # deck" implies presentation_file. This is transmitted via the
    # structured-output JSON schema (LLMProvider.generate_structured's
    # response_format), not embedded in system_prompt/user_prompt text, so
    # adding it changes no fixture_key and needed no scenario re-record.
    # orchestration/fulfillment.py's expected_postconditions() prefers this
    # over its own keyword-matched guess when non-empty, but never requires
    # it - every recorded fixture predates this field and simply omits it,
    # which is indistinguishable from "the classifier chose not to declare
    # anything", the documented safe default.
    expected_postconditions: list[PostconditionType] = Field(
        default_factory=list,
        description=(
            "Which of these fixed outcome types this specific request objectively requires "
            "to count as done, only when that is plain from the request itself (e.g. "
            "'make me a slide deck' implies presentation_file). Leave empty when it is not "
            "obvious - an empty list is the safe default, not a wrong answer."
        ),
    )

    @field_validator("expected_postconditions", mode="before")
    @classmethod
    def _drop_unrecognized_postcondition_types(cls, value: Any) -> Any:
        # Same defensive posture as task_type_accepts_route_aliases below:
        # one hallucinated value must not fail the whole classification.
        if not isinstance(value, list):
            return []
        valid = {item.value for item in PostconditionType}
        return [item for item in value if isinstance(item, str) and item in valid]

    @field_validator("task_type", mode="before")
    @classmethod
    def task_type_accepts_route_aliases(cls, value: Any) -> Any:
        if isinstance(value, TaskType):
            return value
        if value is None:
            return TaskType.OTHER
        route_aliases = {
            "conversation": TaskType.QUESTION,
            "status": TaskType.STATUS_REQUEST,
            "desktop.observe": TaskType.DESKTOP_OBSERVATION,
            "computer.use": TaskType.OTHER,
            "browser.open": TaskType.OTHER,
            "browser.control": TaskType.OTHER,
            "browser.research": TaskType.OTHER,
            "browser.search": TaskType.OTHER,
            "browser.screenshot": TaskType.OTHER,
            "browser.navigate": TaskType.OTHER,
            "browser.fill_form": TaskType.OTHER,
            "filesystem.manage": TaskType.OTHER,
            "filesystem.inspect": TaskType.OTHER,
            "filesystem.search": TaskType.OTHER,
            "filesystem.organize": TaskType.OTHER,
            "filesystem.rename": TaskType.OTHER,
            "filesystem.describe": TaskType.OTHER,
            "document.manage": TaskType.OTHER,
            "document.pdf": TaskType.OTHER,
            "document.presentation": TaskType.OTHER,
            "artifact.deliver": TaskType.OTHER,
            "code.interpreter": TaskType.OTHER,
            "code_interpreter": TaskType.OTHER,
            "python": TaskType.OTHER,
            "python.run": TaskType.OTHER,
            "coding.agent": TaskType.DEVELOPMENT,
            "coding.codex": TaskType.DEVELOPMENT,
            "coding.copilot": TaskType.DEVELOPMENT,
            "schedule.manage": TaskType.OTHER,
            "adapter.factory": TaskType.DEVELOPMENT,
            "workspace.manage": TaskType.DEVELOPMENT,
            "configuration": TaskType.CONFIGURATION,
            "unknown": TaskType.OTHER,
        }
        return route_aliases.get(str(value).strip().lower(), value)


class CapabilityAccessSummary(StrictBaseModel):
    name: str
    label: str | None = None
    mode: CapabilityAccessMode
    capabilities: list[Capability] = Field(default_factory=list)
    options: list[dict[str, str]] = Field(default_factory=list)
    requires_approval: bool = True


class FormattedAuditEvent(StrictBaseModel):
    id: str
    type: AuditEventType
    category: str
    formatted_time: str
    actor: str
    task_id: str | None = None
    title: str
    summary: str
    decision: str | None = None
    reason: str | None = None
    task_type: str | None = None
    source: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class PlanPostcondition(StrictBaseModel):
    """Not plan-era despite the name - this is the Operator loop's own
    deterministic postcondition record (orchestration/fulfillment.py's
    expected_postconditions()), inferred from objective text + tool names,
    not from a PlanModel (deleted; nothing constructs one anymore)."""
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True, validate_assignment=True, use_enum_values=False)
    type: PostconditionType
    description: str
    required: bool = True


class OperatorAction(StrEnum):
    """What the Operator loop's decide() call chose to do next.

    See docs/HISTORY.md P3 / orchestration/operator.py - the additive
    observe/decide/act alternative to plan-once-then-replan.

    CALL_TOOLS_PARALLEL and DELEGATE (docs/HISTORY.md Part 4 T1.1/T1.2) are
    both narrowly scoped, not general replacements for CALL_TOOL: parallel
    calls skip the approval/retry/background-wait machinery entirely (see
    worker.py's _run_parallel_calls), and a delegated sub-task cannot itself
    approval-pause, wait on a background session, ask the user, or delegate
    further (see worker.py's _run_delegate). Both exist for the specific
    case they were built for - independent reads, and a self-contained
    sub-task with its own tool subset - not as drop-in CALL_TOOL upgrades.
    """
    CALL_TOOL = "call_tool"
    CALL_TOOLS_PARALLEL = "call_tools_parallel"
    DELEGATE = "delegate"
    DONE = "done"
    ASK_USER = "ask_user"
    BLOCKED = "blocked"


class ParallelToolCall(StrictBaseModel):
    """One call within a call_tools_parallel batch - same shape as the
    single-call fields on OperatorDecision, just repeated per item."""

    tool_name: str
    tool_input: dict[str, Any] = Field(default_factory=dict)
    risk_level: RiskLevel = RiskLevel.LOW


class OperatorDecision(StrictBaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True, validate_assignment=True, use_enum_values=False)
    action: OperatorAction
    reasoning: str = ""
    tool_name: str | None = None
    tool_input: dict[str, Any] = Field(default_factory=dict)
    # ToolDefinition carries no static risk level - like PlanStep, the model
    # declares it per call (low for reads, high for writes/browser control,
    # critical for desktop UI control) and PolicyEngine.evaluate() gates on
    # it against the capability's max_risk_level.
    risk_level: RiskLevel = RiskLevel.LOW
    final_answer: str | None = None
    question: str | None = None
    reason: str | None = None
    # action=call_tools_parallel only.
    parallel_calls: list[ParallelToolCall] = Field(default_factory=list)
    # action=delegate only. delegate_tools=None means the sub-task inherits
    # the full tool catalog; an explicit list narrows it, enforced in code
    # (worker.py's _run_delegate), not just by prompt instruction.
    delegate_objective: str | None = None
    delegate_tools: list[str] | None = None

    @model_validator(mode="after")
    def call_tool_requires_tool_name(self) -> "OperatorDecision":
        if self.action == OperatorAction.CALL_TOOL and not self.tool_name:
            raise ValueError("action=call_tool requires tool_name")
        if self.action == OperatorAction.CALL_TOOLS_PARALLEL and len(self.parallel_calls) < 2:
            raise ValueError("action=call_tools_parallel requires at least 2 parallel_calls")
        if self.action == OperatorAction.DELEGATE and not self.delegate_objective:
            raise ValueError("action=delegate requires delegate_objective")
        return self


class TaskRecord(StrictBaseModel):
    id: str = Field(default_factory=lambda: new_id("task"))
    objective: str
    status: TaskStatus = TaskStatus.RECEIVED
    conversation_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)


def channel_chat_id(task: TaskRecord, channel: ChannelType) -> str | None:
    """Which `channel` chat a task's output belongs to (docs/UI_UX_AUDIT.md
    Phase 16 - generalized from the original Telegram-only `task_chat_id`
    once a second channel, WhatsApp, existed to validate the seam against).

    Lives here, next to TaskRecord, because it is pure logic over the
    record's own fields and every channel's notifier + the artifact delivery
    tool need it - previously as two byte-identical Telegram-only private
    copies, with no import direction between those packages that would let
    one reuse the other's.
    """
    # A source_chat_id is meaningful only within its channel. Web chat uses
    # its own fixed local id, which must never be handed to a real channel's
    # send API.
    source_channel = task.metadata.get("source_channel")
    if source_channel and source_channel != channel.value:
        return None
    value = task.metadata.get("source_chat_id")
    if value:
        return str(value)
    # Channel-less legacy records predate source_channel entirely - they can
    # only be Telegram, the one channel that existed before the field was
    # added, so this fallback applies only when checking for Telegram.
    if channel == ChannelType.TELEGRAM and task.conversation_id and task.conversation_id.startswith("conv_telegram_"):
        return task.conversation_id.removeprefix("conv_telegram_")
    return None


class ScheduleRecord(StrictBaseModel):
    id: str = Field(default_factory=lambda: new_id("schedule"))
    source_channel: ChannelType = ChannelType.TELEGRAM
    source_chat_id: str | None = None
    objective: str
    cadence: str
    timezone: str = "America/Chicago"
    status: ScheduleStatus = ScheduleStatus.ENABLED
    next_run_at: datetime
    last_run_at: datetime | None = None
    last_task_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class TaskSignal(StrictBaseModel):
    id: str = Field(default_factory=lambda: new_id("sig"))
    task_id: str
    signal: str
    actor: str
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


class ToolCallRequest(StrictBaseModel):
    id: str = Field(default_factory=lambda: new_id("toolreq"))
    task_id: str
    tool_name: str
    capability: Capability
    risk_level: RiskLevel = RiskLevel.LOW
    scope_target: str | None = None
    input: dict[str, Any] = Field(default_factory=dict)
    timeout_seconds: int = Field(default=60, ge=1)
    idempotency_key: str = Field(default_factory=lambda: new_id("idem"))
    requires_approval: bool = False
    created_at: datetime = Field(default_factory=utc_now)
    # Trace-graph correlation (docs/UI_REWRITE_PLAN.md §7/§9 Phase 0.6): which
    # execution context issued this call - "operator" (the parent's own
    # direct call, the default), "parallel_batch:<id>" (one of N concurrent
    # calls from one call_tools_parallel), or "subagent:<id>" (a delegated
    # sub-task's own call), optionally with a "/parallel_batch:<id>" suffix
    # when a sub-task itself fans out. No dedicated tool_invocations column
    # needed - like every other ToolCallRequest field except id/task_id/
    # tool_name/capability, this rides inside the existing request_json blob
    # and is read back via ToolInvocationRepository.list_for_task()'s
    # existing _load(row["request_json"]) with no repository change.
    origin: str = "operator"
    parent_step_id: str | None = None


class ToolVerification(StrictBaseModel):
    """Mechanical proof that a tool call's declared effect actually happened,
    attached by ToolExecutor when the call's ToolDefinition.verify() hook
    fires on a SUCCEEDED result (docs/ROADMAP.md "Proof": check the goal
    instead of inferring it from wording).

    Deliberately distinct from PlanPostcondition/fulfillment.py's
    task-level checks: those infer what a whole task should have done from
    the objective's wording. This is one call's own receipt, produced by a
    tool re-reading the machine state its own request claims to have
    changed (e.g. filesystem.manage's apply_manifest confirming each
    manifest destination now exists on disk) - never by asking the model
    whether it worked.
    """
    checked: int = Field(ge=0)
    verified: int = Field(ge=0)
    missing: list[str] = Field(default_factory=list)
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.checked > 0 and not self.missing


class ToolCallResult(StrictBaseModel):
    id: str = Field(default_factory=lambda: new_id("toolres"))
    request_id: str
    status: ToolResultStatus
    output: dict[str, Any] = Field(default_factory=dict)
    error_class: ErrorClass | None = None
    error_message: str | None = None
    artifact_ids: list[str] = Field(default_factory=list)
    completed_at: datetime = Field(default_factory=utc_now)
    # None means "this tool call has no mechanical verification defined" -
    # distinct from an empty ToolVerification, which would mean "checked
    # and found nothing wrong". Absence of proof is not proof of absence.
    verification: ToolVerification | None = None
    # "untrusted_external" when ToolDefinition.operation_content_trust marks
    # this operation as returning content this machine does not control - a
    # web page, an HTTP response, an MCP server's own output, a document
    # someone else authored (docs/THREAT_MODEL.md: "untrusted data, even
    # when it looks like an instruction"). None for everything else, not a
    # claim that unmarked content is safe - only that nothing here has
    # classified it either way. Attached by ToolExecutor from the tool's own
    # contract, the same boundary as egress/verification. Currently a
    # visibility signal for the trace/evidence views, not yet consumed by
    # the Operator prompt itself - see docs/GAPS.md.
    content_trust: str | None = None


class LLMCallRecord(StrictBaseModel):
    """One LLM API call, persisted for the trace (docs/UI_UX_AUDIT.md Phase
    14d) - the receipts that turn the Duration view's inferred "operator
    thinking" gaps into measured latency. `source` is "operator", "auditor",
    or "subagent" today - the same three the worker's own token-usage
    tracking already distinguishes (see TaskWorker._record_llm_usage);
    Concierge/classifier/memory calls made before a task exists are
    deliberately out of scope, same boundary that tracking already draws.
    `messages`/`response_text` are redacted and size-capped by the caller
    before construction, not here - this model just carries the result.

    `step_id` (docs/UI_UX_AUDIT.md Phase 14e) is the same id stamped onto the
    operator_history entry for this step and any ToolCallRequest.parent_step_id
    it led to - the real parent-child link Graph v2 is built on, not another
    inferred correlation.
    """

    id: str = Field(default_factory=lambda: new_id("llmcall"))
    task_id: str
    source: str
    model: str | None = None
    step_index: int | None = None
    step_id: str | None = None
    messages: list[dict[str, Any]] = Field(default_factory=list)
    response_text: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    latency_ms: float | None = None
    created_at: datetime = Field(default_factory=utc_now)


class ApprovalRequest(StrictBaseModel):
    id: str = Field(default_factory=lambda: new_id("approval"))
    task_id: str
    capability: Capability
    risk_level: RiskLevel
    summary: str
    action_payload: dict[str, Any] = Field(default_factory=dict)
    status: ApprovalStatus = ApprovalStatus.PENDING
    expires_at: datetime
    created_at: datetime = Field(default_factory=utc_now)


class ApprovalGrant(StrictBaseModel):
    """"Allow for this task" (docs/UI_UX_AUDIT.md Phase 1): a human approved
    one call and chose to pre-approve the same tool+capability for the rest
    of this task, instead of being asked again for every subsequent call.

    Deliberately narrower than a one-shot ApprovalRequest match: it does not
    bind to the exact input, only (task_id, tool_name, capability) - so it
    covers "the same kind of call again with different arguments" within one
    task. It does not widen what's allowed: PolicyEngine's scope/pattern/
    risk-ceiling checks still run before a grant is even consulted, so a
    grant only skips the "ask a human" step for a call that policy would
    have permitted anyway. Not "Always allow" - there is no grant that
    outlives its task.

    `scope` (docs/ROADMAP.md "scoped temporary authority") narrows further:
    when set, a matching call's own `scope_target` must fall within it (the
    same prefix containment PolicyEngine.scope_contains already applies to
    a capability's configured scopes) - inherited automatically from the
    approved action's own scope_target at grant creation, not a separate
    field a human has to fill in. `max_operations` bounds the count of calls
    a grant can cover regardless of how much time is left on its TTL;
    `operations_used` is the running count ToolExecutor increments each time
    the grant actually gates a call through. `revoked` is a human's early
    "no more" - the previously-missing revocation list.
    """

    id: str = Field(default_factory=lambda: new_id("grant"))
    task_id: str
    tool_name: str
    capability: Capability
    granted_from_approval_id: str
    created_at: datetime = Field(default_factory=utc_now)
    expires_at: datetime
    scope: str | None = None
    max_operations: int | None = Field(default=None, ge=1)
    operations_used: int = Field(default=0, ge=0)
    revoked: bool = False

    @property
    def active(self) -> bool:
        if self.revoked:
            return False
        if self.max_operations is not None and self.operations_used >= self.max_operations:
            return False
        return self.expires_at > utc_now()


class Artifact(StrictBaseModel):
    id: str = Field(default_factory=lambda: new_id("artifact"))
    task_id: str | None = None
    type: ArtifactType
    uri: str | None = None
    content_preview: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


class MemorySource(StrEnum):
    USER_STATED = "user_stated"
    TASK_DERIVED = "task_derived"
    OPERATOR_ADMIN = "operator_admin"


class MemoryFact(StrictBaseModel):
    """One durable, structured fact (docs/UI_UX_AUDIT.md Phase 4) - the
    replacement for treating the rolling conversation summary as the only
    memory. Deliberately flat and inspectable: a person can read
    `category: content` and understand exactly what the agent believes,
    where it came from, and how sure it is - a free-text summary blob
    can't offer any of that.
    """

    id: str = Field(default_factory=lambda: new_id("mem"))
    category: str = Field(min_length=1, max_length=60)
    content: str = Field(min_length=1, max_length=2000)
    source: MemorySource = MemorySource.USER_STATED
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    task_id: str | None = None
    supersedes_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class AuditEvent(StrictBaseModel):
    id: str = Field(default_factory=lambda: new_id("audit"))
    type: AuditEventType
    actor: str
    task_id: str | None = None
    correlation_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


class TranscriptionResult(StrictBaseModel):
    text: str
    language: str | None = None
    duration_seconds: float | None = Field(default=None, ge=0)
    confidence: float | None = Field(default=None, ge=0, le=1)
    metadata: dict[str, Any] = Field(default_factory=dict)
