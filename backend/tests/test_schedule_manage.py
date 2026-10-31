from __future__ import annotations

from datetime import timedelta

import pytest

from agent_control.config import AppSettings, CapabilityPolicy
from agent_control.scheduler import objective_from_schedule_text, run_scheduler_once
from agent_control.schemas import (
    AuditEventType,
    Capability,
    RiskLevel,
    ScheduleRecord,
    ScheduleStatus,
    TaskStatus,
    ToolCallRequest,
    ToolResultStatus,
    utc_now,
)
from agent_control.tools.registry import build_tool_registry
from agent_control.tools.schedule_manage import ScheduleManageAdapter
from helpers import make_repos


def _settings(tmp_path, *, terminal: bool = False) -> AppSettings:
    capabilities = {
        Capability.SCHEDULE_MANAGE: CapabilityPolicy(
            enabled=True,
            requires_approval=False,
            max_risk_level=RiskLevel.MEDIUM,
        )
    }
    if terminal:
        capabilities[Capability.TERMINAL_RUN] = CapabilityPolicy(
            enabled=True,
            requires_approval=False,
            max_risk_level=RiskLevel.HIGH,
        )
    return AppSettings(
        _env_file=None,
        storage={"database_url": f"sqlite:///{tmp_path / 'agent.db'}"},
        adapters={"coding_agent": {"enabled": True, "workspace_root": str(tmp_path / "workspaces")}},
        capabilities=capabilities,
    )

def test_objective_from_schedule_text_removes_scheduling_wrapper() -> None:
    objective = objective_from_schedule_text(
        "Set up a scheduled job every day to check https://example.com and tell me if a new episode came out."
    )

    assert objective == "check https://example.com and tell me if a new episode came out"


@pytest.mark.asyncio
async def test_schedule_metadata_cannot_create_an_unvalidated_folder_trigger(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create('schedule a task')
    adapter = ScheduleManageAdapter(repos, audit)
    result = await adapter.execute(ToolCallRequest(
        task_id=task.id, tool_name='schedule.manage', capability=Capability.SCHEDULE_MANAGE,
        input={'operation': 'create', 'objective': 'check files', 'metadata': {'folder_trigger': {}}},
    ))
    assert result.status == ToolResultStatus.FAILED
    assert 'Workflows' in result.error_message
    assert repos.schedules.list_recent() == []

@pytest.mark.asyncio
async def test_schedule_manage_creates_lists_pauses_resumes_and_runs_now(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("set up scheduled job every day to check example.com", metadata={"source_chat_id": "100"})
    adapter = ScheduleManageAdapter(repos, audit)

    created = await adapter.execute(
        ToolCallRequest(
            task_id=task.id,
            tool_name="schedule.manage",
            capability=Capability.SCHEDULE_MANAGE,
            risk_level=RiskLevel.MEDIUM,
            input={
                "operation": "create",
                "objective": task.objective,
                "source_chat_id": "100",
            },
        )
    )

    assert created.status == ToolResultStatus.SUCCEEDED
    assert created.output["schedule_id"]
    assert created.output["next_run_at"]

    listed = await adapter.execute(
        ToolCallRequest(
            task_id=task.id,
            tool_name="schedule.manage",
            capability=Capability.SCHEDULE_MANAGE,
            risk_level=RiskLevel.MEDIUM,
            input={"operation": "list"},
        )
    )
    assert len(listed.output["schedules"]) == 1

    schedule_id = created.output["schedule_id"]
    for operation, status in (("pause", "paused"), ("resume", "enabled")):
        result = await adapter.execute(
            ToolCallRequest(
                task_id=task.id,
                tool_name="schedule.manage",
                capability=Capability.SCHEDULE_MANAGE,
                risk_level=RiskLevel.MEDIUM,
                input={"operation": operation, "schedule_id": schedule_id},
            )
        )
        assert result.output["schedules"][0]["status"] == status

    run_now = await adapter.execute(
        ToolCallRequest(
            task_id=task.id,
            tool_name="schedule.manage",
            capability=Capability.SCHEDULE_MANAGE,
            risk_level=RiskLevel.MEDIUM,
            input={"operation": "run_now", "schedule_id": schedule_id},
        )
    )

    assert run_now.status == ToolResultStatus.SUCCEEDED
    assert run_now.output["task_id"]
    assert repos.tasks.get(run_now.output["task_id"]).metadata["source_schedule_id"] == schedule_id

@pytest.mark.asyncio
async def test_due_schedule_creates_task_and_advances_next_run(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    due = utc_now() - timedelta(minutes=1)
    schedule = repos.schedules.create(
        ScheduleRecord(
            source_chat_id="100",
            objective="check https://example.com and tell me if it changed",
            cadence="every 5 minutes",
            next_run_at=due,
        )
    )

    tasks = await run_scheduler_once(repos, audit, now=utc_now())

    updated = repos.schedules.get(schedule.id)
    assert len(tasks) == 1
    assert tasks[0].metadata["source_schedule_id"] == schedule.id
    assert updated.last_task_id == tasks[0].id
    assert updated.next_run_at > due

@pytest.mark.asyncio
async def test_schedule_tracks_consecutive_failures_but_keeps_running_below_threshold(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    due = utc_now() - timedelta(minutes=1)
    schedule = repos.schedules.create(
        ScheduleRecord(
            source_chat_id="100",
            objective="check a target that keeps failing",
            cadence="every 5 minutes",
            next_run_at=due,
        )
    )
    failing_task = repos.tasks.create("previous run")
    repos.tasks.update_status(failing_task.id, TaskStatus.FAILED)
    repos.schedules.mark_run(schedule.id, failing_task.id, due, due)

    tasks = await run_scheduler_once(repos, audit, now=utc_now(), max_consecutive_failures=5)

    assert len(tasks) == 1  # still spawns - one failure is below the threshold
    updated = repos.schedules.get(schedule.id)
    assert updated.status == ScheduleStatus.ENABLED
    assert updated.metadata["consecutive_failures"] == 1

@pytest.mark.asyncio
async def test_schedule_auto_pauses_after_reaching_consecutive_failure_threshold(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    due = utc_now() - timedelta(minutes=1)
    schedule = repos.schedules.create(
        ScheduleRecord(
            source_chat_id="100",
            objective="check a target that has gone away",
            cadence="every 5 minutes",
            next_run_at=due,
            metadata={"consecutive_failures": 2},
        )
    )
    failing_task = repos.tasks.create("previous run, the 3rd failure in a row")
    repos.tasks.update_status(failing_task.id, TaskStatus.FAILED)
    repos.schedules.mark_run(schedule.id, failing_task.id, due, due)

    tasks = await run_scheduler_once(repos, audit, now=utc_now(), max_consecutive_failures=3)

    assert tasks == []  # auto-paused before spawning another failing run
    updated = repos.schedules.get(schedule.id)
    assert updated.status == ScheduleStatus.PAUSED
    assert updated.metadata["consecutive_failures"] == 3
    events = repos.audit.list_for_task(failing_task.id)
    assert any(
        event.type == AuditEventType.ERROR and "auto-paused" in str(event.payload.get("error", ""))
        for event in events
    )

@pytest.mark.asyncio
async def test_schedule_failure_streak_resets_after_a_successful_run(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    due = utc_now() - timedelta(minutes=1)
    schedule = repos.schedules.create(
        ScheduleRecord(
            source_chat_id="100",
            objective="check a target that recovered",
            cadence="every 5 minutes",
            next_run_at=due,
            metadata={"consecutive_failures": 4},
        )
    )
    succeeded_task = repos.tasks.create("previous run, which succeeded")
    repos.tasks.update_status(succeeded_task.id, TaskStatus.COMPLETED)
    repos.schedules.mark_run(schedule.id, succeeded_task.id, due, due)

    tasks = await run_scheduler_once(repos, audit, now=utc_now(), max_consecutive_failures=5)

    assert len(tasks) == 1
    updated = repos.schedules.get(schedule.id)
    assert updated.status == ScheduleStatus.ENABLED
    assert updated.metadata["consecutive_failures"] == 0

def test_schedule_manage_is_registered_under_schedule_capability(tmp_path) -> None:
    settings = _settings(tmp_path)
    repos, audit = make_repos(tmp_path)
    registry = build_tool_registry(settings, "http://127.0.0.1:8765", repositories=repos, audit_logger=audit)
    definition = next(item for item in registry.definitions if item.name == "schedule.manage")

    assert definition.enabled is True
    assert definition.capability == Capability.SCHEDULE_MANAGE
    assert "schedule.manage" in registry.adapters

def test_registry_rejects_invalid_schedule_operation(tmp_path) -> None:
    settings = _settings(tmp_path)
    registry = build_tool_registry(settings, "http://127.0.0.1:8765")
    definition = next(item for item in registry.definitions if item.name == "schedule.manage")

    with pytest.raises(ValueError):
        definition.validate_input({"operation": "bogus", "objective": "check example.com"})



@pytest.mark.asyncio
async def test_creating_the_same_schedule_again_returns_the_existing_one(tmp_path) -> None:
    """A repeated request, or a model repeating its own create, must not make a second identical job."""
    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("check example.com every day")
    adapter = ScheduleManageAdapter(repos, audit)

    def create(objective: str, cadence: str | None = None):
        payload = {"operation": "create", "objective": objective, **({"cadence": cadence} if cadence else {})}
        return adapter.execute(ToolCallRequest(
            task_id=task.id, tool_name="schedule.manage", capability=Capability.SCHEDULE_MANAGE,
            risk_level=RiskLevel.MEDIUM, input=payload,
        ))

    first = await create("Check example.com for new episodes", "daily")
    again = await create("check  example.com for new episodes", "daily")  # case and spacing differ
    weekly = await create("Check example.com for new episodes", "weekly")

    assert again.status == ToolResultStatus.SUCCEEDED
    assert again.output["schedule_id"] == first.output["schedule_id"]
    assert "no new schedule was created" in again.output["summary"]
    assert weekly.output["schedule_id"] != first.output["schedule_id"]  # a different cadence is a different job
    assert repos.schedules.count() == 2

    repos.schedules.update_status(first.output["schedule_id"], ScheduleStatus.DELETED)
    recreated = await create("Check example.com for new episodes", "daily")
    assert recreated.output["schedule_id"] != first.output["schedule_id"]  # a deleted one does not count
