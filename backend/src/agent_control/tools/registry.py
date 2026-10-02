"""Collects each tool module's own register() into one ToolRegistry.

New tool = new adapter module with a register(deps, definitions, adapters)
function (see spec.py for the shared types) + one import line added to
_REGISTRARS below. No other file needs to change (docs/archive/HISTORY.md P3).
"""

from __future__ import annotations

from agent_control.config import AppSettings
from agent_control.tools import (
    adapter_factory,
    artifact_delivery,
    browser,
    coding_agent,
    coding_assistant,
    code_interpreter,
    computer_use,
    document_manage,
    dependencies,
    web_search,
    filesystem_manage,
    http_request,
    knowledge_base,
    local_workspace,
    mcp_client,
    memory_manage,
    persona,
    schedule_manage,
    skills,
    task_status,
    tts,
    vscode_bridge,
)
from agent_control.tools.adapter_factory import AdapterFactoryAdapter
from agent_control.tools.spec import Adapters, Definitions, Registrar, RegistryDeps, ToolDefinition, ToolRegistry

__all__ = ["ToolDefinition", "ToolRegistry", "build_tool_registry"]


# Ordered list of per-tool registrars. build_tool_registry() runs these in
# order; each one appends its ToolDefinition(s) and optionally wires up an
# adapter when the underlying integration is enabled.
_REGISTRARS: tuple[Registrar, ...] = (
    local_workspace.register,
    dependencies.register,
    web_search.register,
    filesystem_manage.register,
    adapter_factory.register,
    code_interpreter.register,
    http_request.register,
    mcp_client.register,
    vscode_bridge.register,
    coding_assistant.register,
    tts.register,
    coding_agent.register,
    knowledge_base.register,
    persona.register,
    memory_manage.register,
    schedule_manage.register,
    skills.register,
    task_status.register,
    artifact_delivery.register,
    document_manage.register,
    computer_use.register,
    browser.register,
)


def build_tool_registry(
    settings: AppSettings,
    backend_base_url: str,
    provider: object | None = None,
    should_continue: object | None = None,
    artifact_repository: object | None = None,
    task_repository: object | None = None,
    repositories: object | None = None,
    audit_logger: object | None = None,
    telegram_client: object | None = None,
) -> ToolRegistry:
    deps = RegistryDeps(
        settings=settings,
        backend_base_url=backend_base_url,
        provider=provider,
        should_continue=should_continue,  # type: ignore[arg-type]
        artifact_repository=artifact_repository,
        task_repository=task_repository,
        repositories=repositories,
        audit_logger=audit_logger,
        telegram_client=telegram_client,
    )
    adapters: Adapters = {}
    definitions: Definitions = []
    for register in _REGISTRARS:
        register(deps, definitions, adapters)
    registry = ToolRegistry(
        adapters=adapters,
        definitions=tuple(definitions),
        definition_index={definition.name: definition for definition in definitions},
        mcp_summary_factory=lambda: mcp_client.mcp_catalog_summary(settings.mcp),
    )
    adapter_factory_adapter = adapters.get("adapter.factory")
    if isinstance(adapter_factory_adapter, AdapterFactoryAdapter):
        adapter_factory_adapter.set_promotion_callback(registry.register_dynamic_tool)
    return registry
