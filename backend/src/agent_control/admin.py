from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from importlib.metadata import PackageNotFoundError, version as _pkg_version
import importlib.util
import logging
import mimetypes
import os
from functools import lru_cache
from pathlib import Path
import secrets
from typing import Any
from urllib.parse import urlparse

import time

import httpx
from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import Field, SecretStr

from agent_control.bootstrap import OLLAMA_TAGS_URL, _http_json, check_llm_configured, collect_checks
from agent_control.config_sync import CONFIG_FILE_PATH, ConfigManager, read_env_value
from agent_control.config import AppSettings, backend_base_url, is_loopback_host
from agent_control.config import LLMProfileConfig
from agent_control.tools.stt import build_stt_adapter
from agent_control.channels import catalog as channel_catalog
from agent_control.error_text import explain_voice_failure
from agent_control.llm import catalog as llm_catalog
from agent_control.llm import hardware as llm_hardware
from agent_control.llm.providers import build_provider_for_profile
from agent_control.observation.artifacts import ArtifactService
from agent_control.orchestration.signals import apply_task_signal, requeue_after_approval_decision
from agent_control.policy import apply_access_modes_to_config, summarize_access_modes
from agent_control.prompts import render_prompt
from agent_control.runtime_status import KNOWN_SERVICE_NAMES, service_summary
from agent_control.supervisor import read_log_tail
from agent_control.channels.memory import memory_context
from agent_control.clarification import find_clarifying_task, resume_clarifying_task
from agent_control.schemas import (
    ApprovalGrant,
    ApprovalStatus,
    ArtifactType,
    AuditEventType,
    Capability,
    CapabilityAccessMode,
    ChannelType,
    MemoryFact,
    MemorySource,
    StrictBaseModel,
    TaskRecord,
    TaskStatus,
    utc_now,
)
from agent_control.storage.audit import AuditLogger
from agent_control.storage.audit_view import format_audit_event
from agent_control.storage.database import Database
from agent_control.storage.repositories import Repositories
from agent_control.storage.redaction import redact_payload
from agent_control.storage.secrets import SecretVault, SecretVaultError
from agent_control.tools.registry import build_tool_registry
from agent_control.tools.artifact_delivery import artifact_delivery_roots, path_from_uri
from agent_control.tools.effects import classify_effect
from agent_control.tools.skills import delete_skill_file, detect_referenced_tools, list_skills_detailed, write_skill_file
from agent_control.tools.vscode_bridge import VSCodeBridgeStore, VSCodeTerminalCommand


class AdminTerminalCommandRequest(StrictBaseModel):
    command: str = Field(min_length=1, max_length=4000)
    terminal_id: str = "agent-control"
    instance_id: str | None = None
    cwd: str | None = None


class AdminTaskSignalRequest(StrictBaseModel):
    signal: str = Field(pattern="^(pause|resume|cancel)$")


class AdminLLMConfigRequest(StrictBaseModel):
    profile_name: str = Field(default="default", min_length=1, max_length=80)
    default_profile: str = Field(default="default", min_length=1, max_length=80)
    provider: str = Field(default="openai_compatible", min_length=1, max_length=80)
    model: str = Field(min_length=1, max_length=200)
    base_url: str | None = None
    api_key_env: str | None = None
    timeout_seconds: int = Field(default=60, ge=1, le=3600)
    max_tokens: int = Field(default=4096, ge=1, le=262144)
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    api_key_value: str | None = None


class AdminLLMPresetRequest(StrictBaseModel):
    preset: str = Field(min_length=1, max_length=80)


class AdminVoiceConfigRequest(StrictBaseModel):
    enabled: bool
    model: str | None = Field(default=None, min_length=1, max_length=80)


class AdminTelegramConfigRequest(StrictBaseModel):
    enabled: bool | None = None
    token_env: str | None = Field(default=None, min_length=1, max_length=120)
    allowed_user_ids: list[int] | None = None
    allowed_chat_ids: list[int] | None = None
    polling: bool | None = None
    bot_token: str | None = None


class AdminTelegramProbeRequest(StrictBaseModel):
    """Shared by the token test and operator detection. bot_token is optional
    so both work against an already-saved token, and so the Connect flow can
    verify a pasted one before it is persisted anywhere."""

    bot_token: str | None = None


class AdminLLMVerifyRequest(StrictBaseModel):
    """Setup-time only: the key may not be saved to .env yet."""

    provider: str = Field(min_length=1, max_length=60)
    api_key: str | None = None
    base_url: str | None = None


class AdminLLMTestRequest(StrictBaseModel):
    """Setup-time only: nothing is saved until the model actually answers."""

    provider: str = Field(min_length=1, max_length=60)
    model: str = Field(min_length=1, max_length=200)
    api_key: str | None = None
    base_url: str | None = None


class AdminTelegramVerifyRequest(StrictBaseModel):
    """Setup-time only: the token may not be saved to .env yet."""

    bot_token: str | None = None
    token_env: str | None = Field(default=None, min_length=1, max_length=120)
    wait_seconds: int | None = Field(default=None, ge=5, le=60)


class AdminVSCodeConfigRequest(StrictBaseModel):
    enabled: bool | None = None
    bridge_host: str | None = None
    bridge_port: int | None = Field(default=None, ge=1, le=65535)
    auth_token_env: str | None = None
    bridge_token: str | None = None


class AdminWorkspaceConfigRequest(StrictBaseModel):
    enabled: bool | None = None
    root_dir: str | None = None
    web_host: str | None = None
    web_port_start: int | None = Field(default=None, ge=1, le=65535)
    open_browser: bool | None = None


class AdminComputerUseConfigRequest(StrictBaseModel):
    enabled: bool | None = None
    max_steps: int | None = Field(default=None, ge=1, le=50)
    step_delay_seconds: float | None = Field(default=None, ge=0.0, le=10.0)
    screenshot_dir: str | None = None
    allowed_apps: list[str] | None = None
    allowed_roots: list[str] | None = None
    require_session_approval: bool | None = None
    max_ui_elements: int | None = Field(default=None, ge=0, le=500)


class AdminAccessModesRequest(StrictBaseModel):
    modes: dict[str, CapabilityAccessMode]


class AdminApprovalDecisionRequest(StrictBaseModel):
    # approve_for_task (docs/UI_UX_AUDIT.md Phase 1's "Allow for this task"):
    # approves this one call exactly like "approve", and additionally
    # creates an ApprovalGrant so the same tool+capability doesn't need
    # re-approval for the rest of this task (ApprovalGrantRepository.
    # find_matching, consulted by ToolExecutor before PolicyEngine.evaluate).
    decision: str = Field(pattern="^(approve|reject|approve_for_task)$")


class AdminSecretSetRequest(StrictBaseModel):
    service: str = Field(min_length=1, max_length=80)
    key: str = Field(min_length=1, max_length=80)
    value: str = Field(min_length=1, max_length=8000)


class AdminMemoryFactCreateRequest(StrictBaseModel):
    category: str = Field(min_length=1, max_length=60)
    content: str = Field(min_length=1, max_length=2000)


class AdminMemoryFactUpdateRequest(StrictBaseModel):
    category: str = Field(min_length=1, max_length=60)
    content: str = Field(min_length=1, max_length=2000)


class AdminSkillInstallRequest(StrictBaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=400)
    body: str = Field(min_length=1, max_length=20000)
    version: str = Field(default="1", max_length=20)
    tools: list[str] = Field(default_factory=list)


class AdminChatMessageRequest(StrictBaseModel):
    text: str = Field(min_length=1, max_length=4000)
    # Artifact ids from a prior POST /api/chat/attachments upload
    # (docs/UI_UX_AUDIT.md Phase 1). Folded into the objective as a plain
    # note, same technique as clarification.py's answer-folding - the
    # operator still needs FILESYSTEM_READ (or whichever capability the
    # tool it picks requires) enabled and in scope to actually read the
    # file; attaching it does not grant access on its own.
    attachment_ids: list[str] = Field(default_factory=list, max_length=10)


MAX_CHAT_ATTACHMENT_BYTES = 10 * 1024 * 1024


# Single fixed local chat "chat_id" (docs/HISTORY.md Part 4 T2.8): this is a
# personal, local-first, single-user system, not multi-tenant - one thread
# is the right scope for v1, the same way there is one Telegram user.
WEB_CHAT_ID = "local"


def _redact_admin_output(value: Any, settings: AppSettings) -> Any:
    """Remove configured secret values from data returned by admin APIs.

    Audit logging already redacts sensitive keys, but older task/audit rows
    may contain an exception string with a credential embedded in a URL.
    Response-boundary redaction protects every admin surface, including raw
    trace JSON, without mutating historical records.
    """
    env_names = {
        settings.server.admin_token_env,
        settings.channels.telegram.token_env,
        settings.secrets.key_env,
        settings.adapters.vscode.auth_token_env,
    }
    env_names.update(
        profile.api_key_env
        for profile in settings.llm.profiles.values()
        if profile.api_key_env
    )
    secret_values = [secret for name in env_names if (secret := read_env_value(name))]
    return redact_payload(
        value,
        patterns=("__ybm_no_key_redaction__",),
        secret_values=secret_values,
    )


def _task_with_artifacts(task: TaskRecord, repositories: Repositories, settings: AppSettings) -> dict[str, Any]:
    """A task dict plus its artifacts inline (docs/UI_UX_AUDIT.md Phase 1's
    "artifact cards") - the trace endpoint already does this
    (repositories.artifacts.list_for_task); chat never had, so a file a task
    produced was invisible unless you opened its trace.
    """
    dumped = _redact_admin_output(task.model_dump(mode="json"), settings)
    dumped["artifacts"] = [
        _redact_admin_output(artifact.model_dump(mode="json"), settings)
        for artifact in repositories.artifacts.list_for_task(task.id)
    ]
    return dumped


try:
    _APP_VERSION = _pkg_version("agent-control-backend")
except PackageNotFoundError:
    # Running from a source checkout without an installed distribution
    # (e.g. a fresh clone before `uv sync`) - not a real version, but not a
    # crash either; the bootstrap endpoint's caller only displays this.
    _APP_VERSION = "dev"

# React console build output (docs/UI_REWRITE_PLAN.md §9 Phase 0.2/0.5,
# frontend/vite.config.ts's build.outDir). Resolved relative to this file,
# not cwd, so it works the same whether the backend is started from the
# repo root (ybm start) or as an installed package.
_STATIC_ADMIN_DIR = Path(__file__).parent / "static" / "admin"

# Bundled starter skills (docs/UI_UX_AUDIT.md Phase 11) - committed, unlike
# adapters.skills.root_dir which is generated/gitignored, so a starter
# catalog has somewhere to actually ship from. Same file-based-not-cwd-based
# resolution as _STATIC_ADMIN_DIR above, four parents up from
# backend/src/agent_control/admin.py to the repo root.
_STARTER_SKILLS_DIR = Path(__file__).resolve().parents[3] / "skills" / "starter"

# Browser-renderable formats that do not execute script. Active content such
# as HTML and SVG is always served as an attachment, even when the caller asks
# for an inline preview. The response also gets a sandbox CSP below as a second
# boundary in case a browser sniffs or a MIME database misclassifies a file.
_SAFE_INLINE_MEDIA_TYPES = frozenset(
    {
        "application/json",
        "application/pdf",
        "audio/mpeg",
        "audio/ogg",
        "audio/wav",
        "image/gif",
        "image/jpeg",
        "image/png",
        "image/webp",
        "text/plain",
        "video/mp4",
        "video/ogg",
        "video/webm",
    }
)
_ARTIFACT_GRANT_TTL_SECONDS = 60


SettingsLoader = Callable[[], AppSettings]
RepositoriesLoader = Callable[[], Repositories]


def _origin_is_trusted(request: Request) -> bool:
    """Reject cross-origin browser requests to the admin API.

    There is no CORSMiddleware on this app, so Starlette sends no
    Access-Control-Allow-Origin header - that stops a malicious page's JS
    from *reading* a cross-origin response, but does not stop the browser
    from *sending* a state-changing request in the first place (a plain
    <form enctype="text/plain"> POST needs no preflight and lands here
    regardless). Without a token configured, require_admin's only other
    check is the server's own bind host, not the caller's origin - so on
    the common local, token-less, loopback-only setup, any website the
    admin's browser visits could otherwise trigger mutations on
    127.0.0.1. A same-origin check closes that regardless of token state.
    request.headers.get("origin") is absent for non-browser clients (curl,
    ybm CLI, direct same-origin navigation in older browsers) - nothing to
    compare against, so those are allowed through unchanged.
    """
    origin = request.headers.get("origin")
    if not origin:
        return True
    host_header = request.headers.get("host", "")
    return urlparse(origin).netloc == host_header


def _serve_admin_app(request: Request, sub_path: str) -> FileResponse | HTMLResponse:
    """Serve the React console (docs/UI_REWRITE_PLAN.md §4/§9 Phase 0.2).

    Deliberately NOT behind require_admin, same reasoning as
    admin_bootstrap: the app shell (HTML/JS/CSS) is what shows the
    token-entry screen in the first place, so it can't itself demand a
    token. Actual data stays gated - every /api/* route this app calls is
    unchanged. Still same-origin checked, for consistency with every other
    admin route.

    Three cases, in order:
    1. ``sub_path`` resolves to a real file under the build output
       (``/admin/assets/*.js``, ``/admin/favicon.svg``, ...) - serve it.
    2. A build exists but ``sub_path`` doesn't match a file - serve
       ``index.html`` so React Router's client-side routes
       (``/admin/tasks``, ``/admin/access``, ...) work on a hard refresh.
    3. No build exists yet - fall back to the pointer page that named this
       function's predecessor, so an unbuilt checkout behaves exactly as it
       always has rather than 404ing or crashing.
    """
    if not _origin_is_trusted(request):
        raise HTTPException(
            status_code=403,
            detail="admin API refused: cross-origin request (Origin header does not match Host)",
        )
    if sub_path:
        candidate = (_STATIC_ADMIN_DIR / sub_path).resolve()
        try:
            candidate.relative_to(_STATIC_ADMIN_DIR.resolve())
        except ValueError:
            raise HTTPException(status_code=404, detail="not found") from None
        if candidate.is_file():
            return FileResponse(candidate)
    index_path = _STATIC_ADMIN_DIR / "index.html"
    if index_path.is_file():
        return FileResponse(index_path)
    return HTMLResponse(_ADMIN_HTML)


# Ollama tags the wizard should prefer when several are installed, best first.
# Matched as a prefix against the tag ("qwen3-vl:8b-instruct" matches
# "qwen3-vl"), so a specific quantisation still counts. Vision-capable and
# instruction-tuned models come first because the operator loop asks for
# structured output and the desktop tools pass screenshots.
_PREFERRED_OLLAMA_MODELS = (
    "qwen3-vl", "qwen3vl", "qwen3", "qwen2.5", "gemma3", "llama3.1", "mistral",
)


def _recommended_ollama_model(models: list[str]) -> str | None:
    """The model the wizard should preselect, or None when it should not guess.

    A single installed model is the answer whatever it is - the user has
    already made the choice by pulling it. With several, prefer the ones this
    project is actually tuned against rather than whichever sorts first.
    """
    if not models:
        return None
    if len(models) == 1:
        return models[0]
    for preferred in _PREFERRED_OLLAMA_MODELS:
        for model in models:
            if model.casefold().startswith(preferred):
                return model
    return None


LLM_PRESETS: dict[str, dict[str, Any]] = {
    "localdeploy_qwen3vl_8b": {
        "label": "Qwen3-VL 8B - free and private, runs locally (~6 GB download)",
        "profile_name": "localdeploy_qwen3vl_8b",
        "provider": "openai_compatible",
        "model": "qwen3vl_8b_ollama",
        "base_url": "http://127.0.0.1:8000/v1",
        "api_key_env": None,
        "timeout_seconds": 800,
        "max_tokens": 4096,
        "temperature": 0.1,
        "context_limit": 32768,
    },
    "localdeploy_gemma3_12b": {
        "label": "Gemma 3 12B - free and private, runs locally (~8 GB download)",
        "profile_name": "localdeploy_gemma3_12b",
        "provider": "openai_compatible",
        "model": "gemma3_12b_ollama_safe",
        "base_url": "http://127.0.0.1:8000/v1",
        "api_key_env": None,
        "timeout_seconds": 360,
        "max_tokens": 9000,
        "temperature": 0.2,
        "context_limit": 32768,
    },
    "localdeploy_gemma3_4b": {
        "label": "Gemma 3 4B - free and private, smallest and fastest (~3 GB download)",
        "profile_name": "localdeploy_gemma3_4b",
        "provider": "openai_compatible",
        "model": "gemma3_4b_ollama_safe",
        "base_url": "http://127.0.0.1:8000/v1",
        "api_key_env": None,
        "timeout_seconds": 180,
        "max_tokens": 1024,
        "temperature": 0.2,
        "context_limit": 32768,
    },
    # Local runtimes only. A cloud model needs a key, and the provider picker
    # is the only path that verifies one and proves the model answers before
    # saving - offering a cloud preset here as well was two routes to the same
    # place with different quality.
    #
    # Inside a container, 127.0.0.1 is the container - so every preset above is
    # unreachable there, including the recommended one. compose already maps
    # host.docker.internal, so this is the same local model seen from inside.
    # A container install that could only be offered the 8B had nothing to
    # choose when the card was busy: the filter hides loopback presets, and the
    # 8B needs ~7 GB. A small model has to be reachable from a container too.
    "localdeploy_gemma3_4b_container": {
        "label": "Gemma 3 4B on your computer - free and private (~3 GB download)",
        "profile_name": "localdeploy_gemma3_4b_container",
        "provider": "openai_compatible",
        "model": "gemma3_4b_ollama_safe",
        "base_url": "http://host.docker.internal:8000/v1",
        "api_key_env": None,
        "timeout_seconds": 180,
        "max_tokens": 9000,
        "temperature": 0.2,
        "context_limit": 32768,
    },
    "localdeploy_qwen3vl_8b_container": {
        "label": "Qwen3-VL 8B on your computer - free and private (~6 GB download)",
        "profile_name": "localdeploy_qwen3vl_8b_container",
        "provider": "openai_compatible",
        "model": "qwen3vl_8b_ollama",
        "base_url": "http://host.docker.internal:8000/v1",
        "api_key_env": None,
        "timeout_seconds": 800,
        "max_tokens": 4096,
        "temperature": 0.1,
        "context_limit": 32768,
    },
}


def create_admin_router(
    settings_loader: SettingsLoader,
    repositories_loader: RepositoriesLoader,
    vscode_store: VSCodeBridgeStore,
) -> APIRouter:
    router = APIRouter(prefix="/admin", tags=["admin"])
    config_manager = ConfigManager()
    artifact_download_grants: dict[str, tuple[str, bool, float]] = {}

    def settings() -> AppSettings:
        loaded = settings_loader()
        if not loaded.server.admin_enabled:
            raise HTTPException(status_code=404, detail="admin UI is disabled")
        return loaded

    def require_admin(request: Request) -> AppSettings:
        loaded = settings()
        if not _origin_is_trusted(request):
            raise HTTPException(
                status_code=403,
                detail="admin API refused: cross-origin request (Origin header does not match Host)",
            )
        expected = read_env_value(loaded.server.admin_token_env)
        # Never accept the long-lived admin token from a query string. URLs
        # leak into browser history, referrers, screenshots, and proxy logs.
        # The SPA captures its one-time launch URL token into an auth header
        # before making API calls; artifact navigation uses a scoped grant.
        provided = request.headers.get("X-Agent-Control-Admin-Token")
        if not expected:
            if not is_loopback_host(loaded.server.host):
                raise HTTPException(
                    status_code=503,
                    detail=(
                        "admin API refused: server.host is not loopback-only and no admin token "
                        f"is configured. Set {loaded.server.admin_token_env} before binding beyond 127.0.0.1."
                    ),
                )
            return loaded
        # compare_digest, not ==: a plain comparison returns as soon as two
        # bytes differ, which leaks the shared prefix length to anything that
        # can time requests.
        if not secrets.compare_digest(str(provided or ""), str(expected)):
            raise HTTPException(status_code=401, detail="invalid admin token")
        return loaded

    @router.get("", response_model=None)
    def admin_page(request: Request) -> FileResponse | HTMLResponse:
        return _serve_admin_app(request, "")

    @router.get("/api/bootstrap")
    def admin_bootstrap(request: Request) -> dict[str, Any]:
        """What the SPA shell needs before its first real paint (docs/UI_REWRITE_PLAN.md
        §9 Phase 0.3) - whether to show a token-entry screen, the onboarding
        wizard, or the console directly. Deliberately NOT behind require_admin:
        a token-required client cannot know it needs a token without calling
        something first, so this is the one endpoint that answers that
        without circularity. Still behind the same same-origin check as
        everything else, and the response is deliberately minimal - no
        capability config, no secrets, nothing require_admin's callers get.
        """
        loaded = settings()
        if not _origin_is_trusted(request):
            raise HTTPException(
                status_code=403,
                detail="admin API refused: cross-origin request (Origin header does not match Host)",
            )
        llm_configured = check_llm_configured(loaded)
        return {
            "token_required": bool(read_env_value(loaded.server.admin_token_env)),
            # Not just "does config.yaml exist" - `ybm setup` always creates
            # one now (bootstrap.run_setup), so that alone would never show
            # the wizard. A real LLM being configured is the actual signal
            # that first-run choices have been made; re-runnable any time
            # from Settings regardless of this value.
            "onboarding_complete": CONFIG_FILE_PATH.exists() and llm_configured,
            "llm_reachable": llm_configured,
            "version": _APP_VERSION,
        }

    @router.get("/api/setup/detect")
    def admin_setup_detect(request: Request) -> dict[str, Any]:
        """What the first-run wizard needs to ask real questions instead of
        blind ones - a local Ollama server's actual installed models, whether
        LocalDeploy/a cloud key/a Telegram token are already present (never
        the values), and the current LLM profile's status. Runs after the
        token gate (App.tsx checks bootstrap.token_required before ever
        rendering the wizard), so this is a normal require_admin route,
        unlike /api/bootstrap.
        """
        loaded = require_admin(request)
        ollama = _http_json(OLLAMA_TAGS_URL, timeout=2.0)
        ollama_models = [str(m.get("name")) for m in (ollama or {}).get("models", []) if m.get("name")]
        return {
            "ollama": {
                # `available` conflated two very different states: a server that
                # is not running, and one that is running with nothing pulled.
                # The second is the only point in onboarding where the user has
                # to leave YBM, find a model name, and come back - so the wizard
                # needs to tell them apart to offer the pull.
                "available": bool(ollama_models),
                "reachable": ollama is not None,
                "models": ollama_models,
                "recommended": _recommended_ollama_model(ollama_models),
            },
            "localdeploy_root_present": bool(read_env_value("YBM_LOCALDEPLOY_ROOT")),
            "openai_key_present": bool(read_env_value("OPENAI_API_KEY")),
            "telegram_token_present": bool(read_env_value("TELEGRAM_BOT_TOKEN")),
            "current_llm_profile": loaded.llm.default_profile,
            "llm_configured": check_llm_configured(loaded),
            "telegram_enabled": loaded.channels.telegram.enabled,
        }

    @router.get("/api/summary")
    def admin_summary(request: Request, task_limit: int = Query(default=5, ge=1, le=100)) -> dict[str, Any]:
        loaded = require_admin(request)
        repositories = repositories_loader()
        tasks = repositories.tasks.list_recent(task_limit)
        task_total = repositories.tasks.count()
        audit_events = repositories.audit.list_recent(20)
        schedules = repositories.schedules.list_recent(10)
        audit_logger = AuditLogger(repositories.audit, loaded.logging.redact_patterns)
        return {
            "status": "ok",
            "config": loaded.safe_summary(),
            "tasks": [_redact_admin_output(task.model_dump(mode="json"), loaded) for task in tasks],
            "task_pagination": {
                "limit": task_limit,
                "offset": 0,
                "total": task_total,
                "has_more": task_total > task_limit,
            },
            "audit": [
                _redact_admin_output(format_audit_event(event).model_dump(mode="json"), loaded)
                for event in audit_events
            ],
            "vscode": _vscode_summary(vscode_store),
            "access_modes": {
                name: summary.model_dump(mode="json")
                for name, summary in summarize_access_modes(loaded).items()
            },
            "warnings": _config_warnings(loaded),
            "database": _database_summary(loaded),
            "services": service_summary(loaded),
            "schedules": {
                "total": repositories.schedules.count(),
                "items": [schedule.model_dump(mode="json") for schedule in schedules],
            },
            "tool_registry": _tool_registry_summary(loaded, repositories, audit_logger),
            "integrations": {
                "telegram": {
                    "enabled": loaded.channels.telegram.enabled,
                    "token_env": loaded.channels.telegram.token_env,
                    "token_present": bool(read_env_value(loaded.channels.telegram.token_env)),
                    "allowed_user_ids": loaded.channels.telegram.allowed_user_ids,
                    "allowed_chat_ids": loaded.channels.telegram.allowed_chat_ids,
                    "allowed_user_count": len(loaded.channels.telegram.allowed_user_ids),
                    "allowed_chat_count": len(loaded.channels.telegram.allowed_chat_ids),
                    "recent_denials": _recent_telegram_denials(repositories),
                },
                "llm": {
                    "default_profile": loaded.llm.default_profile,
                    "profile_count": len(loaded.llm.profiles),
                    "default_profile_configured": loaded.llm.default_profile in loaded.llm.profiles,
                    "hardware": _hardware_probe().as_dict(),
                    "presets": [
                        {
                            **preset,
                            "key": key,
                            "active": loaded.llm.default_profile == preset["profile_name"],
                            # Earned, not asserted: "fits" / "too_big" /
                            # "unknown", with the reason for whichever it is.
                            "fit": llm_hardware.fit(key, _hardware_probe()),
                        }
                        for key, preset in _presets_that_can_work().items()
                    ],
                },
            },
            "admin": {
                "enabled": loaded.server.admin_enabled,
                "token_required": bool(read_env_value(loaded.server.admin_token_env)),
                "config_file": str(CONFIG_FILE_PATH),
            },
        }

    @router.get("/api/doctor")
    def admin_doctor(request: Request) -> dict[str, Any]:
        """Runs the same checks `ybm doctor` runs (collect_checks - this
        endpoint never duplicates that logic, just calls it) from the
        console (docs/UI_UX_AUDIT.md Phase 9), since a person diagnosing a
        problem from the admin UI shouldn't have to drop to a terminal to
        run it. Takes a couple of seconds - it probes ports, the DB, and
        LocalDeploy for real - so it's operator-triggered, not polled.
        """
        require_admin(request)
        checks = collect_checks()
        return {
            "checks": [{"name": c.name, "status": c.status, "detail": c.detail} for c in checks],
            "ok": all(c.status != "fail" for c in checks),
        }

    @router.get("/api/logs/{service}")
    def admin_service_log(request: Request, service: str, lines: int = Query(default=200, ge=1, le=2000)) -> dict[str, Any]:
        require_admin(request)
        if service not in KNOWN_SERVICE_NAMES:
            raise HTTPException(status_code=404, detail=f"unknown service: {service}")
        result = read_log_tail(service, lines=lines)
        if result is None:
            return {"service": service, "log_path": None, "lines": []}
        log_path, tail_lines = result
        return {"service": service, "log_path": log_path, "lines": tail_lines}

    @router.get("/api/tasks")
    def admin_tasks(
        request: Request,
        limit: int = Query(default=25, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, Any]:
        loaded = require_admin(request)
        repositories = repositories_loader()
        total = repositories.tasks.count()
        tasks = repositories.tasks.list_recent(limit, offset=offset)
        return {
            "tasks": [_redact_admin_output(task.model_dump(mode="json"), loaded) for task in tasks],
            "pagination": {
                "limit": limit,
                "offset": offset,
                "total": total,
                "has_more": offset + len(tasks) < total,
            },
        }

    @router.get("/api/tasks/{task_id}/trace")
    def admin_task_trace(request: Request, task_id: str) -> dict[str, Any]:
        loaded = require_admin(request)
        repositories = repositories_loader()
        trace = build_task_trace(repositories, task_id)
        if trace is None:
            raise HTTPException(status_code=404, detail="task not found")
        return _redact_admin_output(trace, loaded)

    @router.get("/api/tasks/{task_id}/receipt")
    def admin_task_receipt(request: Request, task_id: str) -> dict[str, Any]:
        loaded = require_admin(request)
        repositories = repositories_loader()
        receipt = build_task_receipt(repositories, loaded, task_id)
        if receipt is None:
            raise HTTPException(status_code=404, detail="task not found")
        return _redact_admin_output(receipt, loaded)

    def resolve_artifact_file(artifact_id: str, loaded: AppSettings) -> Path:
        repositories = repositories_loader()
        artifact = repositories.artifacts.get(artifact_id)
        if artifact is None or not artifact.uri:
            raise HTTPException(status_code=404, detail="artifact not found")
        path = path_from_uri(artifact.uri)
        if path is None:
            raise HTTPException(status_code=404, detail="artifact has no downloadable file")
        allowed_roots = [Path(root).expanduser().resolve() for root in artifact_delivery_roots(loaded)]
        if allowed_roots and not any(root == path or root in path.parents for root in allowed_roots):
            raise HTTPException(status_code=403, detail="artifact path is outside configured delivery roots")
        if not path.is_file():
            raise HTTPException(status_code=404, detail="artifact file no longer exists on disk")
        return path

    @router.post("/api/artifacts/{artifact_id}/download-grant")
    def admin_artifact_download_grant(
        request: Request,
        artifact_id: str,
        inline: bool = False,
    ) -> dict[str, Any]:
        """Mint a short-lived grant for a single artifact and disposition.

        A normal browser navigation cannot attach the admin-token header. A
        scoped grant keeps that long-lived credential out of URLs and cannot
        be reused against any other admin endpoint or artifact.
        """
        loaded = require_admin(request)
        resolve_artifact_file(artifact_id, loaded)
        now = time.monotonic()
        expired = [token for token, (_, _, expires_at) in artifact_download_grants.items() if expires_at <= now]
        for token in expired:
            artifact_download_grants.pop(token, None)
        grant = secrets.token_urlsafe(32)
        artifact_download_grants[grant] = (artifact_id, inline, now + _ARTIFACT_GRANT_TTL_SECONDS)
        inline_query = "true" if inline else "false"
        return {
            "url": f"/admin/api/artifacts/{artifact_id}/download?grant={grant}&inline={inline_query}",
            "expires_in_seconds": _ARTIFACT_GRANT_TTL_SECONDS,
        }

    @router.get("/api/artifacts/{artifact_id}/download", response_model=None)
    def admin_artifact_download(
        request: Request,
        artifact_id: str,
        inline: bool = False,
        grant: str | None = None,
    ) -> FileResponse:
        """A generated file previously showed only its path in Chat - not
        actionable from a browser (docs/UI_UX_AUDIT.md Phase 8: "generated
        files still are not conveniently usable"). Serves only artifacts
        registered in the database whose resolved path stays inside the
        same allowed roots artifact.deliver itself enforces - reuses that
        exact check rather than re-deriving a second path-safety rule.
        ``?inline=1`` renders only a conservative safe-type allowlist in a
        new tab. Active formats such as HTML and SVG remain attachments.
        Browser navigations authenticate with the short-lived, artifact-
        scoped grant above rather than putting the admin token in the URL.
        """
        loaded = settings()
        if not _origin_is_trusted(request):
            raise HTTPException(
                status_code=403,
                detail="admin API refused: cross-origin request (Origin header does not match Host)",
            )
        if grant:
            granted = artifact_download_grants.get(grant)
            if granted is None:
                raise HTTPException(status_code=401, detail="invalid artifact download grant")
            granted_artifact_id, granted_inline, expires_at = granted
            if expires_at <= time.monotonic():
                artifact_download_grants.pop(grant, None)
                raise HTTPException(status_code=401, detail="artifact download grant expired")
            if granted_artifact_id != artifact_id or granted_inline is not inline:
                raise HTTPException(status_code=401, detail="artifact download grant does not match this request")
        else:
            loaded = require_admin(request)

        path = resolve_artifact_file(artifact_id, loaded)
        media_type, _ = mimetypes.guess_type(path.name)
        allow_inline = inline and media_type in _SAFE_INLINE_MEDIA_TYPES
        response = FileResponse(
            path,
            filename=path.name,
            content_disposition_type="inline" if allow_inline else "attachment",
            media_type=media_type,
        )
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["Content-Security-Policy"] = "sandbox; default-src 'none'"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @router.get("/api/approvals")
    def admin_pending_approvals(request: Request) -> dict[str, Any]:
        loaded = require_admin(request)
        repositories = repositories_loader()
        pending = repositories.approvals.list_pending()
        items = []
        for approval in pending:
            task = repositories.tasks.get(approval.task_id)
            policy = loaded.capabilities.get(approval.capability)
            action_payload = approval.action_payload if isinstance(approval.action_payload, dict) else {}
            items.append(
                {
                    "approval": approval.model_dump(mode="json"),
                    "task_objective": task.objective if task is not None else None,
                    "task_status": task.status.value if task is not None else None,
                    # Evidence Pack fields (docs/UI_REWRITE_PLAN.md §11.2) the
                    # existing ApprovalRequest alone doesn't carry: the
                    # configured ceiling this risk level is measured
                    # against, and what the pending action would actually
                    # touch, reusing the same key-matching approach the
                    # task-trace evidence view already uses.
                    "capability_max_risk_level": policy.max_risk_level.value if policy is not None else None,
                    "blast_radius": _approval_blast_radius(action_payload),
                }
            )
        return {"approvals": items}

    @router.post("/api/approvals/{approval_id}/decide")
    def admin_decide_approval(request: Request, approval_id: str, payload: AdminApprovalDecisionRequest) -> dict[str, Any]:
        loaded = require_admin(request)
        repositories = repositories_loader()
        approval = repositories.approvals.get(approval_id)
        if approval is None:
            raise HTTPException(status_code=404, detail="approval not found")
        if approval.status != ApprovalStatus.PENDING:
            raise HTTPException(status_code=409, detail=f"approval is already {approval.status.value}, not pending")

        new_status = ApprovalStatus.REJECTED if payload.decision == "reject" else ApprovalStatus.APPROVED
        if not repositories.approvals.decide_pending(approval_id, new_status):
            updated = repositories.approvals.get(approval_id)
            state = updated.status.value if updated is not None else "unavailable"
            raise HTTPException(status_code=409, detail=f"approval could not be decided; current status is {state}")
        # A batch approval is the decision a human makes; its children are the
        # per-call authority that decision stands for. Cascade so the resumed
        # batch finds each call already approved, and so a rejected batch
        # cannot leave approved children behind for a later call to reuse.
        for entry in (approval.action_payload.get("batch") or []):
            if not isinstance(entry, dict):
                continue
            child_id = str(entry.get("approval_id") or "")
            if child_id:
                repositories.approvals.decide_pending(child_id, new_status)

        requeue_after_approval_decision(repositories, approval.task_id)
        audit = AuditLogger(repositories.audit, loaded.logging.redact_patterns)
        audit.append(
            AuditEventType.APPROVAL_DECIDED,
            actor="admin",
            task_id=approval.task_id,
            payload={"approval_id": approval_id, "decision": payload.decision, "source": "admin_ui"},
        )

        grant: ApprovalGrant | None = None
        if payload.decision == "approve_for_task":
            tool_name = str(approval.action_payload.get("tool_name") or "")
            grant = ApprovalGrant(
                task_id=approval.task_id,
                tool_name=tool_name,
                capability=approval.capability,
                granted_from_approval_id=approval_id,
                expires_at=utc_now() + timedelta(seconds=loaded.limits.task_budget_seconds),
            )
            repositories.approval_grants.create(grant)
            audit.append(
                AuditEventType.CONFIG_UPDATED,
                actor="admin",
                task_id=approval.task_id,
                payload={
                    "section": "approval_grant",
                    "action": "create",
                    "grant_id": grant.id,
                    "tool_name": tool_name,
                    "capability": approval.capability.value,
                },
            )

        updated = repositories.approvals.get(approval_id)
        return {
            "approval": updated.model_dump(mode="json") if updated else None,
            "grant": grant.model_dump(mode="json") if grant else None,
        }

    @router.delete("/api/tasks")
    def admin_clear_tasks(
        request: Request,
        include_active: bool = Query(default=False),
    ) -> dict[str, Any]:
        loaded = require_admin(request)
        repositories = repositories_loader()
        deleted = repositories.tasks.clear_history(include_active=include_active)
        AuditLogger(repositories.audit, loaded.logging.redact_patterns).append(
            AuditEventType.CONFIG_UPDATED,
            actor="admin",
            payload={
                "section": "task_history",
                "deleted_tasks": deleted,
                "include_active": include_active,
            },
        )
        return {"deleted_tasks": deleted, "include_active": include_active}

    @router.get("/api/audit")
    def admin_audit(
        request: Request,
        task_id: str | None = None,
        category: str | None = None,
        event_type: AuditEventType | None = None,
        actor: str | None = None,
        q: str | None = None,
        limit: int = Query(default=50, ge=1, le=200),
    ) -> dict[str, Any]:
        loaded = require_admin(request)
        repositories = repositories_loader()
        raw_events = repositories.audit.list_for_task(task_id)[-limit:] if task_id else repositories.audit.list_recent(200)
        formatted = [format_audit_event(event) for event in raw_events]
        if category:
            formatted = [event for event in formatted if event.category == category]
        if event_type:
            formatted = [event for event in formatted if event.type == event_type]
        if actor:
            formatted = [event for event in formatted if actor.lower() in event.actor.lower()]
        if q:
            needle = q.lower()
            formatted = [
                event
                for event in formatted
                if needle in event.summary.lower()
                or needle in event.title.lower()
                or needle in str(event.details).lower()
            ]
        return {
            "events": [
                _redact_admin_output(event.model_dump(mode="json"), loaded)
                for event in formatted[:limit]
            ]
        }

    @router.delete("/api/audit")
    def admin_clear_audit(request: Request) -> dict[str, Any]:
        require_admin(request)
        repositories = repositories_loader()
        deleted = repositories.audit.clear_all()
        return {"deleted_audit_events": deleted}

    @router.get("/api/memory")
    def admin_list_memory_facts(request: Request, q: str | None = None, category: str | None = None) -> dict[str, Any]:
        loaded = require_admin(request)
        repositories = repositories_loader()
        facts = repositories.memory_facts.list_all(category=category, query=q)
        return {"facts": [_redact_admin_output(f.model_dump(mode="json"), loaded) for f in facts]}

    @router.post("/api/memory")
    def admin_create_memory_fact(request: Request, payload: AdminMemoryFactCreateRequest) -> dict[str, Any]:
        """"Remember" (docs/UI_UX_AUDIT.md Phase 4): an operator-entered fact,
        distinct from anything a task inferred - MemorySource.OPERATOR_ADMIN
        so a Memory page reader can always tell "I told it this" apart from
        "it figured this out itself".
        """
        loaded = require_admin(request)
        repositories = repositories_loader()
        fact = repositories.memory_facts.create(
            MemoryFact(category=payload.category, content=payload.content, source=MemorySource.OPERATOR_ADMIN)
        )
        AuditLogger(repositories.audit, loaded.logging.redact_patterns).append(
            AuditEventType.CONFIG_UPDATED, actor="admin",
            payload={"section": "memory", "action": "create", "fact_id": fact.id, "category": fact.category},
        )
        return {"fact": fact.model_dump(mode="json")}

    @router.patch("/api/memory/{fact_id}")
    def admin_update_memory_fact(request: Request, fact_id: str, payload: AdminMemoryFactUpdateRequest) -> dict[str, Any]:
        loaded = require_admin(request)
        repositories = repositories_loader()
        if repositories.memory_facts.get(fact_id) is None:
            raise HTTPException(status_code=404, detail="memory fact not found")
        updated = repositories.memory_facts.update_content(fact_id, category=payload.category, content=payload.content)
        AuditLogger(repositories.audit, loaded.logging.redact_patterns).append(
            AuditEventType.CONFIG_UPDATED, actor="admin",
            payload={"section": "memory", "action": "edit", "fact_id": fact_id},
        )
        return {"fact": updated.model_dump(mode="json") if updated else None}

    @router.delete("/api/memory/{fact_id}")
    def admin_delete_memory_fact(request: Request, fact_id: str) -> dict[str, Any]:
        loaded = require_admin(request)
        repositories = repositories_loader()
        deleted = repositories.memory_facts.delete(fact_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="memory fact not found")
        AuditLogger(repositories.audit, loaded.logging.redact_patterns).append(
            AuditEventType.CONFIG_UPDATED, actor="admin",
            payload={"section": "memory", "action": "forget", "fact_id": fact_id},
        )
        return {"fact_id": fact_id, "deleted": True}

    @router.get("/api/skills")
    def admin_list_skills(request: Request) -> dict[str, Any]:
        loaded = require_admin(request)
        skills = list_skills_detailed(loaded.adapters.skills.root_dir, _known_tool_names(loaded))
        return {"root_dir": loaded.adapters.skills.root_dir, "skills": skills}

    @router.get("/api/skills/catalog")
    def admin_skills_catalog(request: Request) -> dict[str, Any]:
        """Bundled starter skills (docs/UI_UX_AUDIT.md Phase 11) - read-only
        browsing of skills/starter/, distinct from the installed catalog
        above. Installing one goes through the existing POST /api/skills
        with this entry's own fields, not a separate install-from-catalog
        endpoint - one install code path, not two.
        """
        loaded = require_admin(request)
        catalog = list_skills_detailed(str(_STARTER_SKILLS_DIR), _known_tool_names(loaded))
        return {"skills": catalog}

    @router.post("/api/skills")
    def admin_install_skill(request: Request, payload: AdminSkillInstallRequest) -> dict[str, Any]:
        """Installs (creates or overwrites by name) a skill - docs/UI_UX_AUDIT.md
        Phase 5's "install ... entirely from the console", writing straight
        to adapters.skills.root_dir the same way a hand-dropped file would
        (skills.py's own docstring: there is no separate database record).
        """
        loaded = require_admin(request)
        repositories = repositories_loader()
        skill = write_skill_file(
            loaded.adapters.skills.root_dir, payload.name, payload.description, payload.body,
            version=payload.version, tools=payload.tools,
        )
        if not skill["tools_declared"]:
            skill["tools"] = detect_referenced_tools(skill["body"], _known_tool_names(loaded))
        AuditLogger(repositories.audit, loaded.logging.redact_patterns).append(
            AuditEventType.CONFIG_UPDATED, actor="admin",
            payload={"section": "skills", "action": "install", "name": skill["name"], "version": skill["version"]},
        )
        return {"skill": skill}

    @router.delete("/api/skills/{name}")
    def admin_uninstall_skill(request: Request, name: str) -> dict[str, Any]:
        loaded = require_admin(request)
        repositories = repositories_loader()
        deleted = delete_skill_file(loaded.adapters.skills.root_dir, name)
        if not deleted:
            raise HTTPException(status_code=404, detail="skill not found")
        AuditLogger(repositories.audit, loaded.logging.redact_patterns).append(
            AuditEventType.CONFIG_UPDATED, actor="admin",
            payload={"section": "skills", "action": "uninstall", "name": name},
        )
        return {"name": name, "deleted": True}

    @router.get("/api/config/effective")
    def admin_effective_config(request: Request) -> dict[str, Any]:
        loaded = require_admin(request)
        return {
            "config": loaded.safe_summary(),
            "access_modes": {
                name: summary.model_dump(mode="json")
                for name, summary in summarize_access_modes(loaded).items()
            },
            "warnings": _config_warnings(loaded),
        }

    @router.post("/api/vscode/terminal-commands")
    def admin_enqueue_vscode_command(
        request: Request,
        payload: AdminTerminalCommandRequest,
    ) -> dict[str, Any]:
        loaded = require_admin(request)
        _ensure_terminal_dispatch_allowed(loaded)

        command = vscode_store.enqueue_terminal_command(
            VSCodeTerminalCommand(
                command=payload.command,
                terminal_id=payload.terminal_id,
                instance_id=payload.instance_id,
                cwd=payload.cwd,
            )
        )
        repositories = repositories_loader()
        AuditLogger(repositories.audit, loaded.logging.redact_patterns).append(
            AuditEventType.TOOL_REQUESTED,
            actor="admin",
            payload={
                "tool": "vscode.terminal_command",
                "command_id": command.id,
                "terminal_id": command.terminal_id,
                "instance_id": command.instance_id,
                "cwd": command.cwd,
                "command_preview": command.command[:160],
            },
        )
        return {"queued": command.model_dump(mode="json")}

    @router.post("/api/tasks/{task_id}/signals")
    def admin_task_signal(
        request: Request,
        task_id: str,
        payload: AdminTaskSignalRequest,
    ) -> dict[str, Any]:
        loaded = require_admin(request)
        repositories = repositories_loader()
        task = repositories.tasks.get(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")

        audit = AuditLogger(repositories.audit, loaded.logging.redact_patterns)
        try:
            signal, _, _ = apply_task_signal(
                repositories,
                audit,
                task_id,
                payload.signal,
                "admin",
                payload.model_dump(mode="json"),
                settings=loaded,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        updated = repositories.tasks.get(task_id)
        if updated is None:
            raise HTTPException(status_code=404, detail="task not found")
        return {
            "signal": signal.model_dump(mode="json"),
            "task": _redact_admin_output(updated.model_dump(mode="json"), loaded),
        }

    @router.post("/api/config/llm")
    def admin_update_llm_config(request: Request, payload: AdminLLMConfigRequest) -> dict[str, Any]:
        loaded = require_admin(request)
        config = _read_config_file(config_manager)
        llm = config.setdefault("llm", {})
        profiles = llm.setdefault("profiles", {})
        llm["default_profile"] = payload.default_profile
        profiles[payload.profile_name] = {
            "provider": payload.provider,
            "model": payload.model,
            "base_url": _blank_to_none(payload.base_url),
            "api_key_env": _blank_to_none(payload.api_key_env),
            "timeout_seconds": payload.timeout_seconds,
            "max_tokens": payload.max_tokens,
            "temperature": payload.temperature,
        }
        _write_config_file(config_manager, config)
        config_manager.remove_env_keys(["AGENT_LLM__DEFAULT_PROFILE", *_llm_profile_env_keys(payload.profile_name)])
        env_updates: dict[str, str | None] = {}
        if payload.api_key_env and payload.api_key_value:
            env_updates[payload.api_key_env] = payload.api_key_value
        if env_updates:
            config_manager.upsert_env(env_updates)
        _audit_config_update(repositories_loader(), loaded, "llm", payload.model_dump(mode="json"))
        return {"config_file": str(CONFIG_FILE_PATH), "llm": llm}

    @router.post("/api/config/llm/preset")
    def admin_select_llm_preset(request: Request, payload: AdminLLMPresetRequest) -> dict[str, Any]:
        loaded = require_admin(request)
        preset = LLM_PRESETS.get(payload.preset)
        if preset is None:
            raise HTTPException(status_code=400, detail=f"unknown LLM preset: {payload.preset}")

        config = _read_config_file(config_manager)
        llm = config.setdefault("llm", {})
        profiles = llm.setdefault("profiles", {})
        profile_name = preset["profile_name"]
        llm["default_profile"] = profile_name
        profiles[profile_name] = {
            "provider": preset["provider"],
            "model": preset["model"],
            "base_url": preset["base_url"],
            "api_key_env": preset["api_key_env"],
            "timeout_seconds": preset["timeout_seconds"],
            "max_tokens": preset["max_tokens"],
            "temperature": preset["temperature"],
        }
        if preset.get("context_limit") is not None:
            profiles[profile_name]["context_limit"] = preset["context_limit"]
        _write_config_file(config_manager, config)
        config_manager.remove_env_keys(["AGENT_LLM__DEFAULT_PROFILE", *_legacy_default_llm_env_keys(), *_llm_profile_env_keys(profile_name)])
        _audit_config_update(repositories_loader(), loaded, "llm_preset", {"preset": payload.preset, "profile": profile_name})
        return {"config_file": str(CONFIG_FILE_PATH), "preset": payload.preset, "llm": llm}

    @router.post("/api/config/telegram")
    def admin_update_telegram_config(request: Request, payload: AdminTelegramConfigRequest) -> dict[str, Any]:
        loaded = require_admin(request)
        config = _read_config_file(config_manager)
        telegram = config.setdefault("channels", {}).setdefault("telegram", {})
        patch = payload.model_dump(exclude_unset=True)
        patch.pop("bot_token", None)
        for key, value in patch.items():
            if value is not None:
                telegram[key] = value
        _write_config_file(config_manager, config)
        env_updates: dict[str, str | None] = {}
        if payload.token_env and payload.bot_token:
            env_updates[payload.token_env] = payload.bot_token
        config_manager.remove_env_keys(_telegram_config_env_keys())
        if env_updates:
            config_manager.upsert_env(env_updates)
        _audit_config_update(repositories_loader(), loaded, "telegram", patch)
        return {"config_file": str(CONFIG_FILE_PATH), "telegram": telegram}

    @router.get("/api/persona/suggestions")
    def admin_list_persona_suggestions(request: Request) -> dict[str, Any]:
        """Preferences the system has inferred but not applied.

        Nothing here has changed behavior yet - that is the point. An accepted
        line goes into persona.md and then shapes every future task, so the
        decision belongs to a person, and this endpoint is where they see what
        is waiting.
        """
        loaded = require_admin(request)
        from agent_control.persona_learning import SuggestionStore

        store = SuggestionStore.load(loaded.adapters.persona)
        return {
            "learning_enabled": loaded.adapters.persona.learning_enabled,
            "persona_path": loaded.adapters.persona.path,
            "pending": [s.to_dict() for s in store.pending()],
            "history": [s.to_dict() for s in store.suggestions if s.status != "pending"][-20:],
        }

    @router.post("/api/persona/suggestions/{suggestion_id}/decide")
    def admin_decide_persona_suggestion(
        request: Request, suggestion_id: str, payload: AdminApprovalDecisionRequest
    ) -> dict[str, Any]:
        loaded = require_admin(request)
        from agent_control.persona_learning import SuggestionStore, apply_to_persona

        store = SuggestionStore.load(loaded.adapters.persona)
        accept = payload.decision != "reject"
        decided = store.decide(suggestion_id, accept)
        if decided is None:
            raise HTTPException(status_code=404, detail="suggestion not found or already decided")
        persona = None
        if accept:
            persona = apply_to_persona(loaded.adapters.persona, decided)
        _audit_config_update(
            repositories_loader(), loaded, "persona_suggestion",
            {"suggestion_id": suggestion_id, "decision": decided.status, "line": decided.line},
        )
        return {"suggestion": decided.to_dict(), "persona": persona}

    @router.get("/api/skills/suggestions")
    def admin_list_skill_suggestions(request: Request) -> dict[str, Any]:
        """Procedures learned from successful runs, awaiting review.

        An installed skill is injected into every future Operator prompt and
        followed as instructions, so this queue is the point at which a person
        decides what the system is allowed to have learned.
        """
        loaded = require_admin(request)
        from agent_control.skill_learning import SkillSuggestionStore

        store = SkillSuggestionStore.load(loaded.adapters.skills)
        return {
            "learning_enabled": loaded.adapters.skills.learning_enabled,
            "skills_root": loaded.adapters.skills.root_dir,
            "pending": [s.to_dict() for s in store.pending()],
            "history": [s.to_dict() for s in store.suggestions if s.status != "pending"][-20:],
        }

    @router.post("/api/skills/suggestions/{suggestion_id}/decide")
    def admin_decide_skill_suggestion(
        request: Request, suggestion_id: str, payload: AdminApprovalDecisionRequest
    ) -> dict[str, Any]:
        loaded = require_admin(request)
        from agent_control.skill_learning import SkillSuggestionStore, install

        store = SkillSuggestionStore.load(loaded.adapters.skills)
        accept = payload.decision != "reject"
        decided = store.decide(suggestion_id, accept)
        if decided is None:
            raise HTTPException(status_code=404, detail="suggestion not found or already decided")
        installed = install(loaded.adapters.skills, decided) if accept else None
        _audit_config_update(
            repositories_loader(), loaded, "skill_suggestion",
            {"suggestion_id": suggestion_id, "decision": decided.status, "name": decided.name},
        )
        return {
            "suggestion": decided.to_dict(),
            "installed": {"name": installed["name"], "path": installed["path"]} if installed else None,
        }

    @router.post("/api/config/telegram/test")
    async def admin_test_telegram(request: Request, payload: AdminTelegramProbeRequest) -> dict[str, Any]:
        """Verify a bot token before anything is written to .env, and report
        the handle it resolves to.

        Telegram setup previously had no equivalent of the LLM card's "Test"
        button, which made a wrong or revoked token indistinguishable from an
        empty allowlist or a stopped poller: all three present as "the bot
        ignores me." getMe separates the first case from the other two in one
        round trip, and gives the operator something recognizable (@handle)
        rather than a silent success.
        """
        loaded = require_admin(request)
        token_env = loaded.channels.telegram.token_env
        token = payload.bot_token or read_env_value(token_env)
        if not token:
            raise HTTPException(
                status_code=400,
                detail=f"No bot token to test - paste one, or set {token_env} in .env.",
            )
        from agent_control.channels.telegram import TelegramBotApi

        try:
            bot = await TelegramBotApi(token).get_me()
        except Exception as exc:  # noqa: BLE001 - reported to the operator, not handled
            # _safe_telegram_http_error already strips the token-bearing URL;
            # this must never widen that back out into the response body.
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {
            "ok": True,
            "bot_id": bot.get("id"),
            "bot_username": bot.get("username"),
            "bot_name": bot.get("first_name"),
        }

    @router.post("/api/config/telegram/detect-operator")
    async def admin_detect_telegram_operator(
        request: Request, payload: AdminTelegramProbeRequest
    ) -> dict[str, Any]:
        """Find who has messaged this bot, so the allowlist can be filled by
        recognizing a person instead of asking for a number.

        Telegram never shows a user their own numeric id, so "Allowed user
        IDs" was a field a new operator had no way to answer - and leaving it
        empty silently denies every message. Two sources, in this order:

        1. The audit trail. Every inbound message already writes a
           TELEGRAM_ACCESS_DECISION carrying user_id/chat_id *including when
           it was denied*, which is exactly the state this is meant to rescue.
           This works while the poller is running.
        2. getUpdates, for first-run setup before the poller has ever started.
           Only consulted when the audit trail has nothing, because Telegram
           allows a single getUpdates consumer at a time - calling it while
           the poller runs either conflicts or steals that update from it.
           No offset is passed, so nothing is consumed either way.
        """
        loaded = require_admin(request)
        repositories = repositories_loader()

        candidates: dict[int, dict[str, Any]] = {}
        for event in repositories.audit.list_by_type_recent(
            AuditEventType.TELEGRAM_ACCESS_DECISION, limit=100
        ):
            user_id = (event.payload or {}).get("user_id")
            if not isinstance(user_id, int):
                continue
            candidates.setdefault(
                user_id,
                {
                    "user_id": user_id,
                    "chat_id": (event.payload or {}).get("chat_id"),
                    "last_seen": event.created_at.isoformat(),
                    "was_allowed": bool((event.payload or {}).get("allowed")),
                    "source": "audit",
                },
            )

        if not candidates:
            token = payload.bot_token or read_env_value(loaded.channels.telegram.token_env)
            if not token:
                raise HTTPException(
                    status_code=400,
                    detail="No bot token available - test the token first.",
                )
            from agent_control.channels.telegram import TelegramBotApi

            try:
                updates = await TelegramBotApi(token).get_updates(timeout=0)
            except Exception as exc:  # noqa: BLE001 - reported to the operator
                raise HTTPException(status_code=502, detail=str(exc)) from exc
            for update in updates:
                message = update.get("message") or update.get("edited_message") or {}
                sender = message.get("from") or {}
                chat = message.get("chat") or {}
                if not isinstance(sender.get("id"), int):
                    continue
                candidates.setdefault(
                    sender["id"],
                    {
                        "user_id": sender["id"],
                        "chat_id": chat.get("id"),
                        "username": sender.get("username"),
                        "display_name": sender.get("first_name"),
                        "was_allowed": False,
                        "source": "telegram",
                    },
                )

        return {"candidates": list(candidates.values())}
    @router.post("/api/setup/telegram/verify")
    async def admin_verify_telegram_token(request: Request, payload: AdminTelegramVerifyRequest) -> dict[str, Any]:
        """Confirm a pasted bot token and name the bot back to the user.

        Pasting a long opaque string and being told nothing is the step people
        get wrong, and the failure is silent and much later. `getMe` costs one
        call and turns it into "Connected to @your_bot".
        """
        require_admin(request)
        token = payload.bot_token.strip() if payload.bot_token else read_env_value(payload.token_env or "TELEGRAM_BOT_TOKEN")
        if not token:
            raise HTTPException(status_code=400, detail="no bot token supplied and none is configured")
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.get(f"https://api.telegram.org/bot{token}/getMe")
        except httpx.HTTPError as exc:
            # Never echo the URL: it contains the token.
            raise HTTPException(status_code=502, detail=f"could not reach Telegram ({type(exc).__name__})") from None
        if response.status_code == 401:
            raise HTTPException(status_code=400, detail="Telegram rejected that token. Check you copied all of it.")
        if response.status_code >= 400:
            raise HTTPException(status_code=502, detail=f"Telegram returned HTTP {response.status_code}")
        result = (response.json() or {}).get("result") or {}
        username = result.get("username")
        return {
            "ok": True,
            "username": username,
            "display_name": result.get("first_name") or username,
            "link": f"https://t.me/{username}" if username else None,
        }

    @router.post("/api/setup/telegram/await-first-message")
    async def admin_await_first_telegram_message(
        request: Request, payload: AdminTelegramVerifyRequest
    ) -> dict[str, Any]:
        """Learn the operator's Telegram id from a message they send.

        The allowlist is what makes the bot answer at all - `_authorization_decision`
        fails closed on an empty one - and the wizard used to collect only a
        token, so the documented happy path produced a bot that ignored every
        message with the reason buried in an audit event. Asking someone to
        find their own numeric id is the worst way to fix that; watching for
        the message they were about to send anyway is the best.

        Long-polls Telegram directly rather than through the polling service,
        which is not running yet during setup.
        """
        require_admin(request)
        token = payload.bot_token.strip() if payload.bot_token else read_env_value(payload.token_env or "TELEGRAM_BOT_TOKEN")
        if not token:
            raise HTTPException(status_code=400, detail="no bot token supplied and none is configured")
        wait_seconds = max(5, min(int(payload.wait_seconds or 45), 60))
        try:
            async with httpx.AsyncClient(timeout=wait_seconds + 10) as client:
                response = await client.get(
                    f"https://api.telegram.org/bot{token}/getUpdates",
                    params={"timeout": wait_seconds, "limit": 1, "allowed_updates": '["message"]'},
                )
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"could not reach Telegram ({type(exc).__name__})") from None
        if response.status_code >= 400:
            raise HTTPException(status_code=502, detail=f"Telegram returned HTTP {response.status_code}")
        for update in (response.json() or {}).get("result") or []:
            message = update.get("message") or {}
            sender = message.get("from") or {}
            chat = message.get("chat") or {}
            if sender.get("id"):
                return {
                    "found": True,
                    "user_id": sender["id"],
                    "chat_id": chat.get("id") or sender["id"],
                    "username": sender.get("username"),
                    "first_name": sender.get("first_name"),
                }
        return {"found": False}

    @router.get("/api/channels")
    def admin_list_channels(request: Request) -> dict[str, Any]:
        """The channel catalog, with each entry's live connection state.

        Data, not code: adding a way to reach YBM is a row in
        channels/catalog.py. `connected` is resolved per request from real
        config rather than stored in the catalog, so the console never claims
        a channel is live when it is not.
        """
        loaded = require_admin(request)
        telegram_connected = bool(
            loaded.channels.telegram.enabled
            and read_env_value(loaded.channels.telegram.token_env)
            and (
                loaded.channels.telegram.allowed_user_ids
                or loaded.channels.telegram.allowed_chat_ids
            )
        )
        whatsapp_connected = bool(getattr(loaded.channels, "whatsapp", None) and loaded.channels.whatsapp.enabled)
        live = {"web": True, "telegram": telegram_connected, "whatsapp": whatsapp_connected}
        return {
            "channels": [
                {
                    "key": spec.key,
                    "label": spec.label,
                    "status": spec.status,
                    "blurb": spec.blurb,
                    "note": spec.note,
                    "guided": spec.guided,
                    "zero_setup": spec.zero_setup,
                    "connected": live.get(spec.key, False),
                }
                for spec in channel_catalog.CHANNELS
            ]
        }

    @router.get("/api/llm/providers")
    def admin_list_llm_providers(request: Request) -> dict[str, Any]:
        """The provider catalog the console renders as a picker.

        Data, not code - adding a provider is a row in llm/catalog.py.
        """
        require_admin(request)
        return {
            "providers": [
                {
                    "key": spec.key,
                    "label": spec.label,
                    "kind": spec.kind,
                    "base_url": spec.base_url,
                    "api_key_env": spec.api_key_env,
                    "default_model": spec.default_model,
                    "needs_key": spec.needs_key,
                    "local": spec.local,
                    "lists_models": spec.lists_models,
                    "keys_url": spec.keys_url,
                    "notes": spec.notes,
                    "example_models": list(spec.example_models),
                }
                for spec in llm_catalog.PROVIDERS
            ]
        }

    @router.post("/api/setup/llm/verify")
    async def admin_verify_llm_provider(request: Request, payload: AdminLLMVerifyRequest) -> dict[str, Any]:
        """Confirm a key works and report back what it can reach.

        Same reason the Telegram step verifies its token: a pasted secret that
        is silently accepted fails much later, somewhere the user will not
        connect to this screen. Asking the provider for its model list proves
        the key and populates the model picker in one call, so the list is
        never a hardcoded one that rots.
        """
        require_admin(request)
        spec = llm_catalog.get(payload.provider)
        if spec is None:
            raise HTTPException(status_code=400, detail=f"unknown provider: {payload.provider}")

        key = (payload.api_key or "").strip() or (
            read_env_value(spec.api_key_env) if spec.api_key_env else None
        )
        if spec.needs_key and not key:
            raise HTTPException(status_code=400, detail=f"{spec.label} needs an API key")
        base_url = (payload.base_url or spec.base_url or "").strip()
        if not base_url and spec.kind != "anthropic":
            raise HTTPException(status_code=400, detail=f"{spec.label} needs a base URL")

        try:
            models = await _list_provider_models(spec, key, base_url)
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001 - normalized for the UI
            raise HTTPException(status_code=502, detail=_provider_error(spec, exc)) from None

        return {
            "ok": True,
            "provider": spec.key,
            "label": spec.label,
            "models": models,
            "default_model": spec.default_model,
            # Only meaningful when a real list came back; the UI shows the
            # count rather than claiming a number it did not verify.
            "listed": bool(models),
        }

    @router.post("/api/setup/llm/test")
    async def admin_test_llm_model(request: Request, payload: AdminLLMTestRequest) -> dict[str, Any]:
        """Make one real completion before setup is allowed to finish.

        Listing models proves a key is valid. It does not prove the chosen
        model answers, that the provider routing is right, or that the response
        parses - and each of those fails later, in a place the user will not
        connect back to this screen. A single real round trip proves the whole
        chain, and the reply is shown to them so "it works" is something they
        can see rather than something we assert.

        Deliberately not free: this spends a token or two of the user's own
        quota. It is triggered by an explicit click, and the alternative is
        discovering the failure on their first real message.
        """
        require_admin(request)
        spec = llm_catalog.get(payload.provider)
        if spec is None:
            raise HTTPException(status_code=400, detail=f"unknown provider: {payload.provider}")

        key = (payload.api_key or "").strip() or (
            read_env_value(spec.api_key_env) if spec.api_key_env else None
        )
        if spec.needs_key and not key:
            raise HTTPException(status_code=400, detail=f"{spec.label} needs an API key")

        profile = LLMProfileConfig(
            provider=spec.kind,
            model=payload.model.strip(),
            base_url=(payload.base_url or spec.base_url or None),
            api_key=SecretStr(key) if key else None,
            timeout_seconds=45,
            max_tokens=128,
        )
        started = time.monotonic()
        try:
            provider = build_provider_for_profile(profile)
            reply = await provider.generate_text(
                render_prompt("base/llm_health_check_system.md"),
                render_prompt("tasks/llm_health_check_user.md"),
            )
        except Exception as exc:  # noqa: BLE001 - normalized for the UI
            raise HTTPException(status_code=502, detail=_provider_error(spec, exc)) from None

        return {
            "ok": True,
            "model": profile.model,
            "reply": reply.strip()[:500],
            "latency_ms": round((time.monotonic() - started) * 1000),
        }

    @router.get("/api/config/voice")
    def admin_get_voice_config(request: Request) -> dict[str, Any]:
        """Whether voice transcription is on, and whether it could be.

        The voice failure reply tells the user to turn voice on in Settings, so
        Settings has to actually offer it - otherwise the advice is a promise
        the console cannot keep.
        """
        loaded = require_admin(request)
        stt = loaded.adapters.stt
        installed = importlib.util.find_spec("faster_whisper") is not None
        return {
            "enabled": stt.enabled,
            "provider": stt.provider,
            "model": stt.model,
            "installed": installed,
            # Turning it on without the package fails at the first voice
            # message, so say so before the switch is flipped.
            "available": installed or stt.provider != "faster_whisper",
            "install_hint": "uv sync --extra voice",
        }

    @router.post("/api/chat/transcribe")
    async def admin_transcribe_chat_audio(request: Request, file: UploadFile = File(...)) -> dict[str, Any]:
        """Turn a recording from the console into text.

        Voice worked on Telegram and nowhere else, which is backwards - the
        console is where someone tries it first. Same STT adapter, so turning
        transcription on lights up both channels at once.

        Returns the text rather than creating a task: the console puts it in
        the composer so the user can read it and correct it before sending,
        which a chat UI can do and a messaging app cannot.
        """
        require_admin(request)
        settings = settings_loader()
        if not settings.adapters.stt.enabled:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Speech-to-text is turned off in this setup, so I can't transcribe that. "
                    "Turn on voice under Settings."
                ),
            )
        audio = await file.read()
        if not audio:
            raise HTTPException(status_code=400, detail="That recording was empty.")
        try:
            transcript = await build_stt_adapter(settings.adapters.stt).transcribe(
                audio, file_name=file.filename, mime_type=file.content_type
            )
        except Exception as exc:  # noqa: BLE001 - normalized for the UI
            raise HTTPException(status_code=502, detail=explain_voice_failure(exc)) from None
        text = (transcript.text or "").strip()
        if not text:
            raise HTTPException(
                status_code=422, detail="I could not make out any words in that recording."
            )
        return {"text": text}

    @router.post("/api/config/voice")
    def admin_update_voice_config(request: Request, payload: AdminVoiceConfigRequest) -> dict[str, Any]:
        loaded = require_admin(request)
        if payload.enabled and loaded.adapters.stt.provider == "faster_whisper":
            if importlib.util.find_spec("faster_whisper") is None:
                raise HTTPException(
                    status_code=400,
                    detail="Voice transcription needs the voice extra installed first: uv sync --extra voice",
                )
        config = _read_config_file(config_manager)
        stt = config.setdefault("adapters", {}).setdefault("stt", {})
        stt["enabled"] = payload.enabled
        if payload.model:
            stt["model"] = payload.model
        _write_config_file(config_manager, config)
        _audit_config_update(repositories_loader(), loaded, "voice", {"enabled": payload.enabled})
        return {"config_file": str(CONFIG_FILE_PATH), "stt": stt}

    @router.post("/api/config/vscode")
    def admin_update_vscode_config(request: Request, payload: AdminVSCodeConfigRequest) -> dict[str, Any]:
        loaded = require_admin(request)
        config = _read_config_file(config_manager)
        vscode = config.setdefault("adapters", {}).setdefault("vscode", {})
        patch = payload.model_dump(exclude_unset=True)
        patch.pop("bridge_token", None)
        for key, value in patch.items():
            if value is not None:
                vscode[key] = value
        _write_config_file(config_manager, config)
        env_updates: dict[str, str | None] = {}
        if payload.auth_token_env and payload.bridge_token:
            env_updates[payload.auth_token_env] = payload.bridge_token
        config_manager.remove_env_keys(_vscode_config_env_keys())
        if env_updates:
            config_manager.upsert_env(env_updates)
        _audit_config_update(repositories_loader(), loaded, "vscode", patch)
        return {"config_file": str(CONFIG_FILE_PATH), "vscode": vscode}

    @router.post("/api/config/workspace")
    def admin_update_workspace_config(request: Request, payload: AdminWorkspaceConfigRequest) -> dict[str, Any]:
        loaded = require_admin(request)
        config = _read_config_file(config_manager)
        workspace = config.setdefault("adapters", {}).setdefault("workspace", {})
        patch = payload.model_dump(exclude_unset=True)
        for key, value in patch.items():
            if value is not None:
                workspace[key] = value
        _write_config_file(config_manager, config)
        config_manager.remove_env_keys(_workspace_config_env_keys())
        _audit_config_update(repositories_loader(), loaded, "workspace", patch)
        return {"config_file": str(CONFIG_FILE_PATH), "workspace": workspace}

    @router.post("/api/config/computer-use")
    def admin_update_computer_use_config(request: Request, payload: AdminComputerUseConfigRequest) -> dict[str, Any]:
        loaded = require_admin(request)
        config = _read_config_file(config_manager)
        computer_use = config.setdefault("adapters", {}).setdefault("computer_use", {})
        patch = payload.model_dump(exclude_unset=True)
        for key, value in patch.items():
            if value is not None:
                computer_use[key] = value
        _write_config_file(config_manager, config)
        config_manager.remove_env_keys(_computer_use_config_env_keys())
        _audit_config_update(repositories_loader(), loaded, "computer_use", patch)
        return {"config_file": str(CONFIG_FILE_PATH), "computer_use": computer_use}

    @router.post("/api/config/access-modes")
    def admin_update_access_modes(request: Request, payload: AdminAccessModesRequest) -> dict[str, Any]:
        loaded = require_admin(request)
        config = _read_config_file(config_manager)
        apply_access_modes_to_config(config, payload.modes)
        _write_config_file(config_manager, config)
        os.environ.pop("AGENT_CAPABILITIES", None)
        config_manager.remove_env_keys(["AGENT_CAPABILITIES"])
        _audit_config_update(
            repositories_loader(),
            loaded,
            "access_modes",
            {"modes": {name: mode.value for name, mode in payload.modes.items()}},
        )
        refreshed = settings_loader()
        return {
            "config_file": str(CONFIG_FILE_PATH),
            "access_modes": {
                name: summary.model_dump(mode="json")
                for name, summary in summarize_access_modes(refreshed).items()
            },
        }

    @router.post("/api/secrets/init")
    def admin_init_secret_vault(request: Request) -> dict[str, Any]:
        """Closes the one real dead end the Access page's "Vault not
        initialized" message used to be: same generation
        (`SecretVault.generate_key()`) `ybm setup` already does, just
        reachable from a button instead of requiring a terminal. Picked up
        immediately - read_env_value() re-reads .env on every call, nothing
        caches it at process start, so no restart is needed.
        """
        loaded = require_admin(request)
        if read_env_value(loaded.secrets.key_env):
            return {"key_env": loaded.secrets.key_env, "generated": False}
        config_manager.upsert_env({loaded.secrets.key_env: SecretVault.generate_key()})
        _audit_config_update(repositories_loader(), loaded, "secrets", {"action": "init_vault"})
        return {"key_env": loaded.secrets.key_env, "generated": True}

    @router.get("/api/secrets")
    def admin_list_secrets(request: Request) -> dict[str, Any]:
        loaded = require_admin(request)
        if not read_env_value(loaded.secrets.key_env):
            return {"available": False, "key_env": loaded.secrets.key_env, "services": {}}
        try:
            services = SecretVault(loaded.secrets).list_secrets()
        except SecretVaultError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return {"available": True, "key_env": loaded.secrets.key_env, "services": services}

    @router.post("/api/secrets")
    def admin_set_secret(request: Request, payload: AdminSecretSetRequest) -> dict[str, Any]:
        loaded = require_admin(request)
        if not read_env_value(loaded.secrets.key_env):
            raise HTTPException(
                status_code=400,
                detail=f"{loaded.secrets.key_env} is not set - run `ybm setup` to generate it first",
            )
        try:
            SecretVault(loaded.secrets).set_secret(payload.service, payload.key, payload.value)
        except SecretVaultError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        # Service/key are audited so changes are traceable; the value never is.
        _audit_config_update(
            repositories_loader(), loaded, "secrets",
            {"action": "set", "service": payload.service, "key": payload.key},
        )
        return {"service": payload.service, "key": payload.key, "set": True}

    @router.delete("/api/secrets/{service}/{key}")
    def admin_delete_secret(request: Request, service: str, key: str) -> dict[str, Any]:
        loaded = require_admin(request)
        if not read_env_value(loaded.secrets.key_env):
            raise HTTPException(status_code=400, detail=f"{loaded.secrets.key_env} is not set")
        try:
            deleted = SecretVault(loaded.secrets).delete_secret(service, key)
        except SecretVaultError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not deleted:
            raise HTTPException(status_code=404, detail=f"secret not found: {service}.{key}")
        _audit_config_update(
            repositories_loader(), loaded, "secrets",
            {"action": "delete", "service": service, "key": key},
        )
        return {"service": service, "key": key, "deleted": True}

    @router.get("/api/chat/messages")
    def admin_list_chat_messages(
        request: Request,
        limit: int = Query(default=50, ge=1, le=200),
    ) -> dict[str, Any]:
        """docs/HISTORY.md Part 4 T2.8: the local web chat channel - lets the
        admin console drive a task the same way Telegram does, without
        needing Telegram configured or reachable. One fixed conversation
        (WEB_CHAT_ID); each message is a normal task, so it goes through the
        exact same policy/approval/worker pipeline as every other channel.

        Deliberately does not support artifact.deliver-style file delivery -
        that tool is Telegram-specific (tools/artifact_delivery.py's
        telegram_client). Text answers and any preview_url/workspace_dir
        already in task.metadata (the same fields Telegram's own
        _with_result_links surfaces inline) work identically regardless of
        channel; a file the user explicitly asked to have "sent" does not,
        and a task ending that way will say so in its own error text rather
        than silently pretending to have sent something.
        """
        loaded = require_admin(request)
        repositories = repositories_loader()
        conversation_id = repositories.conversations.get_or_create(ChannelType.WEB, WEB_CHAT_ID)
        tasks = repositories.tasks.list_for_conversation(conversation_id, limit=limit)
        return {
            "conversation_id": conversation_id,
            "tasks": [_task_with_artifacts(task, repositories, loaded) for task in tasks],
        }

    @router.post("/api/chat/messages")
    async def admin_send_chat_message(request: Request, payload: AdminChatMessageRequest) -> dict[str, Any]:
        """A reply while a task sits in CLARIFYING resumes that task instead
        of spawning an unrelated new one - same behavior Telegram already
        had (clarification.py, shared with channels/telegram.py), just
        never wired up for the web channel before.
        """
        loaded = require_admin(request)
        repositories = repositories_loader()
        conversation_id = repositories.conversations.get_or_create(ChannelType.WEB, WEB_CHAT_ID)
        audit = AuditLogger(repositories.audit, loaded.logging.redact_patterns)

        text = payload.text.strip()
        attached = [
            artifact for artifact_id in payload.attachment_ids
            if (artifact := repositories.artifacts.get(artifact_id)) is not None
        ]
        attachment_note = (
            "; ".join(f"{a.content_preview or a.id} (at: {a.uri})" for a in attached) if attached else ""
        )

        clarifying = find_clarifying_task(repositories, conversation_id=conversation_id, chat_id=WEB_CHAT_ID)
        if clarifying is not None and text:
            reply_text = f"{text}\n\n[Attached files: {attachment_note}]" if attachment_note else text
            result = resume_clarifying_task(
                repositories, audit, clarifying,
                text=reply_text, actor="admin_chat", message_id="", received_at=utc_now(),
            )
            for artifact in attached:
                repositories.artifacts.link_to_task(artifact.id, result.task.id)
            return {
                "conversation_id": conversation_id,
                "task": _task_with_artifacts(result.task, repositories, loaded),
            }

        objective = payload.text
        if attachment_note:
            objective = f"{objective}\n\n[Attached files: {attachment_note}]"

        # Web chat never set this before - Telegram's task-creation path
        # always has (channels/telegram.py), so this was the one channel
        # where a remembered fact (or the rolling summary) never reached
        # the operator at all.
        memory_ctx = memory_context(
            repositories.conversation_memory.get(conversation_id),
            remembered_facts=repositories.memory_facts.list_all(),
            objective=objective,
        )
        # Web chat had no classifier at all: every message became a task,
        # including "hi" and questions the model could simply answer. Asking
        # "what is the capital of France?" sent the Operator to knowledge.search,
        # which returned no matches, three times, until the no-progress guard
        # blocked it (docs/GAPS.md G8). Telegram has always classified first;
        # web chat is a channel too and should behave like one.
        chat_reply = await _web_chat_reply(loaded, objective) if not attached else None

        task = repositories.tasks.create(
            objective,
            conversation_id=conversation_id,
            metadata={
                "source_chat_id": WEB_CHAT_ID,
                "source_channel": ChannelType.WEB.value,
                "attachment_ids": [a.id for a in attached],
                "memory_context": memory_ctx,
                # Present only when the Concierge answered directly, so the
                # worker never picks this up.
                **({"synthesized_answer": chat_reply} if chat_reply else {}),
            },
        )
        if chat_reply:
            task = repositories.tasks.update_status(task.id, TaskStatus.COMPLETED)
        for artifact in attached:
            repositories.artifacts.link_to_task(artifact.id, task.id)
        audit.append(
            AuditEventType.TASK_CREATED,
            actor="admin_chat",
            task_id=task.id,
            payload={"conversation_id": conversation_id, "attachment_ids": [a.id for a in attached]},
        )
        return {
            "conversation_id": conversation_id,
            "task": _task_with_artifacts(task, repositories, loaded),
        }

    @router.post("/api/chat/attachments")
    async def admin_upload_chat_attachment(request: Request, file: UploadFile = File(...)) -> dict[str, Any]:
        """Backs Chat's attachment button with a real Artifact
        (docs/UI_UX_AUDIT.md Phase 1) instead of a client-side-only file
        picker. Storage is ArtifactService.write_bytes() - the same
        mechanism code.interpreter and document_manage already use - into
        the configured artifact_dir, never an arbitrary caller-chosen path.
        Uploading does not grant the agent access to the file: whatever
        tool the operator picks to read it back still goes through the
        normal capability/scope policy check, same as any other path.
        """
        loaded = require_admin(request)
        data = await file.read()
        if len(data) > MAX_CHAT_ATTACHMENT_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"attachment exceeds the {MAX_CHAT_ATTACHMENT_BYTES // (1024 * 1024)} MB limit",
            )
        repositories = repositories_loader()
        service = ArtifactService(loaded.storage, repositories.artifacts)
        extension = (Path(file.filename or "attachment").suffix or ".bin").lstrip(".")
        artifact = service.write_bytes(
            ArtifactType.DOCUMENT,
            data,
            extension,
            metadata={"source": "chat_attachment", "original_filename": file.filename, "mime_type": file.content_type},
            content_preview=file.filename,
        )
        AuditLogger(repositories.audit, loaded.logging.redact_patterns).append(
            AuditEventType.ARTIFACT_CREATED,
            actor="admin_chat",
            payload={"artifact_id": artifact.id, "file_name": file.filename, "size_bytes": len(data)},
        )
        return {"artifact_id": artifact.id, "file_name": file.filename, "size_bytes": len(data)}

    @router.get("/api/folders")
    def admin_list_folders(request: Request, path: str | None = None) -> dict[str, Any]:
        """Server-side folder browsing for the Chat composer's folder picker
        (docs/UI_UX_AUDIT.md Phase 13) - a browser directory picker cannot
        yield a usable absolute path, and hand-typing one is the exact gap
        Phase 1 deferred. Scoped to the same `computer_use.allowed_roots`
        filesystem.manage itself is scoped to, on purpose: a folder this
        picker can reach is a folder the agent can actually act on, and
        vice versa - the same boundary, not a second one to keep in sync.
        No `path` returns the configured roots; a `path` inside one of them
        returns its immediate subdirectories. Selecting one only inserts
        its absolute path into the chat draft - there is no new "folder
        reference" concept, just the same plain path-in-the-objective-text
        filesystem.manage's own alias resolution already understands.
        """
        loaded = require_admin(request)
        roots = [Path(r).expanduser().resolve() for r in loaded.adapters.computer_use.allowed_roots]
        if not roots:
            return {"current_path": None, "parent_path": None, "roots": [], "entries": []}

        if path is None:
            entries = [{"name": root.name or str(root), "path": str(root)} for root in roots if root.is_dir()]
            return {"current_path": None, "parent_path": None, "roots": [str(r) for r in roots], "entries": entries}

        target = Path(path).expanduser().resolve()
        if not _path_within_roots(target, roots):
            raise HTTPException(status_code=400, detail="path is outside the configured allowed roots")
        if not target.is_dir():
            raise HTTPException(status_code=404, detail="folder not found")

        try:
            subdirs = sorted(
                (p for p in target.iterdir() if p.is_dir() and not p.name.startswith(".")),
                key=lambda p: p.name.casefold(),
            )
        except OSError as exc:
            raise HTTPException(status_code=400, detail=f"could not list folder: {exc}") from None

        parent = target.parent
        parent_path = str(parent) if parent != target and _path_within_roots(parent, roots) else None
        return {
            "current_path": str(target),
            "parent_path": parent_path,
            "roots": [str(r) for r in roots],
            "entries": [{"name": p.name, "path": str(p)} for p in subdirs],
        }

    @router.post("/api/llm/test")
    async def admin_test_llm(request: Request) -> dict[str, Any]:
        loaded = require_admin(request)
        profile = loaded.llm.profiles.get(loaded.llm.default_profile)
        if profile is None:
            raise HTTPException(status_code=400, detail="default LLM profile is not configured")
        try:
            # Routes by profile.provider, so the health check works for
            # Anthropic and every catalog provider, not just the OpenAI shape.
            provider = build_provider_for_profile(profile)
            output = await provider.generate_text(
                render_prompt("base/llm_health_check_system.md"),
                render_prompt("tasks/llm_health_check_user.md"),
            )
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {"profile": loaded.llm.default_profile, "output_preview": output[:500]}

    @router.get("/{sub_path:path}", response_model=None)
    def admin_app_catch_all(request: Request, sub_path: str) -> FileResponse | HTMLResponse:
        """SPA fallback for client-side routes (/admin/tasks, /admin/access,
        ...) - registered LAST so every literal /api/* route above always
        wins the match first; FastAPI/Starlette try routes in registration
        order, so this can never shadow a real endpoint, only catch what
        nothing else claimed. A genuinely unmatched /admin/api/* path 404s
        here rather than serving HTML - that's a caller bug, not a page.
        """
        if sub_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="not found")
        return _serve_admin_app(request, sub_path)

    return router


def _ensure_terminal_dispatch_allowed(settings: AppSettings) -> None:
    if not settings.adapters.vscode.enabled:
        raise HTTPException(status_code=403, detail="VS Code adapter is disabled")
    policy = settings.capabilities.get(Capability.TERMINAL_RUN)
    if policy is None or not policy.enabled:
        raise HTTPException(status_code=403, detail="terminal.run capability is disabled")
    if policy.requires_approval:
        raise HTTPException(status_code=403, detail="terminal.run requires approval; use the orchestrated approval flow")


def _vscode_summary(store: VSCodeBridgeStore) -> dict[str, Any]:
    latest = _latest_vscode_observed_at(store)
    age_seconds = None
    if latest is not None:
        age_seconds = max(0, int((datetime.now(timezone.utc) - latest.astimezone(timezone.utc)).total_seconds()))
    connected = age_seconds is not None and age_seconds <= 90
    status = "connected" if connected else "stale" if latest is not None else "waiting"
    return {
        "connected": connected,
        "status": status,
        "last_seen_at": latest.isoformat() if latest is not None else None,
        "last_seen_age_seconds": age_seconds,
        "heartbeat": store.heartbeat.model_dump(mode="json") if store.heartbeat else None,
        "state": store.state.model_dump(mode="json") if store.state else None,
        "pending_terminal_commands": len(store.terminal_commands),
        "terminal_outputs": [output.model_dump(mode="json") for output in store.terminal_outputs[-20:]],
    }


def _latest_vscode_observed_at(store: VSCodeBridgeStore) -> datetime | None:
    candidates = []
    if store.heartbeat is not None:
        candidates.append(store.heartbeat.observed_at)
    if store.state is not None:
        candidates.append(store.state.observed_at)
    if not candidates:
        return None
    return max(item if item.tzinfo else item.replace(tzinfo=timezone.utc) for item in candidates)


def _read_config_file(config_manager: ConfigManager) -> dict[str, Any]:
    try:
        return config_manager.read_config()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _write_config_file(config_manager: ConfigManager, config: dict[str, Any]) -> None:
    config_manager.write_config(config)


def _blank_to_none(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def _legacy_default_llm_env_keys() -> list[str]:
    return _llm_profile_env_keys("default")


def _llm_profile_env_keys(profile_name: str) -> list[str]:
    fields = ("PROVIDER", "MODEL", "BASE_URL", "API_KEY_ENV", "TIMEOUT_SECONDS", "MAX_TOKENS", "TEMPERATURE")
    return [f"AGENT_LLM__PROFILES__{profile_name}__{field}" for field in fields]


async def _list_provider_models(
    spec: "llm_catalog.ProviderSpec", key: str | None, base_url: str
) -> list[dict[str, str]]:
    """Ask the provider what models it has. Never raises for "no list".

    A provider that cannot enumerate models is not a failed key - `custom`
    endpoints often have no /models route - so those return the spec's
    examples and let the user type a name.
    """
    if spec.kind == "anthropic":
        import anthropic

        client = anthropic.AsyncAnthropic(api_key=key, timeout=20.0)
        try:
            page = await client.models.list(limit=100)
        finally:
            await client.close()
        return [
            {"id": m.id, "label": getattr(m, "display_name", None) or m.id}
            for m in page.data
        ]

    if not spec.lists_models:
        return [{"id": m, "label": m} for m in spec.example_models]

    headers = {"Authorization": f"Bearer {key}"} if key else {}
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(f"{base_url.rstrip('/')}/models", headers=headers)
    if response.status_code in (401, 403):
        raise HTTPException(status_code=400, detail=f"{spec.label} rejected that API key")
    response.raise_for_status()
    payload = response.json() or {}
    entries = payload.get("data") if isinstance(payload, dict) else None
    models: list[dict[str, str]] = []
    for entry in entries or []:
        model_id = (entry or {}).get("id") if isinstance(entry, dict) else None
        if model_id:
            models.append({"id": str(model_id), "label": str(model_id)})
    return models


def _provider_error(spec: "llm_catalog.ProviderSpec", exc: Exception) -> str:
    """Describe a provider failure without echoing the key."""
    status = getattr(exc, "status_code", None) or getattr(
        getattr(exc, "response", None), "status_code", None
    )
    if status in (401, 403):
        return f"{spec.label} rejected that API key"
    if status == 429:
        return f"{spec.label} rate limit reached - try again shortly"
    if isinstance(status, int):
        return f"{spec.label} returned HTTP {status}"
    if spec.local:
        return f"Could not reach {spec.label}. Is it running?"
    return f"Could not reach {spec.label} ({type(exc).__name__})"


@lru_cache(maxsize=1)
def _hardware_probe() -> "llm_hardware.Hardware":
    """Probing shells out, so do it once per process rather than per preset."""
    return llm_hardware.probe()


def _in_container() -> bool:
    """Whether this process is running inside the shipped container image.

    The Dockerfile sets YBM_HEADLESS=1; /.dockerenv is the belt-and-braces
    check for other container runtimes.
    """
    if os.environ.get("YBM_HEADLESS"):
        return True
    try:
        return Path("/.dockerenv").exists()
    except OSError:
        return False


def _presets_that_can_work() -> dict[str, dict[str, Any]]:
    """Only offer presets that could actually serve a request here.

    Inside a container, 127.0.0.1 is the container itself, so every loopback
    preset is unreachable - offering them means the recommended choice is the
    one that cannot work. Outside a container the reverse is true: the
    host-gateway preset is meaningless and just looks like a duplicate of the
    loopback one with confusing wording. Which environment we are in is
    something we can actually check, so it should not be a question put to the
    user.
    """
    containerised = _in_container()
    return {
        key: preset
        for key, preset in LLM_PRESETS.items()
        if key.endswith("_container") == containerised
    }


async def _web_chat_reply(settings: AppSettings, objective: str) -> str | None:
    """Let the Concierge answer plain chat instead of spawning a task.

    Returns the reply when the message is not work, None when it is - in which
    case the caller creates a task exactly as before. Any failure returns None
    too: classification is an optimisation, and a broken classifier must not
    stop a real request from running.
    """
    from agent_control.channels.base import ChannelType as _ChannelType
    from agent_control.llm.classifier import LLMMessageClassifier
    from agent_control.llm.providers import build_default_llm_provider
    from agent_control.schemas import InboundMessage, MessageKind

    try:
        provider = build_default_llm_provider(settings)
        if provider is None:
            return None
        message = InboundMessage(
            id="web_chat",
            channel=_ChannelType.WEB,
            chat_id=WEB_CHAT_ID,
            sender_id="admin",
            kind=MessageKind.TEXT,
            text=objective,
            received_at=utc_now(),
        )
        classification = await LLMMessageClassifier(provider).classify(message)
    except Exception:  # noqa: BLE001 - never block a real request on this
        logging.getLogger(__name__).warning(
            "web chat classification failed; treating as a task", exc_info=True
        )
        return None
    if classification.is_task:
        return None
    reply = (classification.reply or "").strip()
    return reply or None


def _telegram_config_env_keys() -> list[str]:
    return [
        "AGENT_CHANNELS__TELEGRAM__ENABLED",
        "AGENT_CHANNELS__TELEGRAM__TOKEN_ENV",
        "AGENT_CHANNELS__TELEGRAM__ALLOWED_USER_IDS",
        "AGENT_CHANNELS__TELEGRAM__ALLOWED_CHAT_IDS",
        "AGENT_CHANNELS__TELEGRAM__POLLING",
    ]


def _vscode_config_env_keys() -> list[str]:
    return [
        "AGENT_ADAPTERS__VSCODE__ENABLED",
        "AGENT_ADAPTERS__VSCODE__BRIDGE_HOST",
        "AGENT_ADAPTERS__VSCODE__BRIDGE_PORT",
        "AGENT_ADAPTERS__VSCODE__AUTH_TOKEN_ENV",
    ]


def _workspace_config_env_keys() -> list[str]:
    return [
        "AGENT_ADAPTERS__WORKSPACE__ENABLED",
        "AGENT_ADAPTERS__WORKSPACE__ROOT_DIR",
        "AGENT_ADAPTERS__WORKSPACE__WEB_HOST",
        "AGENT_ADAPTERS__WORKSPACE__WEB_PORT_START",
        "AGENT_ADAPTERS__WORKSPACE__OPEN_BROWSER",
    ]


def _computer_use_config_env_keys() -> list[str]:
    return [
        "AGENT_ADAPTERS__COMPUTER_USE__ENABLED",
        "AGENT_ADAPTERS__COMPUTER_USE__MAX_STEPS",
        "AGENT_ADAPTERS__COMPUTER_USE__STEP_DELAY_SECONDS",
        "AGENT_ADAPTERS__COMPUTER_USE__SCREENSHOT_DIR",
        "AGENT_ADAPTERS__COMPUTER_USE__ALLOWED_APPS",
        "AGENT_ADAPTERS__COMPUTER_USE__ALLOWED_ROOTS",
        "AGENT_ADAPTERS__COMPUTER_USE__REQUIRE_SESSION_APPROVAL",
        "AGENT_ADAPTERS__COMPUTER_USE__MAX_UI_ELEMENTS",
    ]


def _audit_config_update(
    repositories: Repositories,
    settings: AppSettings,
    section: str,
    payload: dict[str, Any],
) -> None:
    AuditLogger(repositories.audit, settings.logging.redact_patterns).append(
        AuditEventType.CONFIG_UPDATED,
        actor="admin",
        payload={"section": section, "config_file": str(CONFIG_FILE_PATH), "patch": payload},
    )


def build_task_trace(repositories: Repositories, task_id: str) -> dict[str, Any] | None:
    """The full "what did this task do" record - shared by the `/api/tasks/{id}/trace`
    endpoint and `ybm trace` (which calls this directly against the DB, no
    running backend required - see cli.py's `trace_task()`). Returns None if
    the task doesn't exist.
    """
    task = repositories.tasks.get(task_id)
    if task is None:
        return None
    raw_audit_events = _task_trace_audit_events(repositories, task.model_dump(mode="json"))
    tool_invocations = repositories.tool_invocations.list_for_task(task_id)
    formatted_audit = [format_audit_event(event).model_dump(mode="json") for event in raw_audit_events]
    raw_audit = [event.model_dump(mode="json") for event in raw_audit_events]
    # PlanModel is dead (docs/HISTORY.md §1.1 - the Operator loop never creates
    # one); operator_history in task metadata is the real step-by-step
    # execution record now.
    trace_context = _trace_context(task.model_dump(mode="json"), raw_audit)
    return {
        "task": task.model_dump(mode="json"),
        "context": trace_context,
        "operator_history": _enrich_operator_history(task.metadata.get("operator_history") or [], tool_invocations),
        "timeline": _trace_timeline(formatted_audit, tool_invocations),
        "tool_invocations": tool_invocations,
        "evidence": _extract_evidence(tool_invocations),
        "llm_calls": repositories.llm_calls.list_for_task(task_id),
        "approvals": [approval.model_dump(mode="json") for approval in repositories.approvals.list_for_task(task_id)],
        "artifacts": [artifact.model_dump(mode="json") for artifact in repositories.artifacts.list_for_task(task_id)],
        "signals": [signal.model_dump(mode="json") for signal in repositories.task_signals.list_for_task(task_id)],
        "audit": formatted_audit,
        "raw_audit": raw_audit,
    }


def build_task_receipt(repositories: Repositories, settings: AppSettings, task_id: str) -> dict[str, Any] | None:
    """A human-readable "what happened" summary (docs/UI_UX_AUDIT.md Phase 2)
    - what was asked, what changed, what was contacted outside this
    machine, what was approved, how long it took, and what's uncertain.
    Entirely derived from data build_task_trace already assembles plus
    egress.py's EGRESS_CONTACTED audit events; nothing new is persisted for
    this beyond those events. Returns None if the task doesn't exist.
    """
    task = repositories.tasks.get(task_id)
    if task is None:
        return None
    tool_invocations = repositories.tool_invocations.list_for_task(task_id)
    audit_events = repositories.audit.list_for_task(task_id)
    approvals = repositories.approvals.list_for_task(task_id)
    artifacts = repositories.artifacts.list_for_task(task_id)
    metadata = task.metadata

    tools_used: dict[str, dict[str, Any]] = {}
    for invocation in tool_invocations:
        name = str(invocation.get("tool_name") or "unknown")
        entry = tools_used.setdefault(name, {"tool_name": name, "calls": 0, "succeeded": 0, "failed": 0})
        entry["calls"] += 1
        status = str(invocation.get("status") or "")
        if status == "succeeded":
            entry["succeeded"] += 1
        elif status in {"failed", "denied", "timeout"}:
            entry["failed"] += 1

    services_contacted = [
        {"host": event.payload.get("host"), "tool_name": event.payload.get("tool_name"), "at": event.created_at.isoformat()}
        for event in audit_events
        if event.type == AuditEventType.EGRESS_CONTACTED
    ]

    verification = _receipt_verification(tool_invocations)

    token_usage = metadata.get("token_usage") if isinstance(metadata.get("token_usage"), dict) else {}
    default_profile = settings.llm.profiles.get(settings.llm.default_profile)
    llm_left_machine = bool(
        token_usage
        and default_profile is not None
        and not any(host in (default_profile.base_url or "") for host in ("127.0.0.1", "localhost"))
    )

    uncertainties: list[str] = []
    clarify_count = metadata.get("clarify_count")
    if clarify_count:
        uncertainties.append(f"Asked {clarify_count} clarifying question(s) before proceeding.")
    if metadata.get("fulfillment_gap"):
        uncertainties.append(str(metadata["fulfillment_gap"]))
    if task.status in {TaskStatus.FAILED, TaskStatus.BLOCKED} and metadata.get("last_worker_error"):
        uncertainties.append(str(metadata["last_worker_error"]))
    if verification["missing"]:
        shown = verification["missing"][:3]
        more = f" (+{len(verification['missing']) - 3} more)" if len(verification["missing"]) > 3 else ""
        uncertainties.append(f"{len(verification['missing'])} verification issue(s): " + "; ".join(shown) + more)

    duration_seconds = max(0.0, (task.updated_at - task.created_at).total_seconds())

    return {
        "task_id": task.id,
        "objective": task.objective,
        "status": task.status.value,
        "result_summary": metadata.get("synthesized_answer") or metadata.get("document_summary"),
        "changes": _extract_evidence(tool_invocations),
        "tools_used": sorted(tools_used.values(), key=lambda entry: str(entry["tool_name"])),
        "execution": _receipt_execution_counts(tool_invocations),
        "verification": verification,
        "services_contacted": services_contacted,
        "data_left_machine": bool(services_contacted) or llm_left_machine,
        "llm_left_machine": llm_left_machine,
        "approvals": [
            {"id": a.id, "capability": a.capability.value, "risk_level": a.risk_level.value, "status": a.status.value, "summary": a.summary}
            for a in approvals
        ],
        "artifacts": [artifact.model_dump(mode="json") for artifact in artifacts],
        "token_usage": token_usage,
        "duration_seconds": duration_seconds,
        "uncertainties": uncertainties,
        "created_at": task.created_at.isoformat(),
        "updated_at": task.updated_at.isoformat(),
    }


# needs_approval never reached an adapter at all; denied/cancelled were
# refused or withdrawn before one did either - none of the three were
# "attempted" in any sense a receipt should claim.
_NOT_ATTEMPTED_STATUSES = {"needs_approval", "denied", "cancelled"}
_FAILED_STATUSES = {"failed", "timeout", "rate_limited"}


def _receipt_execution_counts(tool_invocations: list[dict[str, Any]]) -> dict[str, int]:
    """Call-level counts for the receipt (docs/ROADMAP.md "Proof") - one
    entry per tool_invocations row, i.e. one per ToolCallRequest actually
    issued this task, not per item a single call's manifest describes (see
    _receipt_verification for that finer count).
    """
    attempted = 0
    succeeded = 0
    failed = 0
    for invocation in tool_invocations:
        status = str(invocation.get("status") or "")
        if status in _NOT_ATTEMPTED_STATUSES:
            continue
        attempted += 1
        if status == "succeeded":
            succeeded += 1
        elif status in _FAILED_STATUSES:
            failed += 1
    return {"calls_attempted": attempted, "calls_succeeded": succeeded, "calls_failed": failed}


def _receipt_verification(tool_invocations: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregates every tool call's ToolVerification (schemas.py) across the
    task - the mechanical "did it actually happen" proof a ToolDefinition's
    verify() hook attaches on success (docs/ROADMAP.md "Proof": "34 files
    are now under Documents; confirmed", not the model's word for it).

    Granularity note: one ToolVerification can cover many items in a single
    call (filesystem.manage's apply_manifest checks one manifest entry at a
    time), so `checked`/`verified` here count items, not calls - a 128-file
    move contributes 128, not 1, matching the receipt's own "43 destination
    paths verified" framing rather than "1 tool call succeeded".
    """
    checked = 0
    verified = 0
    missing: list[str] = []
    for invocation in tool_invocations:
        result = invocation.get("result")
        if not isinstance(result, dict):
            continue
        record = result.get("verification")
        if not isinstance(record, dict):
            continue
        checked += int(record.get("checked") or 0)
        verified += int(record.get("verified") or 0)
        missing.extend(str(item) for item in (record.get("missing") or []))
    return {"checked": checked, "verified": verified, "missing": missing}


# What a completed task actually touched (docs/HISTORY.md N5's "evidence view").
# Deliberately key-based, not a per-tool_name dispatch table: every tool's
# input/output dict already uses one of these field names for a path/URL/
# command whenever it has one (confirmed across filesystem, browser, http,
# code interpreter, workspace, VS Code, artifact delivery) - matching on the
# key means a newly registered tool is covered automatically as long as it
# follows the same naming, instead of needing a new branch here every time.
_EVIDENCE_FILE_KEYS = (
    "path", "paths", "workspace_dir", "changed_paths", "files_created",
    "screenshot_path", "screenshot_uri", "adapter_dir",
)
_EVIDENCE_URL_KEYS = ("url", "urls", "visited_urls", "browser_url", "preview_url")
_EVIDENCE_COMMAND_KEYS = ("command", "commands")


def _collect_evidence_values(source: dict[str, Any], keys: tuple[str, ...]) -> list[str]:
    values: list[str] = []
    for key in keys:
        value = source.get(key)
        if not value:
            continue
        if isinstance(value, list):
            values.extend(str(item) for item in value if item)
        else:
            values.append(str(value))
    return values


def _approval_blast_radius(action_payload: dict[str, Any]) -> dict[str, list[str]]:
    """Files/URLs/commands a PENDING approval's action would touch if
    approved - the Evidence Pack's "blast radius" field
    (docs/UI_REWRITE_PLAN.md §11.2). Reuses the same key-matching approach
    as _extract_evidence() below (the task-trace "what did this touch"
    view), just over one action's input instead of a whole task's
    completed tool_invocations.
    """
    action_input = action_payload.get("input")
    if not isinstance(action_input, dict):
        return {"files": [], "urls": [], "commands": []}
    return {
        "files": _collect_evidence_values(action_input, _EVIDENCE_FILE_KEYS),
        "urls": _collect_evidence_values(action_input, _EVIDENCE_URL_KEYS),
        "commands": _collect_evidence_values(action_input, _EVIDENCE_COMMAND_KEYS),
    }


def _extract_evidence(tool_invocations: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    evidence: dict[str, list[dict[str, Any]]] = {"files": [], "urls": [], "commands": []}
    seen: dict[str, set[str]] = {"files": set(), "urls": set(), "commands": set()}
    for invocation in tool_invocations:
        request = invocation.get("request") or {}
        result = invocation.get("result") or {}
        request_input = request.get("input") if isinstance(request, dict) else None
        result_output = result.get("output") if isinstance(result, dict) else None
        sources = [source for source in (request_input, result_output) if isinstance(source, dict)]
        tool_name = str(invocation.get("tool_name") or "")
        operation = request_input.get("operation") if isinstance(request_input, dict) else None
        effect = classify_effect(tool_name, operation)
        for bucket, keys in (
            ("files", _EVIDENCE_FILE_KEYS),
            ("urls", _EVIDENCE_URL_KEYS),
            ("commands", _EVIDENCE_COMMAND_KEYS),
        ):
            for source in sources:
                for value in _collect_evidence_values(source, keys):
                    if value in seen[bucket]:
                        continue
                    seen[bucket].add(value)
                    evidence[bucket].append(
                        {
                            "value": value,
                            "tool_name": invocation.get("tool_name"),
                            "at": invocation.get("created_at"),
                            "effect": effect,
                        }
                    )
    return evidence


def _task_trace_audit_events(repositories: Repositories, task: dict[str, Any]) -> list:
    events = repositories.audit.list_for_task(str(task["id"]))
    seen = {event.id for event in events}

    for event in list(events):
        if event.correlation_id:
            for related in repositories.audit.list_by_correlation_id(event.correlation_id):
                if related.id not in seen:
                    events.append(related)
                    seen.add(related.id)

    source_message_id = (task.get("metadata") or {}).get("source_message_id")
    if source_message_id:
        for related in repositories.audit.list_matching_payload_value("message_id", str(source_message_id)):
            if related.id not in seen:
                events.append(related)
                seen.add(related.id)
        for related in repositories.audit.list_matching_payload_value("source_message_id", str(source_message_id)):
            if related.id not in seen:
                events.append(related)
                seen.add(related.id)

    return sorted(events, key=lambda event: event.created_at)


def _trace_context(task: dict[str, Any], raw_audit: list[dict[str, Any]]) -> dict[str, Any]:
    classification = next((event for event in raw_audit if event.get("type") == AuditEventType.MESSAGE_CLASSIFIED.value), None)
    message = next((event for event in raw_audit if event.get("type") == AuditEventType.MESSAGE_RECEIVED.value), None)
    classification_payload = classification.get("payload", {}) if classification else {}
    return {
        "inbound_message": message.get("payload", {}) if message else None,
        "classification": classification_payload or {
            "task_type": (task.get("metadata") or {}).get("task_type"),
            "confidence": (task.get("metadata") or {}).get("classification_confidence"),
            "reason": (task.get("metadata") or {}).get("classification_reason"),
            "original_message_text": (task.get("metadata") or {}).get("original_message_text"),
        },
        "classifier_llm": classification_payload.get("llm"),
        "final_metadata": task.get("metadata") or {},
    }


def _elapsed_ms(start: Any, end: Any) -> float | None:
    """Milliseconds between two ISO timestamps, or None if either is
    missing/unparseable - a tool invocation still awaiting completion has
    no completed_at yet, and that must render as "unknown", not zero."""
    if not start or not end:
        return None
    try:
        started = datetime.fromisoformat(str(start))
        finished = datetime.fromisoformat(str(end))
    except ValueError:
        return None
    return max(0.0, (finished - started).total_seconds() * 1000)


def _trace_timeline(audit_events: list[dict[str, Any]], tool_invocations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The full chronological record (docs/HISTORY.md N5, categories and
    durations added in docs/UI_UX_AUDIT.md Phase 14). `category` reuses
    format_audit_event's own CATEGORY_BY_TYPE for audit rows - it was
    already computed, just not included here - and "tool" for every tool
    row, matching the category tool-originated audit events already use.
    `duration_ms` is exact for tool calls (real created_at/completed_at);
    audit events are effectively instantaneous log points, not spans, so
    theirs is always None rather than a fabricated zero.
    """
    items: list[dict[str, Any]] = []
    for event in audit_events:
        items.append(
            {
                "at": event.get("formatted_time") or event.get("created_at"),
                "kind": "audit",
                "category": event.get("category") or "system",
                "title": event.get("title") or event.get("type"),
                "summary": event.get("summary"),
                "actor": event.get("actor"),
                "details": event.get("details"),
                "duration_ms": None,
            }
        )
    for tool in tool_invocations:
        items.append(
            {
                "at": tool.get("completed_at") or tool.get("created_at"),
                "kind": "tool",
                "category": "tool",
                "title": tool.get("tool_name"),
                "summary": tool.get("status"),
                "actor": "orchestrator",
                "details": {"request": tool.get("request"), "result": tool.get("result")},
                "duration_ms": _elapsed_ms(tool.get("created_at"), tool.get("completed_at")),
            }
        )
    return sorted(items, key=lambda item: str(item.get("at") or ""))


def _enrich_operator_history(
    history: list[dict[str, Any]], tool_invocations: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Attaches duration_ms and content_trust to each Steps entry that names
    a real tool call (docs/UI_UX_AUDIT.md Phase 14; content_trust per
    docs/THREAT_MODEL.md), joining on request_id - the same
    ToolCallRequest.id / ToolCallResult.request_id correlation the executor
    already uses (worker.py stamps it onto each history entry at the point
    where it has the real ToolCallResult in hand), not a new key. Entries
    with no request_id - pseudo-checks (audit/fulfillment gap), delegate
    summaries (span many tool calls, not one), unregistered-tool refusals -
    get both as None rather than a fabricated value.
    """
    duration_by_request_id = {
        invocation["id"]: _elapsed_ms(invocation.get("created_at"), invocation.get("completed_at"))
        for invocation in tool_invocations
    }
    trust_by_request_id = {
        invocation["id"]: (invocation.get("result") or {}).get("content_trust")
        for invocation in tool_invocations
    }
    return [
        {
            **entry,
            "duration_ms": duration_by_request_id.get(entry.get("request_id")),
            "content_trust": trust_by_request_id.get(entry.get("request_id")),
        }
        for entry in history
    ]


def _path_within_roots(path: Path, roots: list[Path]) -> bool:
    return any(root == path or root in path.parents for root in roots)


DENIAL_REASON_TEXT = {
    "allowlist_empty": "no one is on the allowlist yet",
    "user_not_allowed": "this user is not on the allowlist",
    "chat_not_allowed": "this chat is not on the allowlist",
    "telegram_disabled": "the Telegram channel is turned off",
}


def _recent_telegram_denials(repositories: Repositories, limit: int = 50) -> dict[str, Any]:
    """Refused Telegram messages, summarized for display.

    These denials were always recorded with the precise reason and the sender's
    id, and never shown anywhere - so the one failure that looks identical to a
    crash from the user's side ("I messaged the bot and nothing happened") was
    also the one the console stayed silent about. The ids returned here are
    what the operator needs to fix it, which is why they come back whole rather
    than as a count.
    """
    denials = [
        event
        for event in repositories.audit.list_by_type_recent(
            AuditEventType.TELEGRAM_ACCESS_DECISION, limit=limit
        )
        if not (event.payload or {}).get("allowed")
    ]
    if not denials:
        return {"count": 0, "latest": None, "user_ids": []}
    latest = denials[0]
    reason = (latest.payload or {}).get("reason") or "unknown"
    user_ids = sorted(
        {
            user_id
            for event in denials
            if isinstance((user_id := (event.payload or {}).get("user_id")), int)
        }
    )
    return {
        "count": len(denials),
        "latest": {
            "at": latest.created_at.isoformat(),
            "reason": reason,
            "explanation": DENIAL_REASON_TEXT.get(reason, reason),
            "user_id": (latest.payload or {}).get("user_id"),
            "chat_id": (latest.payload or {}).get("chat_id"),
        },
        "user_ids": user_ids,
    }


def _config_warnings(settings: AppSettings) -> list[str]:
    warnings: list[str] = []
    telegram = settings.channels.telegram
    if telegram.enabled and not telegram.allowed_user_ids and not telegram.allowed_chat_ids:
        warnings.append("Telegram is enabled but no allowed user IDs or chat IDs are configured; all messages will be denied.")
    whatsapp = settings.channels.whatsapp
    if whatsapp.enabled and not whatsapp.allowed_numbers:
        warnings.append("WhatsApp is enabled but no allowed numbers are configured; all messages will be denied.")
    if settings.llm.default_profile not in settings.llm.profiles:
        warnings.append("Default orchestrator LLM profile is not configured; Telegram task classification will fail.")
    if read_env_value("AGENT_CAPABILITIES"):
        warnings.append("AGENT_CAPABILITIES is set in the environment and may override access-mode changes saved to YAML.")
    schedule_policy = settings.capabilities.get(Capability.SCHEDULE_MANAGE)
    if settings.scheduler.enabled and not (schedule_policy and schedule_policy.enabled):
        warnings.append("Scheduler service is enabled, but schedule.manage capability is off; scheduled jobs cannot be created from tasks.")
    return warnings


def _database_summary(settings: AppSettings) -> dict[str, Any]:
    database = Database(settings.storage.database_url)
    database.initialize()
    tables = [
        "conversations",
        "conversation_memory",
        "messages",
        "tasks",
        "approvals",
        "tool_invocations",
        "artifacts",
        "schedules",
        "audit_events",
    ]
    counts: dict[str, int] = {}
    with database.connect() as connection:
        for table in tables:
            counts[table] = int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        last_task = connection.execute("SELECT updated_at FROM tasks ORDER BY updated_at DESC LIMIT 1").fetchone()
        last_audit = connection.execute("SELECT created_at FROM audit_events ORDER BY created_at DESC LIMIT 1").fetchone()
    return {
        "database_url": settings.storage.database_url,
        "path": database.path,
        "table_counts": counts,
        "last_task_at": last_task[0] if last_task else None,
        "last_audit_at": last_audit[0] if last_audit else None,
        "recommended_vscode_extension": "qwtel.sqlite-viewer",
    }


def _known_tool_names(settings: AppSettings) -> list[str]:
    """Every registered tool's name, regardless of whether it's currently
    enabled - used only to spot which tools a skill's instructions
    reference (skills.py's detect_referenced_tools), not to gate anything.
    """
    registry = build_tool_registry(settings, backend_base_url(settings))
    return [definition.name for definition in registry.definitions]


def _tool_registry_summary(settings: AppSettings, repositories: Repositories, audit: AuditLogger) -> dict[str, Any]:
    registry = build_tool_registry(
        settings,
        backend_base_url(settings),
        artifact_repository=repositories.artifacts,
        task_repository=repositories.tasks,
        repositories=repositories,
        audit_logger=audit,
    )
    tools = []
    for definition in registry.definitions:
        operations = list(definition.operations)
        tools.append(
            {
                "name": definition.name,
                "group": _tool_group(definition.name),
                "capability": definition.capability.value,
                "enabled": definition.enabled,
                "lifecycle": definition.lifecycle,
                "operations": operations,
                "default_operation": definition.default_operation,
                # The tool's own required_risk() for its default operation -
                # a representative risk level for the Tools page
                # (docs/UI_UX_AUDIT.md Phase 11), not every operation's
                # individual risk (operation_risks, already in
                # operation_schemas' sibling data if a caller needs that
                # detail).
                "risk_level": definition.required_risk({"operation": definition.default_operation}).value,
                "input_schema": definition.input_schema.__name__ if definition.input_schema else None,
                "output_schema": definition.output_schema.__name__ if definition.output_schema else None,
                "operation_schemas": {
                    operation: schema.__name__
                    for operation, schema in (definition.operation_schemas or {}).items()
                },
                "operation_output_schemas": {
                    operation: schema.__name__
                    for operation, schema in (definition.operation_output_schemas or {}).items()
                },
            }
        )
    return {
        "total": len(tools),
        "enabled": len([tool for tool in tools if tool["enabled"]]),
        "tools": tools,
    }



def _tool_group(name: str) -> str:
    if name.startswith("browser."):
        return "browser"
    if name.startswith("computer.") or name.startswith("desktop."):
        return "desktop"
    if name.startswith("filesystem.") or name.startswith("workspace."):
        return "filesystem"
    if name.startswith("document."):
        return "documents"
    if name in {"coding.agent", "coding_assistant", "vscode.copilot_terminal", "vscode.terminal_command"}:
        return "coding_agents"
    if name.startswith("schedule."):
        return "schedules"
    if name.startswith("artifact."):
        return "artifacts"
    if name.startswith("adapter."):
        return "adapter_factory"
    return "other"


# The React console (docs/UI_REWRITE_PLAN.md) is the one real admin UI,
# built from frontend/ into static/admin/ and served by _serve_admin_app
# above. This is only the case-3 fallback: no build exists yet at this
# checkout (a fresh clone/CI, or before `ybm ui-build` has ever run) - a
# small pointer telling the operator how to get one, not a 404 or crash.
_ADMIN_HTML = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>YBM</title>
  <style>
    body { font-family: system-ui, sans-serif; max-width: 32rem; margin: 4rem auto; padding: 0 1.5rem; color: #1a1a1a; }
    a { color: #2563eb; }
    code { background: #f1f1f1; padding: 0.1rem 0.35rem; border-radius: 0.25rem; }
  </style>
</head>
<body>
  <h1>YBM</h1>
  <p>No admin console build was found yet.</p>
  <p><b>1. Install Node.js 22.22 or newer</b> if you don't have it -
     <a href="https://nodejs.org">nodejs.org</a>, or <code>winget install OpenJS.NodeJS.LTS</code>.
     The console is a React app, so it cannot be built without it. Open a new terminal afterwards.</p>
  <p><b>2. Build it:</b> <code>.\\scripts\\ybm.ps1 ui-build</code>
     (or <code>npm run build</code> in <code>frontend/</code>), then reload this page.</p>
  <p>This page's JSON API (<code>/admin/api/*</code>) is unchanged and works regardless of whether a build exists.</p>
</body>
</html>
"""

