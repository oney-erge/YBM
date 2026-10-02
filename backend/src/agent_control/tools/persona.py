"""Tool surface for agent_control.persona (docs/archive/HISTORY.md Part 4 T2.5) - lets
the Operator read the current persona/preferences document and update it
when it learns something durable ("the user said they always want file
summaries under 5 bullets"). See persona.py's module docstring for how this
differs from per-conversation memory.
"""

from __future__ import annotations

from typing import Any

from agent_control.persona import read_persona, write_persona
from agent_control.schemas import Capability, ToolCallRequest, ToolCallResult, ToolResultStatus
from agent_control.tools.contracts import PersonaInput, PersonaOutput
from agent_control.tools.spec import (
    Adapters,
    Definitions,
    RegistryDeps,
    ToolDefinition,
    capability_enabled,
    failed_result,
    same_output_schema,
)
from agent_control.config import PersonaAdapterConfig


class PersonaAdapter:
    def __init__(self, config: PersonaAdapterConfig) -> None:
        self.config = config

    async def execute(self, request: ToolCallRequest) -> ToolCallResult:
        operation = str(request.input.get("operation") or "get")
        try:
            if operation == "get":
                output = self._get()
            elif operation == "update":
                output = self._update(request)
            else:
                return failed_result(request, f"unsupported persona operation: {operation}")
        except Exception as exc:
            return failed_result(request, f"persona operation failed: {exc}")
        output["operation"] = operation
        output["terminal_output"] = [_terminal_output(operation, output)]
        return ToolCallResult(request_id=request.id, status=ToolResultStatus.SUCCEEDED, output=output)

    def _get(self) -> dict[str, Any]:
        content = read_persona(self.config)
        return {"summary": f"Persona content: {len(content)} char(s).", "content": content}

    def _update(self, request: ToolCallRequest) -> dict[str, Any]:
        content = write_persona(self.config, str(request.input["content"]))
        return {"summary": f"Persona updated ({len(content)} char(s)).", "content": content}


def _terminal_output(operation: str, output: dict[str, Any]) -> dict[str, Any]:
    return {
        "instance_id": "local-worker",
        "terminal_id": "persona",
        "content": f"{output.get('summary') or ''}\n{output.get('content') or ''}".strip(),
        "is_final": True,
        "exit_code": 0,
        "source": "persona",
    }


def register(deps: RegistryDeps, definitions: Definitions, adapters: Adapters) -> None:
    settings = deps.settings
    # Reuses TELEGRAM_RECEIVE, same reasoning as skills.use and task.status:
    # reading/writing a local preferences document has no side effects
    # outside the document itself.
    enabled = capability_enabled(settings, Capability.AGENT_CORE) and settings.adapters.persona.enabled
    definitions.append(
        ToolDefinition(
            name="persona.manage",
            capability=Capability.AGENT_CORE,
            enabled=enabled,
            description=(
                "read the current user persona/preferences document, or replace it with an updated "
                "version when you learn a durable preference worth remembering across tasks"
            ),
            operations=("get", "update"),
            input_schema=PersonaInput,
            output_schema=PersonaOutput,
            operation_output_schemas=same_output_schema(("get", "update"), PersonaOutput),
            default_operation="get",
            examples=(
                {"operation": "get"},
                {"operation": "update", "content": "Prefers concise answers. Timezone: America/Chicago."},
            ),
        )
    )
    if enabled:
        adapters["persona.manage"] = PersonaAdapter(settings.adapters.persona)
