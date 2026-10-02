"""Stable step_id linking (docs/archive/UI_UX_AUDIT.md Phase 14e) - the real
parent-child link Graph v2 is built on, replacing inferred structure from
`origin` tags alone. One id per observe/decide/act tick, stamped onto that
tick's operator_history entry, its LLM call, and any ToolCallRequest.parent_step_id
it leads to - which then flows into tool_invocations.request and (for a
gated call) approvals.action_payload "for free", since both already store
the full request dump.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agent_control.config import AppSettings, CapabilityPolicy
from agent_control.orchestration import StaticToolAdapter, TaskWorker, ToolExecutor
from agent_control.policy import PolicyEngine
from agent_control.schemas import ApprovalStatus, Capability, OperatorAction, OperatorDecision, RiskLevel, TaskStatus
from agent_control.tools.registry import ToolDefinition
from helpers import make_repos


class RecordingOperator:
    """Fake OperatorLoopService that returns decisions in order and always
    sets the last_* fields a real provider call would, so every tick has a
    real LLM call to persist and link."""

    def __init__(self, decisions: list[OperatorDecision]) -> None:
        self.decisions = list(decisions)
        self.calls = 0
        self.last_usage = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2, "model": "test-model"}
        self.last_request = [{"role": "user", "content": "hello"}]
        self.last_response_text = "ok"
        self.last_model = "test-model"
        self.last_started_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.last_latency_ms = 10.0

    async def decide(self, objective, config_context, history, *, memory_context="", prefer_major=False):
        self.calls += 1
        return self.decisions.pop(0)


def _approval_settings() -> AppSettings:
    return AppSettings(
        _env_file=None,
        capabilities={
            Capability.LLM_GENERATE: CapabilityPolicy(enabled=True, requires_approval=True, max_risk_level=RiskLevel.LOW)
        },
    )


def _executor(settings, audit, repos) -> ToolExecutor:
    return ToolExecutor(
        PolicyEngine(settings, audit),
        repos,
        audit,
        adapters={"llm": StaticToolAdapter({"answer": "42"})},
        tool_definitions={
            "llm": ToolDefinition(name="llm", capability=Capability.LLM_GENERATE, enabled=True, description="test tool"),
        },
    )


@pytest.mark.asyncio
async def test_step_id_links_operator_history_tool_invocation_and_llm_call(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("trivial question")
    settings = AppSettings(
        _env_file=None,
        capabilities={Capability.LLM_GENERATE: CapabilityPolicy(enabled=True, requires_approval=False, max_risk_level=RiskLevel.LOW)},
    )
    executor = _executor(settings, audit, repos)
    operator = RecordingOperator([
        OperatorDecision(action=OperatorAction.CALL_TOOL, tool_name="llm", tool_input={}, risk_level=RiskLevel.LOW),
    ])
    worker = TaskWorker(repos, audit, executor=executor, operator=operator)

    result = await worker.process_task(task.id)

    history_entry = result.metadata["operator_history"][0]
    step_id = history_entry["step_id"]
    assert step_id

    invocations = repos.tool_invocations.list_for_task(result.id)
    assert invocations[0]["request"]["parent_step_id"] == step_id

    llm_calls = repos.llm_calls.list_for_task(result.id)
    assert llm_calls[0]["step_id"] == step_id


@pytest.mark.asyncio
async def test_step_id_survives_an_approval_wait_into_the_resumed_call(tmp_path) -> None:
    """The step_id stamped when a call first needs approval must still be
    on the ToolCallRequest that actually executes once granted - a second,
    later request object, not the one that triggered the approval - and on
    the ApprovalRequest itself (via action_payload, which already carries
    the full request dump), so the whole wait is one step, not two."""
    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("needs approval")
    executor = _executor(_approval_settings(), audit, repos)
    operator = RecordingOperator([
        OperatorDecision(action=OperatorAction.CALL_TOOL, tool_name="llm", tool_input={"q": "?"}, risk_level=RiskLevel.LOW),
        OperatorDecision(action=OperatorAction.DONE, final_answer="42"),
    ])
    worker = TaskWorker(repos, audit, executor=executor, operator=operator)

    awaiting = await worker.process_task(task.id)
    assert awaiting.status == TaskStatus.AWAITING_APPROVAL
    step_id = awaiting.metadata["operator_pending_call"]["step_id"]
    assert step_id

    approvals = repos.approvals.list_for_task(task.id)
    assert approvals[0].action_payload["parent_step_id"] == step_id

    repos.approvals.set_status(approvals[0].id, ApprovalStatus.APPROVED)
    resumed = await worker.process_task(awaiting.id)

    assert resumed.status == TaskStatus.RUNNING
    resumed_entry = resumed.metadata["operator_history"][0]
    assert resumed_entry["step_id"] == step_id

    invocations = repos.tool_invocations.list_for_task(resumed.id)
    # Two rows: the original NEEDS_APPROVAL attempt and the resumed, granted
    # one - different request ids, same parent_step_id.
    assert len(invocations) == 2
    assert {inv["request"]["parent_step_id"] for inv in invocations} == {step_id}
