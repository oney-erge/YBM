from __future__ import annotations

import pytest

from agent_control.config import AppSettings, CapabilityPolicy
from agent_control.orchestration.executor import ToolExecutor
from agent_control.policy import PolicyEngine
from agent_control.schemas import Capability, MemoryFact, MemorySource, RiskLevel, ToolCallRequest, ToolResultStatus
from agent_control.tools.memory_manage import MemoryManageAdapter, register as register_memory_manage
from agent_control.tools.spec import RegistryDeps
from helpers import make_repos


@pytest.mark.asyncio
async def test_memory_manage_remember_creates_a_task_derived_fact(tmp_path) -> None:
    """The tool cannot claim any other source (docs/archive/UI_UX_AUDIT.md Phase 4's
    provenance requirement) - there is no source field on the input at all,
    every fact this adapter creates is stamped TASK_DERIVED."""
    repos, _audit = make_repos(tmp_path)
    task = repos.tasks.create("remember my preference")
    adapter = MemoryManageAdapter(repos)

    result = await adapter.execute(
        ToolCallRequest(
            task_id=task.id, tool_name="memory.manage", capability=Capability.MEMORY_MANAGE,
            input={"operation": "remember", "category": "preference", "content": "Prefers metric units"},
        )
    )

    assert result.status == ToolResultStatus.SUCCEEDED
    fact_id = result.output["fact_id"]
    stored = repos.memory_facts.get(fact_id)
    assert stored is not None
    assert stored.source == MemorySource.TASK_DERIVED
    assert stored.task_id == task.id
    assert stored.category == "preference"
    assert stored.content == "Prefers metric units"


@pytest.mark.asyncio
async def test_memory_manage_list_returns_matching_facts(tmp_path) -> None:
    repos, _audit = make_repos(tmp_path)
    repos.memory_facts.create(MemoryFact(category="preference", content="Likes dark mode"))
    repos.memory_facts.create(MemoryFact(category="project", content="Working on a CLI tool"))
    adapter = MemoryManageAdapter(repos)

    result = await adapter.execute(
        ToolCallRequest(
            task_id="task_1", tool_name="memory.manage", capability=Capability.MEMORY_MANAGE,
            input={"operation": "list", "query": "dark"},
        )
    )

    assert result.status == ToolResultStatus.SUCCEEDED
    assert [f["content"] for f in result.output["facts"]] == ["Likes dark mode"]


@pytest.mark.asyncio
async def test_memory_manage_forget_deletes_the_fact(tmp_path) -> None:
    repos, _audit = make_repos(tmp_path)
    fact = repos.memory_facts.create(MemoryFact(category="preference", content="Likes dark mode"))
    adapter = MemoryManageAdapter(repos)

    result = await adapter.execute(
        ToolCallRequest(
            task_id="task_1", tool_name="memory.manage", capability=Capability.MEMORY_MANAGE,
            input={"operation": "forget", "fact_id": fact.id},
        )
    )

    assert result.status == ToolResultStatus.SUCCEEDED
    assert repos.memory_facts.get(fact.id) is None


@pytest.mark.asyncio
async def test_memory_manage_forget_fails_clearly_for_an_unknown_fact(tmp_path) -> None:
    repos, _audit = make_repos(tmp_path)
    adapter = MemoryManageAdapter(repos)

    result = await adapter.execute(
        ToolCallRequest(
            task_id="task_1", tool_name="memory.manage", capability=Capability.MEMORY_MANAGE,
            input={"operation": "forget", "fact_id": "mem_missing"},
        )
    )

    assert result.status == ToolResultStatus.FAILED
    assert "mem_missing" in (result.error_message or "")


def _executor_for_memory_manage(repos, audit, settings: AppSettings) -> ToolExecutor:
    """Builds the REAL memory.manage ToolDefinition via register() (not a
    synthetic stand-in) so this test exercises the actual operation_risks/
    approval_required_operations shipped in tools/memory_manage.py, through
    the full ToolExecutor/PolicyEngine stack the adapter-only tests above
    bypass (docs/archive/UI_UX_AUDIT.md Phase 15)."""
    definitions: list = []
    adapters: dict = {}
    register_memory_manage(
        RegistryDeps(settings=settings, backend_base_url="http://127.0.0.1", repositories=repos),
        definitions,
        adapters,
    )
    definition = next(d for d in definitions if d.name == "memory.manage")
    return ToolExecutor(
        PolicyEngine(settings, audit),
        repos,
        audit,
        adapters=adapters,
        tool_definitions={"memory.manage": definition},
    )


@pytest.mark.asyncio
async def test_memory_manage_forget_requires_approval_but_remember_and_list_do_not(tmp_path) -> None:
    """docs/archive/UI_UX_AUDIT.md Phase 15: the agent must not be able to silently
    erase a remembered fact - forget is gated, remember/list stay free."""
    repos, audit = make_repos(tmp_path)
    settings = AppSettings()
    settings.capabilities[Capability.MEMORY_MANAGE] = CapabilityPolicy(
        enabled=True, requires_approval=False, max_risk_level=RiskLevel.MEDIUM,
    )
    executor = _executor_for_memory_manage(repos, audit, settings)
    fact = repos.memory_facts.create(MemoryFact(category="preference", content="Likes dark mode"))
    task = repos.tasks.create("manage memory")

    remember_result = await executor.execute(
        ToolCallRequest(
            task_id=task.id, tool_name="memory.manage", capability=Capability.MEMORY_MANAGE,
            input={"operation": "remember", "category": "preference", "content": "Prefers metric units"},
        )
    )
    list_result = await executor.execute(
        ToolCallRequest(
            task_id=task.id, tool_name="memory.manage", capability=Capability.MEMORY_MANAGE,
            input={"operation": "list"},
        )
    )
    forget_result = await executor.execute(
        ToolCallRequest(
            task_id=task.id, tool_name="memory.manage", capability=Capability.MEMORY_MANAGE,
            risk_level=RiskLevel.MEDIUM,
            input={"operation": "forget", "fact_id": fact.id},
        )
    )

    assert remember_result.status == ToolResultStatus.SUCCEEDED
    assert list_result.status == ToolResultStatus.SUCCEEDED
    assert forget_result.status == ToolResultStatus.NEEDS_APPROVAL
    approval = repos.approvals.get(forget_result.output["approval_id"])
    assert "cannot silently erase" in approval.summary
    # The gate actually held: the fact is still there, not deleted.
    assert repos.memory_facts.get(fact.id) is not None


@pytest.mark.asyncio
async def test_memory_manage_forget_is_denied_outright_if_max_risk_level_is_not_raised(tmp_path) -> None:
    """Regression guard for the landmine this change has to avoid: bumping
    forget's operation_risks to MEDIUM without also raising the capability's
    max_risk_level would make PolicyEngine deny it outright
    (risk_exceeds_capability_policy) instead of routing to approval - this
    pins that a low ceiling really does deny, so the config fix is load-
    bearing, not cosmetic."""
    repos, audit = make_repos(tmp_path)
    settings = AppSettings()
    settings.capabilities[Capability.MEMORY_MANAGE] = CapabilityPolicy(
        enabled=True, requires_approval=False, max_risk_level=RiskLevel.LOW,
    )
    executor = _executor_for_memory_manage(repos, audit, settings)
    fact = repos.memory_facts.create(MemoryFact(category="preference", content="Likes dark mode"))
    task = repos.tasks.create("manage memory")

    result = await executor.execute(
        ToolCallRequest(
            task_id=task.id, tool_name="memory.manage", capability=Capability.MEMORY_MANAGE,
            risk_level=RiskLevel.MEDIUM,
            input={"operation": "forget", "fact_id": fact.id},
        )
    )

    assert result.status == ToolResultStatus.DENIED
