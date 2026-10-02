"""Scenario: "summarize the PDF at X" through the real Operator loop ->
document.manage summarize_pdf -> Auditor. Replayed from a fixture recorded
against a live LLM. Ports e2e/all_cases.json's `pdf_open_summary` case down
to the deterministic tier (docs/archive/HISTORY.md P2). Fixture re-recorded
2026-07-28 (`ybm scenario record document_pdf_summary`,
localdeploy_qwen3vl_8b).
"""

from __future__ import annotations

import sys
from pathlib import Path

from agent_control.config import CapabilityPolicy, default_capability_policies
from agent_control.schemas import Capability, RiskLevel
import pytest

from .harness import assert_completed, assert_rejected, build_scenario, isolated_settings, run_task_to_completion, scenario_scratch_dir

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "e2e"))
from fixtures import _write_minimal_pdf  # noqa: E402


@pytest.mark.asyncio
async def test_document_pdf_summary(tmp_path, monkeypatch) -> None:
    docs_dir = scenario_scratch_dir("document_pdf_summary")
    pdf_path = docs_dir / "quarterly_report.pdf"
    _write_minimal_pdf(
        pdf_path,
        "Quarterly Report Summary. Revenue: 4.2 million dollars, up 12 percent quarter over "
        "quarter. Expenses: 2.8 million dollars. Net profit: 1.4 million dollars. Headcount "
        "grew from 40 to 48 employees. Outlook: positive, driven by new enterprise contracts.",
    )

    caps = default_capability_policies()
    caps[Capability.FILESYSTEM_WRITE] = CapabilityPolicy(enabled=True, requires_approval=False, max_risk_level=RiskLevel.HIGH)
    settings = isolated_settings(
        monkeypatch, tmp_path,
        capabilities=caps,
        adapters={"computer_use": {"enabled": True, "allowed_roots": [str(docs_dir)]}},
    )
    scenario = build_scenario(settings, tmp_path=tmp_path, fixture_name="document_pdf_summary")

    task = await run_task_to_completion(scenario, f"summarize the PDF at {pdf_path}")

    assert_completed(task)
    answer = task.metadata["synthesized_answer"]
    assert "4.2 million" in answer or "4,200,000" in answer
    tool_calls = scenario.repositories.tool_invocations.list_for_task(task.id)
    assert any(call["tool_name"] == "document.manage" for call in tool_calls)


@pytest.mark.asyncio
async def test_document_pdf_summary_denied_without_filesystem_write(tmp_path, monkeypatch) -> None:
    docs_dir = scenario_scratch_dir("document_pdf_summary")
    pdf_path = docs_dir / "quarterly_report.pdf"
    _write_minimal_pdf(pdf_path, "placeholder content, not used - capability is off")

    # No capability override: FILESYSTEM_WRITE (and therefore document.manage)
    # stays at its secure-by-default disabled state.
    settings = isolated_settings(
        monkeypatch, tmp_path,
        adapters={"computer_use": {"enabled": True, "allowed_roots": [str(docs_dir)]}},
    )
    scenario = build_scenario(settings, tmp_path=tmp_path, fixture_name="document_pdf_summary")

    task = await run_task_to_completion(scenario, f"summarize the PDF at {pdf_path}")

    assert_rejected(task)