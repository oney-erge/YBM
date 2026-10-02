"""Scenario: "inspect this folder and tell me what's inside" through the real
Operator loop -> filesystem.manage inspect_folder -> Auditor. Ports
e2e/all_cases.json's `folder_open_inspection` case down to the deterministic
tier (docs/archive/HISTORY.md P2) - the inspect_folder operation counterpart to
test_filesystem_search.py's search operation. Fixture re-recorded 2026-07-28
against the Operator loop (`ybm scenario record folder_open_inspection`,
localdeploy_qwen3vl_8b) after the plan-based path it was originally recorded
against was deleted.
"""

from __future__ import annotations

import pytest

from .harness import assert_completed, assert_rejected, build_scenario, filesystem_settings, run_task_to_completion, scenario_scratch_dir




@pytest.mark.asyncio
async def test_folder_inspection_reports_files_and_subfolders(tmp_path, monkeypatch) -> None:
    docs_dir = scenario_scratch_dir("folder_open_inspection")
    (docs_dir / "notes.txt").write_text("notes for e2e organization", encoding="utf-8")
    (docs_dir / "budget.csv").write_bytes(b"name,amount\r\nsample,10\r\n")
    (docs_dir / "archive").mkdir()

    settings = filesystem_settings(monkeypatch, tmp_path, str(docs_dir))
    scenario = build_scenario(settings, tmp_path=tmp_path, fixture_name="folder_open_inspection")

    task = await run_task_to_completion(
        scenario, f"Inspect the folder {docs_dir} and tell me what files and subfolders are inside."
    )

    assert_completed(task)
    answer = task.metadata.get("synthesized_answer", "")
    assert "notes.txt" in answer
    assert "budget.csv" in answer
    tool_calls = scenario.repositories.tool_invocations.list_for_task(task.id)
    assert any(call["tool_name"] == "filesystem.manage" for call in tool_calls)


@pytest.mark.asyncio
async def test_folder_inspection_rejects_path_outside_allowed_roots(tmp_path, monkeypatch) -> None:
    docs_dir = scenario_scratch_dir("folder_open_inspection")
    (docs_dir / "notes.txt").write_text("notes for e2e organization", encoding="utf-8")
    (docs_dir / "budget.csv").write_bytes(b"name,amount\r\nsample,10\r\n")
    (docs_dir / "archive").mkdir()

    # allowed_roots does NOT include docs_dir - the same recorded plan should
    # still be produced (same prompt), but policy/execution must refuse it.
    settings = filesystem_settings(monkeypatch, tmp_path, str(tmp_path / "somewhere_else"))
    scenario = build_scenario(settings, tmp_path=tmp_path, fixture_name="folder_open_inspection")

    task = await run_task_to_completion(
        scenario, f"Inspect the folder {docs_dir} and tell me what files and subfolders are inside."
    )

    assert_rejected(task)