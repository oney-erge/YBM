"""Scenario: "use the code interpreter to build a CSV, then summarize it into
JSON" through the real Operator loop -> code.interpreter (which generates its
own script via a second structured-output call), called TWICE - once to
create expenses.csv, once to read that same file back off disk and compute
the total. Ports e2e/all_cases.json's `code_interpreter_csv_summary` case
down to the deterministic tier (docs/archive/HISTORY.md P2). This is the suite's
only coverage of **multi-call file chaining**: two separate tool calls in
one task sharing one workspace, where call 2 depends on call 1's output.
That is what makes it worth keeping distinct from
test_code_interpreter.py's single-call case.

Real bug found re-recording this fixture 2026-07-29, and fixed the same day:
`code_interpreter.py`'s `_workspace()` appended `uuid4().hex[:8]`, giving
every individual `code.interpreter` call its own fresh directory. Task ids
are already unique, so that suffix only ever separated calls *within* one
task - exactly the thing that must be shared - and it silently broke every
multi-step file workflow: step 2 landed in an empty directory and could
never see the file step 1 wrote. Reproduced identically on three independent
live recording attempts. Fixed by making the workspace stable per task
(`root / f"task_{task_id}"`); see that function's docstring, the three
regression tests in test_code_interpreter.py, and docs/archive/HISTORY.md Part 2 §4
item 7.

The objective now spells out both steps explicitly. Its earlier phrasing
("create a small CSV ... then run Python to calculate the total and write
expense-summary.json") under-specified them, and the model quite reasonably
computed the totals inline in a single call, never writing the intermediate
CSV this test asserts on. The model's one-call answer was not wrong - the
instructions were. Asking for what the test actually verifies is the fix.

Delivery is deliberately NOT part of this scenario. This file used to assert
`scenario.telegram.documents` was non-empty while the objective never asked
for anything to be sent - the same copy-paste assertion bug found and fixed
in test_code_interpreter_generate_file.py. Briefly tried adding "Then send
me expense-summary.json" to satisfy it, and found the local 8B model
reliably drops that trailing clause once the two main steps are done (a
real, mildly interesting instruction-following limit, but not this test's
subject). Removed the clause instead, so objective and assertions match
exactly; artifact.deliver is already covered end to end by
test_send_found_pdf.py and test_desktop_file_search_then_delivery.py.
"""

from __future__ import annotations

from pathlib import Path

from agent_control.config import CapabilityPolicy, default_capability_policies
from agent_control.schemas import Capability, RiskLevel
import pytest

from .harness import assert_completed, assert_rejected, build_scenario, isolated_settings, run_task_to_completion, scenario_scratch_dir


@pytest.mark.asyncio
async def test_code_interpreter_csv_summary_writes_json_totals(tmp_path, monkeypatch) -> None:
    workspace = scenario_scratch_dir("code_interpreter_csv_summary")

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
    scenario = build_scenario(settings, tmp_path=tmp_path, fixture_name="code_interpreter_csv_summary")

    task = await run_task_to_completion(
        scenario,
        "Use the local code interpreter for this, in two separate steps. "
        "Step 1: write a file named expenses.csv containing these expenses - "
        "hosting 25, OCR review 45, browser testing 120. "
        "Step 2: run a second script that reads expenses.csv back from disk, "
        "sums the amounts, and writes expense-summary.json with the total.",
        # generate_and_run is unconditionally approval-gated by design
        # (code_interpreter.py's ToolDefinition, approval_required_operations).
        # Simulate a human approving both of this test's two calls - it's
        # about execution correctness, not the approval gate itself.
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
    # Both files, from two separate calls sharing one workspace - the whole
    # point of this scenario (see module docstring).
    assert any("expenses.csv" in name for name in created_files)
    assert any("expense-summary.json" in name for name in created_files)
    # Note: content correctness of the generated script is NOT verified
    # anywhere in this path today - only that it ran and produced the files.
    # Delivery is deliberately out of scope here (see module docstring);
    # test_send_found_pdf.py and test_desktop_file_search_then_delivery.py
    # both cover artifact.deliver end to end.
    artifacts = scenario.repositories.artifacts.list_for_task(task.id)
    summary_artifact = next(a for a in artifacts if a.uri and "expense-summary.json" in a.uri)
    summary_path = Path(summary_artifact.uri.removeprefix("file://"))
    assert summary_path.exists()


@pytest.mark.asyncio
async def test_code_interpreter_csv_summary_disabled_by_capability_policy(tmp_path, monkeypatch) -> None:
    workspace = scenario_scratch_dir("code_interpreter_csv_summary")

    # TERMINAL_RUN left at its secure-by-default disabled state.
    settings = isolated_settings(
        monkeypatch, tmp_path,
        adapters={"code_interpreter": {"enabled": True, "workspace_root": str(workspace)}},
    )
    scenario = build_scenario(settings, tmp_path=tmp_path, fixture_name="code_interpreter_csv_summary")

    task = await run_task_to_completion(
        scenario,
        "Use the local code interpreter for this, in two separate steps. "
        "Step 1: write a file named expenses.csv containing these expenses - "
        "hosting 25, OCR review 45, browser testing 120. "
        "Step 2: run a second script that reads expenses.csv back from disk, "
        "sums the amounts, and writes expense-summary.json with the total.",
    )

    assert_rejected(task)