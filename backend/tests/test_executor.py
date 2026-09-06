"""Unit tests for the ToolExecutor.

ToolExecutor sits between the worker and the tool adapters: it validates
inputs, consults the policy engine, dispatches to the right adapter, validates
outputs, and records everything in the audit log + tool_invocations table.
These tests pin its decision branches.
"""
from __future__ import annotations

from typing import Any

import pytest

from agent_control.config import AppSettings, CapabilityPolicy
from agent_control.orchestration.executor import StaticToolAdapter, ToolExecutor
from agent_control.policy import PolicyEngine
from agent_control.schemas import (
    ApprovalStatus,
    AuditEventType,
    Capability,
    ErrorClass,
    RiskLevel,
    ToolCallRequest,
    ToolCallResult,
    ToolResultStatus,
    ToolVerification,
)
from agent_control.tools.spec import ToolDefinition
from helpers import make_repos




def _settings_with(capability: Capability, **overrides: Any) -> AppSettings:
    """Build an AppSettings whose only enabled capability is `capability`."""
    base = AppSettings()
    base.capabilities[capability] = CapabilityPolicy(
        enabled=overrides.pop("enabled", True),
        scopes=overrides.pop("scopes", []),
        requires_approval=overrides.pop("requires_approval", False),
        max_risk_level=overrides.pop("max_risk_level", RiskLevel.LOW),
        allow_patterns=overrides.pop("allow_patterns", []),
        deny_patterns=overrides.pop("deny_patterns", []),
    )
    return base


class _ExplodingAdapter:
    async def execute(self, request: ToolCallRequest) -> ToolCallResult:
        raise RuntimeError("adapter blew up")


def _request(task_id: str, *, capability: Capability = Capability.LLM_GENERATE,
             tool_name: str = "llm", risk: RiskLevel = RiskLevel.LOW,
             requires_approval: bool = False) -> ToolCallRequest:
    return ToolCallRequest(
        task_id=task_id,
        tool_name=tool_name,
        capability=capability,
        risk_level=risk,
        requires_approval=requires_approval,
        input={"prompt": "hi"},
    )


@pytest.mark.asyncio
async def test_executor_dispatches_to_registered_adapter(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("t")
    settings = _settings_with(Capability.LLM_GENERATE)
    adapter = StaticToolAdapter(output={"text": "hello"})
    executor = ToolExecutor(
        PolicyEngine(settings, audit),
        repos,
        audit,
        adapters={"llm": adapter},
    )

    result = await executor.execute(_request(task.id))

    assert result.status == ToolResultStatus.SUCCEEDED
    assert result.output == {"text": "hello"}
    assert len(adapter.requests) == 1


@pytest.mark.asyncio
async def test_executor_records_request_and_completion_in_audit(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("t")
    settings = _settings_with(Capability.LLM_GENERATE)
    executor = ToolExecutor(
        PolicyEngine(settings, audit),
        repos,
        audit,
        adapters={"llm": StaticToolAdapter()},
    )

    await executor.execute(_request(task.id))

    invocations = repos.tool_invocations.list_for_task(task.id)
    assert len(invocations) == 1
    assert invocations[0]["result"] is not None
    assert invocations[0]["result"]["status"] == ToolResultStatus.SUCCEEDED.value


@pytest.mark.asyncio
async def test_executor_redacts_free_text_secrets_before_persistence_and_return(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("read a config safely")
    settings = _settings_with(Capability.LLM_GENERATE)
    secret = "sk-live-EVOLEAK-9931-DO-NOT-ECHO"
    adapter = StaticToolAdapter(
        output={"text": f"SERVICE=billing ACME_API_KEY={secret}", "summary": f"token={secret}"}
    )
    executor = ToolExecutor(
        PolicyEngine(settings, audit),
        repos,
        audit,
        adapters={"llm": adapter},
    )

    result = await executor.execute(
        _request(task.id).model_copy(update={"input": {"prompt": f"authorization={secret}"}})
    )
    invocation = repos.tool_invocations.list_for_task(task.id)[0]
    persisted = str(invocation)

    assert secret not in str(result.model_dump())
    assert secret not in persisted
    assert "***" in str(result.output)
    # Execution still received the real value; redaction is a persistence and
    # downstream-context boundary, not mutation of the adapter call.
    assert secret in adapter.requests[0].input["prompt"]


@pytest.mark.asyncio
async def test_executor_denies_when_policy_blocks(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("t")
    # explicitly disable the capability so the policy denies it
    settings = _settings_with(Capability.LLM_GENERATE, enabled=False)
    executor = ToolExecutor(
        PolicyEngine(settings, audit),
        repos,
        audit,
        adapters={"llm": StaticToolAdapter()},
    )

    result = await executor.execute(_request(task.id))

    assert result.status == ToolResultStatus.DENIED
    assert result.error_class == ErrorClass.POLICY_DENIED


@pytest.mark.asyncio
async def test_executor_returns_needs_approval_and_creates_approval(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("t")
    settings = _settings_with(Capability.LLM_GENERATE, requires_approval=True)
    executor = ToolExecutor(
        PolicyEngine(settings, audit),
        repos,
        audit,
        adapters={"llm": StaticToolAdapter()},
    )

    result = await executor.execute(_request(task.id))

    assert result.status == ToolResultStatus.NEEDS_APPROVAL
    assert "approval_id" in result.output
    approvals = repos.approvals.list_for_task(task.id)
    assert len(approvals) == 1


@pytest.mark.asyncio
async def test_executor_runs_when_pre_approved(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("t")
    settings = _settings_with(Capability.LLM_GENERATE, requires_approval=True)
    executor = ToolExecutor(
        PolicyEngine(settings, audit),
        repos,
        audit,
        adapters={"llm": StaticToolAdapter()},
    )

    pending = await executor.execute(_request(task.id))
    approval_id = pending.output["approval_id"]
    assert repos.approvals.decide_pending(approval_id, ApprovalStatus.APPROVED)

    result = await executor.execute(_request(task.id), approval_id=approval_id)

    assert result.status == ToolResultStatus.SUCCEEDED
    assert repos.approvals.get(approval_id).status == ApprovalStatus.CONSUMED


@pytest.mark.asyncio
async def test_executor_strips_model_supplied_approved_flag_without_authorization(tmp_path) -> None:
    """A model can put input.approved=true in its own tool call - the
    executor must never let that survive to the adapter unless a real
    approval or grant authorized this exact call. Uses a capability that
    does NOT require approval, so the request dispatches straight through;
    if the executor were passing the model's `approved` through unchanged,
    the adapter would see approved=True with nothing behind it."""
    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("t")
    settings = _settings_with(Capability.LLM_GENERATE, requires_approval=False)
    adapter = StaticToolAdapter()
    executor = ToolExecutor(
        PolicyEngine(settings, audit),
        repos,
        audit,
        adapters={"llm": adapter},
    )

    request = _request(task.id).model_copy(update={"input": {"prompt": "hi", "approved": True}})
    result = await executor.execute(request)

    assert result.status == ToolResultStatus.SUCCEEDED
    assert adapter.requests[-1].input["approved"] is False


@pytest.mark.asyncio
async def test_executor_approval_is_exact_and_one_shot(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("t")
    settings = _settings_with(Capability.LLM_GENERATE, requires_approval=True)
    adapter = StaticToolAdapter()
    executor = ToolExecutor(
        PolicyEngine(settings, audit),
        repos,
        audit,
        adapters={"llm": adapter},
    )

    pending = await executor.execute(_request(task.id))
    approval_id = pending.output["approval_id"]
    assert repos.approvals.decide_pending(approval_id, ApprovalStatus.APPROVED)

    changed = _request(task.id).model_copy(update={"input": {"prompt": "different"}})
    mismatched = await executor.execute(changed, approval_id=approval_id)
    allowed = await executor.execute(_request(task.id), approval_id=approval_id)
    replayed = await executor.execute(_request(task.id), approval_id=approval_id)

    assert mismatched.status == ToolResultStatus.DENIED
    assert mismatched.error_message == "approval_invalid_or_mismatched"
    assert allowed.status == ToolResultStatus.SUCCEEDED
    assert replayed.status == ToolResultStatus.DENIED
    assert replayed.error_message == "approval_invalid_or_mismatched"
    assert len(adapter.requests) == 1


@pytest.mark.asyncio
async def test_executor_rejects_model_risk_understatement(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("t")
    settings = _settings_with(
        Capability.TERMINAL_RUN,
        max_risk_level=RiskLevel.CRITICAL,
    )
    adapter = StaticToolAdapter()
    definition = ToolDefinition(
        name="danger",
        capability=Capability.TERMINAL_RUN,
        enabled=True,
        description="dangerous operation",
        minimum_risk=RiskLevel.HIGH,
    )
    executor = ToolExecutor(
        PolicyEngine(settings, audit),
        repos,
        audit,
        adapters={"danger": adapter},
        tool_definitions=[definition],
    )

    result = await executor.execute(
        _request(
            task.id,
            capability=Capability.TERMINAL_RUN,
            tool_name="danger",
            risk=RiskLevel.LOW,
        )
    )

    assert result.status == ToolResultStatus.FAILED
    assert result.error_class == ErrorClass.VALIDATION_FAILED
    assert "understates" in (result.error_message or "")
    assert adapter.requests == []


@pytest.mark.asyncio
async def test_executor_rejects_model_capability_substitution(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("t")
    settings = _settings_with(Capability.LLM_GENERATE)
    adapter = StaticToolAdapter()
    definition = ToolDefinition(
        name="terminal",
        capability=Capability.TERMINAL_RUN,
        enabled=True,
        description="terminal operation",
    )
    executor = ToolExecutor(
        PolicyEngine(settings, audit),
        repos,
        audit,
        adapters={"terminal": adapter},
        tool_definitions=[definition],
    )

    result = await executor.execute(
        _request(task.id, capability=Capability.LLM_GENERATE, tool_name="terminal")
    )

    assert result.status == ToolResultStatus.FAILED
    assert result.error_class == ErrorClass.VALIDATION_FAILED
    assert "requires capability terminal.run" in (result.error_message or "")
    assert adapter.requests == []


@pytest.mark.asyncio
async def test_tool_definition_approval_cannot_be_disabled_by_capability_setting(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("persist configuration")
    settings = _settings_with(
        Capability.TERMINAL_RUN,
        requires_approval=False,
        max_risk_level=RiskLevel.CRITICAL,
    )
    settings.approval_policy.require_approval_at_or_above = RiskLevel.CRITICAL
    definition = ToolDefinition(
        name="persistent.tool",
        capability=Capability.TERMINAL_RUN,
        enabled=True,
        description="persistent configuration operation",
        minimum_risk=RiskLevel.HIGH,
        approval_required_operations=("persist",),
    )
    executor = ToolExecutor(
        PolicyEngine(settings, audit),
        repos,
        audit,
        adapters={"persistent.tool": StaticToolAdapter()},
        tool_definitions=[definition],
    )

    result = await executor.execute(
        ToolCallRequest(
            task_id=task.id,
            tool_name="persistent.tool",
            capability=Capability.TERMINAL_RUN,
            risk_level=RiskLevel.HIGH,
            input={"operation": "persist"},
        )
    )

    assert result.status == ToolResultStatus.NEEDS_APPROVAL


@pytest.mark.asyncio
async def test_approval_required_operation_carries_its_specific_reason(tmp_path) -> None:
    # docs/HISTORY.md Part 4's concurrent-hardening note: NEEDS_APPROVAL used
    # to lose the tool-specific "why" (a generic "Approve X using Y" summary
    # only). ToolDefinition.approval_reasons restores it - this pins the
    # restored behavior so it can't silently regress again.
    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("persist configuration")
    settings = _settings_with(
        Capability.TERMINAL_RUN,
        requires_approval=False,
        max_risk_level=RiskLevel.CRITICAL,
    )
    settings.approval_policy.require_approval_at_or_above = RiskLevel.CRITICAL
    definition = ToolDefinition(
        name="persistent.tool",
        capability=Capability.TERMINAL_RUN,
        enabled=True,
        description="persistent configuration operation",
        minimum_risk=RiskLevel.HIGH,
        approval_required_operations=("persist",),
        approval_reasons={"persist": "writes a value that survives task completion"},
    )
    executor = ToolExecutor(
        PolicyEngine(settings, audit),
        repos,
        audit,
        adapters={"persistent.tool": StaticToolAdapter()},
        tool_definitions=[definition],
    )

    result = await executor.execute(
        ToolCallRequest(
            task_id=task.id,
            tool_name="persistent.tool",
            capability=Capability.TERMINAL_RUN,
            risk_level=RiskLevel.HIGH,
            input={"operation": "persist"},
        )
    )

    assert result.status == ToolResultStatus.NEEDS_APPROVAL
    approval_id = result.output["approval_id"]
    approval = repos.approvals.get(approval_id)
    assert "writes a value that survives task completion" in approval.summary

    # An operation with no approval_reasons entry still falls back to the
    # generic summary rather than raising or showing "None".
    definition_no_reason = ToolDefinition(
        name="persistent.tool",
        capability=Capability.TERMINAL_RUN,
        enabled=True,
        description="persistent configuration operation",
        minimum_risk=RiskLevel.HIGH,
        approval_required_operations=("persist",),
    )
    executor_no_reason = ToolExecutor(
        PolicyEngine(settings, audit),
        repos,
        audit,
        adapters={"persistent.tool": StaticToolAdapter()},
        tool_definitions=[definition_no_reason],
    )
    result2 = await executor_no_reason.execute(
        ToolCallRequest(
            task_id=task.id,
            tool_name="persistent.tool",
            capability=Capability.TERMINAL_RUN,
            risk_level=RiskLevel.HIGH,
            input={"operation": "persist"},
        )
    )
    approval2 = repos.approvals.get(result2.output["approval_id"])
    assert approval2.summary == "Approve persistent.tool using terminal.run"


@pytest.mark.asyncio
async def test_executor_fails_when_no_adapter_registered(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("t")
    settings = _settings_with(Capability.LLM_GENERATE)
    executor = ToolExecutor(
        PolicyEngine(settings, audit),
        repos,
        audit,
        adapters={},  # nothing registered
    )

    result = await executor.execute(_request(task.id))

    assert result.status == ToolResultStatus.FAILED
    assert result.error_class == ErrorClass.ADAPTER_FAILED
    assert "not registered" in (result.error_message or "")


@pytest.mark.asyncio
async def test_executor_converts_adapter_exception_to_failed(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("t")
    settings = _settings_with(Capability.LLM_GENERATE)
    executor = ToolExecutor(
        PolicyEngine(settings, audit),
        repos,
        audit,
        adapters={"llm": _ExplodingAdapter()},
    )

    result = await executor.execute(_request(task.id))

    assert result.status == ToolResultStatus.FAILED
    assert result.error_class == ErrorClass.ADAPTER_FAILED
    assert "adapter blew up" in (result.error_message or "")


@pytest.mark.asyncio
async def test_executor_rejects_risk_above_policy(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("t")
    # policy caps at LOW; request asks for HIGH
    settings = _settings_with(Capability.LLM_GENERATE, max_risk_level=RiskLevel.LOW)
    executor = ToolExecutor(
        PolicyEngine(settings, audit),
        repos,
        audit,
        adapters={"llm": StaticToolAdapter()},
    )

    result = await executor.execute(_request(task.id, risk=RiskLevel.HIGH))

    assert result.status == ToolResultStatus.DENIED
    assert result.error_class == ErrorClass.POLICY_DENIED


# ---- docs/ROADMAP.md "Proof": declared egress + mechanical verification --

def _egress_definition(operations: tuple[str, ...] = ("fetch",), egress_operations: tuple[str, ...] | None = None) -> ToolDefinition:
    return ToolDefinition(
        name="fetch.tool",
        capability=Capability.LLM_GENERATE,
        enabled=True,
        description="test tool for egress wiring",
        operations=operations,
        default_operation=operations[0],
        operation_egress=egress_operations if egress_operations is not None else operations,
    )


def _tool_request(task_id: str, tool_name: str, operation: str, **extra_input: Any) -> ToolCallRequest:
    return ToolCallRequest(
        task_id=task_id,
        tool_name=tool_name,
        capability=Capability.LLM_GENERATE,
        risk_level=RiskLevel.LOW,
        input={"operation": operation, **extra_input},
    )


@pytest.mark.asyncio
async def test_executor_records_egress_for_a_declared_operation(tmp_path) -> None:
    """A tool needs zero manual record_egress() call of its own - declaring
    the operation in operation_egress is enough, because ToolExecutor reads
    the destination straight out of the adapter's own output.
    """
    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("t")
    settings = _settings_with(Capability.LLM_GENERATE)
    definition = _egress_definition()
    adapter = StaticToolAdapter(output={"url": "https://real-host.example/data"})
    executor = ToolExecutor(
        PolicyEngine(settings, audit),
        repos,
        audit,
        adapters={"fetch.tool": adapter},
        tool_definitions=[definition],
    )

    await executor.execute(_tool_request(task.id, "fetch.tool", "fetch"))

    events = [e for e in repos.audit.list_for_task(task.id) if e.type == AuditEventType.EGRESS_CONTACTED]
    assert len(events) == 1
    assert events[0].payload == {"host": "real-host.example", "tool_name": "fetch.tool"}


@pytest.mark.asyncio
async def test_executor_records_no_egress_for_an_undeclared_operation(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("t")
    settings = _settings_with(Capability.LLM_GENERATE)
    # "fetch" is declared for egress; "peek" is not, even on the same tool.
    definition = _egress_definition(operations=("fetch", "peek"), egress_operations=("fetch",))
    adapter = StaticToolAdapter(output={"url": "https://real-host.example/data"})
    executor = ToolExecutor(
        PolicyEngine(settings, audit),
        repos,
        audit,
        adapters={"fetch.tool": adapter},
        tool_definitions=[definition],
    )

    await executor.execute(_tool_request(task.id, "fetch.tool", "peek"))

    events = [e for e in repos.audit.list_for_task(task.id) if e.type == AuditEventType.EGRESS_CONTACTED]
    assert events == []


@pytest.mark.asyncio
async def test_executor_records_no_egress_for_a_loopback_destination(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("t")
    settings = _settings_with(Capability.LLM_GENERATE)
    definition = _egress_definition()
    adapter = StaticToolAdapter(output={"url": "http://127.0.0.1:11434/api"})
    executor = ToolExecutor(
        PolicyEngine(settings, audit),
        repos,
        audit,
        adapters={"fetch.tool": adapter},
        tool_definitions=[definition],
    )

    await executor.execute(_tool_request(task.id, "fetch.tool", "fetch"))

    events = [e for e in repos.audit.list_for_task(task.id) if e.type == AuditEventType.EGRESS_CONTACTED]
    assert events == []


@pytest.mark.asyncio
async def test_executor_attaches_declared_verification_to_a_succeeded_result(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("t")
    settings = _settings_with(Capability.LLM_GENERATE)

    def verify(request: ToolCallRequest, result: ToolCallResult) -> ToolVerification | None:
        return ToolVerification(checked=3, verified=3, detail="all 3 destinations exist")

    definition = ToolDefinition(
        name="verified.tool",
        capability=Capability.LLM_GENERATE,
        enabled=True,
        description="test tool for verification wiring",
        operations=("apply",),
        default_operation="apply",
        verify=verify,
    )
    executor = ToolExecutor(
        PolicyEngine(settings, audit),
        repos,
        audit,
        adapters={"verified.tool": StaticToolAdapter()},
        tool_definitions=[definition],
    )

    result = await executor.execute(_tool_request(task.id, "verified.tool", "apply"))

    assert result.verification == ToolVerification(checked=3, verified=3, detail="all 3 destinations exist")
    persisted = repos.tool_invocations.list_for_task(task.id)[0]["result"]
    assert persisted["verification"]["verified"] == 3


@pytest.mark.asyncio
async def test_executor_never_calls_verify_on_a_failed_result(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("t")
    settings = _settings_with(Capability.LLM_GENERATE)
    calls: list[ToolCallResult] = []

    def verify(request: ToolCallRequest, result: ToolCallResult) -> ToolVerification | None:
        calls.append(result)
        return ToolVerification(checked=1, verified=1)

    definition = ToolDefinition(
        name="verified.tool",
        capability=Capability.LLM_GENERATE,
        enabled=True,
        description="test tool for verification wiring",
        operations=("apply",),
        default_operation="apply",
        verify=verify,
    )
    executor = ToolExecutor(
        PolicyEngine(settings, audit),
        repos,
        audit,
        adapters={"verified.tool": _ExplodingAdapter()},
        tool_definitions=[definition],
    )

    result = await executor.execute(_tool_request(task.id, "verified.tool", "apply"))

    assert result.status == ToolResultStatus.FAILED
    assert result.verification is None
    assert calls == []
