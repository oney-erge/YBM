from __future__ import annotations

import asyncio

from agent_control.config import CodingAssistantAdapterConfig
from agent_control.schemas import Capability, ErrorClass, ToolCallRequest, ToolCallResult, ToolResultStatus
from agent_control.tools.contracts import CodingAssistantInput, CodingAssistantOutput
from agent_control.tools.spec import Adapters, Definitions, RegistryDeps, ToolDefinition, capability_enabled


class GenericTerminalAgentAdapter:
    def __init__(self, config: CodingAssistantAdapterConfig) -> None:
        self.config = config

    async def execute(self, request: ToolCallRequest) -> ToolCallResult:
        if not self.config.enabled:
            return self._failed(request, "coding assistant adapter is disabled")

        command = self._command(request)
        if not command:
            return self._failed(request, "coding assistant command template is empty")

        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=self.config.working_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(),
                timeout=self.config.timeout_seconds,
            )
        except TimeoutError:
            return ToolCallResult(
                request_id=request.id,
                status=ToolResultStatus.TIMEOUT,
                error_class=ErrorClass.TRANSIENT,
                error_message="coding assistant command timed out",
            )
        except Exception as exc:
            return self._failed(request, str(exc))

        stdout = self._trim(stdout_bytes.decode(errors="replace"))
        stderr = self._trim(stderr_bytes.decode(errors="replace"))
        combined = f"{stdout}\n{stderr}".lower()

        if self._contains(combined, self.config.usage_limit_patterns):
            return self._limited(request, stdout, stderr, ErrorClass.USAGE_LIMITED)
        if self._contains(combined, self.config.rate_limit_patterns):
            return self._limited(request, stdout, stderr, ErrorClass.RATE_LIMITED)

        status = ToolResultStatus.SUCCEEDED if process.returncode == 0 else ToolResultStatus.FAILED
        return ToolCallResult(
            request_id=request.id,
            status=status,
            output={"stdout": stdout, "stderr": stderr, "returncode": process.returncode},
            error_class=None if status == ToolResultStatus.SUCCEEDED else ErrorClass.ADAPTER_FAILED,
            error_message=None if status == ToolResultStatus.SUCCEEDED else "coding assistant command failed",
        )

    def _command(self, request: ToolCallRequest) -> list[str]:
        prompt = str(request.input.get("prompt", ""))
        return [part.replace("{prompt}", prompt) for part in self.config.command_template]

    def _trim(self, value: str) -> str:
        return value[: self.config.output_limit_chars]

    @staticmethod
    def _contains(value: str, patterns: list[str]) -> bool:
        return any(pattern.lower() in value for pattern in patterns)

    @staticmethod
    def _failed(request: ToolCallRequest, message: str) -> ToolCallResult:
        return ToolCallResult(
            request_id=request.id,
            status=ToolResultStatus.FAILED,
            error_class=ErrorClass.ADAPTER_FAILED,
            error_message=message,
        )

    @staticmethod
    def _limited(
        request: ToolCallRequest,
        stdout: str,
        stderr: str,
        error_class: ErrorClass,
    ) -> ToolCallResult:
        return ToolCallResult(
            request_id=request.id,
            status=ToolResultStatus.RATE_LIMITED,
            output={"stdout": stdout, "stderr": stderr},
            error_class=error_class,
            error_message=error_class.value,
        )


def register(deps: RegistryDeps, definitions: Definitions, adapters: Adapters) -> None:
    settings = deps.settings
    enabled = capability_enabled(settings, Capability.TERMINAL_RUN) and settings.adapters.coding_assistant.enabled
    definitions.append(
        ToolDefinition(
            name="coding_assistant",
            group="code",
            provides=('coding', 'assistant', 'code', 'generate', 'command',),
            capability=Capability.TERMINAL_RUN,
            enabled=enabled,
            description="run the configured local coding assistant command template",
            input_schema=CodingAssistantInput,
            output_schema=CodingAssistantOutput,
        )
    )
    if settings.adapters.coding_assistant.enabled:
        adapters["coding_assistant"] = GenericTerminalAgentAdapter(settings.adapters.coding_assistant)
