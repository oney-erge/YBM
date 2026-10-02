from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import logging
import re
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from agent_control.db_tools import retention_sweep
from agent_control.schemas import AuditEventType, ScheduleRecord, ScheduleStatus, TaskRecord, TaskStatus, utc_now
from agent_control.storage.audit import AuditLogger
from agent_control.storage.repositories import Repositories

logger = logging.getLogger(__name__)


def _resolve_timezone(timezone_name: str | None) -> ZoneInfo | timezone:
    if timezone_name:
        try:
            return ZoneInfo(timezone_name)
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return timezone.utc


def next_run_after(cadence: str, after: datetime | None = None, timezone_name: str | None = None) -> datetime:
    """Compute the next fire time for ``cadence`` after ``after``.

    Minute/hour intervals are elapsed real-time arithmetic, timezone-
    independent. Day/week intervals (including bare "daily"/"weekly") add the
    interval in ``timezone_name``'s local calendar before converting back to
    UTC, so a daily schedule keeps firing at the same local wall-clock time
    across a DST transition instead of drifting by an hour.
    """
    base = after or utc_now()
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    lowered = cadence.lower().strip()

    if match := re.search(r"every\s+(\d+)\s+minutes?", lowered):
        return base + timedelta(minutes=max(1, int(match.group(1))))
    if match := re.search(r"every\s+(\d+)\s+hours?", lowered):
        return base + timedelta(hours=max(1, int(match.group(1))))

    tz = _resolve_timezone(timezone_name)
    local = base.astimezone(tz)

    if match := re.search(r"every\s+(\d+)\s+weeks?", lowered):
        return (local + timedelta(weeks=max(1, int(match.group(1))))).astimezone(timezone.utc)
    if match := re.search(r"every\s+(\d+)\s+days?", lowered):
        return (local + timedelta(days=max(1, int(match.group(1))))).astimezone(timezone.utc)
    if "weekly" in lowered or "every week" in lowered:
        return (local + timedelta(days=7)).astimezone(timezone.utc)
    return (local + timedelta(days=1)).astimezone(timezone.utc)


def cadence_from_text(text: str) -> str:
    lowered = text.lower()
    if match := re.search(r"every\s+\d+\s+(?:minutes?|hours?|days?|weeks?)\b", lowered):
        return match.group(0)
    if "daily" in lowered or "every day" in lowered:
        return "daily"
    if "weekly" in lowered or "every week" in lowered:
        return "weekly"
    return "daily"


def objective_from_schedule_text(text: str) -> str:
    cleaned = re.sub(r"\b(set up|create|add|schedule|scheduled job|job|that runs|every day|daily|weekly)\b", " ", text, flags=re.IGNORECASE)
    cleaned = re.sub(r"every\s+\d+\s+(?:minutes?|hours?|days?|weeks?)\b", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .,:")
    cleaned = re.sub(r"^(?:a\s+)?to\s+", "", cleaned, flags=re.IGNORECASE)
    return cleaned or text


def create_due_task(repositories: Repositories, audit: AuditLogger, schedule: ScheduleRecord) -> TaskRecord:
    conversation_id = (
        repositories.conversations.get_or_create(schedule.source_channel, schedule.source_chat_id)
        if schedule.source_chat_id
        else None
    )
    task = repositories.tasks.create(
        schedule.objective,
        conversation_id=conversation_id,
        metadata={
            "source_schedule_id": schedule.id,
            "source_chat_id": schedule.source_chat_id,
            "schedule_cadence": schedule.cadence,
            **schedule.metadata,
        },
    )
    now = utc_now()
    repositories.schedules.mark_run(
        schedule.id, task.id, now, next_run_after(schedule.cadence, now, schedule.timezone)
    )
    audit.append(
        AuditEventType.TASK_CREATED,
        actor="scheduler",
        task_id=task.id,
        payload={"schedule_id": schedule.id, "objective": schedule.objective},
    )
    return task


def _record_previous_run_outcome(
    repositories: Repositories, audit: AuditLogger, schedule: ScheduleRecord, *, max_consecutive_failures: int
) -> ScheduleRecord:
    """Check whether the task this schedule spawned last time failed, and
    track consecutive failures - auto-pausing the schedule once the streak
    hits the configured limit, rather than letting it keep firing and
    failing unnoticed (the motivating case: 7 real schedules whose target
    had gone away, still firing daily for weeks - docs/archive/HISTORY.md P6).
    Returns the schedule record, refreshed if its metadata/status changed."""
    if not schedule.last_task_id:
        return schedule
    previous_task = repositories.tasks.get(schedule.last_task_id)
    if previous_task is None:
        return schedule
    streak = int(schedule.metadata.get("consecutive_failures", 0))
    if previous_task.status == TaskStatus.FAILED:
        streak += 1
    elif previous_task.status in {TaskStatus.COMPLETED, TaskStatus.BLOCKED, TaskStatus.CANCELLED}:
        streak = 0
    else:
        # Still in flight (or in some non-terminal state) from last time -
        # nothing new to learn about the streak yet.
        return schedule
    if streak == int(schedule.metadata.get("consecutive_failures", 0)):
        return schedule
    metadata = {**schedule.metadata, "consecutive_failures": streak}
    schedule = repositories.schedules.update_metadata(schedule.id, metadata)
    if streak >= max_consecutive_failures:
        schedule = repositories.schedules.update_status(schedule.id, ScheduleStatus.PAUSED)
        audit.append(
            AuditEventType.ERROR,
            actor="scheduler",
            task_id=previous_task.id,
            payload={
                "schedule_id": schedule.id,
                "error": f"schedule auto-paused after {streak} consecutive failures",
                "objective": schedule.objective,
            },
        )
    return schedule


async def run_scheduler_once(
    repositories: Repositories,
    audit: AuditLogger,
    *,
    now: datetime | None = None,
    limit: int = 20,
    max_consecutive_failures: int = 5,
) -> list[TaskRecord]:
    due = repositories.schedules.list_due(now or utc_now(), limit=limit)
    created: list[TaskRecord] = []
    for schedule in due:
        if schedule.status != ScheduleStatus.ENABLED:
            continue
        schedule = _record_previous_run_outcome(
            repositories, audit, schedule, max_consecutive_failures=max_consecutive_failures
        )
        if schedule.status != ScheduleStatus.ENABLED:
            continue  # just auto-paused above; don't spawn another failing run
        try:
            created.append(create_due_task(repositories, audit, schedule))
        except Exception as exc:
            audit.append(
                AuditEventType.ERROR,
                actor="scheduler",
                payload={"schedule_id": schedule.id, "error": str(exc)},
            )
    return created


async def run_scheduler_forever(
    repositories: Repositories,
    audit: AuditLogger,
    *,
    poll_interval_seconds: float = 30.0,
    max_consecutive_failures: int = 5,
    retention_days: int | None = None,
) -> None:
    # None (the default, config.py's storage.retention_days) means task/audit
    # history is never automatically deleted - unchanged from before this
    # existed. Gated to roughly once a day, not every poll tick: unlike
    # expire_stale()'s single indexed UPDATE, retention_sweep()'s
    # `WHERE created_at < ?` scan has no index on tasks.created_at to lean
    # on, so running it on this loop's usual 30s cadence would mean a full
    # table scan every 30 seconds for no benefit - the cutoff barely moves
    # between ticks. Runs in a thread since it's blocking sqlite I/O and this
    # loop is otherwise fully async.
    last_retention_sweep: datetime | None = None
    while True:
        await run_scheduler_once(repositories, audit, max_consecutive_failures=max_consecutive_failures)
        if retention_days:
            now = utc_now()
            if last_retention_sweep is None or now - last_retention_sweep >= timedelta(days=1):
                deleted_tasks, deleted_orphan_audit = await asyncio.to_thread(
                    retention_sweep, repositories.tasks.database, retention_days
                )
                if deleted_tasks or deleted_orphan_audit:
                    logger.info(
                        "retention sweep: deleted %d task(s) and %d orphaned audit event(s) older than %d day(s)",
                        deleted_tasks, deleted_orphan_audit, retention_days,
                    )
                last_retention_sweep = now
        await asyncio.sleep(poll_interval_seconds)


def schedule_to_output(schedule: ScheduleRecord) -> dict[str, Any]:
    return schedule.model_dump(mode="json")
