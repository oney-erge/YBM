"""Lets the agent itself save a durable fact mid-task (docs/archive/UI_UX_AUDIT.md
Phase 4), not just an operator typing into the Memory page.

Every fact this tool creates is stamped MemorySource.TASK_DERIVED and
carries the originating task_id - the provenance distinction the Memory
page needs to tell "I told it this" (operator_admin, via admin.py) apart
from "it decided this was worth keeping while working" (this tool). The
model cannot claim a different source; there is no source field on the
input at all.
"""

from __future__ import annotations

from agent_control.schemas import Capability, MemoryFact, MemorySource, RiskLevel, ToolCallRequest, ToolCallResult, ToolResultStatus
from agent_control.storage.repositories import Repositories
from agent_control.tools.contracts import MemoryManageInput, MemoryManageOutput
from agent_control.tools.spec import (
    Adapters,
    Definitions,
    RegistryDeps,
    ToolDefinition,
    capability_enabled,
    failed_result,
    same_output_schema,
)


class MemoryManageAdapter:
    def __init__(self, repositories: Repositories) -> None:
        self.repositories = repositories

    async def execute(self, request: ToolCallRequest) -> ToolCallResult:
        operation = str(request.input.get("operation") or "remember")
        try:
            if operation == "remember":
                output = self._remember(request)
            elif operation == "list":
                output = self._list(request)
            elif operation == "forget":
                output = self._forget(request)
            else:
                return failed_result(request, f"unsupported memory operation: {operation}")
        except Exception as exc:
            return failed_result(request, f"memory operation failed: {exc}")
        output["operation"] = operation
        output["terminal_output"] = [_terminal_output(operation, output)]
        return ToolCallResult(request_id=request.id, status=ToolResultStatus.SUCCEEDED, output=output)

    def _remember(self, request: ToolCallRequest) -> dict:
        category = str(request.input["category"]).strip()
        content = str(request.input["content"]).strip()
        fact = self.repositories.memory_facts.create(
            MemoryFact(category=category, content=content, source=MemorySource.TASK_DERIVED, task_id=request.task_id)
        )
        return {
            "fact_id": fact.id,
            "facts": [fact.model_dump(mode="json")],
            "summary": f"Remembered [{category}]: {content}",
        }

    def _list(self, request: ToolCallRequest) -> dict:
        query = request.input.get("query")
        facts = self.repositories.memory_facts.list_all(query=str(query) if query else None)
        return {
            "facts": [f.model_dump(mode="json") for f in facts],
            "summary": f"Found {len(facts)} remembered fact(s).",
        }

    def _forget(self, request: ToolCallRequest) -> dict:
        fact_id = str(request.input["fact_id"]).strip()
        deleted = self.repositories.memory_facts.delete(fact_id)
        if not deleted:
            raise ValueError(f"no remembered fact with id {fact_id!r}")
        return {"fact_id": fact_id, "summary": f"Forgot {fact_id}."}


def _terminal_output(operation: str, output: dict) -> dict:
    return {
        "content": output.get("summary") or f"memory.manage {operation} completed.",
        "is_final": True,
        "exit_code": 0,
    }


def register(deps: RegistryDeps, definitions: Definitions, adapters: Adapters) -> None:
    settings = deps.settings
    enabled = capability_enabled(settings, Capability.MEMORY_MANAGE)
    definitions.append(
        ToolDefinition(
            name="memory.manage",
            capability=Capability.MEMORY_MANAGE,
            enabled=enabled,
            description=(
                "remember a durable fact about the user or their setup for future tasks, list "
                "previously remembered facts, or forget one by id"
            ),
            operations=("remember", "list", "forget"),
            input_schema=MemoryManageInput,
            output_schema=MemoryManageOutput,
            operation_output_schemas=same_output_schema(("remember", "list", "forget"), MemoryManageOutput),
            default_operation="remember",
            # docs/archive/UI_UX_AUDIT.md Phase 15: remember/list stay low-risk,
            # no-approval - forgetting a durable fact is the one operation
            # that erases something a human may have relied on the agent
            # to keep, so it gets a real gate, the same pattern
            # schedule.manage already uses for its own destructive ops.
            operation_risks={
                "remember": RiskLevel.LOW,
                "list": RiskLevel.LOW,
                "forget": RiskLevel.MEDIUM,
            },
            approval_required_operations=("forget",),
            approval_reasons={
                "forget": "permanently deletes a remembered fact - the agent cannot silently erase something you asked it to remember",
            },
            examples=(
                {"operation": "remember", "category": "preference", "content": "Prefers metric units"},
                {"operation": "list", "query": "preference"},
                {"operation": "forget", "fact_id": "{{fact_id}}"},
            ),
        )
    )
    if enabled and deps.repositories is not None:
        adapters["memory.manage"] = MemoryManageAdapter(deps.repositories)  # type: ignore[arg-type]
