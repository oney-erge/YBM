"""A batch the person approved must run the calls they approved.

The executor fills in a tool's schema defaults before it checks policy, so the call that runs is the call
with those defaults. A batch approval used to bind the raw call, so for any tool whose schema adds a default
(browser form steps do) the approved batch was refused on replay as "approval_invalid_or_mismatched" and the
person's approvals were wasted.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel, Field

from agent_control.config import AppSettings, CapabilityPolicy
from agent_control.orchestration import TaskWorker, ToolExecutor
from agent_control.policy import PolicyEngine
from agent_control.schemas import (
    ApprovalStatus, Capability, OperatorAction, OperatorDecision, ParallelToolCall, RiskLevel, TaskStatus,
)
from agent_control.tools.registry import ToolDefinition
from helpers import make_repos
from test_worker_parallel_and_delegate import QueueOperator, RecordingAdapter


class FillStep(BaseModel):
    """Like a browser form step: the schema adds a default the model never wrote."""

    operation: str = "fill_form_step"
    field: str
    value: str
    fields: dict[str, str] = Field(default_factory=dict)
    wait_seconds: float = 1.0


@pytest.mark.asyncio
async def test_an_approved_batch_runs_when_the_schema_adds_defaults(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("sign me up")
    settings = AppSettings(
        _env_file=None,
        capabilities={Capability.TERMINAL_RUN: CapabilityPolicy(enabled=True, requires_approval=True, max_risk_level=RiskLevel.HIGH)},
    )
    adapter = RecordingAdapter("form", delay=0.0)
    executor = ToolExecutor(
        PolicyEngine(settings, audit), repos, audit, adapters={"form": adapter},
        tool_definitions={"form": ToolDefinition(
            name="form", capability=Capability.TERMINAL_RUN, enabled=True, description="x", input_schema=FillStep,
        )},
    )
    calls = [
        ParallelToolCall(tool_name="form", tool_input={"operation": "fill_form_step", "field": "Name", "value": "Oney"}, risk_level=RiskLevel.HIGH),
        ParallelToolCall(tool_name="form", tool_input={"operation": "fill_form_step", "field": "Size", "value": "M"}, risk_level=RiskLevel.HIGH),
    ]
    worker = TaskWorker(repos, audit, executor=executor, operator=QueueOperator([
        OperatorDecision(action=OperatorAction.CALL_TOOLS_PARALLEL, parallel_calls=calls),
    ]))

    paused = await worker.process_task(task.id)
    assert paused.status == TaskStatus.AWAITING_APPROVAL

    batch = repos.approvals.list_pending()[0]
    for entry in batch.action_payload["batch"]:
        repos.approvals.decide_pending(entry["approval_id"], ApprovalStatus.APPROVED)
    repos.approvals.decide_pending(batch.id, ApprovalStatus.APPROVED)

    resumed = await worker.process_task(task.id)
    statuses = [entry["status"] for entry in resumed.metadata["operator_history"]]
    errors = [entry.get("error") for entry in resumed.metadata["operator_history"]]
    assert statuses == ["succeeded", "succeeded"], errors
    assert len(adapter.started_at) == 2
