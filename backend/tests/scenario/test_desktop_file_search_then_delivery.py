"""Scenario: "find this file on my desktop and send it to me" through the
real Operator loop - filesystem.manage search then artifact.deliver, chosen
one at a time -> the fake Telegram client. Ports e2e/all_cases.json's
`desktop_file_search_then_delivery` case down to the deterministic tier
(docs/archive/HISTORY.md P2) - combines test_file_find_and_read.py's search step
with test_send_found_pdf.py's delivery step, this time chained from a name
only (no literal path given). Fixture re-recorded 2026-07-28
(`ybm scenario record desktop_file_search_then_delivery`,
localdeploy_qwen3vl_8b).
"""

from __future__ import annotations

import pytest

from .harness import assert_completed, assert_rejected, build_scenario, filesystem_settings, run_task_to_completion, scenario_scratch_dir




@pytest.mark.asyncio
async def test_desktop_file_search_then_delivery_finds_and_sends(tmp_path, monkeypatch) -> None:
    desktop_dir = scenario_scratch_dir("desktop_file_search_then_delivery")
    (desktop_dir / "agent-control-sample.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
    (desktop_dir / "unrelated.txt").write_text("not the file we want", encoding="utf-8")

    settings = filesystem_settings(monkeypatch, tmp_path, str(desktop_dir))
    scenario = build_scenario(settings, tmp_path=tmp_path, fixture_name="desktop_file_search_then_delivery")

    task = await run_task_to_completion(
        scenario, f"Find me the file named agent-control-sample from {desktop_dir} and send it to me."
    )

    assert_completed(task)
    tool_calls = scenario.repositories.tool_invocations.list_for_task(task.id)
    assert any(call["tool_name"] == "filesystem.manage" for call in tool_calls)
    assert any(call["tool_name"] == "artifact.deliver" for call in tool_calls)
    assert scenario.telegram.documents
    assert any("agent-control-sample" in path for _chat_id, path, _caption in scenario.telegram.documents)


@pytest.mark.asyncio
async def test_desktop_file_search_then_delivery_rejects_path_outside_allowed_roots(tmp_path, monkeypatch) -> None:
    desktop_dir = scenario_scratch_dir("desktop_file_search_then_delivery")
    (desktop_dir / "agent-control-sample.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
    (desktop_dir / "unrelated.txt").write_text("not the file we want", encoding="utf-8")

    settings = filesystem_settings(monkeypatch, tmp_path, str(tmp_path / "somewhere_else"))
    scenario = build_scenario(settings, tmp_path=tmp_path, fixture_name="desktop_file_search_then_delivery")

    task = await run_task_to_completion(
        scenario, f"Find me the file named agent-control-sample from {desktop_dir} and send it to me."
    )

    assert_rejected(task)
    assert not scenario.telegram.documents
