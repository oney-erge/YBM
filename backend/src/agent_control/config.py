from __future__ import annotations

import ipaddress
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict, YamlConfigSettingsSource

from agent_control.config_sync import read_env_value
from agent_control.schemas import Capability, RiskLevel, StrictBaseModel


class ServerConfig(StrictBaseModel):
    host: str = "127.0.0.1"
    port: int = Field(default=8765, ge=1, le=65535)
    public_base_url: str | None = None
    admin_enabled: bool = True
    admin_token_env: str = "AGENT_ADMIN_TOKEN"


def is_loopback_host(host: str) -> bool:
    """True when ``host`` only accepts connections from the local machine.

    Used to gate fail-closed auth checks: an unset admin/bridge token is only
    safe while nothing beyond loopback can reach the port. Covers the full
    127.0.0.0/8 range (not just 127.0.0.1) and IPv4-mapped IPv6 loopback
    addresses such as ::ffff:127.0.0.1 - a server bound to any of those is
    still reachable only from this machine.

    The IPv4-mapped case is unwrapped explicitly rather than left to
    IPv6Address.is_loopback: whether that property follows a mapped address
    through to the underlying IPv4 one varies by CPython patch release, so
    relying on it makes the answer depend on the interpreter build.
    """
    normalized = (host or "").strip().lower()
    if normalized in {"localhost", "localhost."}:
        return True
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        return mapped.is_loopback
    return address.is_loopback


class IdentityConfig(StrictBaseModel):
    instance_name: str = "ybm-control"
    owner_label: str = "local-user"


class TelegramConfig(StrictBaseModel):
    enabled: bool = False
    token_env: str = "TELEGRAM_BOT_TOKEN"
    token: SecretStr | None = None
    allowed_user_ids: list[int] = Field(default_factory=list)
    allowed_chat_ids: list[int] = Field(default_factory=list)
    polling: bool = True


class WhatsAppConfig(StrictBaseModel):
    """docs/UI_UX_AUDIT.md Phase 16 - a second channel, via Baileys (an
    unofficial WhatsApp Web client, QR-code linked, no Meta account or
    public webhook needed - see channels/whatsapp_bridge_process.py). No
    bot-token equivalent: there is nothing to put in `.env` here, since
    Baileys' auth state is a local session directory, not a secret string.
    """

    enabled: bool = False
    # E.164-style numbers without the leading "+" (WhatsApp JIDs are
    # "<number>@s.whatsapp.net") - empty means deny all, same
    # deny-by-default posture Telegram's own empty allowlists already have.
    allowed_numbers: list[str] = Field(default_factory=list)
    bridge_port: int = 8091
    # Override if `node` isn't on PATH.
    node_path: str | None = None


class ChannelsConfig(StrictBaseModel):
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    whatsapp: WhatsAppConfig = Field(default_factory=WhatsAppConfig)


class LLMProfileConfig(StrictBaseModel):
    provider: str = "openai_compatible"
    model: str
    base_url: str | None = None
    api_key_env: str | None = None
    api_key: SecretStr | None = None
    timeout_seconds: int = Field(default=60, ge=1)
    max_tokens: int = Field(default=6144, ge=1)
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    context_limit: int | None = None


class LLMConfig(StrictBaseModel):
    default_profile: str = "default"
    major_profile: str | None = None  # Profile for complex/major tasks (e.g. gemma4 with large context)
    # Profile used when the primary profile is unreachable (connection error,
    # timeout, or HTTP 5xx). Keeps the whole stack usable while the local
    # model is down or still warming up. Superseded by fallback_chain when
    # that is non-empty; kept working on its own so an existing config with
    # only this set is unaffected (see providers.py's _chain_profile_names).
    fallback_profile: str | None = None
    # Ordered list of profiles to try, in order, after default_profile fails
    # - each entry gets its own cooldown window on failure (providers.py's
    # ChainLLMProvider) instead of every call re-paying that entry's timeout
    # before moving on. Empty means "use fallback_profile alone", not "no
    # fallback" - this is additive, not a replacement for that field.
    fallback_chain: list[str] = Field(default_factory=list)
    # Per-role model selection (docs/ROADMAP.md): None means "use
    # default_profile", so an existing config naming only default_profile
    # keeps today's behavior - Concierge, Operator, and Auditor all sharing
    # one model - unchanged. Set one to route that role through a different
    # profile (its own fallback_chain/fallback_profile still apply).
    concierge_profile: str | None = None
    operator_profile: str | None = None
    auditor_profile: str | None = None
    profiles: dict[str, LLMProfileConfig] = Field(default_factory=dict)

    @field_validator("profiles")
    @classmethod
    def profile_names_required(cls, value: dict[str, LLMProfileConfig]) -> dict[str, LLMProfileConfig]:
        for name in value:
            if not name:
                raise ValueError("LLM profile names cannot be empty")
        return value


class CapabilityPolicy(StrictBaseModel):
    enabled: bool = False
    scopes: list[str] = Field(default_factory=list)
    requires_approval: bool = True
    max_risk_level: RiskLevel = RiskLevel.LOW
    allow_patterns: list[str] = Field(default_factory=list)
    deny_patterns: list[str] = Field(default_factory=list)


class ApprovalPolicyConfig(StrictBaseModel):
    default_timeout_seconds: int = Field(default=900, ge=1)
    require_approval_at_or_above: RiskLevel = RiskLevel.MEDIUM
    approval_token_ttl_seconds: int = Field(default=900, ge=1)


class StorageConfig(StrictBaseModel):
    # Runtime state otherwise lives entirely under .agent_control/ (artifacts,
    # the secret vault, workspaces, code_interpreter output) - the database
    # used to be the one exception, sitting in the repo root. storage/database.py's
    # Database.__init__ auto-migrates an existing repo-root file into this
    # location the first time it's constructed with this exact default
    # (docs/HISTORY.md P6); it never touches a path a caller customized.
    database_url: str = "sqlite:///.agent_control/agent_control.db"
    artifact_dir: str = ".agent_control/artifacts"
    # The receipts (docs/UI_UX_AUDIT.md Phase 14d): one row per LLM call the
    # operator/auditor/subagent make, with the redacted prompt, the raw
    # response text, tokens, and real latency - what turns the trace's
    # inferred "operator thinking" gaps into measured time. On by default,
    # since receipts are the product's whole claim; llm_call_max_chars caps
    # both the response text and each message's content, independently, so
    # one verbose call can't bloat the database unbounded.
    persist_llm_calls: bool = True
    llm_call_max_chars: int = Field(default=8000, ge=100)
    # Task/audit history otherwise accumulates forever - `ybm db-clean` exists
    # (db_tools.py) but is a manual, explicit action. None (the default) keeps
    # that manual-only behavior unchanged rather than silently starting to
    # delete a user's history on a fresh upgrade; set this to opt into the
    # scheduler sweeping rows older than N days on its own, once a day
    # (scheduler.py's retention_sweep, wired through run_scheduler_forever).
    retention_days: int | None = Field(default=None, ge=1)


class SecretVaultConfig(StrictBaseModel):
    path: str = ".agent_control/secrets/vault.json"
    key_env: str = "AGENT_SECRET_VAULT_KEY"


class SchedulerConfig(StrictBaseModel):
    enabled: bool = True
    poll_interval_seconds: int = Field(default=30, ge=1, le=3600)
    default_timezone: str = "America/Chicago"
    # A schedule whose spawned task fails this many times in a row is
    # auto-paused rather than left to keep failing silently and unnoticed
    # (docs/HISTORY.md P6 - the motivating case was 7 real schedules whose
    # target had gone away, still firing and failing every day for weeks).
    max_consecutive_failures: int = Field(default=5, ge=1, le=100)


class LoggingConfig(StrictBaseModel):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    json_logs: bool = True
    redact_patterns: list[str] = Field(default_factory=lambda: ["token", "api_key", "secret", "password"])


class LimitsConfig(StrictBaseModel):
    max_parallel_tasks: int = Field(default=1, ge=1)
    # Say something while a long step is still running. Progress was purely
    # status-driven, so a task inside one 40-minute tool call went silent -
    # indistinguishable, from the outside, from having died. 0 disables.
    heartbeat_interval_seconds: int = Field(default=300, ge=0)
    max_retries: int = Field(default=3, ge=0)
    tool_timeout_seconds: int = Field(default=900, ge=1)
    max_log_chars: int = Field(default=12000, ge=100)
    retry_backoff_seconds: int = Field(default=30, ge=1)
    # Wall-clock budget per task enforced by the worker. When exceeded the
    # task is forcibly transitioned to FAILED so the queue keeps moving.
    # Individual tasks can override via metadata["task_budget_seconds"].
    task_budget_seconds: int = Field(default=600, ge=30)


class OperatorConfig(StrictBaseModel):
    # The observe/decide/act agent loop (docs/HISTORY.md P3 §2.2) - the sole
    # execution path as of 2026-07-28 (the old plan-once-then-replan path and
    # its keyword-driven recovery were deleted, not just defaulted off).
    max_steps: int = Field(default=12, ge=1, le=50)
    # Who decides whether a `done` actually delivered what was asked.
    #
    # "auditor"   - the Auditor judges it, reading the user's own words and a
    #               factual list of what the task produced. Default.
    # "heuristic" - the legacy gate in fulfillment.py, which infers intent by
    #               intersecting the message with hardcoded word sets and can
    #               veto a `done` both the Operator and the Auditor accepted.
    #               Kept as a rollback path, not as a recommendation.
    fulfillment_mode: Literal["auditor", "heuristic"] = "auditor"
    # Run the Auditor only once a task has made at least this many real tool
    # calls. Measured: 49 auditor calls averaging 11.1s each, on a loop where
    # the whole request is often 12s - so on a one-tool task the grounding
    # pass can cost as much as the work it is checking.
    #
    # The tradeoff is real and this is why it is a setting rather than a
    # heuristic: the Auditor also performs the count/section check ("are all
    # 5 files here?") and extracts the focused answer. Skipping it means
    # trusting the Operator's own final_answer for single-tool tasks, where
    # it has just read that one result directly. Set to 1 to audit everything,
    # as before.
    audit_min_tool_calls: int = Field(default=2, ge=1, le=10)


class VSCodeAdapterConfig(StrictBaseModel):
    enabled: bool = False
    bridge_host: str = "127.0.0.1"
    bridge_port: int = Field(default=8766, ge=1, le=65535)
    auth_token_env: str = "VSCODE_BRIDGE_TOKEN"


class WorkspaceAdapterConfig(StrictBaseModel):
    enabled: bool = True
    root_dir: str = ".agent_control/workspaces"
    web_host: str = "127.0.0.1"
    web_port_start: int = Field(default=8890, ge=1, le=65535)
    open_browser: bool = True


class BrowserAdapterConfig(StrictBaseModel):
    enabled: bool = True
    host: str = "127.0.0.1"
    remote_debugging_port: int = Field(default=9222, ge=1, le=65535)
    chrome_path: str | None = None
    user_data_dir: str = ".agent_control/browser/chrome-profile"
    screenshot_dir: str = ".agent_control/browser/screenshots"
    launch_if_missing: bool = True
    startup_timeout_seconds: int = Field(default=10, ge=1, le=60)
    default_wait_seconds: float = Field(default=1.5, ge=0.0, le=30.0)
    max_summary_chars: int = Field(default=6000, ge=500, le=50000)
    search_url_template: str = "https://www.bing.com/search?q={query}"


class AdapterFactoryConfig(StrictBaseModel):
    enabled: bool = True
    root_dir: str = ".agent_control/adapters"


class HttpRequestAdapterConfig(StrictBaseModel):
    enabled: bool = True
    allowed_hosts: list[str] = Field(default_factory=list)
    allowed_url_prefixes: list[str] = Field(default_factory=list)
    blocked_hosts: list[str] = Field(default_factory=list)
    timeout_seconds: int = Field(default=30, ge=1, le=300)
    max_response_chars: int = Field(default=100000, ge=100, le=1000000)
    max_body_chars: int = Field(default=100000, ge=0, le=1000000)
    user_agent: str = "YBM-http-request/1.0"


class WebSearchAdapterConfig(StrictBaseModel):
    """Structured web search, so "search online and analyse this" does not
    have to mean driving Chrome through a results page.

    `duckduckgo` needs no key and no account, which is why it is the default -
    a fresh install can search immediately. `brave` wants a key in
    `api_key_env`; `searxng` wants `base_url` pointing at an instance the
    operator runs, so no query leaves their own network.
    """

    enabled: bool = True
    provider: Literal["duckduckgo", "brave", "searxng"] = "duckduckgo"
    api_key_env: str | None = "BRAVE_SEARCH_API_KEY"
    base_url: str | None = None
    max_results: int = Field(default=5, ge=1, le=25)
    timeout_seconds: int = Field(default=20, ge=1, le=120)
    user_agent: str = "Mozilla/5.0 (compatible; YBM/1.0)"


class TerminalAdapterConfig(StrictBaseModel):
    enabled: bool = False
    allowed_working_dirs: list[str] = Field(default_factory=list)
    blocked_command_patterns: list[str] = Field(default_factory=lambda: ["rm -rf", "del /s", "format "])


class CodingAssistantAdapterConfig(StrictBaseModel):
    enabled: bool = False
    command_template: list[str] = Field(default_factory=list)
    working_dir: str | None = None
    timeout_seconds: int = Field(default=900, ge=1)
    output_limit_chars: int = Field(default=12000, ge=100)
    rate_limit_patterns: list[str] = Field(default_factory=lambda: ["rate limit", "too many requests"])
    usage_limit_patterns: list[str] = Field(
        default_factory=lambda: ["usage limit", "quota exceeded", "quota_exceeded", "no quota"]
    )


class CodingAgentAdapterConfig(StrictBaseModel):
    enabled: bool = True
    workspace_root: str = ".agent_control/workspaces"
    session_root: str = ".agent_control/coding_sessions"
    codex_path: str | None = None
    copilot_path: str | None = None
    claude_path: str | None = None
    # Production sessions are launched through a small runner process so a
    # worker restart does not lose the final result. Unit tests may inject a
    # spawner and keep the direct CLI command path.
    use_runner: bool = True
    # Max wall-clock for a background session before it is terminated.
    timeout_seconds: int = Field(default=3600, ge=1)
    # How long `start` waits inline before handing the run to the background
    # watcher. Quick runs return their final result immediately.
    start_wait_seconds: int = Field(default=20, ge=0)
    # Keep the source chat informed while a coding CLI is still running. The
    # watcher persists each heartbeat in the session file, so restarts do not
    # reset the cadence or produce duplicate updates.
    progress_interval_seconds: int = Field(default=300, ge=30, le=3600)
    output_limit_chars: int = Field(default=20000, ge=100)
    rate_limit_patterns: list[str] = Field(default_factory=lambda: ["rate limit", "too many requests"])
    usage_limit_patterns: list[str] = Field(
        default_factory=lambda: [
            "usage limit",
            "quota exceeded",
            "quota_exceeded",
            "no quota",
            "limit reached",
            "messages are exhausted",
        ]
    )
    codex_sandbox: str = "workspace-write"
    codex_skip_git_repo_check: bool = True
    claude_permission_mode: str = "acceptEdits"
    copilot_allow_all: bool = True
    copilot_no_ask_user: bool = True


class MCPServerConfig(StrictBaseModel):
    enabled: bool = True
    command: str = Field(min_length=1)
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str | None = None
    timeout_seconds: int = Field(default=30, ge=1, le=900)
    capability: Capability = Capability.TERMINAL_RUN
    risk_level: RiskLevel = RiskLevel.HIGH
    disabled_tools: list[str] = Field(default_factory=list)
    max_output_chars: int = Field(default=20000, ge=100, le=200000)


class MCPConfig(StrictBaseModel):
    enabled: bool = False
    servers: dict[str, MCPServerConfig] = Field(default_factory=dict)
    cache_ttl_seconds: int = Field(default=60, ge=0, le=3600)
    catalog_path: str = ".agent_control/mcp/tool_catalog.json"


class CodeInterpreterResourceLimitsConfig(StrictBaseModel):
    memory: str = "512m"
    cpus: float = Field(default=1.0, gt=0, le=32)
    pids_limit: int = Field(default=128, ge=1, le=4096)


class CodeInterpreterDockerConfig(StrictBaseModel):
    enabled: bool = False
    image: str = "python:3.12-slim"
    docker_path: str = "docker"
    pull_policy: Literal["never", "missing", "always"] = "missing"
    network_enabled: bool = False
    read_only_rootfs: bool = False
    workspace_mount_target: str = "/workspace"
    remove_container: bool = True
    run_as_user: str | None = None


class CodeInterpreterJupyterConfig(StrictBaseModel):
    enabled: bool = False
    image: str = "python:3.12-slim"
    idle_timeout_seconds: int = Field(default=900, ge=30, le=86400)


class CodeInterpreterRemoteBackendConfig(StrictBaseModel):
    enabled: bool = False
    api_key_env: str | None = None
    timeout_seconds: int = Field(default=120, ge=1, le=3600)
    risk_level: RiskLevel = RiskLevel.HIGH
    max_output_chars: int = Field(default=20000, ge=100, le=200000)


class CodeInterpreterAdapterConfig(StrictBaseModel):
    enabled: bool = True
    workspace_root: str = ".agent_control/code_interpreter"
    timeout_seconds: int = Field(default=60, ge=1, le=900)
    max_code_chars: int = Field(default=20000, ge=100, le=200000)
    max_output_chars: int = Field(default=12000, ge=100, le=100000)
    max_files_listed: int = Field(default=200, ge=1, le=5000)
    python_executable: str | None = None
    allowed_imports: list[str] = Field(default_factory=list)
    blocked_imports: list[str] = Field(
        default_factory=lambda: [
            "ctypes",
            "ftplib",
            "httpx",
            # importlib.import_module("os")/("subprocess") dynamically
            # imports a module without a static Import/ImportFrom AST node -
            # the other entries in this list wouldn't catch it on their own.
            "importlib",
            # multiprocessing can spawn new OS processes, the same risk
            # class as subprocess, just a different stdlib door to it.
            "multiprocessing",
            "os",
            "pip",
            "requests",
            "shutil",
            "socket",
            "subprocess",
            "sys",
            "urllib",
            # Windows registry read/write - high-impact, Windows-specific.
            "winreg",
        ]
    )
    default_backend: str = "local_subprocess"
    backends: list[str] = Field(default_factory=lambda: ["local_subprocess"])
    untrusted_default_backend: str = "docker_python"
    fallback_to_local_when_backend_unavailable: bool = True
    require_approval_for_untrusted_run_python: bool = True
    network_policy: Literal["always_disabled", "disabled_by_default", "allow_if_requested"] = "disabled_by_default"
    package_policy: Literal["disabled", "allow_configured", "allow_request"] = "disabled"
    allowed_packages: list[str] = Field(default_factory=list)
    resource_limits: CodeInterpreterResourceLimitsConfig = Field(default_factory=CodeInterpreterResourceLimitsConfig)
    docker: CodeInterpreterDockerConfig = Field(default_factory=CodeInterpreterDockerConfig)
    jupyter: CodeInterpreterJupyterConfig = Field(default_factory=CodeInterpreterJupyterConfig)
    remote_backends: dict[str, CodeInterpreterRemoteBackendConfig] = Field(default_factory=dict)
    session_ttl_seconds: int = Field(default=900, ge=30, le=86400)


class DesktopAdapterConfig(StrictBaseModel):
    screenshot_enabled: bool = False
    control_enabled: bool = False
    screenshot_interval_seconds: int = Field(default=10, ge=1)
    screenshot_format: Literal["png"] = "png"


class ComputerUseAdapterConfig(StrictBaseModel):
    enabled: bool = False
    max_steps: int = Field(default=8, ge=1, le=50)
    step_delay_seconds: float = Field(default=0.4, ge=0.0, le=10.0)
    screenshot_dir: str = ".agent_control/computer_use/screenshots"
    allowed_apps: list[str] = Field(default_factory=list)
    allowed_roots: list[str] = Field(default_factory=lambda: [".agent_control/workspaces"])
    require_session_approval: bool = True
    max_ui_elements: int = Field(default=80, ge=0, le=500)


class ArtifactDeliveryAdapterConfig(StrictBaseModel):
    recent_artifact_fallback_enabled: bool = False


class STTAdapterConfig(StrictBaseModel):
    enabled: bool = False
    provider: str = "faster_whisper"
    model: str = "base"
    device: str = "cpu"
    compute_type: str = "int8"
    language: str | None = None
    vad_filter: bool = True
    beam_size: int = Field(default=5, ge=1, le=20)
    timeout_seconds: int = Field(default=120, ge=1)
    temp_dir: str = ".agent_control/stt"
    command: list[str] = Field(default_factory=list)
    static_transcript_env: str = "AGENT_STT_STATIC_TRANSCRIPT"


class TTSAdapterConfig(StrictBaseModel):
    enabled: bool = False
    provider: str = "kokoro_onnx"
    model_path: str | None = None
    voices_path: str | None = None
    voice: str = "af_sarah"
    language: str = "en-us"
    speed: float = Field(default=1.0, ge=0.25, le=4.0)
    output_dir: str = ".agent_control/tts"
    timeout_seconds: int = Field(default=120, ge=1)


class DependenciesAdapterConfig(StrictBaseModel):
    """Package installs, allowlisted and scoped to a task directory.

    `allowed_packages` is empty by default on purpose: enabling the capability
    should not by itself let an agent install arbitrary code. Name what is
    acceptable first. Installs never touch the interpreter running YBM - they
    go to `target_root/<task_id>` via pip --target.
    """

    enabled: bool = False
    target_root: str = ".agent_control/dependencies"
    allowed_packages: list[str] = Field(default_factory=list)
    max_packages_per_call: int = Field(default=5, ge=1, le=50)
    timeout_seconds: int = Field(default=300, ge=10, le=1800)
    max_output_chars: int = Field(default=8000, ge=500, le=100000)


class SkillsAdapterConfig(StrictBaseModel):
    """User-droppable capability packs (docs/HISTORY.md Part 4 T1.3): a flat
    directory of markdown files, each with a name/description in YAML
    frontmatter and full instructions as the body. Adding one is copying a
    file in - no code change, no restart of anything but the worker process
    picking up the new file on its next tool-registry build.
    """

    enabled: bool = True
    root_dir: str = ".agent_control/skills"
    # Offer to save a procedure after a task succeeds through a novel
    # sequence. Off by default, and proposals are queued for review rather
    # than installed - a skill is instructions the Operator will follow on
    # future tasks, so writing one unattended changes tomorrow's behavior
    # with nobody having agreed to it. See skill_learning.py.
    learning_enabled: bool = False
    max_skills_listed: int = Field(default=50, ge=1, le=500)


class PersonaAdapterConfig(StrictBaseModel):
    """A single stable identity/preference document, injected into every
    Operator prompt (docs/HISTORY.md Part 4 T2.5) - separate from
    channels/memory.py's ConversationMemoryService, which is per-conversation
    short-term recall, not a global, cross-conversation preference store.
    """

    enabled: bool = True
    path: str = ".agent_control/persona.md"
    # Watch the operator's own messages for durable preferences ("keep it
    # short", "stop asking me that") and queue them for review. Off by
    # default: learning from someone's phrasing should be chosen, not
    # discovered later. Suggestions are never written straight to the file -
    # see persona_learning.py for why review is not optional here.
    learning_enabled: bool = False
    max_chars: int = Field(default=4000, ge=1, le=20000)


class KnowledgeBaseAdapterConfig(StrictBaseModel):
    """A local, personal document index (docs/HISTORY.md Part 4 T2.7):
    lexical (keyword-overlap) search over a folder of the user's own files -
    notes, docs, reference material - so the Operator can answer from what
    the user already has instead of only from tool output gathered mid-task.

    Deliberately keyword-based, not embedding/vector search: no extra model
    dependency (embeddings would need either a live API call per index/query
    or bundling a local embedding model), fully deterministic, and testable
    with zero network or GPU - a real trade-off (semantic near-misses are
    not found), not a placeholder for "real" search later.
    """

    enabled: bool = True
    root_dir: str = ".agent_control/knowledge"
    max_files_indexed: int = Field(default=500, ge=1, le=5000)
    max_chars_per_file: int = Field(default=20000, ge=100, le=200000)
    max_results: int = Field(default=5, ge=1, le=50)


class AdaptersConfig(StrictBaseModel):
    vscode: VSCodeAdapterConfig = Field(default_factory=VSCodeAdapterConfig)
    workspace: WorkspaceAdapterConfig = Field(default_factory=WorkspaceAdapterConfig)
    browser: BrowserAdapterConfig = Field(default_factory=BrowserAdapterConfig)
    adapter_factory: AdapterFactoryConfig = Field(default_factory=AdapterFactoryConfig)
    http_request: HttpRequestAdapterConfig = Field(default_factory=HttpRequestAdapterConfig)
    terminal: TerminalAdapterConfig = Field(default_factory=TerminalAdapterConfig)
    coding_assistant: CodingAssistantAdapterConfig = Field(default_factory=CodingAssistantAdapterConfig)
    coding_agent: CodingAgentAdapterConfig = Field(default_factory=CodingAgentAdapterConfig)
    code_interpreter: CodeInterpreterAdapterConfig = Field(default_factory=CodeInterpreterAdapterConfig)
    desktop: DesktopAdapterConfig = Field(default_factory=DesktopAdapterConfig)
    computer_use: ComputerUseAdapterConfig = Field(default_factory=ComputerUseAdapterConfig)
    artifact_delivery: ArtifactDeliveryAdapterConfig = Field(default_factory=ArtifactDeliveryAdapterConfig)
    stt: STTAdapterConfig = Field(default_factory=STTAdapterConfig)
    tts: TTSAdapterConfig = Field(default_factory=TTSAdapterConfig)
    skills: SkillsAdapterConfig = Field(default_factory=SkillsAdapterConfig)
    dependencies: DependenciesAdapterConfig = Field(default_factory=DependenciesAdapterConfig)
    web_search: WebSearchAdapterConfig = Field(default_factory=WebSearchAdapterConfig)
    persona: PersonaAdapterConfig = Field(default_factory=PersonaAdapterConfig)
    knowledge_base: KnowledgeBaseAdapterConfig = Field(default_factory=KnowledgeBaseAdapterConfig)


def default_capability_policies() -> dict[Capability, CapabilityPolicy]:
    policies = {capability: CapabilityPolicy() for capability in Capability}
    policies[Capability.TELEGRAM_RECEIVE] = CapabilityPolicy(
        enabled=True,
        requires_approval=False,
        max_risk_level=RiskLevel.LOW,
    )
    policies[Capability.TELEGRAM_SEND] = CapabilityPolicy(
        enabled=True,
        requires_approval=False,
        max_risk_level=RiskLevel.LOW,
    )
    policies[Capability.LLM_GENERATE] = CapabilityPolicy(
        enabled=True,
        requires_approval=False,
        max_risk_level=RiskLevel.LOW,
    )
    return policies


class AppSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AGENT_",
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        dotenv_filtering="only_existing",
        yaml_file="config/config.yaml",
        yaml_file_encoding="utf-8",
        extra="forbid",
    )

    server: ServerConfig = Field(default_factory=ServerConfig)
    identity: IdentityConfig = Field(default_factory=IdentityConfig)
    channels: ChannelsConfig = Field(default_factory=ChannelsConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    capabilities: dict[Capability, CapabilityPolicy] = Field(default_factory=default_capability_policies)
    approval_policy: ApprovalPolicyConfig = Field(default_factory=ApprovalPolicyConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    secrets: SecretVaultConfig = Field(default_factory=SecretVaultConfig)
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    adapters: AdaptersConfig = Field(default_factory=AdaptersConfig)
    mcp: MCPConfig = Field(default_factory=MCPConfig)
    operator: OperatorConfig = Field(default_factory=OperatorConfig)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            YamlConfigSettingsSource(settings_cls),
            file_secret_settings,
        )

    def safe_summary(self) -> dict[str, Any]:
        return {
            "server": self.server.model_dump(),
            "identity": self.identity.model_dump(),
            "channels": {
                "telegram": {
                    "enabled": self.channels.telegram.enabled,
                    "token_env": self.channels.telegram.token_env,
                    "token": "***" if self.channels.telegram.token else None,
                    "token_present": bool(self.channels.telegram.token)
                    or bool(read_env_value(self.channels.telegram.token_env)),
                    "allowed_user_ids": self.channels.telegram.allowed_user_ids,
                    "allowed_chat_ids": self.channels.telegram.allowed_chat_ids,
                    "allowed_user_count": len(self.channels.telegram.allowed_user_ids),
                    "allowed_chat_count": len(self.channels.telegram.allowed_chat_ids),
                    "polling": self.channels.telegram.polling,
                },
                "whatsapp": {
                    "enabled": self.channels.whatsapp.enabled,
                    # Phone numbers are PII, unlike Telegram's numeric chat/user
                    # ids above - count only, never the numbers themselves, in
                    # a response an admin API caller could log or screenshot.
                    "allowed_number_count": len(self.channels.whatsapp.allowed_numbers),
                    "bridge_port": self.channels.whatsapp.bridge_port,
                    "node_path": self.channels.whatsapp.node_path,
                },
            },
            "llm": {
                "default_profile": self.llm.default_profile,
                "major_profile": self.llm.major_profile,
                "fallback_profile": self.llm.fallback_profile,
                "fallback_chain": self.llm.fallback_chain,
                "concierge_profile": self.llm.concierge_profile,
                "operator_profile": self.llm.operator_profile,
                "auditor_profile": self.llm.auditor_profile,
                "profiles": {
                    name: {
                        "provider": profile.provider,
                        "model": profile.model,
                        "base_url": profile.base_url,
                        "api_key_env": profile.api_key_env,
                        "api_key": "***" if profile.api_key else None,
                        "api_key_present": bool(profile.api_key)
                        or bool(profile.api_key_env and read_env_value(profile.api_key_env)),
                        "timeout_seconds": profile.timeout_seconds,
                        "max_tokens": profile.max_tokens,
                        "temperature": profile.temperature,
                    }
                    for name, profile in self.llm.profiles.items()
                },
            },
            "capabilities": {
                capability.value: policy.model_dump(mode="json")
                for capability, policy in sorted(self.capabilities.items(), key=lambda item: item[0].value)
            },
            "approval_policy": self.approval_policy.model_dump(mode="json"),
            "storage": self.storage.model_dump(),
            "secrets": {
                "path": self.secrets.path,
                "key_env": self.secrets.key_env,
                "key_present": bool(read_env_value(self.secrets.key_env)),
            },
            "scheduler": self.scheduler.model_dump(),
            "logging": self.logging.model_dump(),
            "limits": self.limits.model_dump(),
            "adapters": self.adapters.model_dump(),
            "mcp": {
                "enabled": self.mcp.enabled,
                "cache_ttl_seconds": self.mcp.cache_ttl_seconds,
                "catalog_path": self.mcp.catalog_path,
                "servers": {
                    # MCP servers commonly need secrets (API tokens, etc.) passed via
                    # env - dump every other field but replace values with just their
                    # key names, the same "list the key, never the value" invariant
                    # storage/secrets.py's vault already enforces.
                    name: {
                        **server.model_dump(mode="json", exclude={"env"}),
                        "env_keys": sorted(server.env.keys()),
                    }
                    for name, server in self.mcp.servers.items()
                },
            },
            "operator": self.operator.model_dump(),
        }


def backend_base_url(settings: AppSettings) -> str:
    """The URL other local components should use to reach this backend.

    Falls back from an explicit `public_base_url` to host:port, rewriting a
    wildcard bind (`0.0.0.0` / `::`) to loopback since a wildcard is an
    accept-on-any-interface instruction, not a dialable address. Shared by
    `admin.py` and `cli.py`, which each carried an identical private copy.
    """
    if settings.server.public_base_url:
        return settings.server.public_base_url
    host = "127.0.0.1" if settings.server.host in {"0.0.0.0", "::"} else settings.server.host
    return f"http://{host}:{settings.server.port}"


class ConfigValidationError(ValueError):
    """A settings load failed, described without echoing any offending value.

    Subclasses ``ValueError`` so it stays catchable everywhere pydantic's own
    ``ValidationError`` (also a ``ValueError``) already was.
    """


def _redacted_validation_message(exc: ValidationError) -> str:
    """Describe a config failure by location and reason, never by value.

    Settings are populated from `.env`, so the values pydantic echoes as
    ``input_value=`` are exactly the API keys, bot tokens, and vault keys the
    project promises never to log. `ybm doctor` formats this text straight into
    its output (`bootstrap._load_settings_checked`), which users paste into bug
    reports, so the field path and error type have to be enough on their own.
    """
    lines = [f"{exc.error_count()} configuration error(s) in {exc.title}:"]
    for error in exc.errors(include_url=False, include_input=False, include_context=False):
        location = ".".join(str(part) for part in error.get("loc") or ()) or "(root)"
        lines.append(f"  {location}: {error.get('msg') or 'invalid'} [{error.get('type') or 'unknown'}]")
    return "\n".join(lines)


def load_settings(config_path: str | Path | None = None, **overrides: Any) -> AppSettings:
    try:
        if config_path is not None:
            config_file = str(Path(config_path))

            class RuntimePathSettings(AppSettings):
                model_config = SettingsConfigDict(
                    **{
                        **AppSettings.model_config,
                        "yaml_file": config_file,
                    }
                )

            return RuntimePathSettings(**overrides)
        return AppSettings(**overrides)
    except ValidationError as exc:
        redacted = _redacted_validation_message(exc)
    # Raised outside the handler on purpose. `raise ... from None` inside it
    # only sets __suppress_context__, which hides the original unredacted
    # ValidationError from a printed traceback while leaving it reachable on
    # __context__ - where exception reporters that walk the chain still find
    # the secret. Raising here leaves no chain at all.
    raise ConfigValidationError(redacted)
