"""The days and time a person names win over a plain daily or weekly the model picked (tools/schedule_manage.py)."""

from __future__ import annotations

import pytest

from agent_control.schemas import Capability, RiskLevel, ToolCallRequest
from agent_control.tools.schedule_manage import ScheduleManageAdapter
from helpers import make_repos


async def _create(tmp_path, said: str, **payload):
    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create(said, metadata={"original_message_text": said})
    result = await ScheduleManageAdapter(repos, audit).execute(ToolCallRequest(
        task_id=task.id, tool_name="schedule.manage", capability=Capability.SCHEDULE_MANAGE, risk_level=RiskLevel.MEDIUM,
        input={"operation": "create", "source_chat_id": "100", **payload},
    ))
    return repos.schedules.get(result.output["schedule_id"]), result


@pytest.mark.asyncio
async def test_a_daily_the_model_chose_gives_way_to_the_days_and_time_the_person_said(tmp_path) -> None:
    schedule, _ = await _create(tmp_path, "Remind me every Monday at 9am to check my email", objective="check my email", cadence="daily")
    assert schedule.cadence == "every monday at 09:00" and schedule.objective == "check my email"


@pytest.mark.asyncio
async def test_a_weekly_the_model_chose_gives_way_too_and_a_missing_cadence_reads_the_objective(tmp_path) -> None:
    weekly, _ = await _create(tmp_path, "Every weekday at 8:30 send me the weather", objective="send me the weather", cadence="weekly")
    assert weekly.cadence == "every weekday at 08:30"
    missing, _ = await _create(tmp_path, "x", objective="Every Friday at 5pm send me the timesheet reminder")
    assert missing.cadence == "every friday at 17:00" and missing.objective == "send me the timesheet reminder"


@pytest.mark.asyncio
async def test_what_the_person_did_not_say_is_left_as_the_model_wrote_it(tmp_path) -> None:
    plain, _ = await _create(tmp_path, "Remind me every 2 hours to stretch", objective="Remind me to stretch", cadence="every 2 hours")
    assert plain.cadence == "every 2 hours"
    unspecific, _ = await _create(tmp_path, "Send me the news every morning", objective="send me the news", cadence="daily")
    assert unspecific.cadence == "daily"  # no time and no day was named: nothing to prefer
    other, _ = await _create(tmp_path, "Check the disk space", objective="check the disk space", cadence="every 3 days")
    assert other.cadence == "every 3 days"


@pytest.mark.asyncio
async def test_a_one_off_day_is_not_a_schedule(tmp_path) -> None:
    schedule, _ = await _create(tmp_path, "On Monday at 9am call the bank", objective="call the bank", cadence="daily")
    assert schedule.cadence == "daily"
