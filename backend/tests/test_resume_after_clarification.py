"""A task whose question was answered goes back to work: its first tool call after the answer runs.

The answer puts the task back to RECEIVED (clarification.resume_clarifying_task). The tick only moved a task to RUNNING the first time
it started, and admitting a call (storage/repositories.admit_operator_call, since the lifecycle controls) needs RUNNING, so the first
call after an answer was never admitted and the tick returned without a history entry. The worker then asked the model again, every
poll, for as long as it was left running: a live run made 281 model calls on one task and ran no tool.
"""

from __future__ import annotations

import pytest

from agent_control.clarification import resume_clarifying_task
from agent_control.config import AppSettings, CapabilityPolicy
from agent_control.orchestration import StaticToolAdapter, TaskWorker, ToolExecutor
from agent_control.policy import PolicyEngine
from agent_control.schemas import (
    Capability,
    OperatorAction,
    OperatorDecision,
    RiskLevel,
    TaskStatus,
    utc_now,
)
from agent_control.tools.registry import ToolDefinition
from helpers import make_repos


class ScriptedOperator:
    def __init__(self, decisions: list[OperatorDecision]) -> None:
        self.decisions = list(decisions)
        self.calls = 0
        self.last_usage = None
        self.last_fallback_used = False

    async def decide(self, *args, **kwargs) -> OperatorDecision:
        self.calls += 1
        return self.decisions.pop(0)


def _worker(repos, audit, operator, adapter) -> TaskWorker:
    settings = AppSettings(
        _env_file=None,
        capabilities={Capability.LLM_GENERATE: CapabilityPolicy(enabled=True, requires_approval=False, max_risk_level=RiskLevel.LOW)},
    )
    executor = ToolExecutor(
        PolicyEngine(settings, audit), repos, audit, adapters={"llm": adapter},
        tool_definitions={"llm": ToolDefinition(name="llm", capability=Capability.LLM_GENERATE, enabled=True, description="test tool")},
    )
    return TaskWorker(repos, audit, executor=executor, operator=operator)


def _answered_question(repos, audit):
    task = repos.tasks.create(
        "Get the page titles.",
        metadata={"operator_loop": True, "operator_history": [], "clarifying_question": "The search gave snippets. Read the pages instead?"},
    )
    repos.tasks.update_status(task.id, TaskStatus.CLARIFYING)
    clarifying = repos.tasks.get(task.id)
    resumed = resume_clarifying_task(
        repos, audit, clarifying, text="Yes, go ahead", actor="user", message_id="m1", received_at=utc_now(),
    ).task
    assert resumed.status == TaskStatus.RECEIVED
    return resumed


@pytest.mark.asyncio
async def test_the_first_tool_call_after_an_answered_question_runs(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    task = _answered_question(repos, audit)
    adapter = StaticToolAdapter({"answer": "ok"})
    operator = ScriptedOperator([
        OperatorDecision(action=OperatorAction.CALL_TOOL, tool_name="llm", tool_input={}, risk_level=RiskLevel.LOW),
        OperatorDecision(action=OperatorAction.DONE, final_answer="done"),
    ])
    worker = _worker(repos, audit, operator, adapter)

    after_call = await worker.process_task(task.id)

    assert len(adapter.requests) == 1, "the call was never admitted, so the model would be asked again on every poll"
    assert after_call.status == TaskStatus.RUNNING
    assert [entry["status"] for entry in after_call.metadata["operator_history"]] == ["succeeded"]
    assert (await worker.process_task(task.id)).status == TaskStatus.COMPLETED
    assert operator.calls == 2


@pytest.mark.asyncio
async def test_a_batch_of_calls_after_an_answered_question_runs(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    task = _answered_question(repos, audit)
    adapter = StaticToolAdapter({"answer": "ok"})
    operator = ScriptedOperator([
        OperatorDecision(
            action=OperatorAction.CALL_TOOLS_PARALLEL,
            parallel_calls=[
                {"tool_name": "llm", "tool_input": {"n": 1}, "risk_level": "low"},
                {"tool_name": "llm", "tool_input": {"n": 2}, "risk_level": "low"},
            ],
        ),
    ])

    after_batch = await _worker(repos, audit, operator, adapter).process_task(task.id)

    assert len(adapter.requests) == 2
    assert after_batch.status == TaskStatus.RUNNING
