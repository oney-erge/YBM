"""TaskWorker._record_llm_call (docs/archive/UI_UX_AUDIT.md Phase 14d) - the
receipts that turn the Duration view's inferred "operator thinking" gaps
into measured latency. Uses the same QueueOperator-style fakes as
test_worker_operator_loop.py, extended to also set the new
last_request/last_response_text/last_model/last_started_at/last_latency_ms
fields _record_llm_usage's existing last_usage reading already mirrors.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agent_control.config import AppSettings, CapabilityPolicy
from agent_control.orchestration import StaticToolAdapter, TaskWorker, ToolExecutor
from agent_control.orchestration.auditor import AuditResult
from agent_control.policy import PolicyEngine
from agent_control.schemas import Capability, OperatorAction, OperatorDecision, RiskLevel
from agent_control.tools.registry import ToolDefinition
from helpers import make_repos


class OneShotOperator:
    """Fake OperatorLoopService that returns one decision and sets every
    last_* field a real provider call would set, so _record_llm_call has
    something real to persist."""

    def __init__(self, decision: OperatorDecision, **llm_fields) -> None:
        self.decision = decision
        self.last_usage = llm_fields.get("usage", {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "model": "test-model"})
        self.last_request = llm_fields.get("request", [{"role": "system", "content": "sys"}, {"role": "user", "content": "hello"}])
        self.last_response_text = llm_fields.get("response_text", "hi there")
        self.last_model = llm_fields.get("model", "test-model")
        self.last_started_at = llm_fields.get("started_at", datetime(2026, 1, 1, tzinfo=timezone.utc))
        self.last_latency_ms = llm_fields.get("latency_ms", 123.4)

    async def decide(self, objective, config_context, history, *, memory_context="", prefer_major=False):
        return self.decision


class OneShotAuditor:
    def __init__(self, result: AuditResult, **llm_fields) -> None:
        self.result = result
        self.last_usage = llm_fields.get("usage", {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5, "model": "test-model"})
        self.last_request = llm_fields.get("request", [{"role": "system", "content": "audit sys"}, {"role": "user", "content": "check this"}])
        self.last_response_text = llm_fields.get("response_text", "sufficient")
        self.last_model = llm_fields.get("model", "test-model")
        self.last_started_at = llm_fields.get("started_at", datetime(2026, 1, 1, tzinfo=timezone.utc))
        self.last_latency_ms = llm_fields.get("latency_ms", 50.0)

    async def audit(self, objective, raw_output, *, original_message=None, deliverable_evidence="", response_context=None):
        return self.result


def _settings() -> AppSettings:
    return AppSettings(
        _env_file=None,
        capabilities={
            Capability.LLM_GENERATE: CapabilityPolicy(enabled=True, requires_approval=False, max_risk_level=RiskLevel.LOW)
        },
    )


def _executor(settings, audit, repos, *, tool_name="llm", output=None) -> ToolExecutor:
    return ToolExecutor(
        PolicyEngine(settings, audit),
        repos,
        audit,
        adapters={tool_name: StaticToolAdapter(output or {"answer": "42"})},
        tool_definitions={
            tool_name: ToolDefinition(name=tool_name, capability=Capability.LLM_GENERATE, enabled=True, description="test tool"),
        },
    )


@pytest.mark.asyncio
async def test_operator_decide_call_is_persisted_with_real_fields(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("trivial question")
    settings = _settings()
    executor = _executor(settings, audit, repos)
    operator = OneShotOperator(OperatorDecision(action=OperatorAction.DONE, final_answer="answer"))
    worker = TaskWorker(repos, audit, executor=executor, operator=operator)

    completed = await worker.process_task(task.id)

    calls = repos.llm_calls.list_for_task(completed.id)
    assert len(calls) == 1
    call = calls[0]
    assert call["source"] == "operator"
    assert call["model"] == "test-model"
    assert call["step_index"] == 0
    assert call["messages"] == [{"role": "system", "content": "sys"}, {"role": "user", "content": "hello"}]
    assert call["response_text"] == "hi there"
    assert call["prompt_tokens"] == 10
    assert call["completion_tokens"] == 5
    assert call["total_tokens"] == 15
    assert call["latency_ms"] == 123.4


@pytest.mark.asyncio
async def test_auditor_call_is_persisted_as_a_separate_source(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("summarize this file")
    settings = _settings()
    # filesystem.manage is a real CONTENT_TOOLS member (auditor.py) - the
    # auditor only runs on a `done` that follows one.
    executor = _executor(settings, audit, repos, tool_name="filesystem.manage", output={"text": "the file says hello"})
    # A real read carries operation/path, and the SOURCE_CONTENT postcondition
    # "summarize this file" derives now reads exactly those off the history
    # (fulfillment._source_content_satisfied). An empty tool_input would leave
    # the objective unfulfilled and replan instead of completing.
    operator = OneShotOperator(
        OperatorDecision(
            action=OperatorAction.CALL_TOOL,
            tool_name="filesystem.manage",
            tool_input={"operation": "read_file", "path": "notes.txt"},
            risk_level=RiskLevel.LOW,
        )
    )
    auditor = OneShotAuditor(AuditResult(sufficient=True, answer="grounded answer"))
    # audit_min_tool_calls=1: this asserts an Auditor call is persisted, on a
    # single-tool task that the production default (2) skips.
    worker = TaskWorker(repos, audit, executor=executor, operator=operator, auditor=auditor,
                        audit_min_tool_calls=1)

    running = await worker.process_task(task.id)

    # Step past the tool call, then let a second decide() call declare done.
    # past the tool call first, then let a second decide() call declare done.
    operator.decision = OperatorDecision(action=OperatorAction.DONE, final_answer="grounded answer")
    completed = await worker.process_task(running.id)

    assert completed.status.value == "completed"
    calls = repos.llm_calls.list_for_task(completed.id)
    sources = {call["source"] for call in calls}
    assert "operator" in sources
    assert "auditor" in sources
    auditor_call = next(call for call in calls if call["source"] == "auditor")
    assert auditor_call["response_text"] == "sufficient"


@pytest.mark.asyncio
async def test_llm_call_persistence_disabled_by_config_flag(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("trivial question")
    settings = _settings()
    executor = _executor(settings, audit, repos)
    operator = OneShotOperator(OperatorDecision(action=OperatorAction.DONE, final_answer="answer"))
    worker = TaskWorker(repos, audit, executor=executor, operator=operator, persist_llm_calls=False)

    completed = await worker.process_task(task.id)

    assert repos.llm_calls.list_for_task(completed.id) == []


@pytest.mark.asyncio
async def test_llm_call_with_no_request_captured_is_not_persisted(tmp_path) -> None:
    """A fake (or real) operator that never set last_request - e.g. because
    the provider call failed before reaching that point - must not produce
    a fabricated row. This is the same shape QueueOperator (used by every
    other operator-loop test) already has, confirming those tests never
    accidentally wrote to llm_calls."""
    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("trivial question")
    settings = _settings()
    executor = _executor(settings, audit, repos)
    operator = OneShotOperator(OperatorDecision(action=OperatorAction.DONE, final_answer="answer"), request=None)
    worker = TaskWorker(repos, audit, executor=executor, operator=operator)

    completed = await worker.process_task(task.id)

    assert repos.llm_calls.list_for_task(completed.id) == []


@pytest.mark.asyncio
async def test_llm_call_messages_are_redacted_and_capped(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("trivial question")
    settings = _settings()
    executor = _executor(settings, audit, repos)
    operator = OneShotOperator(
        OperatorDecision(action=OperatorAction.DONE, final_answer="answer"),
        request=[{"role": "user", "content": "hello", "api_key": "sk-should-not-appear"}],
        response_text="x" * 50,
    )
    worker = TaskWorker(repos, audit, executor=executor, operator=operator, llm_call_max_chars=10)

    completed = await worker.process_task(task.id)

    call = repos.llm_calls.list_for_task(completed.id)[0]
    assert call["messages"][0]["api_key"] == "***"
    assert call["response_text"] == f"{'x' * 10}...[truncated]"
