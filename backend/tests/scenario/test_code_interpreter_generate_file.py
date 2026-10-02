"""Scenario: "use the code interpreter to create a report file" through the
real Operator loop -> a single code.interpreter generate_and_run call -> real
local Python execution -> Auditor. Ports e2e/all_cases.json's
`code_interpreter_generate_file` case down to the deterministic tier
(docs/archive/HISTORY.md P2) - the single-step, no-delivery counterpart to
test_code_interpreter_csv_summary.py's two-step, delivery-ending case (the
objective only asks to be told the file's location, not for it to be sent).
Fixture re-recorded 2026-07-28 (`ybm scenario record
code_interpreter_generate_file`, localdeploy_qwen3vl_8b) - re-recording
surfaced a real pre-existing bug in this file: despite the "no-delivery"
docstring above, the test asserted `scenario.telegram.documents` was
non-empty (copy-pasted from the delivery-ending sibling test and never
removed). The Operator loop correctly does not call artifact.deliver for an
objective that never asks to have the file sent - fixed by removing the
incorrect delivery assertions rather than changing the objective or the
model's (correct) behavior.
"""

from __future__ import annotations

from agent_control.config import CapabilityPolicy, default_capability_policies
from agent_control.schemas import Capability, RiskLevel
import pytest

from .harness import assert_completed, assert_rejected, build_scenario, isolated_settings, run_task_to_completion, scenario_scratch_dir


@pytest.mark.asyncio
async def test_code_interpreter_generate_file_writes_report(tmp_path, monkeypatch) -> None:
    workspace = scenario_scratch_dir("code_interpreter_generate_file")

    caps = default_capability_policies()
    caps[Capability.TERMINAL_RUN] = CapabilityPolicy(enabled=True, requires_approval=False, max_risk_level=RiskLevel.HIGH)
    settings = isolated_settings(
        monkeypatch, tmp_path,
        capabilities=caps,
        # require_approval_for_untrusted_run_python: False - the
        # code.interpreter-specific "would run unsandboxed" gate; see
        # test_code_interpreter.py's
        # test_code_interpreter_generated_run_needs_approval_on_silent_docker_fallback.
        # Does NOT disable generate_and_run's OWN separate, unconditional
        # approval requirement - see run_task_to_completion's auto_approve.
        adapters={
            "code_interpreter": {
                "enabled": True,
                "workspace_root": str(workspace),
                "require_approval_for_untrusted_run_python": False,
            }
        },
    )
    scenario = build_scenario(settings, tmp_path=tmp_path, fixture_name="code_interpreter_generate_file")

    task = await run_task_to_completion(
        scenario,
        "Use the local code interpreter to create a file named interpreter-report.txt with a "
        "two-line summary of this test, run the script, and tell me where the file is.",
        # generate_and_run is unconditionally approval-gated by design
        # (code_interpreter.py's ToolDefinition, approval_required_operations).
        # This test is about execution correctness, not the gate itself.
        auto_approve=True,
    )

    assert_completed(task)
    tool_calls = scenario.repositories.tool_invocations.list_for_task(task.id)
    interpreter_calls = [call for call in tool_calls if call["tool_name"] == "code.interpreter"]
    assert interpreter_calls
    created_files = [
        name
        for call in interpreter_calls
        for name in (call["result"] or {}).get("output", {}).get("files_created", [])
    ]
    assert any("interpreter-report.txt" in name for name in created_files)
    # No artifact.deliver expected - the objective only asks to be told
    # where the file is, not for it to be sent. See module docstring.
    assert scenario.telegram.documents == []


@pytest.mark.asyncio
async def test_code_interpreter_generate_file_disabled_by_capability_policy(tmp_path, monkeypatch) -> None:
    workspace = scenario_scratch_dir("code_interpreter_generate_file")

    # TERMINAL_RUN left at its secure-by-default disabled state.
    settings = isolated_settings(
        monkeypatch, tmp_path,
        adapters={"code_interpreter": {"enabled": True, "workspace_root": str(workspace)}},
    )
    scenario = build_scenario(settings, tmp_path=tmp_path, fixture_name="code_interpreter_generate_file")

    task = await run_task_to_completion(
        scenario,
        "Use the local code interpreter to create a file named interpreter-report.txt with a "
        "two-line summary of this test, run the script, and tell me where the file is.",
    )

    assert_rejected(task)