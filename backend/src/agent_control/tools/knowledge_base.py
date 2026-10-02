"""Tool surface for agent_control.knowledge_base (docs/HISTORY.md Part 3
T2.7) - lexical search over a folder of the user's own reference material.
See that module's docstring for why this is keyword-overlap, not embeddings.
"""

from __future__ import annotations

from typing import Any

from agent_control.config import KnowledgeBaseAdapterConfig
from agent_control.knowledge_base import list_sources, search
from agent_control.schemas import Capability, ToolCallRequest, ToolCallResult, ToolResultStatus
from agent_control.tools.contracts import KnowledgeBaseInput, KnowledgeBaseOutput
from agent_control.tools.spec import (
    Adapters,
    Definitions,
    RegistryDeps,
    ToolDefinition,
    capability_enabled,
    failed_result,
    same_output_schema,
)


class KnowledgeBaseAdapter:
    def __init__(self, config: KnowledgeBaseAdapterConfig) -> None:
        self.config = config

    async def execute(self, request: ToolCallRequest) -> ToolCallResult:
        operation = str(request.input.get("operation") or "search")
        try:
            if operation == "list_sources":
                output = self._list_sources()
            elif operation == "search":
                output = self._search(request)
            else:
                return failed_result(request, f"unsupported knowledge base operation: {operation}")
        except Exception as exc:
            return failed_result(request, f"knowledge base operation failed: {exc}")
        output["operation"] = operation
        output["terminal_output"] = [_terminal_output(operation, output)]
        return ToolCallResult(request_id=request.id, status=ToolResultStatus.SUCCEEDED, output=output)

    def _list_sources(self) -> dict[str, Any]:
        sources = list_sources(self.config)
        summary = (
            f"{len(sources)} indexable document(s) under {self.config.root_dir}."
            if sources
            else f"No indexable documents found under {self.config.root_dir}."
        )
        return {"summary": summary, "sources": sources}

    def _search(self, request: ToolCallRequest) -> dict[str, Any]:
        query = str(request.input["query"]).strip()
        results = search(self.config, query)
        summary = (
            f"{len(results)} matching passage(s) for {query!r}."
            if results
            else f"No matches for {query!r} in the knowledge base."
        )
        return {"summary": summary, "results": results}


def _terminal_output(operation: str, output: dict[str, Any]) -> dict[str, Any]:
    lines = [output.get("summary") or f"knowledge.search {operation} completed."]
    for source in output.get("sources") or []:
        lines.append(f"- {source}")
    for result in output.get("results") or []:
        lines.append(f"\n--- {result['path']} (score={result['score']}) ---\n{result['excerpt']}")
    return {
        "instance_id": "local-worker",
        "terminal_id": "knowledge-base",
        "content": "\n".join(lines),
        "is_final": True,
        "exit_code": 0,
        "source": "knowledge_base",
    }


def register(deps: RegistryDeps, definitions: Definitions, adapters: Adapters) -> None:
    settings = deps.settings
    # Reuses TELEGRAM_RECEIVE, same reasoning as skills.use/persona.manage:
    # read-only local search with no side effects.
    enabled = capability_enabled(settings, Capability.AGENT_CORE) and settings.adapters.knowledge_base.enabled
    definitions.append(
        ToolDefinition(
            name="knowledge.search",
            capability=Capability.AGENT_CORE,
            enabled=enabled,
            description=(
                "search the user's local knowledge base (personal notes/reference documents) by "
                "keyword, or list which documents are indexed"
            ),
            operations=("list_sources", "search"),
            input_schema=KnowledgeBaseInput,
            output_schema=KnowledgeBaseOutput,
            operation_output_schemas=same_output_schema(("list_sources", "search"), KnowledgeBaseOutput),
            default_operation="search",
            examples=(
                {"operation": "list_sources"},
                {"operation": "search", "query": "invoice payment terms"},
            ),
        )
    )
    if enabled:
        adapters["knowledge.search"] = KnowledgeBaseAdapter(settings.adapters.knowledge_base)
