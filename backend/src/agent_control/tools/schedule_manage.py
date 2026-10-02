from __future__ import annotations

from agent_control.scheduler import cadence_from_text, create_due_task, next_run_after, objective_from_schedule_text, schedule_to_output
from agent_control.schemas import (
    Capability,
    ChannelType,
    RiskLevel,
    ScheduleRecord,
    ScheduleStatus,
    ToolCallRequest,
    ToolCallResult,
    ToolResultStatus,
    utc_now,
)
from agent_control.storage.audit import AuditLogger
from agent_control.storage.repositories import Repositories
from agent_control.tools.contracts import ScheduleManageInput, ScheduleManageOutput
from agent_control.tools.spec import (
    Adapters,
    Definitions,
    RegistryDeps,
    ToolDefinition,
    capability_enabled,
    failed_result,
    same_output_schema,
)


class ScheduleManageAdapter:
    def __init__(self, repositories: Repositories, audit: AuditLogger, *, default_timezone: str = "America/Chicago") -> None:
        self.repositories = repositories
        self.audit = audit
        self.default_timezone = default_timezone

    async def execute(self, request: ToolCallRequest) -> ToolCallResult:
        operation = str(request.input.get("operation") or "create")
        try:
            if operation == "create":
                output = self._create(request)
            elif operation == "list":
                output = self._list()
            elif operation == "pause":
                output = self._status(request, ScheduleStatus.PAUSED)
            elif operation == "resume":
                output = self._status(request, ScheduleStatus.ENABLED)
            elif operation == "delete":
                output = self._status(request, ScheduleStatus.DELETED)
            elif operation == "run_now":
                output = self._run_now(request)
            else:
                return failed_result(request, f"unsupported schedule operation: {operation}")
        except Exception as exc:
            return failed_result(request, f"schedule operation failed: {exc}")
        output["operation"] = operation
        output["terminal_output"] = [_terminal_output(operation, output)]
        return ToolCallResult(request_id=request.id, status=ToolResultStatus.SUCCEEDED, output=output)

    def _create(self, request: ToolCallRequest) -> dict:
        raw_objective = str(request.input.get("objective") or request.input.get("prompt") or "").strip()
        if not raw_objective:
            raise ValueError("objective is required")
        cadence = str(request.input.get("cadence") or cadence_from_text(raw_objective))
        objective = objective_from_schedule_text(raw_objective)
        timezone_name = str(request.input.get("timezone") or self.default_timezone)
        schedule = self.repositories.schedules.create(
            ScheduleRecord(
                source_channel=ChannelType.TELEGRAM,
                source_chat_id=request.input.get("source_chat_id"),
                objective=objective,
                cadence=cadence,
                timezone=timezone_name,
                next_run_at=next_run_after(cadence, utc_now(), timezone_name),
                metadata=dict(request.input.get("metadata") or {}),
            )
        )
        return {
            "schedule_id": schedule.id,
            "schedules": [schedule_to_output(schedule)],
            "next_run_at": schedule.next_run_at.isoformat(),
            "summary": f"Created schedule {schedule.id}: {schedule.cadence} -> {schedule.objective}",
        }

    def _list(self) -> dict:
        schedules = self.repositories.schedules.list_recent(50)
        return {
            "schedules": [schedule_to_output(schedule) for schedule in schedules],
            "summary": f"Found {len(schedules)} schedule(s).",
        }

    def _status(self, request: ToolCallRequest, status: ScheduleStatus) -> dict:
        schedule_id = str(request.input.get("schedule_id") or "")
        if not schedule_id:
            raise ValueError("schedule_id is required")
        schedule = self.repositories.schedules.update_status(schedule_id, status)
        return {
            "schedule_id": schedule.id,
            "schedules": [schedule_to_output(schedule)],
            "next_run_at": schedule.next_run_at.isoformat(),
            "summary": f"Set schedule {schedule.id} to {status.value}.",
        }

    def _run_now(self, request: ToolCallRequest) -> dict:
        schedule_id = str(request.input.get("schedule_id") or "")
        if not schedule_id:
            raise ValueError("schedule_id is required")
        schedule = self.repositories.schedules.get(schedule_id)
        if schedule is None:
            raise ValueError(f"schedule not found: {schedule_id}")
        task = create_due_task(self.repositories, self.audit, schedule)
        updated = self.repositories.schedules.get(schedule.id) or schedule
        return {
            "schedule_id": schedule.id,
            "task_id": task.id,
            "schedules": [schedule_to_output(updated)],
            "next_run_at": updated.next_run_at.isoformat(),
            "summary": f"Created task {task.id} from schedule {schedule.id}.",
        }


def _terminal_output(operation: str, output: dict) -> dict:
    return {
        "content": output.get("summary") or f"schedule.manage {operation} completed.",
        "is_final": True,
        "exit_code": 0,
    }




def register(deps: RegistryDeps, definitions: Definitions, adapters: Adapters) -> None:
    settings = deps.settings
    enabled = capability_enabled(settings, Capability.SCHEDULE_MANAGE) and settings.scheduler.enabled
    definitions.append(
        ToolDefinition(
            name="schedule.manage",
            capability=Capability.SCHEDULE_MANAGE,
            enabled=enabled,
            description="create, list, pause, resume, delete, or run recurring task schedules",
            operations=("create", "list", "pause", "resume", "delete", "run_now"),
            input_schema=ScheduleManageInput,
            output_schema=ScheduleManageOutput,
            operation_output_schemas=same_output_schema(
                ("create", "list", "pause", "resume", "delete", "run_now"),
                ScheduleManageOutput,
            ),
            default_operation="create",
            operation_risks={
                "list": RiskLevel.LOW,
                "create": RiskLevel.MEDIUM,
                "pause": RiskLevel.MEDIUM,
                "resume": RiskLevel.MEDIUM,
                "delete": RiskLevel.MEDIUM,
                "run_now": RiskLevel.MEDIUM,
            },
            approval_required_operations=("create", "pause", "resume", "delete"),
            approval_reasons={
                "create": "creates a recurring task that will run unattended on the schedule you set",
                "pause": "changes a recurring schedule's active state",
                "resume": "changes a recurring schedule's active state",
                "delete": "permanently deletes a recurring schedule",
            },
            # Without a worked example the planner has only the bare schema to
            # go on and reliably invents a nonexistent shape (a "frequency"
            # field, a nested "task" object) instead of using "objective" +
            # "cadence" - confirmed empirically while recording a scenario
            # test fixture for this exact tool (docs/archive/HISTORY.md P2).
            examples=(
                {"operation": "create", "objective": "Check https://example.com/status for updates", "cadence": "daily"},
                {"operation": "list"},
                {"operation": "pause", "schedule_id": "{{schedule_id}}"},
            ),
        )
    )
    if deps.repositories is not None and deps.audit_logger is not None:
        adapters["schedule.manage"] = ScheduleManageAdapter(
            deps.repositories,  # type: ignore[arg-type]
            deps.audit_logger,  # type: ignore[arg-type]
            default_timezone=settings.scheduler.default_timezone,
        )
