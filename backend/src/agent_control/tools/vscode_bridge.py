from __future__ import annotations

import asyncio
from datetime import datetime
import logging
import os
from pathlib import Path
import re
from typing import Any

from fastapi import Header, HTTPException
import httpx
from pydantic import Field

from agent_control.config import VSCodeAdapterConfig, is_loopback_host
from agent_control.config_sync import read_env_value

from agent_control.prompts import render_prompt
from agent_control.schemas import (
    Capability,
    ErrorClass,
    StrictBaseModel,
    ToolCallRequest,
    ToolCallResult,
    ToolResultStatus,
    new_id,
    utc_now,
)
from agent_control.tools.contracts import (
    VSCodeCopilotTerminalInput,
    VSCodeTerminalCommandInput,
    VSCodeTerminalToolOutput,
)
from agent_control.tools.spec import Adapters, Definitions, RegistryDeps, ToolDefinition, capability_enabled


logger = logging.getLogger(__name__)


class VSCodeHeartbeat(StrictBaseModel):
    instance_id: str
    workspace_folders: list[str] = Field(default_factory=list)
    active_file: str | None = None
    diagnostics_count: int = 0
    observed_at: datetime = Field(default_factory=utc_now)


class VSCodeWorkspaceState(StrictBaseModel):
    instance_id: str
    workspace_folders: list[str] = Field(default_factory=list)
    active_file: str | None = None
    open_files: list[str] = Field(default_factory=list)
    diagnostics_count: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)
    observed_at: datetime = Field(default_factory=utc_now)


class VSCodeTerminalOutput(StrictBaseModel):
    instance_id: str
    terminal_id: str
    content: str
    command_id: str | None = None
    is_final: bool = False
    exit_code: int | None = None
    observed_at: datetime = Field(default_factory=utc_now)


_TERMINAL_CONTROL_RE = re.compile(
    r"\x1b\][^\x07]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]"
)


def clean_terminal_output(content: str) -> str:
    return _TERMINAL_CONTROL_RE.sub("", content).strip()


class VSCodeTerminalCommand(StrictBaseModel):
    id: str = Field(default_factory=lambda: new_id("vscode_terminal_command"))
    command: str
    terminal_id: str = "agent-control"
    instance_id: str | None = None
    cwd: str | None = None
    capture_output: bool = True
    created_at: datetime = Field(default_factory=utc_now)


class VSCodeBridgeStore:
    def __init__(self) -> None:
        self.heartbeat: VSCodeHeartbeat | None = None
        self.state: VSCodeWorkspaceState | None = None
        self.terminal_outputs: list[VSCodeTerminalOutput] = []
        self.terminal_commands: list[VSCodeTerminalCommand] = []

    def update_heartbeat(self, heartbeat: VSCodeHeartbeat) -> VSCodeHeartbeat:
        self.heartbeat = heartbeat
        return heartbeat

    def update_state(self, state: VSCodeWorkspaceState) -> VSCodeWorkspaceState:
        self.state = state
        return state

    def add_terminal_output(self, output: VSCodeTerminalOutput) -> VSCodeTerminalOutput:
        output.content = clean_terminal_output(output.content) or output.content
        self.terminal_outputs.append(output)
        return output

    def list_terminal_outputs(self, command_id: str | None = None) -> list[VSCodeTerminalOutput]:
        if command_id is None:
            return list(self.terminal_outputs)
        return [output for output in self.terminal_outputs if output.command_id == command_id]

    def enqueue_terminal_command(self, command: VSCodeTerminalCommand) -> VSCodeTerminalCommand:
        self.terminal_commands.append(command)
        return command

    def take_terminal_commands(self, instance_id: str | None = None) -> list[VSCodeTerminalCommand]:
        ready: list[VSCodeTerminalCommand] = []
        remaining: list[VSCodeTerminalCommand] = []
        for command in self.terminal_commands:
            if command.instance_id is None or command.instance_id == instance_id:
                ready.append(command)
            else:
                remaining.append(command)
        self.terminal_commands = remaining
        return ready


def require_vscode_bridge_token(
    config: VSCodeAdapterConfig,
    server_host: str = "127.0.0.1",
    token: str | None = Header(default=None, alias="X-Agent-Control-Token"),
) -> None:
    expected = read_env_value(config.auth_token_env)
    if not expected:
        if not is_loopback_host(config.bridge_host) or not is_loopback_host(server_host):
            raise HTTPException(
                status_code=503,
                detail=(
                    "VS Code bridge refused: host is not loopback-only and no bridge token "
                    f"is configured. Set {config.auth_token_env} before binding beyond 127.0.0.1."
                ),
            )
        return
    if token != expected:
        raise HTTPException(status_code=401, detail="invalid VS Code bridge token")


class VSCodeBridgeTerminalAdapter:
    def __init__(
        self,
        config: VSCodeAdapterConfig,
        backend_base_url: str,
        command_template: list[str] | None = None,
    ) -> None:
        self.config = config
        self.backend_base_url = backend_base_url.rstrip("/")
        self.command_template = command_template

    async def execute(self, request: ToolCallRequest) -> ToolCallResult:
        if not self.config.enabled:
            return self._failed(request, "VS Code adapter is disabled")

        command_text = self._command_text(request)
        if not command_text:
            return self._failed(request, "VS Code command input is empty")

        command = VSCodeTerminalCommand(
            command=command_text,
            terminal_id=str(request.input.get("terminal_id") or "agent-control-copilot"),
            instance_id=request.input.get("instance_id"),
            cwd=request.input.get("cwd"),
            capture_output=bool(request.input.get("capture_output", True)),
        )

        headers = self._headers()
        try:
            async with httpx.AsyncClient(timeout=request.timeout_seconds) as client:
                if bool(request.input.get("allow_local_fallback", True)) and not await self._bridge_connected(client, headers):
                    return await self._execute_local(request, command_text)
                response = await client.post(
                    f"{self.backend_base_url}/vscode/terminal-commands",
                    headers=headers,
                    json=command.model_dump(mode="json"),
                )
                response.raise_for_status()
                queued = dict(response.json())
                output = await self._wait_for_output(client, command.id, headers, request)
        except TimeoutError:
            return ToolCallResult(
                request_id=request.id,
                status=ToolResultStatus.TIMEOUT,
                error_class=ErrorClass.TRANSIENT,
                error_message="timed out waiting for VS Code bridge terminal output",
                output={"command_id": command.id},
            )
        except Exception as exc:
            return self._failed(request, str(exc), command_id=command.id)

        return ToolCallResult(
            request_id=request.id,
            status=ToolResultStatus.SUCCEEDED,
            output={
                "command_id": command.id,
                "queued": queued,
                "terminal_output": output,
            },
        )

    async def _bridge_connected(self, client: httpx.AsyncClient, headers: dict[str, str]) -> bool:
        try:
            response = await client.get(f"{self.backend_base_url}/vscode/state", headers=headers)
            response.raise_for_status()
            return response.json() is not None
        except Exception:
            logger.debug("VS Code bridge /vscode/state probe failed", exc_info=True)
            return False

    async def _execute_local(self, request: ToolCallRequest, command_text: str) -> ToolCallResult:
        try:
            returncode, content = await _run_powershell(command_text, request.input.get("cwd"), request.timeout_seconds)
            retried = False
            prompt = str(request.input.get("prompt") or "").strip()
            if returncode != 0 and prompt and not request.input.get("command"):
                retry_prompt = render_prompt("tools/copilot_plain_text_retry.md", prompt=prompt)
                retry_command = self._command_text_for_prompt(retry_prompt)
                retry_code, retry_content = await _run_powershell(retry_command, request.input.get("cwd"), request.timeout_seconds)
                content = (
                    "First attempt failed; retried once with a plain-text-only prompt.\n\n"
                    f"First attempt output:\n{content}\n\nRetry output:\n{retry_content}"
                ).strip()
                returncode = retry_code
                retried = True
            elif (
                prompt
                and bool(request.input.get("require_file_blocks"))
                and not request.input.get("command")
                and not _has_materializable_file_blocks(content)
            ):
                retry_prompt = render_prompt("tools/copilot_file_blocks_retry.md", prompt=prompt, output=content)
                retry_command = self._command_text_for_prompt(retry_prompt)
                retry_code, retry_content = await _run_powershell(retry_command, request.input.get("cwd"), request.timeout_seconds)
                content = (
                    "First attempt did not include materializable file blocks; retried once with a file-block-only prompt.\n\n"
                    f"First attempt output:\n{content}\n\nRetry output:\n{retry_content}"
                ).strip()
                returncode = retry_code
                retried = True
        except TimeoutError:
            return ToolCallResult(
                request_id=request.id,
                status=ToolResultStatus.TIMEOUT,
                error_class=ErrorClass.TRANSIENT,
                error_message="timed out waiting for local Copilot CLI fallback",
            )
        except Exception as exc:
            return self._failed(request, f"local Copilot CLI fallback failed: {exc}")

        usage = _extract_copilot_usage(content)
        status = ToolResultStatus.SUCCEEDED if returncode == 0 else _failed_status(content)
        return ToolCallResult(
            request_id=request.id,
            status=status,
            output={
                "command_id": None,
                "queued": None,
                "usage": usage,
                "retried": retried,
                "terminal_output": [
                    {
                        "instance_id": "local-worker",
                        "terminal_id": request.input.get("terminal_id") or "agent-control-copilot",
                        "content": content,
                        "command_id": None,
                        "is_final": True,
                        "exit_code": returncode,
                        "source": "local_copilot_cli_fallback",
                    }
                ],
            },
            error_class=None if status == ToolResultStatus.SUCCEEDED else _error_class(content),
            error_message=None if status == ToolResultStatus.SUCCEEDED else "local Copilot CLI fallback failed",
        )

    def _command_text(self, request: ToolCallRequest) -> str:
        explicit = request.input.get("command")
        if explicit:
            return str(explicit)

        prompt = str(request.input.get("prompt") or "").strip()
        if not prompt:
            return ""

        if self.command_template:
            quoted = _powershell_single_quote(prompt)
            return " ".join(part.replace("{prompt}", quoted).replace("{prompt_raw}", prompt) for part in self.command_template)

        return self._command_text_for_prompt(prompt)

    def _command_text_for_prompt(self, prompt: str) -> str:
        copilot = _copilot_executable()
        if copilot:
            return f"& {_powershell_single_quote(str(copilot))} -p {_powershell_single_quote(prompt)}"
        return f"gh copilot -p {_powershell_single_quote(prompt)}"

    async def _wait_for_output(
        self,
        client: httpx.AsyncClient,
        command_id: str,
        headers: dict[str, str],
        request: ToolCallRequest,
    ) -> list[dict[str, Any]]:
        deadline = asyncio.get_running_loop().time() + request.timeout_seconds
        latest: list[dict[str, Any]] = []
        while asyncio.get_running_loop().time() < deadline:
            response = await client.get(
                f"{self.backend_base_url}/vscode/terminal-output",
                headers=headers,
                params={"command_id": command_id},
            )
            response.raise_for_status()
            latest = list(response.json().get("outputs", []))
            if any(output.get("is_final") for output in latest):
                return latest
            await asyncio.sleep(1)
        raise TimeoutError

    def _headers(self) -> dict[str, str]:
        token = read_env_value(self.config.auth_token_env)
        return {"X-Agent-Control-Token": token} if token else {}

    @staticmethod
    def _failed(request: ToolCallRequest, message: str, command_id: str | None = None) -> ToolCallResult:
        output = {"command_id": command_id} if command_id else {}
        return ToolCallResult(
            request_id=request.id,
            status=ToolResultStatus.FAILED,
            output=output,
            error_class=ErrorClass.ADAPTER_FAILED,
            error_message=message,
        )


def _powershell_single_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _copilot_executable() -> Path | None:
    local_app_data = os.getenv("LOCALAPPDATA")
    if not local_app_data:
        return None
    winget_root = Path(local_app_data) / "Microsoft" / "WinGet" / "Packages"
    matches = sorted(winget_root.glob("GitHub.Copilot_*/*copilot.exe"), reverse=True)
    for candidate in matches:
        if candidate.is_file():
            return candidate
    return None


async def _run_powershell(command_text: str, cwd: str | None, timeout_seconds: int) -> tuple[int, str]:
    process = await asyncio.create_subprocess_exec(
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        command_text,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_bytes, stderr_bytes = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
    stdout = stdout_bytes.decode(errors="replace")
    stderr = stderr_bytes.decode(errors="replace")
    content = "\n".join(part for part in [stdout.strip(), stderr.strip()] if part)
    return int(process.returncode or 0), content


def _extract_copilot_usage(content: str) -> dict[str, str]:
    usage: dict[str, str] = {}
    for line in content.splitlines():
        stripped = line.strip()
        if re.search(r"\bRequests\b", stripped, re.IGNORECASE):
            usage["requests"] = stripped
        elif re.search(r"\bTokens\b", stripped, re.IGNORECASE):
            usage["tokens"] = stripped
        elif re.search(r"(usage|quota|rate).{0,30}(limit|exceeded|remaining)", stripped, re.IGNORECASE):
            usage.setdefault("limit", stripped)
    return usage


def _has_materializable_file_blocks(content: str) -> bool:
    fence_pattern = re.compile(
        r"```(?P<lang>[A-Za-z0-9_+.-]*)[ \t]*(?P<meta>[^\n`]*)\n(?P<code>.*?)```",
        re.DOTALL,
    )
    for match in fence_pattern.finditer(content):
        metadata = match.group("meta") or ""
        language = (match.group("lang") or "").strip().lower()
        if re.search(r"(?:filename|file|path)\s*=\s*['\"]?[^'\"\s`]+\.(?:html|css|js|mjs)", metadata, re.IGNORECASE):
            return True
        if language in {"html", "htm"}:
            return True
    return False


def _failed_status(content: str) -> ToolResultStatus:
    lowered = content.lower()
    if "rate limit" in lowered or "too many requests" in lowered:
        return ToolResultStatus.RATE_LIMITED
    return ToolResultStatus.FAILED


def _error_class(content: str) -> ErrorClass:
    lowered = content.lower()
    if "usage limit" in lowered or "quota exceeded" in lowered:
        return ErrorClass.USAGE_LIMITED
    if "rate limit" in lowered or "too many requests" in lowered:
        return ErrorClass.RATE_LIMITED
    return ErrorClass.ADAPTER_FAILED


def register(deps: RegistryDeps, definitions: Definitions, adapters: Adapters) -> None:
    settings = deps.settings
    enabled = capability_enabled(settings, Capability.VSCODE_WRITE_FILES) and settings.adapters.vscode.enabled
    definitions.append(
        ToolDefinition(
            name="vscode.copilot_terminal",
            group="code",
            provides=('vscode', 'copilot', 'editor', 'terminal', 'prompt',),
            capability=Capability.VSCODE_WRITE_FILES,
            enabled=enabled,
            description="send a prompt to VS Code/Copilot terminal or local Copilot CLI fallback",
            input_schema=VSCodeCopilotTerminalInput,
            output_schema=VSCodeTerminalToolOutput,
        )
    )
    definitions.append(
        ToolDefinition(
            name="vscode.terminal_command",
            group="code",
            provides=('vscode', 'terminal', 'command', 'run', 'editor',),
            capability=Capability.VSCODE_WRITE_FILES,
            enabled=enabled,
            description="queue an explicit terminal command through the VS Code bridge",
            input_schema=VSCodeTerminalCommandInput,
            output_schema=VSCodeTerminalToolOutput,
        )
    )
    if settings.adapters.vscode.enabled:
        vscode = VSCodeBridgeTerminalAdapter(settings.adapters.vscode, deps.backend_base_url)
        adapters["vscode.terminal_command"] = vscode
        adapters["vscode.copilot_terminal"] = vscode
