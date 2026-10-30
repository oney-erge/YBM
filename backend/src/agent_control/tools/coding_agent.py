from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Any, Protocol
from uuid import uuid4

from agent_control.config import CodingAgentAdapterConfig
from agent_control.schemas import Capability, ErrorClass, RiskLevel, ToolCallRequest, ToolCallResult, ToolResultStatus
from agent_control.tools.contracts import CodingAgentInput, CodingAgentOutput
from agent_control.tools.spec import (
    Adapters,
    Definitions,
    RegistryDeps,
    ToolDefinition,
    capability_enabled,
    failed_result,
    same_output_schema,
)


logger = logging.getLogger(__name__)

PROVIDERS = ("codex", "github_copilot", "claude_code")
#: The names people use for each provider. The intake prompt and the decision model's route text must
#: name every one, or a request like "ask Claude Code to ..." is routed somewhere else
#: (tests/test_decision_classifier.py checks this).
PROVIDER_NAMES = {"codex": "Codex", "github_copilot": "Copilot", "claude_code": "Claude Code"}

RUN_OPERATIONS = {"start", "plan", "run_step", "run_goal", "resume"}

# A runner writes the terminal session record immediately after its child
# exits. The independent watcher can observe the PID disappear in that tiny
# interval; finalizing at once races the authoritative runner and turns a
# clean completion into "process ended without a final report". One watcher
# grace window removes that race while still recovering a genuinely orphaned
# session promptly.
PROCESS_EXIT_GRACE_SECONDS = 15


class ProcessHandle(Protocol):
    pid: int

    async def wait(self) -> int:
        ...

    def terminate(self) -> None:
        ...


class ProcessSpawner(Protocol):
    async def spawn(
        self,
        command: list[str],
        *,
        cwd: str,
        log_path: str,
        env: dict[str, str] | None = None,
    ) -> ProcessHandle:
        ...


class AsyncProcessSpawner:
    """Spawn the coding CLI detached from the tool call, streaming output to a log file."""

    async def spawn(
        self,
        command: list[str],
        *,
        cwd: str,
        log_path: str,
        env: dict[str, str] | None = None,
    ) -> ProcessHandle:
        log_file = open(log_path, "ab")
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=cwd,
                env=env,
                stdout=log_file,
                stderr=subprocess.STDOUT,
            )
        finally:
            # The child holds its own copy of the file descriptor.
            log_file.close()
        return _AsyncioProcessHandle(process)


class _AsyncioProcessHandle:
    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self._process = process
        self.pid = process.pid

    async def wait(self) -> int:
        return await self._process.wait()

    def terminate(self) -> None:
        if self._process.returncode is None:
            self._process.terminate()


# Async callback invoked with the final session dict when a background run ends.
SessionCompletionCallback = Callable[[dict], Awaitable[None]]


class CodingAgentAdapter:
    """Session-based access to external coding CLIs (Codex, Claude Code, Copilot).

    Runs are background sessions persisted as JSON files under
    ``config.session_root`` so status/stop/log queries work from any process.
    ``start`` waits up to ``config.start_wait_seconds`` for quick runs to finish
    inline; longer runs keep going in the background and ``on_complete`` is
    invoked with the final session when they end.
    """

    def __init__(
        self,
        config: CodingAgentAdapterConfig,
        spawner: ProcessSpawner | None = None,
        on_complete: SessionCompletionCallback | None = None,
    ) -> None:
        self.config = config
        self.spawner = spawner or AsyncProcessSpawner()
        self._custom_spawner = spawner is not None
        self.on_complete = on_complete
        self._processes: dict[str, ProcessHandle] = {}
        self._watchers: set[asyncio.Task] = set()

    async def execute(self, request: ToolCallRequest) -> ToolCallResult:
        if not self.config.enabled:
            return failed_result(request, "coding agent adapter is disabled")
        operation = str(request.input.get("operation") or "run_goal")
        provider = str(request.input.get("provider") or "")
        try:
            if operation in RUN_OPERATIONS:
                return await self._start(request, operation, provider)
            if operation == "status":
                output = self._status(request, provider)
            elif operation == "get_latest_output":
                output = self._latest_output(request, provider)
            elif operation == "stop":
                output = self._stop(request, provider)
            elif operation == "limits":
                output = self._probe(request, provider)
            else:
                return failed_result(request, f"unsupported coding agent operation: {operation}")
        except Exception as exc:
            return failed_result(request, f"coding agent operation failed: {exc}")
        output.setdefault("operation", operation)
        output.setdefault("provider", provider or str(output.get("provider") or ""))
        output["terminal_output"] = [_terminal_output(operation, output)]
        return ToolCallResult(request_id=request.id, status=ToolResultStatus.SUCCEEDED, output=output)

    async def _start(self, request: ToolCallRequest, operation: str, provider: str) -> ToolCallResult:
        if provider not in PROVIDERS:
            return failed_result(request, f"unsupported coding provider: {provider or '<missing>'}")
        prompt = str(request.input.get("prompt") or request.input.get("objective") or "").strip()
        if not prompt:
            return failed_result(request, "prompt or objective is required")
        executable = self._executable(provider)
        if executable is None:
            return failed_result(request, f"{provider} CLI was not found on PATH or config")

        workspace = self._workspace(request)
        workspace.mkdir(parents=True, exist_ok=True)
        session_root = self._session_root()
        session_root.mkdir(parents=True, exist_ok=True)

        session_id = str(request.input.get("session_id") or f"{provider}_{uuid4().hex[:12]}")
        log_path = session_root / f"{session_id}.log"
        event_path = session_root / f"{session_id}.events.jsonl"
        command = self._command(provider, executable, prompt, workspace, operation=operation)
        environment_overrides = _coding_agent_environment_overrides(provider)

        session = {
            "session_id": session_id,
            "request_id": request.id,
            "provider": provider,
            "operation": operation,
            "prompt": prompt[:2000],
            "task_id": request.task_id,
            "workspace_dir": str(workspace),
            "log_path": str(log_path),
            "event_path": str(event_path),
            "status": "starting" if self._use_runner() else "running",
            "pid": None,
            "child_pid": None,
            "runner_pid": None,
            "returncode": None,
            "command": command,
            "environment_overrides": environment_overrides,
            "runner_enabled": self._use_runner(),
            "started_at": _now(),
            "ended_at": None,
            "changed_files": [],
            "files_before": _workspace_snapshot(workspace),
            "summary": None,
            "limit_state": {"limited": False, "source": "cli_output"},
            "timeout_seconds": self.config.timeout_seconds,
            "output_limit_chars": self.config.output_limit_chars,
            "rate_limit_patterns": list(self.config.rate_limit_patterns),
            "usage_limit_patterns": list(self.config.usage_limit_patterns),
        }

        if self._use_runner():
            _write_session(session_root, session)
            append_session_event(str(session_root), session_id, "session_queued", {"provider": provider})
            runner_command = [
                sys.executable,
                "-m",
                "agent_control.cli",
                "run-coding-agent-session",
                "--session-root",
                str(session_root),
                "--session-id",
                session_id,
            ]
            runner_log_path = session_root / f"{session_id}.runner.log"
            runner = await self.spawner.spawn(
                runner_command,
                cwd=str(workspace),
                log_path=str(runner_log_path),
                env=None,
            )
            session["runner_pid"] = runner.pid
            _write_session(session_root, session)
            try:
                await asyncio.wait_for(runner.wait(), timeout=self.config.start_wait_seconds)
            except (asyncio.TimeoutError, TimeoutError):
                stored = load_session(str(session_root), session_id) or session
                output = _session_output(stored, log_tail=read_log_tail(str(log_path)))
                output["summary"] = (
                    f"{provider} is working in the background (session {session_id}). "
                    "Ask for its status any time; the coding-session watcher will report completion."
                )
                output["terminal_output"] = [_terminal_output(operation, output)]
                return ToolCallResult(request_id=request.id, status=ToolResultStatus.SUCCEEDED, output=output)
            stored = load_session(str(session_root), session_id) or session
            if stored.get("status") in {"starting", "running"}:
                stored = _finalize_session(
                    session_root,
                    session_id,
                    -1,
                    summary="coding session runner exited before writing a final result",
                )
            return self._result_from_session(request, operation, stored)

        process = await self.spawner.spawn(
            command,
            cwd=str(workspace),
            log_path=str(log_path),
            env=_merged_process_environment(environment_overrides),
        )
        session["pid"] = process.pid
        session["child_pid"] = process.pid
        self._processes[session_id] = process
        _write_session(session_root, session)
        append_session_event(str(session_root), session_id, "session_started", {"pid": process.pid})

        try:
            returncode = await asyncio.wait_for(process.wait(), timeout=self.config.start_wait_seconds)
        except (asyncio.TimeoutError, TimeoutError):
            watcher = asyncio.create_task(self._watch(process, session_id))
            self._watchers.add(watcher)
            watcher.add_done_callback(self._watchers.discard)
            output = _session_output(session, log_tail=read_log_tail(str(log_path)))
            output["summary"] = (
                f"{provider} is working in the background (session {session_id}). "
                "Ask for its status any time; a completion report is sent when it finishes."
            )
            output["terminal_output"] = [_terminal_output(operation, output)]
            return ToolCallResult(request_id=request.id, status=ToolResultStatus.SUCCEEDED, output=output)

        session = self._finalize(session_id, returncode)
        return self._result_from_session(request, operation, session)

    def _use_runner(self) -> bool:
        return bool(self.config.use_runner and not self._custom_spawner)

    async def _watch(self, process: ProcessHandle, session_id: str) -> None:
        timed_out = False
        remaining = max(self.config.timeout_seconds - self.config.start_wait_seconds, 1)
        try:
            returncode = await asyncio.wait_for(process.wait(), timeout=remaining)
        except (asyncio.TimeoutError, TimeoutError):
            timed_out = True
            process.terminate()
            try:
                returncode = await asyncio.wait_for(process.wait(), timeout=10)
            except (asyncio.TimeoutError, TimeoutError):
                returncode = -1
        session = self._finalize(session_id, returncode, timed_out=timed_out)
        if self.on_complete is not None:
            await self.on_complete(session)

    def _finalize(self, session_id: str, returncode: int, *, timed_out: bool = False) -> dict:
        self._processes.pop(session_id, None)
        return _finalize_session(self._session_root(), session_id, returncode, timed_out=timed_out)

    def _result_from_session(self, request: ToolCallRequest, operation: str, session: dict) -> ToolCallResult:
        output = _session_output(session, log_tail=read_log_tail(str(session.get("log_path") or "")))
        output["terminal_output"] = [_terminal_output(operation, output)]
        limit_state = session.get("limit_state") or {}
        if limit_state.get("limited"):
            return ToolCallResult(
                request_id=request.id,
                status=ToolResultStatus.RATE_LIMITED,
                output=output,
                error_class=ErrorClass.USAGE_LIMITED,
                error_message="coding agent usage limit reached",
            )
        if session.get("returncode") not in (None, 0):
            return ToolCallResult(
                request_id=request.id,
                status=ToolResultStatus.FAILED,
                output=output,
                error_class=ErrorClass.ADAPTER_FAILED,
                error_message=str(session.get("summary") or "coding agent run failed"),
            )
        return ToolCallResult(request_id=request.id, status=ToolResultStatus.SUCCEEDED, output=output)

    def _status(self, request: ToolCallRequest, provider: str) -> dict:
        session = self._resolve_session(request, provider)
        if session is None:
            return self._probe(request, provider, summary_prefix="No coding sessions found yet. ")
        log_tail = read_log_tail(str(session.get("log_path") or ""))
        output = _session_output(session, log_tail=log_tail)
        if session.get("status") == "running":
            workspace = Path(str(session.get("workspace_dir") or "."))
            output["changed_files"] = _changed_files(session.get("files_before") or {}, _workspace_snapshot(workspace))
            output["summary"] = format_session_status(session, log_tail=log_tail, changed_files=output["changed_files"])
        else:
            output["summary"] = str(session.get("summary") or format_session_status(session, log_tail=log_tail))
        return output

    def _latest_output(self, request: ToolCallRequest, provider: str) -> dict:
        session = self._resolve_session(request, provider)
        if session is None:
            raise ValueError("no coding sessions found")
        log_tail = read_log_tail(str(session.get("log_path") or ""), max_chars=self.config.output_limit_chars)
        output = _session_output(session, log_tail=log_tail)
        output["summary"] = log_tail or "The session log is empty so far."
        return output

    def _stop(self, request: ToolCallRequest, provider: str) -> dict:
        session = self._resolve_session(request, provider)
        if session is None:
            raise ValueError("no coding sessions found")
        session_id = str(session["session_id"])
        if session.get("status") != "running":
            output = _session_output(session)
            output["summary"] = f"Session {session_id} is not running (status: {session.get('status')})."
            return output
        handle = self._processes.get(session_id)
        if handle is not None:
            handle.terminate()
            summary = f"Stop requested for session {session_id}; final report follows when it exits."
        else:
            stop_session_process(session)
            session["status"] = "stopped"
            session["ended_at"] = _now()
            session["summary"] = f"Session {session_id} was stopped."
            _write_session(self._session_root(), session)
            summary = str(session["summary"])
        output = _session_output(session)
        output["summary"] = summary
        return output

    def _probe(self, request: ToolCallRequest, provider: str, *, summary_prefix: str = "") -> dict:
        providers = [provider] if provider in PROVIDERS else list(PROVIDERS)
        lines = []
        for name in providers:
            available = self._executable(name) is not None
            lines.append(f"{name}: {'available' if available else 'not found on PATH or config'}")
        return {
            "provider": provider or "all",
            "workspace_dir": str(self._workspace(request)),
            "limit_state": {"limited": False, "source": "local_cli_probe"},
            "summary": summary_prefix + " | ".join(lines),
        }

    def _resolve_session(self, request: ToolCallRequest, provider: str) -> dict | None:
        session_root = str(self._session_root())
        session_id = str(request.input.get("session_id") or "")
        if session_id:
            return load_session(session_root, session_id)
        return latest_session(session_root, provider=provider or None)

    def _command(
        self,
        provider: str,
        executable: str,
        prompt: str,
        workspace: Path,
        *,
        operation: str,
    ) -> list[str]:
        if operation != "plan":
            prompt = _execution_prompt(prompt)
        if provider == "codex":
            command = [
                executable,
                "exec",
                "--json",
                "--cd",
                str(workspace),
            ]
            if self.config.codex_sandbox:
                command.extend(["--sandbox", self.config.codex_sandbox])
            if self.config.codex_skip_git_repo_check:
                command.append("--skip-git-repo-check")
            command.append(prompt)
            return command
        if provider == "claude_code":
            command = [
                executable,
                "-p",
                prompt,
                "--output-format",
                "text",
            ]
            if self.config.claude_permission_mode:
                command.extend(["--permission-mode", self.config.claude_permission_mode])
            return command
        args = [
            "-p",
            prompt,
            "-C",
            str(workspace),
            "--output-format",
            "json",
        ]
        if self.config.copilot_allow_all:
            args.append("--allow-all")
        if self.config.copilot_no_ask_user:
            args.append("--no-ask-user")
        if Path(executable).name.lower() in {"gh", "gh.exe"}:
            return [executable, "copilot", "--", *args]
        return [executable, *args]

    def _executable(self, provider: str) -> str | None:
        configured = {
            "codex": self.config.codex_path,
            "github_copilot": self.config.copilot_path,
            "claude_code": self.config.claude_path,
        }.get(provider)
        if configured and Path(configured).exists():
            return _prefer_native_claude_executable(configured) if provider == "claude_code" else configured
        names = {
            "codex": ["codex"],
            "github_copilot": ["copilot", "gh"],
            "claude_code": ["claude"],
        }.get(provider, [])
        for name in names:
            found = shutil.which(name)
            if found:
                return _prefer_native_claude_executable(found) if provider == "claude_code" else found
        return None

    def _workspace(self, request: ToolCallRequest) -> Path:
        value = request.input.get("workspace_dir")
        if value:
            return Path(str(value)).expanduser().resolve()
        task_dir = request.task_id if request.task_id.startswith("task_") else f"task_{request.task_id}"
        return Path(self.config.workspace_root).expanduser().resolve() / task_dir

    def _session_root(self) -> Path:
        return Path(self.config.session_root).expanduser().resolve()


# --- session store helpers (pure file reads/writes, usable from any process) ---


def load_session(session_root: str, session_id: str) -> dict | None:
    path = Path(session_root) / f"{session_id}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def load_sessions(session_root: str, limit: int | None = 20) -> list[dict]:
    root = Path(session_root)
    if not root.exists():
        return []
    sessions = []
    for path in root.glob("*.json"):
        try:
            sessions.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            # A corrupt session file means that session's history silently
            # disappears from every status listing - worth knowing about.
            logger.warning("failed to read coding session file %s; skipping", path, exc_info=True)
            continue
    sessions.sort(key=lambda item: str(item.get("started_at") or ""), reverse=True)
    return sessions[:limit] if limit is not None else sessions


def latest_session(session_root: str, provider: str | None = None) -> dict | None:
    for session in load_sessions(session_root):
        if provider is None or session.get("provider") == provider:
            return session
    return None


def read_log_tail(log_path: str, max_chars: int = 2000) -> str:
    path = Path(log_path)
    if not log_path or not path.exists():
        return ""
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    return data[-max_chars:].decode(errors="replace").strip()


def append_session_event(session_root: str, session_id: str, event: str, payload: dict[str, Any] | None = None) -> None:
    root = Path(session_root)
    root.mkdir(parents=True, exist_ok=True)
    session = load_session(session_root, session_id) or {}
    event_path = Path(str(session.get("event_path") or root / f"{session_id}.events.jsonl"))
    event_path.parent.mkdir(parents=True, exist_ok=True)
    record = {"event": event, "created_at": _now(), **(payload or {})}
    with event_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def mark_session_notified(session_root: str, session_id: str, *, error: str | None = None) -> dict | None:
    root = Path(session_root)
    session = load_session(session_root, session_id)
    if session is None:
        return None
    session["notified_at"] = _now()
    if error:
        session["notification_error"] = error[:1000]
    _write_session(root, session)
    return session


def session_progress_due(
    session: dict,
    interval_seconds: int,
    *,
    now: datetime | None = None,
) -> bool:
    """Return whether a live session is due for a source-chat heartbeat.

    The timestamp lives with the external session rather than the task. That
    keeps the cadence durable across watcher restarts and avoids racing a
    terminal callback by rewriting stale task metadata.
    """
    if session.get("status") not in {"starting", "running"}:
        return False
    baseline = _parse_time(session.get("progress_notified_at")) or _parse_time(session.get("started_at"))
    if baseline is None:
        return False
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return (current - baseline).total_seconds() >= interval_seconds


def mark_session_progress_notified(session_root: str, session_id: str) -> dict | None:
    root = Path(session_root)
    session = load_session(session_root, session_id)
    if session is None:
        return None
    session["progress_notified_at"] = _now()
    _write_session(root, session)
    return session


def session_progress_message(session: dict) -> str:
    provider = str(session.get("provider") or "Coding agent").replace("_", " ")
    session_id = str(session.get("session_id") or "unknown")
    workspace = str(session.get("workspace_dir") or "").strip()
    lines = [f"{provider} is still working (session {session_id})."]
    if workspace:
        lines.append(f"Workspace preserved at: {workspace}")
    lines.append("I will keep monitoring it and report completion or a safe pause.")
    return "\n".join(lines)


def terminal_session_result(session: dict) -> ToolCallResult:
    output = _session_output(session, log_tail=read_log_tail(str(session.get("log_path") or "")))
    output["terminal_output"] = [_terminal_output(str(session.get("operation") or "status"), output)]
    request_id = str(session.get("request_id") or f"external_{session.get('session_id') or 'unknown'}")
    limit_state = session.get("limit_state") or {}
    if limit_state.get("limited"):
        return ToolCallResult(
            request_id=request_id,
            status=ToolResultStatus.RATE_LIMITED,
            output=output,
            error_class=ErrorClass.USAGE_LIMITED,
            error_message="coding agent usage limit reached",
        )
    if session.get("status") == "completed" and session.get("returncode") in (0, None):
        return ToolCallResult(request_id=request_id, status=ToolResultStatus.SUCCEEDED, output=output)
    return ToolCallResult(
        request_id=request_id,
        status=ToolResultStatus.FAILED,
        output=output,
        error_class=ErrorClass.ADAPTER_FAILED,
        error_message=str(session.get("summary") or "coding agent session failed"),
    )


def _finalize_session(
    session_root: Path,
    session_id: str,
    returncode: int,
    *,
    timed_out: bool = False,
    summary: str | None = None,
) -> dict:
    session = load_session(str(session_root), session_id) or {"session_id": session_id}
    output_limit = int(session.get("output_limit_chars") or 20000)
    log_tail = read_log_tail(str(session.get("log_path") or ""), max_chars=output_limit)
    limit_state = _limit_state(
        log_tail,
        list(session.get("rate_limit_patterns") or ["rate limit", "too many requests"]),
        list(session.get("usage_limit_patterns") or ["usage limit", "quota exceeded", "no quota"]),
    )
    workspace = Path(str(session.get("workspace_dir") or "."))
    changed = _changed_files(session.get("files_before") or {}, _workspace_snapshot(workspace))

    if summary:
        status = "failed" if returncode not in (0, None) else "completed"
        final_summary = summary
    elif timed_out:
        status = "failed"
        final_summary = f"{session.get('provider')} run exceeded {session.get('timeout_seconds')}s and was terminated."
    elif limit_state.get("limited"):
        status = "failed"
        final_summary = f"{session.get('provider')} reported a {limit_state.get('kind')} limit."
    elif returncode == 0:
        status = "completed"
        final_summary = _completion_summary(session, changed, log_tail)
    else:
        status = "failed"
        final_summary = f"{session.get('provider')} exited with code {returncode}. Last output: {log_tail[-600:]}"

    session.update(
        status=status,
        returncode=returncode,
        ended_at=_now(),
        changed_files=changed,
        summary=final_summary,
        limit_state=limit_state,
    )
    _write_session(session_root, session)
    append_session_event(str(session_root), session_id, status, {"returncode": returncode, "timed_out": timed_out})
    return session


def stop_session_process(session: dict) -> bool:
    """Best-effort cross-process kill by pid; used when the owning worker is gone."""
    pids = [session.get("pid"), session.get("child_pid"), session.get("runner_pid")]
    pids = [pid for pid in pids if pid]
    if not pids:
        return False
    stopped = False
    for pid in dict.fromkeys(str(pid) for pid in pids):
        stopped = _stop_pid(pid) or stopped
    return stopped


def _stop_pid(pid: str) -> bool:
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/PID", pid, "/T", "/F"],
                capture_output=True,
                timeout=15,
                check=False,
            )
        else:
            os.kill(int(pid), 15)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def session_process_alive(session: dict) -> bool:
    for key in ("runner_pid", "pid", "child_pid"):
        pid = session.get(key)
        if pid and _pid_alive(int(pid)):
            return True
    return False


def _pid_alive(pid: int) -> bool:
    try:
        if sys.platform == "win32":
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            return str(pid) in result.stdout
        os.kill(pid, 0)
        return True
    except (OSError, subprocess.SubprocessError, ValueError):
        return False


def format_session_status(session: dict, *, log_tail: str = "", changed_files: list[str] | None = None) -> str:
    provider = session.get("provider") or "coding agent"
    status = session.get("status") or "unknown"
    lines = [f"{provider} session {session.get('session_id')}: {status}"]
    started = _parse_time(session.get("started_at"))
    if started is not None:
        end = _parse_time(session.get("ended_at")) or datetime.now(timezone.utc)
        minutes = max(int((end - started).total_seconds() // 60), 0)
        lines.append(f"Runtime: {minutes} min")
    changed = changed_files if changed_files is not None else session.get("changed_files") or []
    if changed:
        lines.append(f"Changed files ({len(changed)}): " + ", ".join(changed[:10]))
    if session.get("summary") and status != "running":
        lines.append(str(session["summary"]))
    if log_tail:
        lines.append("")
        lines.append("Latest output:")
        lines.append(log_tail[-1200:])
    return "\n".join(lines)


def session_completion_message(session: dict) -> str:
    provider = session.get("provider") or "coding agent"
    status = session.get("status")
    headline = {
        "completed": f"{provider} finished.",
        "failed": f"{provider} did not finish cleanly.",
        "stopped": f"{provider} was stopped.",
    }.get(str(status), f"{provider} session ended ({status}).")
    lines = [headline]
    summary = str(session.get("summary") or "").strip()
    if summary:
        lines.append(summary)
    changed = session.get("changed_files") or []
    if changed:
        lines.append(f"Changed files ({len(changed)}):")
        lines.extend(f"- {item}" for item in changed[:20])
    lines.append(f"Workspace: {session.get('workspace_dir')}")
    lines.append(f"Session: {session.get('session_id')}")
    return "\n".join(lines)


def _sandbox_compatible_codex_path(
    path_value: str,
    resolved_pwsh: str | None,
    *,
    is_windows: bool,
) -> str:
    """Hide Microsoft Store shell aliases that the Windows sandbox cannot execute.

    Codex prefers ``pwsh`` when it is discoverable. Microsoft Store installs
    resolve that name inside ``WindowsApps``; a restricted/elevated sandbox
    token cannot execute that packaged path even though the interactive user
    can. Removing only WindowsApps entries makes Codex fall back to the normal
    System32 Windows PowerShell while preserving workspace-write isolation.
    """
    if not is_windows or not resolved_pwsh or "\\windowsapps\\" not in resolved_pwsh.casefold():
        return path_value
    return ";".join(
        entry
        for entry in path_value.split(";")
        if "\\windowsapps" not in entry.strip().strip('"').casefold()
    )


def _prefer_native_claude_executable(executable: str, *, is_windows: bool | None = None) -> str:
    """Resolve the official npm shim to its native Windows binary when present.

    A ``.CMD`` shim can return while ``claude.exe`` remains detached. The
    session runner then observes the wrapper, not the real long-lived process,
    and cannot report completion or stop it reliably. The official package
    ships the native executable at this stable path beside its npm shims.

    ``is_windows`` is injected the same way ``_sandbox_compatible_codex_path``
    takes it, so this branch is testable off Windows. Patching ``os.name``
    instead reaches the real ``os`` module and changes it process-wide, which
    makes ``pathlib`` hand out ``WindowsPath`` on POSIX and raise.
    """
    if not (os.name == "nt" if is_windows is None else is_windows):
        return executable
    path = Path(executable).resolve()
    if path.suffix.casefold() not in {".cmd", ".ps1", ""}:
        return executable
    native = path.parent / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
    return str(native) if native.is_file() else executable


def _coding_agent_environment_overrides(provider: str) -> dict[str, str]:
    if provider != "codex" or os.name != "nt":
        return {}
    current_path = os.environ.get("PATH", "")
    resolved_pwsh = shutil.which("pwsh", path=current_path)
    compatible_path = _sandbox_compatible_codex_path(
        current_path,
        resolved_pwsh,
        is_windows=True,
    )
    if compatible_path == current_path or shutil.which("powershell", path=compatible_path) is None:
        return {}
    return {"PATH": compatible_path}


def _merged_process_environment(overrides: dict[str, str] | None) -> dict[str, str] | None:
    if not overrides:
        return None
    return {**os.environ, **overrides}


async def run_coding_agent_session(session_root: str, session_id: str) -> dict:
    """Run one queued coding session to completion.

    The adapter writes the session file and command first, then starts this
    runner as a separate process. The runner updates the same session file with
    child PID, return code, changed files, and event records.
    """
    root = Path(session_root).expanduser().resolve()
    session = load_session(str(root), session_id)
    if session is None:
        raise KeyError(f"coding session not found: {session_id}")
    command = list(session.get("command") or [])
    if not command:
        raise ValueError(f"coding session has no command: {session_id}")

    workspace = Path(str(session.get("workspace_dir") or ".")).expanduser().resolve()
    log_path = Path(str(session.get("log_path") or root / f"{session_id}.log")).expanduser().resolve()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    session.update(status="running", runner_pid=os.getpid(), started_at=session.get("started_at") or _now())
    _write_session(root, session)
    append_session_event(str(root), session_id, "session_started", {"runner_pid": os.getpid(), "command": command[:3]})

    process = None
    timed_out = False
    environment_overrides = session.get("environment_overrides")
    if not isinstance(environment_overrides, dict):
        environment_overrides = {}
    try:
        log_file = open(log_path, "ab")
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(workspace),
                env=_merged_process_environment(environment_overrides),
                stdout=log_file,
                stderr=subprocess.STDOUT,
            )
        finally:
            log_file.close()
        session = load_session(str(root), session_id) or session
        session.update(pid=process.pid, child_pid=process.pid, status="running")
        _write_session(root, session)
        append_session_event(str(root), session_id, "child_started", {"pid": process.pid})

        timeout = int(session.get("timeout_seconds") or 3600)
        try:
            returncode = await asyncio.wait_for(process.wait(), timeout=timeout)
        except (asyncio.TimeoutError, TimeoutError):
            timed_out = True
            process.terminate()
            try:
                returncode = await asyncio.wait_for(process.wait(), timeout=10)
            except (asyncio.TimeoutError, TimeoutError):
                returncode = -1
        return _finalize_session(root, session_id, int(returncode), timed_out=timed_out)
    except Exception as exc:
        session = load_session(str(root), session_id) or session
        session.update(
            status="failed",
            returncode=-1,
            ended_at=_now(),
            summary=f"coding session runner failed: {exc}",
        )
        _write_session(root, session)
        append_session_event(str(root), session_id, "failed", {"error": str(exc)})
        return session


async def scan_coding_sessions_once(session_root: str) -> list[dict]:
    """Finalize stale running sessions and return terminal sessions needing notification."""
    root = Path(session_root).expanduser().resolve()
    completed: list[dict] = []
    for session in load_sessions(str(root), limit=None):
        status = str(session.get("status") or "")
        session_id = str(session.get("session_id") or "")
        if status in {"starting", "running"} and session_id and not session_process_alive(session):
            missing_since = _parse_time(session.get("process_missing_since"))
            now = datetime.now(timezone.utc)
            if missing_since is None:
                session["process_missing_since"] = now.isoformat()
                _write_session(root, session)
                continue
            if (now - missing_since).total_seconds() < PROCESS_EXIT_GRACE_SECONDS:
                continue
            session = _finalize_session(
                root,
                session_id,
                int(session.get("returncode") if session.get("returncode") is not None else -1),
                summary="coding session process ended without a runner final report",
            )
            status = str(session.get("status") or "")
        elif status in {"starting", "running"} and session.get("process_missing_since"):
            session.pop("process_missing_since", None)
            _write_session(root, session)
        if status in {"completed", "failed", "stopped"} and not session.get("notified_at"):
            completed.append(session)
    return completed


def _write_session(session_root: Path, session: dict) -> None:
    session_root.mkdir(parents=True, exist_ok=True)
    path = session_root / f"{session['session_id']}.json"
    path.write_text(json.dumps(session, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def _session_output(session: dict, *, log_tail: str = "") -> dict:
    return {
        "operation": str(session.get("operation") or "status"),
        "provider": str(session.get("provider") or ""),
        "workspace_dir": session.get("workspace_dir"),
        "session_id": session.get("session_id"),
        "status": session.get("status"),
        "pid": session.get("pid"),
        "returncode": session.get("returncode"),
        "started_at": session.get("started_at"),
        "ended_at": session.get("ended_at"),
        "limit_state": session.get("limit_state") or {},
        "changed_files": session.get("changed_files") or [],
        "log_path": session.get("log_path"),
        "log_tail": log_tail,
        "summary": session.get("summary"),
    }


def _completion_summary(session: dict, changed: list[str], log_tail: str) -> str:
    provider = session.get("provider")
    parts = [f"{provider} completed the run."]
    if changed:
        parts.append(f"{len(changed)} file(s) changed.")
    tail = log_tail[-600:].strip()
    if tail:
        parts.append(tail)
    return " ".join(parts)


def _limit_state(text: str, rate_patterns: list[str], usage_patterns: list[str]) -> dict:
    if any(_contains_limit_pattern(text, pattern) for pattern in usage_patterns):
        return {"limited": True, "kind": "usage", "source": "cli_output"}
    if any(_contains_limit_pattern(text, pattern) for pattern in rate_patterns):
        return {"limited": True, "kind": "rate", "source": "cli_output"}
    return {"limited": False, "source": "cli_output"}


def _contains_limit_pattern(text: str, pattern: str) -> bool:
    """Match a configured provider phrase without accepting word fragments.

    Provider output is also ordinary prose. A substring search classified
    ``no quota-introspection tool`` as the configured ``no quota`` failure,
    turning a successful, exit-0 Claude build into a usage-limit failure.
    Treat hyphen/underscore as part of a token here so diagnostic prose cannot
    accidentally manufacture a provider state from the start of a longer
    compound word.
    """
    phrase = pattern.strip()
    if not phrase:
        return False
    expression = rf"(?<![A-Za-z0-9_-]){re.escape(phrase)}(?![A-Za-z0-9_-])"
    return re.search(expression, text, flags=re.IGNORECASE) is not None


def _execution_prompt(prompt: str) -> str:
    """Turn an external coding run into an on-disk task, not a chat answer.

    The Operator composes this prompt and can accidentally phrase an artifact
    request as "output the files". The user-facing contract is execution: when
    files are requested, the external agent must use its editing tools in the
    assigned workspace. Planning operations are intentionally excluded by the
    caller so a request for a plan remains read-only.
    """
    return (
        f"{prompt.rstrip()}\n\n"
        "Execution contract from YBM:\n"
        "- Work directly in the current working directory assigned to this session.\n"
        "- If the task asks to create, edit, fix, scaffold, or build files, make those changes on disk.\n"
        "- Prioritize the requested deliverable and keep verification bounded. Do not launch GUI/headless browsers, "
        "install dependencies, or start persistent servers unless the user explicitly requested that.\n"
        "- Run focused static or syntax checks when available. If optional verification is blocked, report the gap "
        "and finish instead of repeatedly trying alternate browsers, shells, or cleanup strategies.\n"
        "- Do not treat pasted code or a prose description as completion when an artifact was requested.\n"
        "- Before finishing, verify the requested paths exist and briefly report what you actually changed."
    )


def _workspace_snapshot(workspace: Path) -> dict[str, list[int]]:
    snapshot: dict[str, list[int]] = {}
    if not workspace.exists():
        return snapshot
    for path in workspace.rglob("*"):
        if not path.is_file():
            continue
        relative = path.resolve().relative_to(workspace)
        # Coding CLIs and YBM may write their own control/log files below the
        # working directory. They are not user project output and must never
        # turn a text-only response into a false "files changed" success.
        if relative.parts and relative.parts[0].casefold() in {".agent_control", ".git"}:
            continue
        try:
            stat = path.stat()
            snapshot[str(relative)] = [stat.st_size, int(stat.st_mtime_ns)]
        except OSError:
            continue
    return snapshot


def _changed_files(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    changed = []
    for path, stat in sorted(after.items()):
        if list(before.get(path) or []) != list(stat):
            changed.append(path)
    return changed


def _terminal_output(operation: str, output: dict) -> dict:
    return {
        "content": output.get("summary") or f"coding.agent {operation} completed.",
        "is_final": True,
        "exit_code": output.get("returncode") or 0,
    }




def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def register(deps: RegistryDeps, definitions: Definitions, adapters: Adapters) -> None:
    settings = deps.settings
    enabled = capability_enabled(settings, Capability.TERMINAL_RUN) and settings.adapters.coding_agent.enabled
    operations = ("start", "plan", "run_step", "run_goal", "status", "limits", "resume", "stop", "get_latest_output")
    definitions.append(
        ToolDefinition(
            name="coding.agent",
            capability=Capability.TERMINAL_RUN,
            enabled=enabled,
            description=(
                "start background Codex, Claude Code, or GitHub Copilot CLI sessions in a task workspace "
                "and report their status; completion is announced to the source chat automatically"
            ),
            operations=operations,
            input_schema=CodingAgentInput,
            output_schema=CodingAgentOutput,
            operation_output_schemas=same_output_schema(operations, CodingAgentOutput),
            default_operation="run_goal",
            operation_risks={
                "status": RiskLevel.LOW,
                "limits": RiskLevel.LOW,
                "get_latest_output": RiskLevel.LOW,
                "stop": RiskLevel.MEDIUM,
                "start": RiskLevel.HIGH,
                "plan": RiskLevel.HIGH,
                "run_step": RiskLevel.HIGH,
                "run_goal": RiskLevel.HIGH,
                "resume": RiskLevel.HIGH,
            },
            examples=(
                {"operation": "start", "provider": "codex", "prompt": "fix the failing tests in this repo"},
                {"operation": "status"},
                {"operation": "stop", "provider": "codex"},
            ),
        )
    )
    if settings.adapters.coding_agent.enabled:
        adapters["coding.agent"] = CodingAgentAdapter(
            settings.adapters.coding_agent,
            on_complete=_coding_session_completion_callback(deps),
        )


def _coding_session_completion_callback(deps: RegistryDeps):
    """Push a report to the task's source chat when a background coding session ends."""
    telegram = deps.telegram_client
    tasks = deps.task_repository

    async def notify(session: dict) -> None:
        task = None
        task_id = session.get("task_id")
        if tasks is not None and task_id:
            task = tasks.get(str(task_id))  # type: ignore[attr-defined]
            if task is not None:
                brief = {
                    key: session.get(key)
                    for key in ("session_id", "provider", "status", "returncode", "changed_files", "summary")
                }
                tasks.update_metadata(task.id, {**task.metadata, "coding_agent_session": brief})  # type: ignore[attr-defined]
        chat_id = task.metadata.get("source_chat_id") if task is not None else None
        if telegram is not None and chat_id:
            await telegram.send_message(str(chat_id), session_completion_message(session))  # type: ignore[attr-defined]

    if telegram is None and tasks is None:
        return None
    return notify
