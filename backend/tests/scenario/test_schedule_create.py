"""Scenario: "create a daily schedule that checks X" through the real
Operator loop -> schedule.manage create. schedule.manage is not a content
tool (no Auditor step), so this locks in the non-content deterministic-
completion path - the counterpart to the content-tool round-trips in the
other scenario tests. Ports e2e/all_cases.json's `scheduled_jobs` case down
to the deterministic tier (docs/archive/HISTORY.md P2). Fixture re-recorded
2026-07-28 (`ybm scenario record schedule_create`, localdeploy_qwen3vl_8b).
"""

from __future__ import annotations

from agent_control.config import CapabilityPolicy, default_capability_policies
from agent_control.schemas import Capability, RiskLevel
import pytest

from .harness import assert_completed, assert_rejected, build_scenario, isolated_settings, run_task_to_completion


@pytest.mark.asyncio
async def test_schedule_create_persists_a_schedule(tmp_path, monkeypatch) -> None:
    caps = default_capability_policies()
    caps[Capability.SCHEDULE_MANAGE] = CapabilityPolicy(enabled=True, requires_approval=False, max_risk_level=RiskLevel.MEDIUM)
    settings = isolated_settings(monkeypatch, tmp_path, capabilities=caps)
    scenario = build_scenario(settings, tmp_path=tmp_path, fixture_name="schedule_create")

    task = await run_task_to_completion(
        scenario, "create a daily schedule that checks https://example.com/status for updates",
        # schedule.manage's `create` operation is unconditionally
        # approval-gated by design (schedule_manage.py's ToolDefinition,
        # approval_required_operations). This test is about persistence,
        # not the approval gate itself.
        auto_approve=True,
    )

    assert_completed(task)
    schedules = scenario.repositories.schedules.list_recent(10)
    assert len(schedules) == 1
    assert "example.com/status" in schedules[0].objective
    tool_calls = scenario.repositories.tool_invocations.list_for_task(task.id)
    assert any(call["tool_name"] == "schedule.manage" for call in tool_calls)


@pytest.mark.asyncio
async def test_schedule_create_denied_without_capability(tmp_path, monkeypatch) -> None:
    # SCHEDULE_MANAGE left at its secure-by-default disabled state.
    settings = isolated_settings(monkeypatch, tmp_path)
    scenario = build_scenario(settings, tmp_path=tmp_path, fixture_name="schedule_create")

    task = await run_task_to_completion(
        scenario, "create a daily schedule that checks https://example.com/status for updates"
    )

    assert_rejected(task)
    assert scenario.repositories.schedules.list_recent(10) == []
