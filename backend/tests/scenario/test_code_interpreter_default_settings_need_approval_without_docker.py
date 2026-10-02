"""Scenario: proves the code.interpreter approval gate (docs/archive/HISTORY.md P5)
holds through the *entire* real worker/planner/policy/registry/executor
stack under genuinely default settings - not just at the adapter unit-test
level (see test_code_interpreter.py's
test_code_interpreter_generated_run_needs_approval_on_silent_docker_fallback
for that).

Every other code.interpreter scenario test explicitly sets
`require_approval_for_untrusted_run_python: False` to test execution
correctness in isolation from this gate - this is the one test in the suite
that deliberately leaves it at its real default (True), reusing
test_code_interpreter_generate_file.py's fixture (recorded against a live
LLM, no live LLM needed here since the plan is scripted-replayed) to confirm
that with zero configuration changes, on a machine without Docker running
(true of the machine these fixtures were recorded on), the worker correctly
stops short of running LLM-generated code unsandboxed and surfaces a
clarifying question instead of silently executing it.
"""

from __future__ import annotations

from agent_control.config import CapabilityPolicy, default_capability_policies
from agent_control.schemas import Capability, RiskLevel, TaskStatus
import pytest

from .harness import build_scenario, isolated_settings, run_task_to_completion, scenario_scratch_dir


@pytest.mark.asyncio
async def test_generated_code_needs_approval_under_true_default_settings(tmp_path, monkeypatch) -> None:
    workspace = scenario_scratch_dir("code_interpreter_generate_file")

    caps = default_capability_policies()
    caps[Capability.TERMINAL_RUN] = CapabilityPolicy(enabled=True, requires_approval=False, max_risk_level=RiskLevel.HIGH)
    settings = isolated_settings(
        monkeypatch, tmp_path,
        capabilities=caps,
        # require_approval_for_untrusted_run_python left unset - true default (True).
        adapters={"code_interpreter": {"enabled": True, "workspace_root": str(workspace)}},
    )
    scenario = build_scenario(settings, tmp_path=tmp_path, fixture_name="code_interpreter_generate_file")

    task = await run_task_to_completion(
        scenario,
        "Use the local code interpreter to create a file named interpreter-report.txt with a "
        "two-line summary of this test, run the script, and tell me where the file is.",
    )

    # Not COMPLETED: the code never ran, so no file was created and nothing
    # was delivered - the gate fires before any execution, not after.
    # AWAITING_APPROVAL, specifically, and staying there: this scenario
    # never approves the request (that's the point), so the harness treats
    # it as settled rather than looping until its tick budget raises - see
    # harness.py's TERMINAL_STATUSES.
    assert task.status == TaskStatus.AWAITING_APPROVAL
    tool_calls = scenario.repositories.tool_invocations.list_for_task(task.id)
    interpreter_calls = [call for call in tool_calls if call["tool_name"] == "code.interpreter"]
    assert interpreter_calls
    # Not necessarily every call: the executor also enforces that a
    # declared risk_level can't understate a tool's actual minimum (a
    # separate, independently-added safety check - see
    # orchestration/executor.py) - a first attempt that under-declares risk
    # fails validation before the approval gate is even reached, and the
    # model may retry with the correct risk_level before the gate does fire.
    # The gate itself firing at least once is what this test is about.
    needing_approval = [call for call in interpreter_calls if call["result"]["status"] == "needs_approval"]
    assert needing_approval
    # The NEEDS_APPROVAL result itself carries only {"approval_id": ...} -
    # the human-facing "why" lives on the ApprovalRequest.summary instead,
    # via ToolDefinition.approval_reasons (docs/archive/HISTORY.md Part 4). Asserts
    # both the record's identity (tool/capability/risk) and that a human
    # reading it actually learns why - not just a generic "Approve X using Y".
    approvals = scenario.repositories.approvals.list_for_task(task.id)
    assert approvals
    assert all(a.capability.value == "terminal.run" and a.risk_level.value == "high" for a in approvals)
    assert any("unsandboxed" in a.summary for a in approvals)
    assert not scenario.telegram.documents
