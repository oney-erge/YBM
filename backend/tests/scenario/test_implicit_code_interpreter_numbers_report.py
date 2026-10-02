"""Scenario: "write and run a small local Python script" through the real
Operator loop -> code.interpreter, WITHOUT the objective naming the adapter.
Ports e2e/all_cases.json's `implicit_code_interpreter_numbers_report` case
down to the deterministic tier (docs/archive/HISTORY.md P2) - unlike
test_code_interpreter_csv_summary.py/test_code_interpreter_generate_file.py
(which both say "use the local code interpreter" explicitly), this locks in
implicit routing: the Operator has to recognize a bounded local script task
from context alone and NOT send it to coding.agent (Codex/Copilot), which is
also in the registry and could plausibly be chosen instead. Fixture
re-recorded 2026-07-29 (`ybm scenario record
implicit_code_interpreter_numbers_report`, localdeploy_qwen3vl_8b).
"""

from __future__ import annotations

from agent_control.config import CapabilityPolicy, default_capability_policies
from agent_control.schemas import Capability, RiskLevel
import pytest

from .harness import assert_completed, assert_rejected, build_scenario, isolated_settings, run_task_to_completion, scenario_scratch_dir


@pytest.mark.asyncio
async def test_implicit_routing_selects_code_interpreter_not_coding_agent(tmp_path, monkeypatch) -> None:
    workspace = scenario_scratch_dir("implicit_code_interpreter_numbers_report")

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
    scenario = build_scenario(settings, tmp_path=tmp_path, fixture_name="implicit_code_interpreter_numbers_report")

    task = await run_task_to_completion(
        scenario,
        "Write and run a small local Python script that creates numbers-summary.json for the "
        "numbers 3, 5, and 8. Include the count, total, and average, then tell me where the file is.",
        # generate_and_run is unconditionally approval-gated by design
        # (code_interpreter.py's ToolDefinition, approval_required_operations).
        # This test is about routing correctness, not the gate itself.
        auto_approve=True,
    )

    assert_completed(task)
    tool_calls = scenario.repositories.tool_invocations.list_for_task(task.id)
    tool_names = {call["tool_name"] for call in tool_calls}
    assert "code.interpreter" in tool_names
    assert "coding.agent" not in tool_names
    assert "vscode.copilot_terminal" not in tool_names
    created_files = [
        name
        for call in tool_calls
        if call["tool_name"] == "code.interpreter"
        for name in (call["result"] or {}).get("output", {}).get("files_created", [])
    ]
    assert any("numbers-summary.json" in name for name in created_files)


@pytest.mark.asyncio
async def test_implicit_routing_disabled_by_capability_policy(tmp_path, monkeypatch) -> None:
    workspace = scenario_scratch_dir("implicit_code_interpreter_numbers_report")

    # TERMINAL_RUN left at its secure-by-default disabled state.
    settings = isolated_settings(
        monkeypatch, tmp_path,
        adapters={"code_interpreter": {"enabled": True, "workspace_root": str(workspace)}},
    )
    scenario = build_scenario(settings, tmp_path=tmp_path, fixture_name="implicit_code_interpreter_numbers_report")

    task = await run_task_to_completion(
        scenario,
        "Write and run a small local Python script that creates numbers-summary.json for the "
        "numbers 3, 5, and 8. Include the count, total, and average, then tell me where the file is.",
    )

    assert_rejected(task)