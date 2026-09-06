from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from pathlib import Path
import sys

from agent_control.backup import run_backup
from agent_control.bootstrap import run_doctor, run_setup
from agent_control.updates import check_for_updates
from agent_control.config_sync import set_config_path
from agent_control.onboarding import run_onboard
from agent_control.db_tools import db_clean, db_inspect, db_reset
from agent_control.logging_setup import configure_logging
from agent_control.channels.telegram import (
    TelegramAdapter,
    TelegramBotApi,
    TelegramIntakeService,
    TelegramPollingRunner,
    load_telegram_token,
)
from agent_control.channels.memory import ConversationMemoryService
from agent_control.channels.responder import LLMChatResponder
from agent_control.channels.telegram_notifications import TelegramTaskNotifier
from agent_control.channels.whatsapp import WhatsAppAdapter, WhatsAppBridgeClient, WhatsAppIntakeService, WhatsAppPollingRunner
from agent_control.channels.whatsapp_bridge_process import WhatsAppBridgeProcess, load_whatsapp_bridge_client
from agent_control.channels.whatsapp_notifications import WhatsAppTaskNotifier
from agent_control.config import AppSettings, backend_base_url, load_settings
from agent_control.llm import LLMMessageClassifier, build_default_llm_provider
from agent_control.llm.providers import build_major_llm_provider, build_role_llm_provider
from agent_control.observation import ArtifactService, ScreenshotService
from agent_control.persona import persona_prompt_section
from agent_control.tools.skills import skills_context_section
from agent_control.orchestration import AuditorService, OperatorLoopService, TaskWorker, ToolExecutor, reconcile_orphaned_tasks
from agent_control.orchestration.worker import TaskNotificationSink
from agent_control.policy import PolicyEngine
from agent_control.recovery import RetryPolicy
from agent_control.heartbeat import run_heartbeat_forever
from agent_control.scheduler import run_scheduler_forever
from agent_control.schemas import AuditEventType, ChannelType, TaskRecord, TaskStatus
from agent_control.storage import ApprovalRepository, AuditLogger, Database, Repositories
from agent_control.tools.registry import build_tool_registry
from agent_control.tools.stt import build_stt_adapter
from agent_control.tools.coding_agent import (
    load_sessions,
    mark_session_progress_notified,
    mark_session_notified,
    run_coding_agent_session as run_coding_agent_session_once,
    scan_coding_sessions_once,
    session_completion_message,
    session_progress_due,
    session_progress_message,
    terminal_session_result,
)


logger = logging.getLogger(__name__)


def build_repositories() -> tuple[Repositories, AuditLogger]:
    settings = load_settings()
    database = Database(settings.storage.database_url)
    database.initialize()
    repositories = Repositories.for_database(database)
    return repositories, AuditLogger(repositories.audit, settings.logging.redact_patterns)


def _frontend_dir() -> Path:
    return Path(__file__).resolve().parents[3] / "frontend"


def _run_npm_script(script: str) -> int:
    """Shared front half of `ui-build` and `ui-dev`.

    Both used to bail with "frontend/node_modules is missing - run `npm
    install` in frontend/ first" and, if npm itself was absent, whatever
    FileNotFoundError/exit code the shell produced. That mattered because
    these two commands are the *only* documented way out of an unbuilt admin
    console: the placeholder page at /admin points here, so failing with a
    prerequisite the message doesn't name left no next step. Install what's
    missing, and when the missing thing is Node itself, say so.
    """
    import shutil
    import subprocess

    frontend_dir = _frontend_dir()
    if not frontend_dir.exists():
        print(f"no frontend/ checkout found at {frontend_dir}")
        return 1

    if shutil.which("npm") is None:
        print("npm was not found on PATH.")
        print()
        print("The admin console is a React app, so building it needs Node.js 22.22 or newer:")
        print("  https://nodejs.org   (or: winget install OpenJS.NodeJS.LTS)")
        print()
        print("Open a NEW terminal after installing, then re-run this command.")
        return 1

    use_shell = os.name == "nt"
    if not (frontend_dir / "node_modules").exists():
        print("Installing admin console dependencies (npm install)...")
        installed = subprocess.run(["npm", "install"], cwd=frontend_dir, shell=use_shell)
        if installed.returncode != 0:
            print("`npm install` failed - see the output above.")
            return installed.returncode

    return subprocess.run(["npm", "run", script], cwd=frontend_dir, shell=use_shell).returncode


def ui_build() -> int:
    """`npm run build` for the React console (docs/UI_REWRITE_PLAN.md §12.3) -
    output lands under agent_control/static/admin per vite.config.ts's
    build.outDir, where admin.py's SPA route serves it from."""
    code = _run_npm_script("build")
    if code == 0:
        print()
        print("Admin console built - reload http://127.0.0.1:8765/admin")
    return code


def ui_dev() -> int:
    """`npm run dev` for the React console - the Vite dev server with hot
    reload, proxying /admin/api/* to this backend (vite.config.ts)."""
    return _run_npm_script("dev")


def init_db() -> None:
    settings = load_settings()
    database = Database(settings.storage.database_url)
    database.initialize()
    print(f"initialized {settings.storage.database_url}")


def config_summary() -> None:
    print(json.dumps(load_settings().safe_summary(), indent=2, default=str))


def trace_task(task_id: str, *, as_json: bool = False) -> int:
    """One-command task post-mortem, reading the DB directly - no running
    backend required, unlike the admin UI (docs/HISTORY.md §2.4: "to debug a
    failed task today you need the stack running, then the admin UI, then
    click into a trace"). Shares `build_task_trace()` with the
    `/admin/api/tasks/{id}/trace` endpoint so the two never drift apart.
    Returns a process exit code (0 found, 1 not found).
    """
    from agent_control.admin import build_task_trace

    repositories, _audit = build_repositories()
    trace = build_task_trace(repositories, task_id)
    if trace is None:
        print(f"no task found with id {task_id}")
        return 1
    if as_json:
        print(json.dumps(trace, indent=2, default=str))
        return 0

    task = trace["task"]
    print(f"task    {task['id']}")
    print(f"status  {task['status']}")
    print(f"goal    {task['objective']}")
    metadata = task.get("metadata") or {}
    if metadata.get("synthesized_answer"):
        print(f"answer  {metadata['synthesized_answer']}")
    if metadata.get("last_worker_error"):
        print(f"error   {metadata['last_worker_error']}")
    usage = metadata.get("token_usage")
    if usage:
        by_source = usage.get("by_source") or {}
        breakdown = ", ".join(f"{name}={entry.get('total_tokens', 0)}" for name, entry in by_source.items())
        print(
            f"tokens  {usage.get('total_tokens', 0)} total over {usage.get('calls', 0)} call(s)"
            f"{f' ({breakdown})' if breakdown else ''}"
        )

    history = trace.get("operator_history") or []
    print(f"\noperator steps ({len(history)}):")
    if not history:
        print("  (none recorded)")
    for index, step in enumerate(history, 1):
        tool_name = step.get("tool_name") or "?"
        status = step.get("status") or "?"
        print(f"  {index}. {tool_name}  [{status}]")
        if step.get("input"):
            print(f"     input:  {json.dumps(step['input'], default=str)}")
        if step.get("output_summary"):
            summary = str(step["output_summary"]).replace("\n", " ")
            print(f"     output: {summary[:300]}")
        if step.get("error"):
            print(f"     error:  {step['error']}")

    tool_invocations = trace.get("tool_invocations") or []
    print(f"\ntool invocations recorded in DB: {len(tool_invocations)}")
    print(f"audit events: {len(trace.get('audit') or [])}")

    evidence = trace.get("evidence") or {}
    files = evidence.get("files") or []
    urls = evidence.get("urls") or []
    commands = evidence.get("commands") or []
    if files or urls or commands:
        print("\nevidence (what this task touched):")
        for label, items in (("files", files), ("urls", urls), ("commands", commands)):
            for item in items:
                print(f"  [{label}] {item.get('value')}  ({item.get('tool_name')})")
    return 0


async def poll_telegram() -> None:
    settings = load_settings()
    repositories, audit = build_repositories()
    adapter = TelegramAdapter(settings.channels.telegram, audit)
    provider = build_default_llm_provider(settings)
    concierge_provider = build_role_llm_provider(settings, "concierge") or provider
    classifier = LLMMessageClassifier(concierge_provider) if concierge_provider else None
    responder = LLMChatResponder(concierge_provider, settings, repositories) if concierge_provider else None
    memory_service = ConversationMemoryService(repositories, provider=concierge_provider)
    client = TelegramBotApi(load_telegram_token(settings.channels.telegram), audit=audit)
    service = TelegramIntakeService(
        adapter,
        repositories,
        audit,
        settings=settings,
        bot_api=client,
        stt=build_stt_adapter(settings.adapters.stt),
        screenshot_service=_screenshot_service(settings, repositories),
        classifier=classifier,
        responder=responder,
        memory_service=memory_service,
    )
    runner = TelegramPollingRunner(client, service)
    offset: int | None = None
    while True:
        # Pick up allowlist/enabled edits without a restart. Everything above
        # is built once from a startup snapshot, so before this the console
        # could save a perfectly correct allowlist and the running poller
        # would go on denying every message from it - with the save toast
        # saying only "restart polling to reload it" and offering no way to
        # do that. The authorization check reads adapter.config directly, so
        # refreshing that one object is enough for those edits to take effect
        # live. Rotating the *token* still needs a restart: the API client and
        # its offset cursor are bound to the old bot.
        try:
            adapter.config = load_settings().channels.telegram
        except Exception as exc:  # noqa: BLE001 - a bad edit must not kill the poller
            audit.append(
                AuditEventType.ERROR,
                actor="telegram_polling",
                payload={"error": "config_reload_failed", "reason": str(exc)},
            )
        try:
            offset, _ = await runner.poll_once(offset=offset, timeout=30)
        except Exception as exc:
            audit.append(
                AuditEventType.ERROR,
                actor="telegram_polling",
                payload={"error": "poll_once_failed", "reason": str(exc)},
            )
            await asyncio.sleep(5)


async def poll_whatsapp() -> None:
    """Mirrors poll_telegram() (docs/UI_UX_AUDIT.md Phase 16), with one extra
    step: it also owns the whatsapp-bridge Node sidecar for its whole
    lifetime (start it before polling, stop it on exit) - see
    channels/whatsapp_bridge_process.py for why that lives here rather than
    as a separate service. Raises immediately, the same way
    load_telegram_token() already does for an unconfigured Telegram, if the
    channel isn't enabled - `run_supervised.ps1`'s crash-loop breaker turns
    that into a clean "failed" status rather than a busy-loop, matching how
    an unconfigured Telegram already behaves.
    """
    settings = load_settings()
    if not settings.channels.whatsapp.enabled:
        raise RuntimeError("WhatsApp channel is not enabled (channels.whatsapp.enabled: false in config.yaml)")
    repositories, audit = build_repositories()
    bridge = WhatsAppBridgeProcess(settings.channels.whatsapp)
    try:
        await bridge.start()
    except Exception as exc:
        audit.append(AuditEventType.ERROR, actor="whatsapp_polling", payload={"error": "bridge_start_failed", "reason": str(exc)})
        # A health-check timeout still leaves a spawned node child running -
        # without this it survives this process's exit as an orphan holding
        # the bridge port, so every subsequent start fails the same way.
        bridge.stop()
        raise

    adapter = WhatsAppAdapter(settings.channels.whatsapp, audit)
    provider = build_default_llm_provider(settings)
    concierge_provider = build_role_llm_provider(settings, "concierge") or provider
    classifier = LLMMessageClassifier(concierge_provider) if concierge_provider else None
    responder = LLMChatResponder(concierge_provider, settings, repositories) if concierge_provider else None
    memory_service = ConversationMemoryService(repositories, provider=concierge_provider)
    client = WhatsAppBridgeClient(bridge.base_url, bridge.secret)
    service = WhatsAppIntakeService(
        adapter, repositories, audit,
        settings=settings, bridge_client=client,
        classifier=classifier, responder=responder, memory_service=memory_service,
    )
    runner = WhatsAppPollingRunner(client, service)
    offset = 0
    try:
        while True:
            if not bridge.is_alive():
                # The node child exited on its own (crash, killed
                # externally, port taken by something else) - without this
                # check, poll_once below would just fail against a closed
                # port every 2s forever with a "running" service status.
                # Raising instead lets run_supervised.ps1's existing
                # restart-with-backoff / crash-loop breaker handle recovery,
                # the same machinery every other supervised service already
                # relies on.
                audit.append(
                    AuditEventType.ERROR,
                    actor="whatsapp_polling",
                    payload={"error": "bridge_process_exited", "reason": f"whatsapp-bridge child exited (code {bridge.returncode})"},
                )
                raise RuntimeError(f"whatsapp-bridge child process exited unexpectedly (code {bridge.returncode})")
            try:
                offset, _ = await runner.poll_once(offset=offset)
            except Exception as exc:
                audit.append(
                    AuditEventType.ERROR,
                    actor="whatsapp_polling",
                    payload={"error": "poll_once_failed", "reason": str(exc)},
                )
            await asyncio.sleep(2)
    finally:
        bridge.stop()


async def run_worker() -> None:
    settings = load_settings()
    repositories, audit = build_repositories()
    reconciled = reconcile_orphaned_tasks(repositories, audit)
    if reconciled:
        print(f"reconciled {reconciled} task(s) left running/interpreting by a previous worker (failed explicitly)")
    provider = build_default_llm_provider(settings)
    policy = PolicyEngine(settings, audit)
    registry = build_tool_registry(
        settings,
        backend_base_url(settings),
        provider=provider,
        should_continue=lambda task_id: _task_allows_tool_continue(repositories, task_id),
        artifact_repository=repositories.artifacts,
        task_repository=repositories.tasks,
        repositories=repositories,
        audit_logger=audit,
        telegram_client=_telegram_client(settings, audit),
    )
    major_provider = build_major_llm_provider(settings)
    operator_provider = build_role_llm_provider(settings, "operator") or provider
    auditor_provider = build_role_llm_provider(settings, "auditor") or provider
    operator = OperatorLoopService(operator_provider, major_provider=major_provider) if operator_provider else None
    auditor = AuditorService(auditor_provider) if auditor_provider else None
    executor = ToolExecutor(
        policy,
        repositories,
        audit,
        adapters=registry.adapters,
        tool_definitions=registry.definition_index,
    )
    notifier = RoutingNotificationSink(settings, audit, approvals=repositories.approvals)
    # Run max_parallel_tasks worker loops in one process. claim_next() claims
    # atomically per worker_id, so quick tasks (status, delivery) are not
    # starved behind a long-running coding or browser task.
    workers = [
        TaskWorker(
            repositories,
            audit,
            executor=executor,
            retry_policy=RetryPolicy(settings.limits),
            config_context=_worker_config_context(registry, settings),
            config_context_factory=lambda registry=registry, settings=settings: _worker_config_context(registry, settings),
            notification_sink=notifier,
            task_budget_seconds=float(settings.limits.task_budget_seconds),
            operator=operator,
            operator_max_steps=settings.operator.max_steps,
            fulfillment_mode=settings.operator.fulfillment_mode,
            audit_min_tool_calls=settings.operator.audit_min_tool_calls,
            persona_config=settings.adapters.persona,
            skills_config=settings.adapters.skills,
            auditor=auditor,
            persist_llm_calls=settings.storage.persist_llm_calls,
            llm_call_max_chars=settings.storage.llm_call_max_chars,
            redact_patterns=settings.logging.redact_patterns,
        )
        for _ in range(max(settings.limits.max_parallel_tasks, 1))
    ]
    loops = [worker.run_forever() for worker in workers]
    # Beside the workers, not inside them: a worker awaiting a long subprocess
    # cannot emit anything, which is exactly when an update matters most.
    if settings.limits.heartbeat_interval_seconds > 0:
        loops.append(
            run_heartbeat_forever(
                repositories,
                notifier,
                interval_seconds=float(settings.limits.heartbeat_interval_seconds),
            )
        )
    try:
        await asyncio.gather(*loops)
    except Exception:
        # Structured service logs are append-only, unlike the supervisor's
        # redirected child stderr file which is reopened on a restart. Keep
        # the terminating traceback durable so a self-restart is diagnosable.
        logger.exception("worker_service_terminated")
        raise


async def run_scheduler() -> None:
    settings = load_settings()
    repositories, audit = build_repositories()
    await run_scheduler_forever(
        repositories,
        audit,
        poll_interval_seconds=settings.scheduler.poll_interval_seconds,
        max_consecutive_failures=settings.scheduler.max_consecutive_failures,
        retention_days=settings.storage.retention_days,
    )


async def run_coding_agent_session(session_root: str, session_id: str) -> None:
    await run_coding_agent_session_once(session_root, session_id)


async def run_coding_session_watcher(poll_interval_seconds: float = 5.0) -> None:
    settings = load_settings()
    repositories, audit = build_repositories()
    telegram = _telegram_client(settings, audit)
    while True:
        try:
            sessions = await scan_coding_sessions_once(settings.adapters.coding_agent.session_root)
            for session in sessions:
                error = await _handle_coding_session_completion(settings, repositories, session, telegram)
                mark_session_notified(
                    settings.adapters.coding_agent.session_root,
                    str(session.get("session_id")),
                    error=error,
                )
            if telegram is not None:
                active_sessions = load_sessions(settings.adapters.coding_agent.session_root, limit=None)
                for session in active_sessions:
                    if not session_progress_due(
                        session,
                        settings.adapters.coding_agent.progress_interval_seconds,
                    ):
                        continue
                    if await _handle_coding_session_progress(repositories, session, telegram):
                        mark_session_progress_notified(
                            settings.adapters.coding_agent.session_root,
                            str(session.get("session_id")),
                        )
        except Exception as exc:
            audit.append(
                AuditEventType.ERROR,
                actor="coding_session_watcher",
                payload={"error": "watcher_scan_failed", "reason": str(exc)},
            )
        await asyncio.sleep(poll_interval_seconds)


async def _handle_coding_session_progress(repositories: Repositories, session: dict, telegram) -> bool:
    """Send one durable heartbeat only for the task actually awaiting this session."""
    task_id = session.get("task_id")
    task = repositories.tasks.get(str(task_id)) if task_id else None
    if task is None or task.status != TaskStatus.AWAITING_EXTERNAL:
        return False
    awaiting = task.metadata.get("awaiting_external") if isinstance(task.metadata, dict) else None
    expected_session_id = str(awaiting.get("session_id") or "") if isinstance(awaiting, dict) else ""
    if expected_session_id != str(session.get("session_id") or ""):
        return False
    chat_id = task.metadata.get("source_chat_id")
    if not chat_id:
        return False
    await telegram.send_message(str(chat_id), session_progress_message(session))
    return True


async def _handle_coding_session_completion(settings, repositories: Repositories, session: dict, telegram) -> str | None:
    error: str | None = None
    task_id = session.get("task_id")
    task = repositories.tasks.get(str(task_id)) if task_id else None
    if task is not None:
        result = terminal_session_result(session)
        awaiting = task.metadata.get("awaiting_external") if isinstance(task.metadata, dict) else None
        metadata = {
            **task.metadata,
            "coding_agent_session": _coding_session_brief(session),
        }
        if task.status == TaskStatus.AWAITING_EXTERNAL and isinstance(awaiting, dict):
            metadata["pending_tool_result"] = {
                "step_id": awaiting.get("step_id"),
                "tool_name": "coding.agent",
                "result": result.model_dump(mode="json"),
            }
        status = TaskStatus.RUNNING if task.status == TaskStatus.AWAITING_EXTERNAL else task.status
        repositories.tasks.update_metadata(task.id, metadata, status)
    chat_id = task.metadata.get("source_chat_id") if task is not None else None
    if telegram is not None and chat_id:
        try:
            await telegram.send_message(str(chat_id), session_completion_message(session))
        except Exception as exc:
            error = str(exc)
    return error


def _coding_session_brief(session: dict) -> dict:
    return {
        key: session.get(key)
        for key in ("session_id", "provider", "status", "returncode", "changed_files", "summary", "ended_at")
    }


def _telegram_notifier(
    settings, audit: AuditLogger | None = None, approvals: ApprovalRepository | None = None
) -> TelegramTaskNotifier | None:
    if not settings.channels.telegram.enabled:
        return None
    try:
        client = _telegram_client(settings, audit)
        return TelegramTaskNotifier(client, approvals=approvals) if client else None
    except RuntimeError:
        return None


def _telegram_client(settings, audit: AuditLogger | None = None) -> TelegramBotApi | None:
    if not settings.channels.telegram.enabled:
        return None
    try:
        return TelegramBotApi(load_telegram_token(settings.channels.telegram), audit=audit)
    except RuntimeError:
        return None


def _whatsapp_notifier() -> WhatsAppTaskNotifier | None:
    """No settings check here (unlike _telegram_notifier) - if the bridge's
    state file exists at all, WhatsApp must be enabled, since only
    poll_whatsapp() (which refuses to start otherwise) ever writes it."""
    client = load_whatsapp_bridge_client()
    return WhatsAppTaskNotifier(client) if client else None


class RoutingNotificationSink:
    """Implements `TaskNotificationSink` (orchestration/worker.py) - routes
    a task-completion notification to whichever channel it came from
    (docs/UI_UX_AUDIT.md Phase 16), replacing the single hardcoded
    `notifier` `run_worker()` used to build once at startup for every task
    regardless of source. Rebuilds the per-channel notifier fresh on every
    call rather than caching one at construction time: WhatsApp's bridge
    lives in a different process (poll-whatsapp) that can start, restart,
    or reconfigure independently of run-worker's own lifetime, and this
    keeps Telegram consistent with that rather than special-cased.
    An unconfigured channel (e.g. a web-chat task, or WhatsApp with no
    bridge running) is a silent no-op, not a hard failure - notifying is
    best-effort and must never block the task itself finishing. Only a
    failure while actually attempting to notify a configured channel is
    audited (below), not the "nothing to notify" case itself.
    """

    def __init__(self, settings: AppSettings, audit: AuditLogger, approvals: ApprovalRepository | None = None) -> None:
        self.settings = settings
        self.audit = audit
        self.approvals = approvals

    async def notify(self, task: TaskRecord) -> None:
        source_channel = task.metadata.get("source_channel") or ChannelType.TELEGRAM.value
        sink: TaskNotificationSink | None = None
        if source_channel == ChannelType.TELEGRAM.value:
            sink = _telegram_notifier(self.settings, self.audit, approvals=self.approvals)
        elif source_channel == ChannelType.WHATSAPP.value:
            sink = _whatsapp_notifier()
        if sink is None:
            return
        try:
            await sink.notify(task)
        except Exception as exc:
            self.audit.append(
                AuditEventType.ERROR, actor="notifier", task_id=task.id,
                payload={"error": "notify_failed", "channel": source_channel, "reason": str(exc)},
            )


def _task_allows_tool_continue(repositories: Repositories, task_id: str) -> bool:
    task = repositories.tasks.get(task_id)
    if task is None:
        return False
    return task.status not in {TaskStatus.PAUSED, TaskStatus.CANCELLED}


def _screenshot_service(settings, repositories: Repositories) -> ScreenshotService | None:
    if not settings.adapters.desktop.screenshot_enabled:
        return None
    return ScreenshotService(
        settings.adapters.desktop,
        ArtifactService(settings.storage, repositories.artifacts),
    )



def _worker_config_context(registry, settings: AppSettings) -> str:
    persona_section = persona_prompt_section(settings.adapters.persona)
    # Rebuilt per decide() call via config_context_factory, so installing a
    # skill takes effect on the next step rather than needing a worker restart.
    skills_section = (
        skills_context_section(
            settings.adapters.skills.root_dir, settings.adapters.skills.max_skills_listed
        )
        if settings.adapters.skills.enabled
        else ""
    )
    # registry.vault_summary() used to be rendered here too. It lists the same
    # 24 tools with the same descriptions as registry.context() above, differing
    # only in the words "available/known_gap" versus "enabled/disabled" - ~790
    # tokens of duplication in every single Operator step. Measured: the static
    # prefix was 4247 tokens against an average completion of 93, so this loop
    # is prefill-bound and the catalog is the largest single item in it.
    return f"""{registry.context()}

{persona_section}

{skills_section}

Prefer conservative plans. Use registered tool names exactly and include explicit operations when a tool supports them. For exact JSON/REST APIs, prefer http.request when the target is allowlisted. If a needed connector is missing, refresh or install MCP when a matching server is known; otherwise use adapter.factory to scaffold and test a proposal instead of inventing unregistered tool names."""


# Long-running services get their own log file, named to match ybm.ps1's
# service names (`ybm logs <service>` / `ybm start` use the same strings) so
# a log file and a running process are trivially correlated. One-off CLI
# utility commands (doctor, db-*, config-*) share a catch-all "cli" log.
_COMMAND_SERVICE_NAMES = {
    "run-worker": "worker",
    "run-scheduler": "scheduler",
    "poll-telegram": "telegram_polling",
    "poll-whatsapp": "whatsapp",
    "run-coding-session-watcher": "coding_session_watcher",
    "run-coding-agent-session": "coding_agent_session",
}


def _configure_logging_for_command(command: str) -> None:
    service_name = _COMMAND_SERVICE_NAMES.get(command, "cli")
    try:
        configure_logging(load_settings(), service_name)
    except Exception:
        # Config itself might be broken (that's what `doctor` exists to
        # diagnose) - fall back to basic stderr logging rather than let a
        # settings-loading failure take down the command before it even runs.
        logging.basicConfig(level=logging.INFO, stream=sys.stderr)
        logging.getLogger(__name__).warning(
            "structured logging setup failed; falling back to basic logging", exc_info=True
        )


def main() -> None:
    parser = argparse.ArgumentParser("ybm")
    parser.add_argument(
        "command",
        choices=[
            "doctor",
            "setup",
            "onboard",
            "start",
            "stop",
            "status",
            "logs",
            "ui-build",
            "ui-dev",
            "init-db",
            "config-summary",
            "config-set",
            "channel-enabled",
            "db-inspect",
            "db-clean",
            "db-reset",
            "trace-task",
            "poll-telegram",
            "poll-whatsapp",
            "run-worker",
            "run-scheduler",
            "run-coding-agent-session",
            "run-coding-session-watcher",
            "backup",
            "check-updates",
        ],
    )
    parser.add_argument("--session-root", default=None)
    parser.add_argument("--session-id", default=None)
    parser.add_argument("--poll-interval-seconds", type=float, default=5.0)
    parser.add_argument("--telegram-token", default=None, help="used by `setup` to save TELEGRAM_BOT_TOKEN")
    parser.add_argument("path", nargs="?", default=None,
                         help="dotted config path (config-set) / task id (trace-task) / service name (logs)")
    parser.add_argument("value", nargs="?", default=None, help="new value, for `config-set`")
    parser.add_argument("--days", type=int, default=30, help="retention window for `db-clean`")
    parser.add_argument("--yes", action="store_true", help="required to confirm `db-reset`")
    parser.add_argument("--json", action="store_true", help="raw JSON output for `trace-task`")
    parser.add_argument("--follow", "-f", action="store_true", help="follow log output, for `logs`")
    parser.add_argument("--lines", type=int, default=60, help="tail line count, for `logs`")
    parser.add_argument("--no-telegram", action="store_true", help="for `start`: skip the Telegram polling service")
    parser.add_argument("--no-whatsapp", action="store_true", help="for `start`: skip the WhatsApp polling service")
    parser.add_argument("--no-worker", action="store_true", help="for `start`: skip the worker + coding session watcher")
    parser.add_argument("--no-scheduler", action="store_true", help="for `start`: skip the scheduler")
    parser.add_argument("--no-localdeploy", action="store_true", help="for `start`: skip launching LocalDeploy")
    parser.add_argument("--open", action="store_true", help="for `start`: open the admin console in a browser once ready")
    parser.add_argument(
        "--foreground", action="store_true",
        help="for `start`: stay in the foreground until a service exits or a signal arrives "
             "(what a container or systemd Type=simple needs; a detached start would look like an immediate exit)",
    )
    parser.add_argument("--out", default=None, help="output directory for `backup` (default: .agent_control/backups)")
    args = parser.parse_args()

    _configure_logging_for_command(args.command)

    if args.command == "doctor":
        raise SystemExit(run_doctor())
    elif args.command == "setup":
        raise SystemExit(run_setup(telegram_token=args.telegram_token))
    elif args.command == "onboard":
        raise SystemExit(run_onboard())
    elif args.command == "start":
        from agent_control.supervisor import run_foreground, start_all
        launch = run_foreground if args.foreground else start_all
        raise SystemExit(launch(
            no_telegram=args.no_telegram, no_whatsapp=args.no_whatsapp, no_worker=args.no_worker,
            no_scheduler=args.no_scheduler,
            no_localdeploy=args.no_localdeploy,
            open_browser=args.open,
        ))
    elif args.command == "stop":
        from agent_control.supervisor import stop_all
        raise SystemExit(stop_all())
    elif args.command == "status":
        from agent_control.supervisor import status_all
        raise SystemExit(status_all())
    elif args.command == "logs":
        from agent_control.supervisor import tail_log
        if not args.path:
            raise SystemExit("usage: ybm logs <service> [--follow] [--lines N]")
        raise SystemExit(tail_log(args.path, follow=args.follow, lines=args.lines))
    elif args.command == "ui-build":
        raise SystemExit(ui_build())
    elif args.command == "ui-dev":
        raise SystemExit(ui_dev())
    elif args.command == "init-db":
        init_db()
    elif args.command == "config-summary":
        config_summary()
    elif args.command == "config-set":
        if not args.path or args.value is None:
            raise SystemExit("usage: ybm config-set <dotted.path> <value>")
        ok, message = set_config_path(args.path, args.value)
        print(message)
        raise SystemExit(0 if ok else 1)
    elif args.command == "channel-enabled":
        # Exit-code contract (0 enabled, 1 disabled/unknown) rather than
        # stdout - lets scripts/ybm.ps1 gate a service start on it with a
        # plain $LASTEXITCODE check, no JSON parsing needed. Used to decide
        # whether to even attempt the whatsapp service, so a broken config
        # here must not itself crash `ybm start` - fail closed (treat as
        # disabled) and let `ybm doctor` be where a bad config surfaces.
        if not args.path:
            raise SystemExit("usage: ybm channel-enabled <telegram|whatsapp>")
        try:
            channel_config = getattr(load_settings().channels, args.path)
        except Exception:
            raise SystemExit(1) from None
        raise SystemExit(0 if getattr(channel_config, "enabled", False) else 1)
    elif args.command == "db-inspect":
        raise SystemExit(db_inspect())
    elif args.command == "db-clean":
        raise SystemExit(db_clean(args.days))
    elif args.command == "db-reset":
        raise SystemExit(db_reset(yes=args.yes))
    elif args.command == "trace-task":
        if not args.path:
            raise SystemExit("usage: ybm trace-task <task_id> [--json]")
        raise SystemExit(trace_task(args.path, as_json=args.json))
    elif args.command == "poll-telegram":
        asyncio.run(poll_telegram())
    elif args.command == "poll-whatsapp":
        asyncio.run(poll_whatsapp())
    elif args.command == "run-worker":
        asyncio.run(run_worker())
    elif args.command == "run-scheduler":
        asyncio.run(run_scheduler())
    elif args.command == "run-coding-agent-session":
        if not args.session_root or not args.session_id:
            raise SystemExit("--session-root and --session-id are required")
        asyncio.run(run_coding_agent_session(args.session_root, args.session_id))
    elif args.command == "run-coding-session-watcher":
        asyncio.run(run_coding_session_watcher(args.poll_interval_seconds))
    elif args.command == "backup":
        raise SystemExit(run_backup(args.out))
    elif args.command == "check-updates":
        result = check_for_updates()
        if result.status == "update_available":
            print(f"Update available: {result.latest_version} (currently {result.current_version}) - {result.release_url}")
        elif result.status == "up_to_date":
            print(f"Up to date ({result.current_version}).")
        elif result.status == "no_releases":
            print(f"Running {result.current_version}. {result.detail}")
        else:
            print(f"Could not check for updates: {result.detail}")
        raise SystemExit(0 if result.status != "check_failed" else 1)


if __name__ == "__main__":
    main()
