"""Scenario: "find a .txt file and read it to me" through the real Operator
loop - a 2-step filesystem.manage sequence (search, then read_file) chosen
one at a time -> Auditor. Ports e2e/all_cases.json's `file_find_and_read`
case down to the deterministic tier (docs/archive/HISTORY.md P2) - locks in
multi-step tool use within a single tool, not just multi-tool sequences.
Fixture re-recorded 2026-07-28 against the Operator loop
(`ybm scenario record file_find_and_read`, localdeploy_qwen3vl_8b).
"""

from __future__ import annotations

import pytest

from .harness import assert_completed, assert_rejected, build_scenario, filesystem_settings, run_task_to_completion, scenario_scratch_dir




@pytest.mark.asyncio
async def test_file_find_and_read_returns_file_contents(tmp_path, monkeypatch) -> None:
    desktop_dir = scenario_scratch_dir("file_find_and_read")
    (desktop_dir / "resume-notes.txt").write_text(
        "Oney resume notes: Python automation, local LLM orchestration, desktop control.",
        encoding="utf-8",
    )
    (desktop_dir / "receipt.png").write_bytes(b"\x89PNG\r\n\x1a\n")

    settings = filesystem_settings(monkeypatch, tmp_path, str(desktop_dir))
    scenario = build_scenario(settings, tmp_path=tmp_path, fixture_name="file_find_and_read")

    task = await run_task_to_completion(
        scenario, f"Find a .txt file in {desktop_dir} and read me its contents"
    )

    assert_completed(task)
    answer = task.metadata.get("synthesized_answer", "")
    assert "resume-notes.txt" in answer
    assert "Python automation" in answer
    tool_calls = scenario.repositories.tool_invocations.list_for_task(task.id)
    operations = [
        call["request"].get("input", {}).get("operation")
        for call in tool_calls
        if call["tool_name"] == "filesystem.manage"
    ]
    # Either operation is a grounded discovery step for a known folder: the
    # operator may list it directly or search it before selecting the file.
    assert {"inspect_folder", "search"} & set(operations)
    assert "read_file" in operations


@pytest.mark.asyncio
async def test_file_find_and_read_rejects_path_outside_allowed_roots(tmp_path, monkeypatch) -> None:
    desktop_dir = scenario_scratch_dir("file_find_and_read")
    (desktop_dir / "resume-notes.txt").write_text(
        "Oney resume notes: Python automation, local LLM orchestration, desktop control.",
        encoding="utf-8",
    )
    (desktop_dir / "receipt.png").write_bytes(b"\x89PNG\r\n\x1a\n")

    settings = filesystem_settings(monkeypatch, tmp_path, str(tmp_path / "somewhere_else"))
    scenario = build_scenario(settings, tmp_path=tmp_path, fixture_name="file_find_and_read")

    task = await run_task_to_completion(
        scenario, f"Find a .txt file in {desktop_dir} and read me its contents"
    )

    assert_rejected(task)