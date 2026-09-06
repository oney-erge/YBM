"""Unit tests for the Operator loop path in orchestration/worker.py - the
sole execution path (docs/HISTORY.md P3 §2.2). Uses the same
StaticToolAdapter/PolicyEngine harness as test_worker.py, but drives
TaskWorker with a scripted decision sequence instead of a persisted plan.
See orchestration/operator.py's module docstring for the design.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_control.config import AppSettings, CapabilityPolicy
from agent_control.orchestration import StaticToolAdapter, TaskWorker, ToolExecutor
from agent_control.orchestration.auditor import AuditResult
from agent_control.orchestration.signals import requeue_after_approval_decision
from agent_control.orchestration.worker import (
    _canonical_operator_tool_call,
    _coding_agent_input_with_task_defaults,
    _coding_agent_input_with_explicit_workspace,
    _effective_operator_risk,
    _filesystem_search_input_with_content_intent,
    _ground_operator_final_answer,
    _clarification_recovery_reason,
    _operator_audit_evidence,
    _ordered_artifact_delivery_call,
    _required_named_coding_agent_call,
    _satisfied_task_does_not_need_clarification,
    _stale_read_recovery_call,
    _unsupported_write_claim,
)
from agent_control.policy import PolicyEngine
from agent_control.recovery import RetryPolicy
from agent_control.schemas import (
    ApprovalStatus,
    Capability,
    ErrorClass,
    OperatorAction,
    OperatorDecision,
    RiskLevel,
    TaskRecord,
    TaskStatus,
    ToolCallRequest,
    ToolCallResult,
    ToolResultStatus,
)
from agent_control.tools.registry import ToolDefinition
from helpers import make_repos


class RateLimitedAdapter:
    async def execute(self, request: ToolCallRequest) -> ToolCallResult:
        return ToolCallResult(
            request_id=request.id,
            status=ToolResultStatus.RATE_LIMITED,
            error_class=ErrorClass.RATE_LIMITED,
            error_message="rate limited, try later",
        )


class TimedOutAdapter:
    async def execute(self, request: ToolCallRequest) -> ToolCallResult:
        return ToolCallResult(
            request_id=request.id,
            status=ToolResultStatus.TIMEOUT,
            error_message="the call did not return in time",
        )


class UsageLimitedAdapter:
    async def execute(self, request: ToolCallRequest) -> ToolCallResult:
        return ToolCallResult(
            request_id=request.id,
            status=ToolResultStatus.RATE_LIMITED,
            error_class=ErrorClass.USAGE_LIMITED,
            error_message="quota exhausted",
        )


class FailsOnceThenSucceedsAdapter:
    """Rate-limited on the first call, succeeds on every call after - models
    a real backoff-then-retry-and-succeed sequence."""

    def __init__(self, output: dict | None = None) -> None:
        self.output = output or {"ok": True}
        self.calls = 0

    async def execute(self, request: ToolCallRequest) -> ToolCallResult:
        self.calls += 1
        if self.calls == 1:
            return ToolCallResult(
                request_id=request.id,
                status=ToolResultStatus.RATE_LIMITED,
                error_class=ErrorClass.RATE_LIMITED,
                error_message="rate limited, try later",
            )
        return ToolCallResult(request_id=request.id, status=ToolResultStatus.SUCCEEDED, output=self.output)


class BackgroundSessionAdapter:
    """Mimics coding.agent starting a session that finishes asynchronously -
    the initial call returns immediately with status=running."""

    async def execute(self, request: ToolCallRequest) -> ToolCallResult:
        return ToolCallResult(
            request_id=request.id,
            status=ToolResultStatus.SUCCEEDED,
            output={"status": "running", "session_id": "sess-1", "provider": "codex"},
        )




class QueueOperator:
    """Fake OperatorLoopService - returns decisions in order, one per decide() call.

    `usages`, if given, is a same-length list of usage dicts (or None) - one
    per decide() call, mirroring how the real OperatorLoopService sets
    self.last_usage after each call (docs/HISTORY.md Part 4 T1.4).
    """

    def __init__(self, decisions: list[OperatorDecision], usages: list[dict | None] | None = None) -> None:
        self.decisions = list(decisions)
        self._usages = list(usages) if usages is not None else None
        self.calls = 0
        self.last_usage: dict | None = None
        # docs/HISTORY.md Part 4 T2.6: one entry per decide() call, so tests
        # can assert exactly when the caller asked for the stronger model.
        self.prefer_major_calls: list[bool] = []

    async def decide(self, objective, config_context, history, *, memory_context="", prefer_major=False):
        self.calls += 1
        self.prefer_major_calls.append(prefer_major)
        if self._usages is not None:
            self.last_usage = self._usages.pop(0)
        return self.decisions.pop(0)


class QueueAuditor:
    """Fake AuditorService - returns results in order, one per audit() call."""

    def __init__(self, results: list[AuditResult], usages: list[dict | None] | None = None) -> None:
        self.results = list(results)
        self._usages = list(usages) if usages is not None else None
        self.calls: list[tuple[str, str]] = []
        self.response_contexts: list[str | None] = []
        self.last_usage: dict | None = None

    async def audit(self, objective, raw_output, *, original_message=None, deliverable_evidence="", response_context=None):
        self.calls.append((objective, raw_output))
        self.response_contexts.append(response_context)
        if self._usages is not None:
            self.last_usage = self._usages.pop(0)
        return self.results.pop(0)


def test_ground_operator_final_answer_preserves_read_file_identity() -> None:
    history = [{
        "tool_name": "filesystem.manage",
        "input": {"operation": "read_file", "path": r"C:\evidence\resume-notes.txt"},
        "status": "succeeded",
        "output_summary": "career evidence",
    }]

    grounded = _ground_operator_final_answer("career evidence", history)

    assert grounded == "career evidence\n\nSource file: C:\\evidence\\resume-notes.txt"
    assert _ground_operator_final_answer("Read resume-notes.txt: career evidence", history) == (
        "Read resume-notes.txt: career evidence"
    )


def _executor(settings, audit, repos, *, tool_name="llm", output=None) -> ToolExecutor:
    return ToolExecutor(
        PolicyEngine(settings, audit),
        repos,
        audit,
        adapters={tool_name: StaticToolAdapter(output)},
        tool_definitions={
            tool_name: ToolDefinition(
                name=tool_name, capability=Capability.LLM_GENERATE, enabled=True, description="test tool",
            )
        },
    )


def _settings() -> AppSettings:
    return AppSettings(
        _env_file=None,
        capabilities={
            Capability.LLM_GENERATE: CapabilityPolicy(enabled=True, requires_approval=False, max_risk_level=RiskLevel.LOW)
        },
    )


def _executor_with_adapter(settings, audit, repos, adapter, *, tool_name="llm") -> ToolExecutor:
    return ToolExecutor(
        PolicyEngine(settings, audit),
        repos,
        audit,
        adapters={tool_name: adapter},
        tool_definitions={
            tool_name: ToolDefinition(
                name=tool_name, capability=Capability.LLM_GENERATE, enabled=True, description="test tool",
            )
        },
    )


@pytest.mark.asyncio
async def test_operator_loop_calls_tool_then_completes(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("Answer a question")
    settings = _settings()
    executor = _executor(settings, audit, repos, output={"answer": "42"})
    operator = QueueOperator([
        OperatorDecision(action=OperatorAction.CALL_TOOL, tool_name="llm", tool_input={}, risk_level=RiskLevel.LOW),
        OperatorDecision(action=OperatorAction.DONE, final_answer="The answer is 42."),
    ])
    worker = TaskWorker(repos, audit, executor=executor, operator=operator)

    running = await worker.process_task(task.id)
    completed = await worker.process_task(running.id)

    assert running.status == TaskStatus.RUNNING
    assert running.metadata["operator_loop"] is True
    assert len(running.metadata["operator_history"]) == 1
    assert running.metadata["operator_history"][0]["tool_name"] == "llm"
    assert running.metadata["operator_history"][0]["status"] == "succeeded"
    assert completed.status == TaskStatus.COMPLETED
    assert completed.metadata["synthesized_answer"] == "The answer is 42."


@pytest.mark.asyncio
async def test_operator_loop_done_on_first_step_needs_no_tool(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("trivial question")
    settings = _settings()
    executor = _executor(settings, audit, repos)
    operator = QueueOperator([OperatorDecision(action=OperatorAction.DONE, final_answer="answer")])
    worker = TaskWorker(repos, audit, executor=executor, operator=operator)

    completed = await worker.process_task(task.id)

    assert completed.status == TaskStatus.COMPLETED
    assert completed.metadata["synthesized_answer"] == "answer"


@pytest.mark.asyncio
async def test_operator_loop_ask_user_transitions_to_clarifying(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("ambiguous request")
    settings = _settings()
    executor = _executor(settings, audit, repos)
    operator = QueueOperator([OperatorDecision(action=OperatorAction.ASK_USER, question="Which folder?")])
    worker = TaskWorker(repos, audit, executor=executor, operator=operator)

    result = await worker.process_task(task.id)

    assert result.status == TaskStatus.CLARIFYING
    assert result.metadata["clarifying_question"] == "Which folder?"


@pytest.mark.asyncio
async def test_operator_loop_blocked_action_transitions_to_blocked(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("impossible request")
    settings = _settings()
    executor = _executor(settings, audit, repos)
    operator = QueueOperator([OperatorDecision(action=OperatorAction.BLOCKED, reason="no tool can do this")])
    worker = TaskWorker(repos, audit, executor=executor, operator=operator)

    result = await worker.process_task(task.id)

    assert result.status == TaskStatus.BLOCKED


@pytest.mark.asyncio
async def test_operator_loop_exhausts_step_budget(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("endless task")
    settings = _settings()
    executor = _executor(settings, audit, repos)
    # Always calls the tool, never finishes - should hit the step budget.
    operator = QueueOperator([
        OperatorDecision(action=OperatorAction.CALL_TOOL, tool_name="llm", tool_input={}, risk_level=RiskLevel.LOW)
        for _ in range(10)
    ])
    worker = TaskWorker(repos, audit, executor=executor, operator=operator, operator_max_steps=3)

    result = task
    for _ in range(6):
        result = await worker.process_task(result.id)
        if result.status not in {TaskStatus.RUNNING}:
            break

    assert result.status == TaskStatus.BLOCKED
    assert len(result.metadata["operator_history"]) == 3
    assert "Blocked after the bounded 3-step operator budget" in result.metadata["last_worker_error"]
    audit_events = repos.audit.list_for_task(task.id)
    assert any(
        event.payload.get("error") == "operator_step_budget_exhausted"
        for event in audit_events
        if event.type == "error"
    )


@pytest.mark.asyncio
async def test_operator_step_budget_completes_when_nonempty_fulfillment_contract_is_satisfied(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("Create an adapter proposal for stock quotes and keep it cached")
    settings = _settings()
    executor = _executor(
        settings,
        audit,
        repos,
        tool_name="llm",
        output={"adapter_dir": "/tmp/adapters/stock_quotes", "adapter_name": "stock_quotes"},
    )
    operator = QueueOperator(
        [OperatorDecision(action=OperatorAction.CALL_TOOL, tool_name="llm", tool_input={}, risk_level=RiskLevel.LOW)]
    )
    worker = TaskWorker(repos, audit, executor=executor, operator=operator, operator_max_steps=1)

    running = await worker.process_task(task.id)
    completed = await worker.process_task(running.id)

    assert completed.status == TaskStatus.COMPLETED
    assert completed.metadata["operator_step_budget_completed_after_fulfillment"] is True
    assert "/tmp/adapters/stock_quotes" in completed.metadata["synthesized_answer"]


@pytest.mark.asyncio
async def test_operator_step_budget_allows_targeted_fulfillment_repair(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("Open the page and send me a screenshot")
    settings = _settings()

    class BrowserThenDeliveryAdapter:
        async def execute(self, request: ToolCallRequest) -> ToolCallResult:
            if request.tool_name == "browser.open":
                output = {
                    "browser_url": "https://example.test",
                    "screenshot_path": "/tmp/page.png",
                }
            else:
                output = {
                    "delivered": True,
                    "operation": "send_screenshot",
                    "path": "/tmp/page.png",
                    "delivery_method": "telegram.sendPhoto",
                }
            return ToolCallResult(
                request_id=request.id,
                status=ToolResultStatus.SUCCEEDED,
                output=output,
            )

    adapter = BrowserThenDeliveryAdapter()
    executor = ToolExecutor(
        PolicyEngine(settings, audit),
        repos,
        audit,
        adapters={"browser.open": adapter, "artifact.deliver": adapter},
        tool_definitions={
            name: ToolDefinition(
                name=name,
                capability=Capability.LLM_GENERATE,
                enabled=True,
                description="test tool",
            )
            for name in ("browser.open", "artifact.deliver")
        },
    )
    operator = QueueOperator([
        OperatorDecision(
            action=OperatorAction.CALL_TOOL,
            tool_name="browser.open",
            tool_input={"operation": "screenshot"},
            risk_level=RiskLevel.LOW,
        ),
        OperatorDecision(
            action=OperatorAction.CALL_TOOL,
            tool_name="artifact.deliver",
            tool_input={"operation": "send_screenshot"},
            risk_level=RiskLevel.LOW,
        ),
    ])
    worker = TaskWorker(
        repos,
        audit,
        executor=executor,
        operator=operator,
        operator_max_steps=1,
    )

    after_browser = await worker.process_task(task.id)
    repair_requested = await worker.process_task(after_browser.id)
    after_delivery = await worker.process_task(repair_requested.id)
    completed = await worker.process_task(after_delivery.id)

    assert repair_requested.status == TaskStatus.RUNNING
    assert repair_requested.metadata["operator_history"][-1]["tool_name"] == "_fulfillment_check"
    assert "operation=send_screenshot" in repair_requested.metadata["operator_history"][-1]["error"]
    assert completed.status == TaskStatus.COMPLETED
    real_calls = [
        entry for entry in completed.metadata["operator_history"]
        if not entry["tool_name"].startswith("_")
    ]
    assert [entry["tool_name"] for entry in real_calls] == ["browser.open", "artifact.deliver"]


@pytest.mark.asyncio
async def test_operator_loop_blocks_repeated_successful_observations(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("find a download link")
    settings = _settings()
    executor = _executor(settings, audit, repos, output={"summary": "same page content"})
    operator = QueueOperator([
        OperatorDecision(
            action=OperatorAction.CALL_TOOL,
            tool_name="llm",
            tool_input={"operation": "summarize_page", "objective": f"attempt {index}"},
            risk_level=RiskLevel.LOW,
        )
        for index in range(3)
    ])
    worker = TaskWorker(repos, audit, executor=executor, operator=operator, operator_max_steps=8)

    result = task
    for _ in range(3):
        result = await worker.process_task(result.id)

    assert result.status == TaskStatus.BLOCKED
    assert "repeated llm:summarize_page calls returned the same result" in result.metadata["last_worker_error"]
    events = repos.audit.list_for_task(result.id)
    assert any(event.payload.get("error") == "operator_repeated_no_progress" for event in events)


@pytest.mark.asyncio
async def test_gap_check_entries_do_not_consume_the_tool_call_budget(tmp_path) -> None:
    """Regression guard (docs/HISTORY.md §3.1): the fulfillment/audit gap paths
    append check pseudo-entries to operator_history so the next decide() sees
    why `done` was rejected. Those are bookkeeping - counting them against
    operator_max_steps meant every gap stole a slot from the tool calls the
    model needs to close that gap, and a task could exhaust its whole budget
    having called zero tools."""
    repos, audit = make_repos(tmp_path)
    # Not "summarize the report" - fulfillment.py's SOURCE_CONTENT trigger
    # matches that wording and grants the operator extra targeted-repair
    # slots on top of operator_max_steps (see
    # test_operator_step_budget_allows_targeted_fulfillment_repair), which
    # would conflate that budget-extension feature with the one under test
    # here: that CHECK pseudo-entries themselves are excluded from the count.
    task = repos.tasks.create("endless task")
    settings = _settings()
    executor = _executor(settings, audit, repos, tool_name="filesystem.manage", output={"text": "content"})

    class GapThenOkAuditor:
        def __init__(self) -> None:
            self.calls = 0
        async def audit(self, objective, raw_output, *, original_message=None, deliverable_evidence="", response_context=None):
            self.calls += 1
            if self.calls == 1:
                return AuditResult(sufficient=False, reason="need more")
            return AuditResult(sufficient=True, answer="grounded")

    class AlwaysCallTool:
        def __init__(self, prefix) -> None:
            self.prefix = list(prefix)
        async def decide(self, objective, config_context, history, *, memory_context="", prefer_major=False):
            if self.prefix:
                return self.prefix.pop(0)
            return OperatorDecision(
                action=OperatorAction.CALL_TOOL, tool_name="filesystem.manage",
                tool_input={}, risk_level=RiskLevel.LOW,
            )

    operator = AlwaysCallTool([
        OperatorDecision(action=OperatorAction.CALL_TOOL, tool_name="filesystem.manage", tool_input={}, risk_level=RiskLevel.LOW),
        OperatorDecision(action=OperatorAction.DONE, final_answer="early"),
    ])
    worker = TaskWorker(
        repos, audit, executor=executor, operator=operator,
        auditor=GapThenOkAuditor(), operator_max_steps=3, audit_min_tool_calls=1,
    )

    result = task
    for _ in range(12):
        result = await worker.process_task(result.id)
        if result.status != TaskStatus.RUNNING:
            break

    history = result.metadata["operator_history"]
    real = [h for h in history if not str(h.get("tool_name") or "").startswith("_")]
    pseudo = [h for h in history if str(h.get("tool_name") or "").startswith("_")]
    assert pseudo, "expected an audit-gap check entry in this scenario"
    # The budget bought 3 REAL tool calls; the check row did not steal one.
    assert len(real) == 3


@pytest.mark.asyncio
async def test_auditor_receives_full_output_not_the_truncated_history_summary(tmp_path) -> None:
    """Regression guard (docs/HISTORY.md §3.2): history entries store a
    2000-char display summary. The Auditor judges count/section sufficiency,
    so handing it a truncation produces false INSUFFICIENT verdicts on exactly
    the long-content objectives it exists for."""
    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("list every line")
    settings = _settings()
    long_text = "EPISODE-LINE " * 900  # ~11.7k chars, well past the 2000 summary cut
    executor = _executor(settings, audit, repos, tool_name="filesystem.manage", output={"text": long_text})

    class RecordingAuditor:
        def __init__(self) -> None:
            self.seen: list[str] = []
        async def audit(self, objective, raw_output, *, original_message=None, deliverable_evidence="", response_context=None):
            self.seen.append(raw_output)
            return AuditResult(sufficient=True, answer="ok")

    auditor = RecordingAuditor()
    operator = QueueOperator([
        OperatorDecision(action=OperatorAction.CALL_TOOL, tool_name="filesystem.manage", tool_input={}, risk_level=RiskLevel.LOW),
        OperatorDecision(action=OperatorAction.DONE, final_answer="done"),
    ])
    # audit_min_tool_calls=1: these exercise the Auditor on single-tool
    # tasks, which the production default (2) deliberately skips.
    worker = TaskWorker(repos, audit, executor=executor, operator=operator, auditor=auditor,
                        audit_min_tool_calls=1)

    running = await worker.process_task(task.id)
    await worker.process_task(running.id)

    assert auditor.seen, "auditor should have run"
    assert len(auditor.seen[0]) > 2000
    assert auditor.seen[0] == long_text


@pytest.mark.asyncio
async def test_pre_clarification_content_cannot_replace_the_answer_after_user_declines(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    old_history = [
        {
            "tool_name": "filesystem.manage",
            "input": {"operation": "read_file", "path": "policy.md"},
            "status": "succeeded",
            "output_summary": "The old retention value is 400 days.",
        }
    ]
    task = repos.tasks.create(
        "Read the policy, then change it only if I approve.\n"
        "[User clarification: No. Do not make that change.]",
        metadata={
            "operator_loop": True,
            "operator_history": old_history,
            "operator_history_offset_after_clarification": len(old_history),
            "answered_clarifying_question": "Should I make the edit?",
            "clarification_answer": "No. Do not make that change.",
            "last_tool_output_text": "The old retention value is 400 days.",
        },
    )
    operator = QueueOperator(
        [OperatorDecision(action=OperatorAction.DONE, final_answer="No change was made; I left the file unchanged.")]
    )
    auditor = QueueAuditor([])
    worker = TaskWorker(
        repos,
        audit,
        executor=_executor(_settings(), audit, repos),
        operator=operator,
        auditor=auditor,
    )

    result = await worker.process_task(task.id)

    assert result.status == TaskStatus.COMPLETED
    assert result.metadata["synthesized_answer"].startswith(
        "No change was made; I left the file unchanged."
    )
    assert auditor.calls == []


@pytest.mark.asyncio
async def test_auditor_receives_latest_clarification_as_response_context(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    history = [
        {
            "tool_name": "filesystem.manage",
            "input": {"operation": "read_file", "path": "policy.md"},
            "status": "succeeded",
            "output_summary": "The value is 400 days.",
            "request_id": "req-1",
        }
    ]
    task = repos.tasks.create(
        "Read policy.md.\n[User clarification: Use one short sentence.]",
        metadata={
            "operator_loop": True,
            "operator_history": history,
            "operator_history_offset_after_clarification": 0,
            "answered_clarifying_question": "How should I format the answer?",
            "clarification_answer": "Use one short sentence.",
            "last_tool_output_text": "The value is 400 days.",
        },
    )
    auditor = QueueAuditor([AuditResult(sufficient=True, answer="The value is 400 days.")])
    worker = TaskWorker(
        repos,
        audit,
        executor=_executor(_settings(), audit, repos),
        operator=QueueOperator([OperatorDecision(action=OperatorAction.DONE, final_answer="done")]),
        auditor=auditor,
        # Only one tool call precedes the done - default audit_min_tool_calls
        # (2) would skip auditing entirely, and this test is about
        # response_context content, not that threshold.
        audit_min_tool_calls=1,
    )

    result = await worker.process_task(task.id)

    assert result.status == TaskStatus.COMPLETED
    assert "Latest clarifying question: How should I format the answer?" in auditor.response_contexts[0]
    assert "Latest user clarification: Use one short sentence." in auditor.response_contexts[0]


@pytest.mark.asyncio
async def test_delivery_receipt_does_not_replace_latest_content_for_auditor(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    full_content = "Complete evidence with marker CAREER-PROOF-47 and every requested detail."
    history = [
        {
            "tool_name": "filesystem.manage",
            "input": {"operation": "read_file", "path": "career-evidence.txt"},
            "status": "succeeded",
            "output_summary": "Complete evidence with marker...",
            "request_id": "read-request",
        },
        {
            "tool_name": "artifact.deliver",
            "input": {"path": "career-evidence.txt"},
            "status": "succeeded",
            "output_summary": "Delivered career-evidence.txt to Telegram.",
            "request_id": "delivery-request",
        },
    ]
    task = repos.tasks.create(
        "Read career-evidence.txt and send me the file.",
        metadata={
            "operator_loop": True,
            "operator_history": history,
            "artifact_delivery": {"delivered": True},
            "last_tool_output_text": "Delivered career-evidence.txt to Telegram.",
            "last_content_tool_output_text": full_content,
            "last_content_tool_request_id": "read-request",
        },
    )
    auditor = QueueAuditor([AuditResult(sufficient=True, answer="The evidence was read and delivered.")])
    worker = TaskWorker(
        repos,
        audit,
        executor=_executor(_settings(), audit, repos),
        operator=QueueOperator([OperatorDecision(action=OperatorAction.DONE, final_answer="Done.")]),
        auditor=auditor,
    )

    result = await worker.process_task(task.id)

    assert result.status == TaskStatus.COMPLETED
    assert "CAREER-PROOF-47" in auditor.calls[0][1]
    assert "Delivered career-evidence.txt" in auditor.calls[0][1]


def test_coding_agent_inherits_only_a_workspace_explicitly_named_by_user() -> None:
    requested = r"C:\work\dog-plugin"
    task = TaskRecord(
        objective="Build the plugin",
        metadata={
            "original_message_text": f"Use Claude Code in {requested} to build the plugin.",
            "orchestration_intent": {"folder_path": requested},
        },
    )

    enriched = _coding_agent_input_with_explicit_workspace(task, "coding.agent", {"provider": "claude_code"})

    assert enriched["workspace_dir"] == requested

    workspace_route = TaskRecord(
        objective="Build the plugin",
        metadata={
            "original_message_text": f"Use Claude Code in {requested} to build the plugin.",
            "orchestration_intent": {"route": "workspace.manage", "path": requested},
        },
    )
    route_enriched = _coding_agent_input_with_explicit_workspace(
        workspace_route, "coding.agent", {"provider": "claude_code"}
    )
    assert route_enriched["workspace_dir"] == requested

    invented = TaskRecord(
        objective="Build the plugin",
        metadata={
            "original_message_text": "Use Claude Code to build the plugin.",
            "orchestration_intent": {"folder_path": requested},
        },
    )
    unchanged = _coding_agent_input_with_explicit_workspace(
        invented, "coding.agent", {"provider": "claude_code"}
    )
    assert "workspace_dir" not in unchanged

    overridden = _coding_agent_input_with_explicit_workspace(
        workspace_route,
        "coding.agent",
        {"provider": "codex", "workspace_dir": r"C:\work\generated-task"},
    )
    assert overridden["workspace_dir"] == requested

    unresolved = TaskRecord(
        objective="Build the plugin",
        metadata={
            "original_message_text": "Use Codex in {{dog_workspace}} to build it.",
            "orchestration_intent": {"path": "dog_workspace"},
        },
    )
    unresolved_input = _coding_agent_input_with_explicit_workspace(
        unresolved, "coding.agent", {"provider": "codex"}
    )
    assert "workspace_dir" not in unresolved_input


def test_coding_agent_start_recovers_named_provider_and_task_objective() -> None:
    task = TaskRecord(
        objective="Build the accessible dog app in the requested folder.",
        metadata={
            "original_message_text": "Could you ask Codex to start my dog app?",
        },
    )

    enriched = _coding_agent_input_with_task_defaults(
        task,
        "coding.agent",
        {"operation": "start"},
    )

    assert enriched["provider"] == "codex"
    assert enriched["objective"] == task.objective

    ambiguous = task.model_copy(
        update={
            "metadata": {
                "original_message_text": "Compare Codex and Claude Code before doing anything."
            }
        }
    )
    ambiguous_input = _coding_agent_input_with_task_defaults(
        ambiguous,
        "coding.agent",
        {"operation": "start"},
    )
    assert "provider" not in ambiguous_input


@pytest.mark.asyncio
async def test_completed_coding_session_finishes_without_redundant_status_polls(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("Use Codex to build a small accessible dog app")
    settings = _settings()
    executor = _executor(
        settings,
        audit,
        repos,
        tool_name="coding.agent",
        output={
            "status": "completed",
            "returncode": 0,
            "provider": "codex",
            "session_id": "codex_done",
            "workspace_dir": str(tmp_path / "dog-app"),
            "changed_files": ["index.html", "styles.css", "script.js"],
            "summary": "Created and syntax-checked the dog app.",
        },
    )
    operator = QueueOperator(
        [
            OperatorDecision(
                action=OperatorAction.CALL_TOOL,
                tool_name="coding.agent",
                tool_input={"operation": "start"},
                risk_level=RiskLevel.LOW,
            )
        ]
    )
    worker = TaskWorker(repos, audit, executor=executor, operator=operator)

    result = await worker.process_task(task.id)

    assert result.status == TaskStatus.COMPLETED
    assert result.metadata["coding_agent_session_id"] == "codex_done"
    assert "Changed files:" in result.metadata["synthesized_answer"]
    assert operator.calls == 1


def test_coding_agent_recovers_one_existing_path_when_classifier_omits_it(tmp_path) -> None:
    requested = tmp_path / "dog app workspace"
    generated = tmp_path / "generated task"
    requested.mkdir()
    generated.mkdir()
    task = TaskRecord(
        objective="Build the dog app in the specified workspace.",
        metadata={
            "original_message_text": f"Use Codex in {requested} to build a dog app.",
            "orchestration_intent": {"route": "coding.agent", "path": None},
        },
    )

    enriched = _coding_agent_input_with_explicit_workspace(
        task,
        "coding.agent",
        {"provider": "codex", "workspace_dir": str(generated)},
    )

    assert enriched["workspace_dir"] == str(requested.resolve())


def test_coding_agent_does_not_guess_between_multiple_existing_paths(tmp_path) -> None:
    first = tmp_path / "first workspace"
    second = tmp_path / "second workspace"
    generated = tmp_path / "generated task"
    first.mkdir()
    second.mkdir()
    generated.mkdir()
    task = TaskRecord(
        objective="Work on the project.",
        metadata={
            "original_message_text": f"Compare {first} with {second} before coding.",
            "orchestration_intent": {"route": "coding.agent", "path": None},
        },
    )

    unchanged = _coding_agent_input_with_explicit_workspace(
        task,
        "coding.agent",
        {"provider": "codex", "workspace_dir": str(generated)},
    )

    assert unchanged["workspace_dir"] == str(generated)


def test_later_workspace_result_does_not_overwrite_coding_agent_handoff(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("Build the dog app with Codex")
    worker = TaskWorker(
        repos,
        audit,
        executor=_executor(_settings(), audit, repos),
        operator=QueueOperator([]),
    )
    canonical = str(tmp_path / "canonical dog app")
    generated = str(tmp_path / "generated preview")

    worker._record_tool_result(
        task.id,
        "coding.agent",
        ToolCallResult(
            request_id="coding-result",
            status=ToolResultStatus.SUCCEEDED,
            output={
                "workspace_dir": canonical,
                "session_id": "codex_123",
                "limit_state": {"limited": False},
            },
        ),
    )
    updated = worker._record_tool_result(
        task.id,
        "workspace.manage",
        ToolCallResult(
            request_id="preview-result",
            status=ToolResultStatus.SUCCEEDED,
            output={"workspace_dir": generated, "url": "http://127.0.0.1:8890/"},
        ),
    )

    assert updated.metadata["workspace_dir"] == generated
    assert updated.metadata["coding_agent_workspace"] == canonical
    assert updated.metadata["coding_agent_session_id"] == "codex_123"


def test_content_search_intent_enables_content_scan() -> None:
    task = TaskRecord(
        objective="Find the handoff file whose contents contain ORBIT-GLASS-27.",
        metadata={
            "original_message_text": (
                "Somewhere under C:/handoffs is the text file whose contents "
                "contain ORBIT-GLASS-27."
            )
        },
    )

    enriched = _filesystem_search_input_with_content_intent(
        task,
        "filesystem.manage",
        {"operation": "search", "root": "C:/handoffs", "query": "ORBIT-GLASS-27"},
    )

    assert enriched["include_content"] is True


def test_content_search_recovers_marker_query_from_human_request() -> None:
    task = TaskRecord(
        objective="Find the file whose contents contain ORBIT-GLASS-27.",
        metadata={
            "original_message_text": (
                "Somewhere under C:/handoffs is the file whose contents contain ORBIT-GLASS-27."
            )
        },
    )

    enriched = _filesystem_search_input_with_content_intent(
        task,
        "filesystem.manage",
        {"operation": "search", "root": "C:/handoffs"},
    )

    assert enriched["query"] == "orbit-glass-27"
    assert enriched["include_content"] is True


def test_failed_explicit_stale_read_switches_to_nearest_existing_parent_search(tmp_path) -> None:
    recovery_root = tmp_path / "career"
    recovery_root.mkdir()
    stale_path = recovery_root / "old_location" / "career-master.txt"
    task = TaskRecord(
        objective="Read the career master file and recover it if moved.",
        metadata={
            "original_message_text": (
                f"Read {stale_path}. If that location no longer works, search under "
                f"{recovery_root} for the renamed file."
            )
        },
    )
    history = [
        {
            "tool_name": "filesystem.manage",
            "status": "failed",
            "input": {"operation": "read_file", "path": str(stale_path)},
            "error": "file not found",
        }
    ]

    tool_name, tool_input = _stale_read_recovery_call(
        task,
        "filesystem.manage",
        {"operation": "read_file", "path": str(stale_path)},
        history,
    )

    assert tool_name == "filesystem.manage"
    assert tool_input == {
        "operation": "search",
        "root": str(recovery_root.resolve()),
        "query": "career-master",
        "include_content": False,
    }


def test_named_coding_provider_is_enforced_before_fallback_workspace_build() -> None:
    task = TaskRecord(
        objective="Use Codex to build a small dog app.",
        metadata={"original_message_text": "Please use Codex to build a small dog app."},
    )
    definitions = {
        "coding.agent": ToolDefinition(
            name="coding.agent",
            capability=Capability.LLM_GENERATE,
            enabled=True,
            description="test",
            operations=("start", "status"),
        ),
        "workspace.manage": ToolDefinition(
            name="workspace.manage",
            capability=Capability.FILESYSTEM_WRITE,
            enabled=True,
            description="test",
            operations=("prepare", "write_files"),
        ),
    }

    tool_name, tool_input = _required_named_coding_agent_call(
        task,
        "workspace.manage",
        {"operation": "prepare"},
        definitions,
    )

    assert tool_name == "coding.agent"
    assert tool_input == {
        "operation": "start",
        "provider": "codex",
        "objective": task.objective,
    }


def test_filename_search_does_not_enable_content_scan() -> None:
    task = TaskRecord(objective="Find report.md by filename.", metadata={})

    unchanged = _filesystem_search_input_with_content_intent(
        task,
        "filesystem.manage",
        {"operation": "search", "root": "C:/docs", "query": "report.md"},
    )

    assert "include_content" not in unchanged


def test_completed_typed_work_rejects_optional_clarification() -> None:
    history = [
        {
            "tool_name": "adapter.factory",
            "input": {"operation": "scaffold"},
            "status": "succeeded",
            "output_summary": "Adapter proposal created in cache.",
        }
    ]
    task = TaskRecord(
        objective="Create an adapter proposal with a manifest and tests.",
        metadata={"adapter_dir": "C:/cache/example", "operator_history": history},
    )

    assert _satisfied_task_does_not_need_clarification(task, history)


def test_untyped_request_can_still_ask_for_required_user_input() -> None:
    history = [{"tool_name": "memory.search", "status": "succeeded"}]
    task = TaskRecord(objective="Help me with it.", metadata={"operator_history": history})

    assert not _satisfied_task_does_not_need_clarification(task, history)


def test_known_written_file_rejects_delivery_clarification() -> None:
    history = [
        {
            "tool_name": "filesystem.manage",
            "input": {"operation": "write_text_file", "path": "C:/output/brief.md"},
            "status": "succeeded",
        }
    ]
    task = TaskRecord(
        objective="Write a brief and send me that exact file.",
        metadata={"operator_history": history},
    )

    reason = _clarification_recovery_reason(task, history)

    assert reason is not None
    assert "C:/output/brief.md" in reason
    assert "delivery tool" in reason


def test_explicit_delivery_before_schedule_reorders_the_tool_call() -> None:
    history = [
        {
            "tool_name": "filesystem.manage",
            "input": {"operation": "write_text_file", "path": "C:/output/brief.md"},
            "status": "succeeded",
        }
    ]
    task = TaskRecord(
        objective="Write a brief, deliver it, and create a schedule.",
        metadata={
            "original_message_text": (
                "Write the brief and send me that exact file. Only after the file is delivered, "
                "create a weekly schedule."
            ),
            "operator_history": history,
        },
    )
    definitions = {
        "artifact.deliver": ToolDefinition(
            name="artifact.deliver",
            capability=Capability.TELEGRAM_SEND,
            enabled=True,
            description="test",
            operations=("send_file",),
        )
    }

    tool_name, tool_input = _ordered_artifact_delivery_call(
        task,
        "schedule.manage",
        {"operation": "create", "cadence": "weekly"},
        history,
        definitions,
    )

    assert tool_name == "artifact.deliver"
    assert tool_input["operation"] == "send_file"
    assert tool_input["path"] == "C:/output/brief.md"


def test_explicit_delivery_replaces_local_open_with_send() -> None:
    task = TaskRecord(objective="Find the file at C:/output/brief.md and send it to me.")
    definitions = {
        "artifact.deliver": ToolDefinition(
            name="artifact.deliver",
            capability=Capability.TELEGRAM_SEND,
            enabled=True,
            description="test",
            operations=("send_file",),
        )
    }

    tool_name, tool_input = _ordered_artifact_delivery_call(
        task,
        "filesystem.manage",
        {"operation": "open_file", "path": "C:/output/brief.md"},
        [],
        definitions,
    )

    assert tool_name == "artifact.deliver"
    assert tool_input == {
        "operation": "send_file",
        "path": "C:/output/brief.md",
        "caption": "Requested file",
    }


def test_named_cache_only_adapter_rejects_premature_clarification() -> None:
    task = TaskRecord(
        objective="Create the adapter proposal.",
        metadata={
            "original_message_text": (
                "Create a reusable adapter proposal named linkedin_evidence_compare with a "
                "generic contract and keep it cache-only."
            )
        },
    )

    reason = _clarification_recovery_reason(task, [])

    assert reason is not None
    assert "scaffold" in reason


def test_multi_tool_audit_evidence_includes_every_observed_outcome() -> None:
    delivery = {
        "tool_name": "artifact.deliver",
        "status": "succeeded",
        "output_summary": "Delivered report.md to Telegram.",
    }
    evidence = _operator_audit_evidence(
        [
            {
                "tool_name": "filesystem.manage",
                "status": "succeeded",
                "output_summary": "Inspected three career evidence files.",
            },
            {
                "tool_name": "filesystem.manage",
                "status": "succeeded",
                "input": {
                    "operation": "write_text_file",
                    "path": "report.md",
                    "content": "Evidence-backed LinkedIn brief",
                },
                "output_summary": "Wrote report.md.",
            },
            delivery,
        ],
        delivery,
        "Delivered report.md to Telegram chat 123.",
    )

    assert "Inspected three career evidence files" in evidence
    assert "Evidence-backed LinkedIn brief" in evidence
    assert "Delivered report.md to Telegram chat 123" in evidence


@pytest.mark.asyncio
async def test_multi_step_task_sends_a_progress_notification_per_step(tmp_path) -> None:
    """Regression guard (docs/HISTORY.md §3.3): the RUNNING dedupe key used to be
    built from metadata["attempt_history"] + current_step_id, both plan-era
    fields with zero writers since P3. That collapsed the key to the constant
    "running", so a 30-step task sent one "working on it" and then went
    silent."""
    repos, audit = make_repos(tmp_path)
    repos.tasks.create("do three things", metadata={"source_chat_id": "100"})
    settings = _settings()
    executor = _executor(settings, audit, repos, output={"text": "ok"})
    operator = QueueOperator([
        OperatorDecision(action=OperatorAction.CALL_TOOL, tool_name="llm", tool_input={}, risk_level=RiskLevel.LOW),
        OperatorDecision(action=OperatorAction.CALL_TOOL, tool_name="llm", tool_input={}, risk_level=RiskLevel.LOW),
        OperatorDecision(action=OperatorAction.CALL_TOOL, tool_name="llm", tool_input={}, risk_level=RiskLevel.LOW),
        OperatorDecision(action=OperatorAction.DONE, final_answer="finished"),
    ])

    class RecordingNotifier:
        def __init__(self) -> None:
            self.sent: list[str] = []
        async def notify(self, task) -> None:
            self.sent.append(task.status.value)

    notifier = RecordingNotifier()
    worker = TaskWorker(repos, audit, executor=executor, operator=operator, notification_sink=notifier)

    for _ in range(6):
        processed = await worker.process_next()
        if processed is None or processed.status in {TaskStatus.COMPLETED, TaskStatus.FAILED}:
            break

    assert notifier.sent.count("running") == 3, notifier.sent
    assert notifier.sent[-1] == "completed"


@pytest.mark.asyncio
async def test_operator_loop_unregistered_tool_does_not_crash(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("uses a nonexistent tool")
    settings = _settings()
    executor = _executor(settings, audit, repos)
    operator = QueueOperator([
        OperatorDecision(action=OperatorAction.CALL_TOOL, tool_name="not.a.real.tool", tool_input={}, risk_level=RiskLevel.LOW),
    ])
    worker = TaskWorker(repos, audit, executor=executor, operator=operator)

    result = await worker.process_task(task.id)

    assert result.status == TaskStatus.RUNNING
    assert result.metadata["operator_history"][0]["error"] == "unregistered tool: not.a.real.tool"


@pytest.mark.asyncio
async def test_operator_loop_applies_runtime_risk_floor_to_model_tool_call(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("run a generated script")
    settings = AppSettings(
        _env_file=None,
        approval_policy={"require_approval_at_or_above": RiskLevel.CRITICAL},
        capabilities={
            Capability.TERMINAL_RUN: CapabilityPolicy(
                enabled=True,
                requires_approval=False,
                max_risk_level=RiskLevel.CRITICAL,
            )
        },
    )
    adapter = StaticToolAdapter({"stdout": "done"})
    executor = ToolExecutor(
        PolicyEngine(settings, audit),
        repos,
        audit,
        adapters={"code.interpreter": adapter},
        tool_definitions={
            "code.interpreter": ToolDefinition(
                name="code.interpreter",
                capability=Capability.TERMINAL_RUN,
                enabled=True,
                description="run generated code",
                minimum_risk=RiskLevel.HIGH,
            )
        },
    )
    operator = QueueOperator([
        OperatorDecision(
            action=OperatorAction.CALL_TOOL,
            tool_name="code.interpreter",
            tool_input={"operation": "generate_and_run", "objective": "write a report"},
            risk_level=RiskLevel.LOW,
        ),
    ])
    worker = TaskWorker(repos, audit, executor=executor, operator=operator)

    result = await worker.process_task(task.id)

    assert result.status == TaskStatus.RUNNING
    assert len(adapter.requests) == 1
    assert adapter.requests[0].risk_level == RiskLevel.HIGH
    assert result.metadata["operator_history"][0]["status"] == "succeeded"


def test_operator_risk_normalizes_conservative_overstatement_to_runtime_definition() -> None:
    definition = ToolDefinition(
        name="schedule.manage",
        capability=Capability.SCHEDULE_MANAGE,
        enabled=True,
        description="manage schedules",
        operation_risks={"create": RiskLevel.MEDIUM},
    )

    risk = _effective_operator_risk(definition, {"operation": "create"}, RiskLevel.HIGH)

    assert risk == RiskLevel.MEDIUM


def _approval_settings() -> AppSettings:
    return AppSettings(
        _env_file=None,
        capabilities={
            Capability.LLM_GENERATE: CapabilityPolicy(enabled=True, requires_approval=True, max_risk_level=RiskLevel.LOW)
        },
    )


@pytest.mark.asyncio
async def test_operator_loop_needs_approval_creates_request_and_awaits(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("needs approval")
    executor = _executor(_approval_settings(), audit, repos, output={"answer": "42"})
    operator = QueueOperator([
        OperatorDecision(action=OperatorAction.CALL_TOOL, tool_name="llm", tool_input={"q": "?"}, risk_level=RiskLevel.LOW),
    ])
    worker = TaskWorker(repos, audit, executor=executor, operator=operator)

    result = await worker.process_task(task.id)

    assert result.status == TaskStatus.AWAITING_APPROVAL
    assert result.metadata["operator_pending_call"]["tool_name"] == "llm"
    assert result.metadata["operator_pending_call"]["tool_input"] == {"q": "?"}
    assert "pending_approval_preview" in result.metadata
    approvals = repos.approvals.list_for_task(task.id)
    assert len(approvals) == 1
    assert approvals[0].status == ApprovalStatus.PENDING
    assert approvals[0].capability == Capability.LLM_GENERATE


@pytest.mark.asyncio
async def test_operator_loop_awaiting_approval_stays_put_while_pending(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("needs approval")
    executor = _executor(_approval_settings(), audit, repos, output={"answer": "42"})
    operator = QueueOperator([
        OperatorDecision(action=OperatorAction.CALL_TOOL, tool_name="llm", tool_input={}, risk_level=RiskLevel.LOW),
    ])
    worker = TaskWorker(repos, audit, executor=executor, operator=operator)

    awaiting = await worker.process_task(task.id)
    still_awaiting = await worker.process_task(awaiting.id)

    assert still_awaiting.status == TaskStatus.AWAITING_APPROVAL
    assert still_awaiting.metadata["operator_pending_call"]["tool_name"] == "llm"
    assert operator.calls == 1  # decide() is not called again while approval is pending


@pytest.mark.asyncio
async def test_run_forever_sleeps_instead_of_busy_looping_on_a_pending_approval(tmp_path, monkeypatch) -> None:
    """docs/UI_UX_AUDIT.md Phase 8: originally, process_next() returned the
    SAME AWAITING_APPROVAL task on every call (claim_next always re-picked
    the oldest task this worker already claimed, and that check returned
    instantly) - without a sleep here, run_forever spun the CPU at 100%
    hammering the DB for as long as a human takes to decide.

    Second pass (docs/UI_UX_AUDIT.md Phase 8, second review): AWAITING_APPROVAL
    was removed from WORKABLE_STATUSES entirely, so process_next() now
    returns None here (nothing else is queued in this test) rather than
    re-picking the same task - but the risk of a busy loop is the same
    shape (a fast, empty return with no sleep), so this test still earns
    its keep even though the specific mechanism it guards against changed.
    """
    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("needs approval")
    executor = _executor(_approval_settings(), audit, repos, output={"answer": "42"})
    operator = QueueOperator([
        OperatorDecision(action=OperatorAction.CALL_TOOL, tool_name="llm", tool_input={}, risk_level=RiskLevel.LOW),
    ])
    worker = TaskWorker(repos, audit, executor=executor, operator=operator)
    await worker.process_task(task.id)  # gets it into AWAITING_APPROVAL once, for real

    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        if len(sleep_calls) >= 3:
            raise RuntimeError("stop the loop - three sleeps observed is enough to prove it isn't busy-looping")

    monkeypatch.setattr("agent_control.orchestration.worker.asyncio.sleep", fake_sleep)

    with pytest.raises(RuntimeError, match="stop the loop"):
        await worker.run_forever(poll_interval_seconds=5.0)

    assert sleep_calls == [5.0, 5.0, 5.0]


@pytest.mark.asyncio
async def test_run_forever_survives_one_infrastructure_poll_failure(tmp_path, monkeypatch) -> None:
    repos, audit = make_repos(tmp_path)
    worker = TaskWorker(
        repos,
        audit,
        executor=_executor(_settings(), audit, repos),
        operator=QueueOperator([]),
    )
    poll_calls = 0

    async def flaky_process_next():
        nonlocal poll_calls
        poll_calls += 1
        if poll_calls == 1:
            raise RuntimeError("transient claim failure")
        return None

    sleep_calls: list[float] = []

    async def stop_after_retry(seconds: float) -> None:
        sleep_calls.append(seconds)
        if len(sleep_calls) == 2:
            raise RuntimeError("stop after proving the loop retried")

    monkeypatch.setattr(worker, "process_next", flaky_process_next)
    monkeypatch.setattr("agent_control.orchestration.worker.asyncio.sleep", stop_after_retry)

    with pytest.raises(RuntimeError, match="stop after proving"):
        await worker.run_forever(poll_interval_seconds=3.0)

    assert poll_calls == 2
    assert sleep_calls == [3.0, 3.0]
    assert any(
        event.payload.get("error") == "worker_poll_failed"
        for event in repos.audit.list_recent(limit=20)
    )


@pytest.mark.asyncio
async def test_operator_loop_resumes_and_executes_after_approval_granted(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("needs approval")
    executor = _executor(_approval_settings(), audit, repos, output={"answer": "42"})
    operator = QueueOperator([
        OperatorDecision(action=OperatorAction.CALL_TOOL, tool_name="llm", tool_input={"q": "?"}, risk_level=RiskLevel.LOW),
        OperatorDecision(action=OperatorAction.DONE, final_answer="42"),
    ])
    worker = TaskWorker(repos, audit, executor=executor, operator=operator)

    awaiting = await worker.process_task(task.id)
    approvals = repos.approvals.list_for_task(task.id)
    repos.approvals.set_status(approvals[0].id, ApprovalStatus.APPROVED)
    requeue_after_approval_decision(repos, task.id)
    assert repos.tasks.get(task.id).status == TaskStatus.RUNNING

    resumed = await worker.process_task(awaiting.id)

    assert resumed.status == TaskStatus.RUNNING
    assert "operator_pending_call" not in resumed.metadata
    assert "pending_approval_preview" not in resumed.metadata
    assert len(resumed.metadata["operator_history"]) == 1
    assert resumed.metadata["operator_history"][0]["tool_name"] == "llm"
    assert resumed.metadata["operator_history"][0]["status"] == "succeeded"
    assert len(repos.approvals.list_for_task(task.id)) == 1


@pytest.mark.asyncio
async def test_operator_loop_resumes_via_requeue_instead_of_asking_again(tmp_path) -> None:
    """Granting an approval the way every real caller does must replay the call.

    The test above sets the approval status directly and leaves the task in
    AWAITING_APPROVAL. Real callers (Telegram buttons, the admin API) instead go
    through requeue_after_approval_decision(), which flips the task to RUNNING so
    the worker will claim it - and the resume used to be gated on
    `status == AWAITING_APPROVAL`, so it was skipped exactly when it mattered.

    The operator then re-planned from scratch and asked for approval again. Every
    grant produced a new request, so a gated task could never finish no matter how
    many times its approval was granted; one observed live task burned 14 approvals
    in 11 minutes and completed nothing. Only ONE decision is queued below, so a
    re-plan cannot silently look like success - it exhausts the operator instead.
    """
    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("needs approval")
    executor = _executor(_approval_settings(), audit, repos, output={"answer": "42"})
    operator = QueueOperator([
        OperatorDecision(action=OperatorAction.CALL_TOOL, tool_name="llm", tool_input={"q": "?"}, risk_level=RiskLevel.LOW),
        OperatorDecision(action=OperatorAction.DONE, final_answer="42"),
    ])
    worker = TaskWorker(repos, audit, executor=executor, operator=operator)

    awaiting = await worker.process_task(task.id)
    assert awaiting.status == TaskStatus.AWAITING_APPROVAL

    approvals = repos.approvals.list_for_task(task.id)
    assert repos.approvals.decide_pending(approvals[0].id, ApprovalStatus.APPROVED)
    requeue_after_approval_decision(repos, task.id)
    assert repos.tasks.get(task.id).status == TaskStatus.RUNNING

    resumed = await worker.process_task(task.id)

    assert len(repos.approvals.list_for_task(task.id)) == 1, "a second approval means it re-planned"
    assert "operator_pending_call" not in resumed.metadata
    assert resumed.metadata["operator_history"][0]["tool_name"] == "llm"
    assert resumed.metadata["operator_history"][0]["status"] == "succeeded"

    completed = await worker.process_task(resumed.id)
    assert completed.status == TaskStatus.COMPLETED
    assert completed.metadata["synthesized_answer"] == "42"


@pytest.mark.asyncio
async def test_a_pending_approval_no_longer_blocks_the_worker_from_a_second_task(tmp_path) -> None:
    """docs/UI_UX_AUDIT.md Phase 8, second review: the CPU busy-loop was
    fixed, but the architectural bottleneck wasn't - with the default
    single worker, an AWAITING_APPROVAL task stayed claimable so the
    worker could notice when a decision landed, and claim_next's
    ORDER BY created_at ASC meant it always re-picked that same older task
    ahead of any newer one, starving every later task for as long as a
    human took to decide.

    This is the actual regression test for that: Task A goes to
    AWAITING_APPROVAL, Task B is submitted after it, and the worker's next
    process_next() call must reach Task B - not return None, and not
    re-select Task A.
    """
    repos, audit = make_repos(tmp_path)
    task_a = repos.tasks.create("needs approval")
    task_b = repos.tasks.create("a second, unrelated task")
    executor = _executor(_approval_settings(), audit, repos, output={"answer": "ok"})
    operator = QueueOperator([
        OperatorDecision(action=OperatorAction.CALL_TOOL, tool_name="llm", tool_input={}, risk_level=RiskLevel.LOW),
        OperatorDecision(action=OperatorAction.DONE, final_answer="ok"),
    ])
    worker = TaskWorker(repos, audit, executor=executor, operator=operator)

    awaiting = await worker.process_task(task_a.id)
    assert awaiting.status == TaskStatus.AWAITING_APPROVAL

    claimed = await worker.process_next()

    assert claimed is not None
    assert claimed.id == task_b.id
    assert claimed.status == TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_operator_loop_blocked_when_approval_rejected(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("needs approval")
    executor = _executor(_approval_settings(), audit, repos)
    operator = QueueOperator([
        OperatorDecision(action=OperatorAction.CALL_TOOL, tool_name="llm", tool_input={}, risk_level=RiskLevel.LOW),
    ])
    worker = TaskWorker(repos, audit, executor=executor, operator=operator)

    awaiting = await worker.process_task(task.id)
    approvals = repos.approvals.list_for_task(task.id)
    repos.approvals.set_status(approvals[0].id, ApprovalStatus.REJECTED)

    result = await worker.process_task(awaiting.id)

    assert result.status == TaskStatus.BLOCKED


@pytest.mark.asyncio
async def test_operator_loop_done_with_fulfillment_gap_continues_instead_of_completing(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("create a script that prints hello")
    settings = _settings()
    executor = _executor(settings, audit, repos)
    operator = QueueOperator([
        OperatorDecision(action=OperatorAction.DONE, final_answer="Done, but no workspace was ever created."),
    ])
    worker = TaskWorker(repos, audit, executor=executor, operator=operator)

    result = await worker.process_task(task.id)

    assert result.status == TaskStatus.RUNNING
    assert result.metadata["operator_fulfillment_gap_count"] == 1
    assert result.metadata["operator_history"][-1]["status"] == "fulfillment_gap"
    assert "workspace_dir" in result.metadata["operator_history"][-1]["error"]
    assert "workspace.manage or code.interpreter" in result.metadata["operator_history"][-1]["error"]


def test_write_claim_without_any_successful_write_is_rejected() -> None:
    """The observed failure: one unsupported-operation error, then a synthesized
    "The following files were created:" for an empty workspace, and the task
    completed (docs/E2E_FINDINGS.md P0-2)."""
    answer = "Scaffolded the extension. The following files were created: package.json, extension.js"
    history = [{"tool_name": "filesystem.manage", "status": "failed", "input": {"operation": "list_directory"}}]

    assert _unsupported_write_claim(answer, history, {}) is not None


def test_write_claim_is_accepted_on_real_evidence() -> None:
    answer = "The following files were created: package.json"
    wrote = [{"tool_name": "filesystem.manage", "status": "succeeded", "input": {"operation": "write_text_file"}}]

    assert _unsupported_write_claim(answer, wrote, {}) is None
    # Merely preparing a workspace is not write evidence for the requested
    # project. A concrete produced path is.
    assert _unsupported_write_claim(answer, [], {"workspace_dir": "/tmp/ws"}) is not None
    assert _unsupported_write_claim(answer, [], {"changed_files": ["package.json"]}) is None


def test_write_claim_must_match_the_files_actually_written() -> None:
    """Evidence that *some* write happened is not evidence the claimed one did.

    P0-2 listed package.json for an empty workspace; a task that had already
    written something unrelated would otherwise carry the same fabrication
    through, because any truthy evidence key disabled the check task-wide.
    """
    answer = "The following files were created: package.json and extension.ts"

    unrelated = {"changed_files": ["notes.txt"]}
    assert _unsupported_write_claim(answer, [], unrelated) is not None

    matching = {"changed_files": ["/ws/package.json", "/ws/extension.ts"]}
    assert _unsupported_write_claim(answer, [], matching) is None

    # Partial evidence is enough - the operator named one file it really wrote.
    assert _unsupported_write_claim(answer, [], {"changed_files": ["package.json"]}) is None


def test_unspecific_write_claim_passes_on_any_recorded_write() -> None:
    """Without named files there is nothing to compare, so a recorded write is
    all that can reasonably be required."""
    wrote = [{"tool_name": "filesystem.manage", "status": "succeeded", "input": {"operation": "write_text_file"}}]

    assert _unsupported_write_claim("I created the files you asked for.", wrote, {}) is None


def test_honest_report_of_a_failed_write_is_not_treated_as_a_claim() -> None:
    """Flagging this would push a correctly-behaving run into a pointless
    replan and penalize the exact honesty the guard exists to encourage."""
    history = [{"tool_name": "filesystem.manage", "status": "failed", "input": {"operation": "list_directory"}}]

    for honest in (
        "I could not create the files: the operation is unsupported.",
        "No files were created because filesystem.manage rejected the operation.",
        "I was unable to write the README file.",
    ):
        assert _unsupported_write_claim(honest, history, {}) is None, honest


def test_answer_that_claims_no_write_is_ignored() -> None:
    history = [{"tool_name": "filesystem.manage", "status": "succeeded", "input": {"operation": "read_file"}}]

    assert _unsupported_write_claim("The folder contains three CSV files.", history, {}) is None


@pytest.mark.asyncio
async def test_operator_loop_rejects_a_fabricated_write_and_keeps_working(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("scaffold the extension files")
    settings = _settings()
    executor = _executor(settings, audit, repos)
    operator = QueueOperator([
        OperatorDecision(
            action=OperatorAction.DONE,
            final_answer="The following files were created: package.json, extension.js",
        ),
    ])
    worker = TaskWorker(repos, audit, executor=executor, operator=operator)

    result = await worker.process_task(task.id)

    assert result.status == TaskStatus.RUNNING
    assert result.metadata["operator_history"][-1]["status"] == "fulfillment_gap"
    assert "expected_workspace_dir_missing" in result.metadata["operator_history"][-1]["error"]


@pytest.mark.asyncio
async def test_operator_loop_fulfillment_gap_resolves_once_postcondition_met(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("create a script that prints hello")
    settings = _settings()
    executor = _executor(
        settings,
        audit,
        repos,
        output={"workspace_dir": "/tmp/ws", "changed_paths": ["/tmp/ws/hello.py"]},
    )
    operator = QueueOperator([
        OperatorDecision(action=OperatorAction.CALL_TOOL, tool_name="llm", tool_input={}, risk_level=RiskLevel.LOW),
        OperatorDecision(action=OperatorAction.DONE, final_answer="Created the script in /tmp/ws."),
    ])
    worker = TaskWorker(repos, audit, executor=executor, operator=operator)

    running = await worker.process_task(task.id)
    completed = await worker.process_task(running.id)

    assert completed.status == TaskStatus.COMPLETED
    assert completed.metadata["workspace_dir"] == "/tmp/ws"
    assert "fulfillment_gap" not in completed.metadata


@pytest.mark.asyncio
async def test_operator_loop_fulfillment_gap_exhausts_and_blocks_with_gap_flagged(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("create a script that prints hello")
    settings = _settings()
    executor = _executor(settings, audit, repos)
    operator = QueueOperator([
        OperatorDecision(action=OperatorAction.DONE, final_answer=f"attempt {i}") for i in range(3)
    ])
    worker = TaskWorker(repos, audit, executor=executor, operator=operator)

    result = task
    for _ in range(4):
        result = await worker.process_task(result.id)
        if result.status != TaskStatus.RUNNING:
            break

    assert result.status == TaskStatus.BLOCKED
    assert result.metadata["fulfillment_gap"] == "expected_workspace_dir_missing"
    assert result.metadata["operator_fulfillment_gap_count"] == 2


@pytest.mark.asyncio
async def test_operator_loop_blocked_without_reason_reports_latest_failed_capability(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("send something using a capability that is unavailable")
    settings = _settings()
    executor = _executor(settings, audit, repos)
    operator = QueueOperator([
        OperatorDecision(
            action=OperatorAction.CALL_TOOL,
            tool_name="missing.integration",
            tool_input={"operation": "send"},
        ),
        OperatorDecision(action=OperatorAction.BLOCKED),
    ])
    worker = TaskWorker(repos, audit, executor=executor, operator=operator)

    running = await worker.process_task(task.id)
    result = await worker.process_task(running.id)

    assert result.status == TaskStatus.BLOCKED
    assert "No available capability completed the request" in result.metadata["last_worker_error"]
    assert "missing.integration" in result.metadata["last_worker_error"]


def test_operator_call_normalization_accepts_common_filesystem_dialect() -> None:
    definition = ToolDefinition(
        name="filesystem.manage",
        capability=Capability.FILESYSTEM_WRITE,
        enabled=True,
        description="test",
        operations=("inspect_folder", "search", "read_file", "write_text_file"),
    )

    tool_name, tool_input = _canonical_operator_tool_call(
        "filesystem",
        {"operation": "write_file", "path": "README.md", "content": "hello"},
        {definition.name: definition},
    )
    list_name, list_input = _canonical_operator_tool_call(
        "filesystem_manager",
        {"operation": "list_directory", "path": "documents"},
        {definition.name: definition},
    )
    tree_name, tree_input = _canonical_operator_tool_call(
        "filesystem.manage",
        {"operation": "directory_tree", "root": "documents"},
        {definition.name: definition},
    )
    search_name, search_input = _canonical_operator_tool_call(
        "filesystem.manage",
        {"operation": "search_files", "root_folder": "documents", "pattern": "report"},
        {definition.name: definition},
    )
    read_name, read_input = _canonical_operator_tool_call(
        "filesystem.manage",
        {"operation": "read_file", "folder_path": "documents", "file_name": "report.md"},
        {definition.name: definition},
    )

    assert tool_name == "filesystem.manage"
    assert tool_input["operation"] == "write_text_file"
    assert list_name == "filesystem.manage"
    assert list_input == {"operation": "inspect_folder", "root": "documents"}
    assert tree_name == "filesystem.manage"
    assert tree_input == {"operation": "inspect_folder", "root": "documents"}
    assert search_name == "filesystem.manage"
    assert search_input == {"operation": "search", "root": "documents", "query": "report"}
    assert read_name == "filesystem.manage"
    assert read_input == {"operation": "read_file", "path": str(Path("documents") / "report.md")}


@pytest.mark.asyncio
async def test_operator_loop_audit_replaces_final_answer_with_grounded_synthesis(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("what is the invoice total?")
    settings = _settings()
    executor = _executor(settings, audit, repos, tool_name="filesystem.manage", output={"text": "Invoice #4471 - $250.00"})
    operator = QueueOperator([
        OperatorDecision(action=OperatorAction.CALL_TOOL, tool_name="filesystem.manage", tool_input={}, risk_level=RiskLevel.LOW),
        OperatorDecision(action=OperatorAction.DONE, final_answer="I found an invoice."),
    ])
    auditor = QueueAuditor([AuditResult(sufficient=True, answer="The invoice total is $250.00.")])
    # audit_min_tool_calls=1: these exercise the Auditor on single-tool
    # tasks, which the production default (2) deliberately skips.
    worker = TaskWorker(repos, audit, executor=executor, operator=operator, auditor=auditor,
                        audit_min_tool_calls=1)

    running = await worker.process_task(task.id)
    completed = await worker.process_task(running.id)

    assert completed.status == TaskStatus.COMPLETED
    assert completed.metadata["synthesized_answer"] == "The invoice total is $250.00."
    assert len(auditor.calls) == 1
    assert auditor.calls[0][0] == "what is the invoice total?"


@pytest.mark.asyncio
async def test_operator_loop_audit_gap_continues_loop_instead_of_completing(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("list the first 5 episodes")
    settings = _settings()
    executor = _executor(settings, audit, repos, tool_name="filesystem.manage", output={"text": "1. Pilot\n2. Second"})
    operator = QueueOperator([
        OperatorDecision(action=OperatorAction.CALL_TOOL, tool_name="filesystem.manage", tool_input={}, risk_level=RiskLevel.LOW),
        OperatorDecision(action=OperatorAction.DONE, final_answer="Here are the episodes."),
    ])
    auditor = QueueAuditor([AuditResult(sufficient=False, reason="only 2 of 5 requested episodes present")])
    # audit_min_tool_calls=1: these exercise the Auditor on single-tool
    # tasks, which the production default (2) deliberately skips.
    worker = TaskWorker(repos, audit, executor=executor, operator=operator, auditor=auditor,
                        audit_min_tool_calls=1)

    running = await worker.process_task(task.id)
    result = await worker.process_task(running.id)

    assert result.status == TaskStatus.RUNNING
    assert result.metadata["operator_audit_gap_count"] == 1
    assert result.metadata["operator_history"][-1]["status"] == "audit_gap"
    assert "only 2 of 5" in result.metadata["operator_history"][-1]["error"]
    assert "synthesized_answer" not in result.metadata


@pytest.mark.asyncio
async def test_operator_loop_audit_gap_exhausts_and_blocks_without_unverified_answer(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("list the first 5 episodes")
    settings = _settings()
    executor = _executor(settings, audit, repos, tool_name="filesystem.manage", output={"text": "1. Pilot"})
    operator = QueueOperator([
        OperatorDecision(action=OperatorAction.CALL_TOOL, tool_name="filesystem.manage", tool_input={}, risk_level=RiskLevel.LOW),
        OperatorDecision(action=OperatorAction.DONE, final_answer="attempt 1"),
        OperatorDecision(action=OperatorAction.DONE, final_answer="attempt 2"),
        OperatorDecision(action=OperatorAction.DONE, final_answer="attempt 3"),
    ])
    auditor = QueueAuditor([
        AuditResult(sufficient=False, reason="still insufficient"),
        AuditResult(sufficient=False, reason="still insufficient"),
    ])
    # audit_min_tool_calls=1: these exercise the Auditor on single-tool
    # tasks, which the production default (2) deliberately skips.
    worker = TaskWorker(repos, audit, executor=executor, operator=operator, auditor=auditor,
                        audit_min_tool_calls=1)

    result = task
    for _ in range(5):
        result = await worker.process_task(result.id)
        if result.status != TaskStatus.RUNNING:
            break

    assert result.status == TaskStatus.BLOCKED
    assert result.metadata["operator_audit_gap_count"] == 2
    assert len(auditor.calls) == 2
    assert "unverified answer" in result.metadata["last_worker_error"]


@pytest.mark.asyncio
async def test_operator_loop_skips_audit_when_no_content_tool_was_called(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("trivial question")
    settings = _settings()
    executor = _executor(settings, audit, repos)
    operator = QueueOperator([OperatorDecision(action=OperatorAction.DONE, final_answer="answer")])
    auditor = QueueAuditor([])
    # audit_min_tool_calls=1: these exercise the Auditor on single-tool
    # tasks, which the production default (2) deliberately skips.
    worker = TaskWorker(repos, audit, executor=executor, operator=operator, auditor=auditor,
                        audit_min_tool_calls=1)

    completed = await worker.process_task(task.id)

    assert completed.status == TaskStatus.COMPLETED
    assert completed.metadata["synthesized_answer"] == "answer"
    assert auditor.calls == []


@pytest.mark.asyncio
async def test_operator_loop_rate_limited_result_backs_off_instead_of_hot_looping(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("rate limited task")
    settings = _settings()
    executor = _executor_with_adapter(settings, audit, repos, RateLimitedAdapter())
    operator = QueueOperator([
        OperatorDecision(action=OperatorAction.CALL_TOOL, tool_name="llm", tool_input={}, risk_level=RiskLevel.LOW),
    ])
    worker = TaskWorker(
        repos, audit, executor=executor, operator=operator,
        retry_policy=RetryPolicy(settings.limits),
    )

    result = await worker.process_task(task.id)

    assert result.status == TaskStatus.RETRYING
    assert result.metadata["operator_retry_count"] == 1
    assert result.metadata["next_retry_at"] is not None
    assert result.metadata["operator_history"][-1]["status"] == "rate_limited"


@pytest.mark.asyncio
async def test_operator_loop_retrying_stays_put_before_backoff_elapses(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("rate limited task")
    settings = _settings()
    executor = _executor_with_adapter(settings, audit, repos, RateLimitedAdapter())
    operator = QueueOperator([
        OperatorDecision(action=OperatorAction.CALL_TOOL, tool_name="llm", tool_input={}, risk_level=RiskLevel.LOW),
    ])
    worker = TaskWorker(
        repos, audit, executor=executor, operator=operator,
        retry_policy=RetryPolicy(settings.limits),
    )

    retrying = await worker.process_task(task.id)
    still_retrying = await worker.process_task(retrying.id)

    assert still_retrying.status == TaskStatus.RETRYING
    assert operator.calls == 1  # decide() is not called again while backoff hasn't elapsed


@pytest.mark.asyncio
async def test_operator_loop_retrying_resumes_and_retries_after_backoff_elapses(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("rate limited task")
    settings = _settings()
    executor = _executor_with_adapter(settings, audit, repos, FailsOnceThenSucceedsAdapter(output={"answer": "ok"}))
    operator = QueueOperator([
        OperatorDecision(action=OperatorAction.CALL_TOOL, tool_name="llm", tool_input={}, risk_level=RiskLevel.LOW),
        OperatorDecision(action=OperatorAction.CALL_TOOL, tool_name="llm", tool_input={}, risk_level=RiskLevel.LOW),
        OperatorDecision(action=OperatorAction.DONE, final_answer="ok"),
    ])
    worker = TaskWorker(
        repos, audit, executor=executor, operator=operator,
        retry_policy=RetryPolicy(settings.limits),
    )
    retrying = await worker.process_task(task.id)
    assert retrying.status == TaskStatus.RETRYING
    # Force the backoff window to have already elapsed instead of sleeping in the test.
    repos.tasks.update_metadata(
        retrying.id, {**retrying.metadata, "next_retry_at": "2000-01-01T00:00:00+00:00"},
    )

    resumed = await worker.process_task(retrying.id)
    assert resumed.status == TaskStatus.RUNNING

    running_after_retry = await worker.process_task(resumed.id)
    completed = await worker.process_task(running_after_retry.id)

    assert running_after_retry.metadata["operator_history"][-1]["status"] == "succeeded"
    assert completed.status == TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_retrying_a_timed_out_write_warns_it_may_have_already_happened(tmp_path) -> None:
    """docs/HISTORY.md P6's crash-recovery reasoning ("silently retrying can
    do a thing twice") applies just as much to an ordinary TIMEOUT on a risky
    write - the difference is nothing crashed, so the loop reaches
    decide() again on its own next step instead of asking a human. That
    decide() call only ever sees history as text (operator.py's
    _format_history), so the warning has to live in the history entry's
    error field itself, not in a side channel the model never reads.
    """
    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("move a file")
    settings = AppSettings(
        _env_file=None,
        # Raised above HIGH so this test exercises the retry-warning logic
        # in isolation, not the (separately tested) approval gate that would
        # otherwise intercept a HIGH-risk filesystem write first.
        approval_policy={"require_approval_at_or_above": RiskLevel.CRITICAL},
        capabilities={
            Capability.FILESYSTEM_WRITE: CapabilityPolicy(enabled=True, requires_approval=False, max_risk_level=RiskLevel.HIGH),
        },
    )
    executor = ToolExecutor(
        PolicyEngine(settings, audit),
        repos,
        audit,
        adapters={"filesystem.manage": TimedOutAdapter()},
        tool_definitions={
            "filesystem.manage": ToolDefinition(
                name="filesystem.manage", capability=Capability.FILESYSTEM_WRITE, enabled=True, description="test tool",
            )
        },
    )
    operator = QueueOperator([
        OperatorDecision(
            action=OperatorAction.CALL_TOOL, tool_name="filesystem.manage",
            tool_input={"operation": "apply_manifest"}, risk_level=RiskLevel.HIGH,
        ),
    ])
    worker = TaskWorker(
        repos, audit, executor=executor, operator=operator,
        retry_policy=RetryPolicy(settings.limits),
    )

    result = await worker.process_task(task.id)

    assert result.status == TaskStatus.RETRYING
    error = result.metadata["operator_history"][-1]["error"]
    assert "the call did not return in time" in error
    assert "may have already taken effect" in error


@pytest.mark.asyncio
async def test_retrying_a_timed_out_read_does_not_warn(tmp_path) -> None:
    """The same TIMEOUT on a low-risk read carries no such warning - nothing
    was left ambiguous by repeating a read, so nothing should make the model
    more hesitant to just try again."""
    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("check a status page")
    settings = _settings()
    executor = _executor_with_adapter(settings, audit, repos, TimedOutAdapter())
    operator = QueueOperator([
        OperatorDecision(action=OperatorAction.CALL_TOOL, tool_name="llm", tool_input={}, risk_level=RiskLevel.LOW),
    ])
    worker = TaskWorker(
        repos, audit, executor=executor, operator=operator,
        retry_policy=RetryPolicy(settings.limits),
    )

    result = await worker.process_task(task.id)

    assert result.status == TaskStatus.RETRYING
    error = result.metadata["operator_history"][-1]["error"]
    assert error == "the call did not return in time"


@pytest.mark.asyncio
async def test_operator_loop_usage_limited_waits_and_resumes_by_itself(tmp_path) -> None:
    """A usage limit is a timer, not a question for the operator.

    This asserted CLARIFYING: the task stopped and waited for a human reply.
    That is the wrong trade for the case it exists to serve - an unattended
    overnight Copilot build - because the quota comes back on its own and the
    human contributes only the delay until they next check their phone.

    It now parks as RETRYING with next_retry_at set, which
    _process_operator_retrying already honors, and claim_next skips while it
    is not due so the wait cannot starve other work.
    """
    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("usage limited task")
    settings = _settings()
    executor = _executor_with_adapter(settings, audit, repos, UsageLimitedAdapter())
    operator = QueueOperator([
        OperatorDecision(action=OperatorAction.CALL_TOOL, tool_name="llm", tool_input={}, risk_level=RiskLevel.LOW),
    ])
    worker = TaskWorker(
        repos, audit, executor=executor, operator=operator,
        retry_policy=RetryPolicy(settings.limits),
    )

    result = await worker.process_task(task.id)

    assert result.status == TaskStatus.RETRYING
    assert result.metadata["next_retry_at"]
    assert result.metadata["usage_limit_wait"]["wait_seconds"] > 0
    assert "clarifying_question" not in result.metadata


@pytest.mark.asyncio
async def test_operator_loop_background_session_awaits_external_completion(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("kick off a coding session")
    settings = _settings()
    executor = _executor_with_adapter(settings, audit, repos, BackgroundSessionAdapter(), tool_name="coding.agent")
    operator = QueueOperator([
        OperatorDecision(action=OperatorAction.CALL_TOOL, tool_name="coding.agent", tool_input={"prompt": "fix it"}, risk_level=RiskLevel.LOW),
    ])
    worker = TaskWorker(repos, audit, executor=executor, operator=operator)

    result = await worker.process_task(task.id)

    assert result.status == TaskStatus.AWAITING_EXTERNAL
    assert result.metadata["awaiting_external"]["session_id"] == "sess-1"
    assert result.metadata["operator_pending_call"]["tool_name"] == "coding.agent"
    assert result.metadata["operator_history"][-1]["status"] == "running"


@pytest.mark.asyncio
async def test_operator_loop_awaiting_external_is_a_no_op_tick_until_callback_resolves(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("kick off a coding session")
    settings = _settings()
    executor = _executor_with_adapter(settings, audit, repos, BackgroundSessionAdapter(), tool_name="coding.agent")
    operator = QueueOperator([
        OperatorDecision(action=OperatorAction.CALL_TOOL, tool_name="coding.agent", tool_input={"prompt": "fix it"}, risk_level=RiskLevel.LOW),
    ])
    worker = TaskWorker(repos, audit, executor=executor, operator=operator)

    awaiting = await worker.process_task(task.id)
    still_awaiting = await worker.process_task(awaiting.id)

    assert still_awaiting.status == TaskStatus.AWAITING_EXTERNAL
    assert operator.calls == 1  # decide() is not called again while the session is still running


@pytest.mark.asyncio
async def test_operator_loop_resumes_after_external_completion_callback(tmp_path) -> None:
    """Mirrors cli.py's _coding_session_completion_callback: writes
    pending_tool_result and flips AWAITING_EXTERNAL back to RUNNING - that
    callback only checks task.status, so it works unchanged for an
    operator-loop task."""
    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("kick off a coding session")
    settings = _settings()
    executor = _executor_with_adapter(settings, audit, repos, BackgroundSessionAdapter(), tool_name="coding.agent")
    operator = QueueOperator([
        OperatorDecision(action=OperatorAction.CALL_TOOL, tool_name="coding.agent", tool_input={"prompt": "fix it"}, risk_level=RiskLevel.LOW),
        OperatorDecision(action=OperatorAction.DONE, final_answer="Session completed successfully."),
    ])
    worker = TaskWorker(repos, audit, executor=executor, operator=operator)

    awaiting = await worker.process_task(task.id)
    assert awaiting.status == TaskStatus.AWAITING_EXTERNAL

    finished_result = ToolCallResult(
        request_id="toolreq_x", status=ToolResultStatus.SUCCEEDED,
        output={"status": "completed", "returncode": 0, "session_id": "sess-1"},
    )
    repos.tasks.update_metadata(
        awaiting.id,
        {
            **awaiting.metadata,
            "pending_tool_result": {"tool_name": "coding.agent", "result": finished_result.model_dump(mode="json")},
        },
        TaskStatus.RUNNING,
    )

    resumed = await worker.process_task(awaiting.id)
    assert resumed.status == TaskStatus.RUNNING
    assert "pending_tool_result" not in resumed.metadata
    assert "awaiting_external" not in resumed.metadata
    assert resumed.metadata["operator_history"][-1]["status"] == "succeeded"

    completed = await worker.process_task(resumed.id)
    assert completed.status == TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_worker_without_operator_or_executor_blocks_instead_of_crashing(tmp_path) -> None:
    """A misconfigured worker (no LLM provider available to build an operator
    from, e.g.) fails safely and explicitly rather than raising or silently
    doing nothing."""
    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("plain task, nothing configured")
    worker = TaskWorker(repos, audit)

    result = await worker.process_task(task.id)

    assert result.status == TaskStatus.BLOCKED
    events = repos.audit.list_for_task(task.id)
    assert any(event.payload.get("error") == "operator_loop_not_configured" for event in events)


@pytest.mark.asyncio
async def test_worker_accumulates_token_usage_across_operator_and_auditor_calls(tmp_path) -> None:
    """docs/HISTORY.md Part 4 T1.4: the worker used to discard LLM usage
    entirely, so there was no way to see what a task cost. Two operator
    decide() calls plus one auditor audit() call must accumulate into
    task.metadata["token_usage"], split both as a running total and per
    source (operator vs auditor), since they're typically different models
    (major_provider escalation, or the Auditor using a smaller profile)."""
    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("what is the invoice total?")
    settings = _settings()
    executor = _executor(settings, audit, repos, tool_name="filesystem.manage", output={"text": "Invoice #4471 - $250.00"})
    operator = QueueOperator(
        [
            OperatorDecision(action=OperatorAction.CALL_TOOL, tool_name="filesystem.manage", tool_input={}, risk_level=RiskLevel.LOW),
            OperatorDecision(action=OperatorAction.DONE, final_answer="I found an invoice."),
        ],
        usages=[
            {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120, "model": "gpt-4.1"},
            {"prompt_tokens": 150, "completion_tokens": 10, "total_tokens": 160, "model": "gpt-4.1"},
        ],
    )
    auditor = QueueAuditor(
        [AuditResult(sufficient=True, answer="The invoice total is $250.00.")],
        usages=[{"prompt_tokens": 50, "completion_tokens": 15, "total_tokens": 65, "model": "gpt-4.1-mini"}],
    )
    # audit_min_tool_calls=1: these exercise the Auditor on single-tool
    # tasks, which the production default (2) deliberately skips.
    worker = TaskWorker(repos, audit, executor=executor, operator=operator, auditor=auditor,
                        audit_min_tool_calls=1)

    running = await worker.process_task(task.id)
    completed = await worker.process_task(running.id)

    assert completed.status == TaskStatus.COMPLETED
    usage = completed.metadata["token_usage"]
    assert usage["calls"] == 3
    assert usage["prompt_tokens"] == 100 + 150 + 50
    assert usage["completion_tokens"] == 20 + 10 + 15
    assert usage["total_tokens"] == 120 + 160 + 65
    assert usage["by_source"]["operator"] == {
        "calls": 2, "prompt_tokens": 250, "completion_tokens": 30, "total_tokens": 280,
    }
    assert usage["by_source"]["auditor"] == {
        "calls": 1, "prompt_tokens": 50, "completion_tokens": 15, "total_tokens": 65,
    }
    assert usage["last_model"] == "gpt-4.1-mini"  # the auditor's model, since it ran last


@pytest.mark.asyncio
async def test_worker_does_not_record_token_usage_when_provider_reports_none(tmp_path) -> None:
    """A ScriptedLLMProvider (scenario replay) or a server that omits usage
    must leave token_usage entirely absent, not create a fabricated zero
    entry - see the docstring on TaskWorker._record_llm_usage."""
    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("plain status-ish task")
    settings = _settings()
    executor = _executor(settings, audit, repos)
    operator = QueueOperator([OperatorDecision(action=OperatorAction.DONE, final_answer="Done.")])
    worker = TaskWorker(repos, audit, executor=executor, operator=operator)

    completed = await worker.process_task(task.id)

    assert completed.status == TaskStatus.COMPLETED
    assert "token_usage" not in completed.metadata


@pytest.mark.asyncio
async def test_worker_prefers_major_provider_on_the_step_after_an_audit_gap(tmp_path) -> None:
    """docs/HISTORY.md Part 4 T2.6: once a `done` has been rejected once
    (an audit-gap or fulfillment-gap marker is in history), the NEXT
    decide() call should prefer the major provider - a done-was-rejected
    signal is exactly the kind of observed difficulty the escalation logic
    was already designed to react to, just available before the call this
    time instead of only from a caught parse exception."""
    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("list the first 5 episodes")
    settings = _settings()
    executor = _executor(settings, audit, repos, tool_name="filesystem.manage", output={"text": "1. Pilot\n2. Second"})
    operator = QueueOperator([
        OperatorDecision(action=OperatorAction.CALL_TOOL, tool_name="filesystem.manage", tool_input={}, risk_level=RiskLevel.LOW),
        OperatorDecision(action=OperatorAction.DONE, final_answer="Here are the episodes."),
        OperatorDecision(action=OperatorAction.DONE, final_answer="Here are the (better) episodes."),
    ])
    auditor = QueueAuditor([
        AuditResult(sufficient=False, reason="only 2 of 5 requested episodes present"),
        AuditResult(sufficient=True, answer="grounded final answer"),
    ])
    # audit_min_tool_calls=1: these exercise the Auditor on single-tool
    # tasks, which the production default (2) deliberately skips.
    worker = TaskWorker(repos, audit, executor=executor, operator=operator, auditor=auditor,
                        audit_min_tool_calls=1)

    running = await worker.process_task(task.id)  # step 1: call_tool
    gapped = await worker.process_task(running.id)  # step 2: done -> audit gap
    await worker.process_task(gapped.id)  # step 3: done again, now with the gap marker in history

    # Steps 1 and 2 have no prior gap marker yet; step 3 runs after the
    # audit-gap entry was appended to history, so only it should prefer major.
    assert operator.prefer_major_calls == [False, False, True]


@pytest.mark.asyncio
async def test_auditor_mode_does_not_let_word_matching_overrule_the_agents(tmp_path) -> None:
    """A `done` both agents accepted must not be vetoed by hardcoded intent inference.

    fulfillment.py derives what a task "should" produce by intersecting the
    objective with word sets - `{"organize","move","rename","sort"}` and
    friends - and could reject a `done` the Operator declared AND the Auditor
    confirmed. A semantic judgment made in Python, enforced over two LLM
    judgments that already agreed.

    The objective below deliberately trips the FILE_ORGANIZATION word sets
    ("organize" + "files") while producing no organization metadata, which is
    exactly the shape the legacy gate rejects.
    """
    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("organize the files and tell me what you found")
    executor = _executor(_settings(), audit, repos, output={"answer": "42"})
    decisions = [
        OperatorDecision(action=OperatorAction.CALL_TOOL, tool_name="llm", tool_input={}, risk_level=RiskLevel.LOW),
        OperatorDecision(action=OperatorAction.DONE, final_answer="Nothing needed moving."),
    ]
    auditor = QueueAuditor([AuditResult(sufficient=True, answer="Nothing needed moving.")])
    worker = TaskWorker(
        repos, audit, executor=executor,
        operator=QueueOperator(list(decisions)), auditor=auditor,
    )

    running = await worker.process_task(task.id)
    done = await worker.process_task(running.id)

    assert done.status == TaskStatus.COMPLETED
    assert "operator_fulfillment_gap_count" not in done.metadata


@pytest.mark.asyncio
async def test_heuristic_mode_still_available_as_a_rollback(tmp_path) -> None:
    """The legacy gate must remain reachable, so the change can be undone
    from config without a code revert."""
    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("organize the files and tell me what you found")
    executor = _executor(_settings(), audit, repos, output={"answer": "42"})
    decisions = [
        OperatorDecision(action=OperatorAction.CALL_TOOL, tool_name="llm", tool_input={}, risk_level=RiskLevel.LOW),
        OperatorDecision(action=OperatorAction.DONE, final_answer="Nothing needed moving."),
    ]
    auditor = QueueAuditor([AuditResult(sufficient=True, answer="Nothing needed moving.")])
    worker = TaskWorker(
        repos, audit, executor=executor,
        operator=QueueOperator(list(decisions)), auditor=auditor,
        fulfillment_mode="heuristic",
    )

    running = await worker.process_task(task.id)
    gated = await worker.process_task(running.id)

    assert gated.status == TaskStatus.RUNNING
    assert gated.metadata["operator_fulfillment_gap_count"] == 1


def test_deliverable_evidence_reports_facts_without_inferring_intent(tmp_path) -> None:
    """The evidence handed to the Auditor must describe the task record only.

    This is the piece that replaces `_postconditions_from_objective`: the same
    `_postcondition_satisfied` checks, reported for every postcondition type,
    with the "which of these did the user actually want" decision left to the
    agent that can read the request.
    """
    from agent_control.orchestration.fulfillment import deliverable_evidence

    repos, _audit = make_repos(tmp_path)
    task = repos.tasks.create("anything at all")
    task = repos.tasks.update_metadata(task.id, {**task.metadata, "workspace_dir": "/tmp/ws"})

    evidence = deliverable_evidence(task)

    assert "workspace_dir" in evidence.split("Not produced:")[0]
    assert "schedule_created" in evidence.split("Not produced:")[1]
    # No objective text anywhere - it must not re-derive intent.
    assert "anything at all" not in evidence
