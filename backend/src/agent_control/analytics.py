"""Cross-task reliability dashboard (docs/ROADMAP.md "reliability
dashboard") - success/verified-success rate, retries, tool failures, token
cost, and a per-tool/per-model/per-task-type breakdown over a time window.

Entirely derived from data that already exists in tasks.metadata and
tool_invocations - the same "nothing new persisted, computed on read"
approach admin.py's build_task_receipt already uses for one task; this is
the same idea run across every task in a window instead of one.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from agent_control.schemas import TaskStatus, utc_now
from agent_control.storage.repositories import Repositories

_FAILED_INVOCATION_STATUSES = {"failed", "denied", "timeout"}
# A tool needs at least this many calls in the window before its failure
# rate counts toward "most unreliable" - one failed call out of one is a
# 100% rate that says nothing.
_MIN_CALLS_FOR_RELIABILITY_RANKING = 3


def build_reliability_dashboard(repositories: Repositories, window_days: int = 7) -> dict[str, Any]:
    cutoff = utc_now() - timedelta(days=window_days)
    tasks = repositories.tasks.list_since(cutoff)

    total = len(tasks)
    completed = 0
    verified_completed = 0
    checked_completed = 0
    failed = 0
    blocked = 0
    cancelled = 0
    total_retries = 0
    tasks_with_retries = 0
    fallback_tasks = 0
    total_tokens = 0
    total_duration_seconds = 0.0
    duration_samples = 0
    by_model: dict[str, dict[str, int]] = {}
    by_task_type: dict[str, dict[str, int]] = {}
    tool_stats: dict[str, dict[str, int]] = {}

    for task in tasks:
        metadata = task.metadata if isinstance(task.metadata, dict) else {}
        status = task.status

        if status == TaskStatus.COMPLETED:
            completed += 1
            duration_samples += 1
            total_duration_seconds += (task.updated_at - task.created_at).total_seconds()
        elif status == TaskStatus.FAILED:
            failed += 1
        elif status == TaskStatus.BLOCKED:
            blocked += 1
        elif status == TaskStatus.CANCELLED:
            cancelled += 1

        retry_count = int(metadata.get("operator_retry_count") or 0)
        if retry_count:
            tasks_with_retries += 1
            total_retries += retry_count

        token_usage = metadata.get("token_usage")
        token_usage = token_usage if isinstance(token_usage, dict) else {}
        if token_usage.get("fallback_used"):
            fallback_tasks += 1
        total_tokens += int(token_usage.get("total_tokens") or 0)
        model = token_usage.get("last_model")
        if isinstance(model, str) and model:
            model_entry = by_model.setdefault(model, {"tasks": 0, "completed": 0, "total_tokens": 0})
            model_entry["tasks"] += 1
            model_entry["total_tokens"] += int(token_usage.get("total_tokens") or 0)
            if status == TaskStatus.COMPLETED:
                model_entry["completed"] += 1

        task_type = metadata.get("task_type")
        if isinstance(task_type, str) and task_type:
            type_entry = by_task_type.setdefault(task_type, {"tasks": 0, "completed": 0, "failed": 0})
            type_entry["tasks"] += 1
            if status == TaskStatus.COMPLETED:
                type_entry["completed"] += 1
            elif status == TaskStatus.FAILED:
                type_entry["failed"] += 1

        checked = 0
        missing = 0
        for invocation in repositories.tool_invocations.list_for_task(task.id):
            tool_name = str(invocation.get("tool_name") or "unknown")
            tool_entry = tool_stats.setdefault(
                tool_name, {"calls": 0, "succeeded": 0, "failed": 0, "not_checked": 0}
            )
            tool_entry["calls"] += 1
            inv_status = str(invocation.get("status") or "")
            if inv_status == "succeeded":
                tool_entry["succeeded"] += 1
            elif inv_status in _FAILED_INVOCATION_STATUSES:
                tool_entry["failed"] += 1
            result = invocation.get("result")
            verification = result.get("verification") if isinstance(result, dict) else None
            if isinstance(verification, dict):
                checked += int(verification.get("checked") or 0)
                missing += len(verification.get("missing") or [])
            elif inv_status == "succeeded":
                # Succeeded, but this tool (or this operation on it) has no
                # verify() hook - admin.py's build_task_receipt calls the
                # same gap "not_checked": absence of proof, not proof it
                # went fine. Only counted for calls that actually ran; a
                # failed/denied call was never eligible for verification in
                # the first place.
                tool_entry["not_checked"] += 1
        # "Verified" mirrors build_task_receipt's own definition exactly: at
        # least one mechanical check ran, and none of them came up missing.
        if status == TaskStatus.COMPLETED:
            if checked > 0:
                checked_completed += 1
                if missing == 0:
                    verified_completed += 1

    total_tool_calls = sum(entry["calls"] for entry in tool_stats.values())
    total_tool_failures = sum(entry["failed"] for entry in tool_stats.values())

    most_unreliable_tool = None
    worst_failure_rate = 0.0
    for name, entry in tool_stats.items():
        if entry["calls"] < _MIN_CALLS_FOR_RELIABILITY_RANKING:
            continue
        rate = entry["failed"] / entry["calls"]
        if rate > worst_failure_rate:
            worst_failure_rate = rate
            most_unreliable_tool = name

    def pct(count: int) -> float:
        return round(100 * count / total, 1) if total else 0.0

    return {
        "window_days": window_days,
        "tasks_attempted": total,
        "completed": completed,
        "completed_pct": pct(completed),
        "verified_completed": verified_completed,
        "verified_completed_pct": pct(verified_completed),
        # Of completed tasks, how many had *any* mechanical check run at
        # all - the denominator "verified_completed_pct" is silently
        # missing. A low verified_completed_pct reads very differently
        # depending on whether coverage is high (things were checked and
        # came up wrong) or low (most completions were simply never
        # checkable yet, per docs/GAPS.md's one-tool verify() coverage).
        "checked_completed": checked_completed,
        "checked_completed_pct": pct(checked_completed),
        "verification_coverage_pct": (
            round(100 * checked_completed / completed, 1) if completed else 0.0
        ),
        "failed": failed,
        "failed_pct": pct(failed),
        "blocked": blocked,
        "cancelled": cancelled,
        "tasks_with_retries": tasks_with_retries,
        "mean_retries": round(total_retries / total, 2) if total else 0.0,
        "fallback_tasks": fallback_tasks,
        "total_tokens": total_tokens,
        "avg_task_duration_seconds": (
            round(total_duration_seconds / duration_samples, 1) if duration_samples else None
        ),
        "tool_call_failure_rate_pct": (
            round(100 * total_tool_failures / total_tool_calls, 1) if total_tool_calls else 0.0
        ),
        "most_unreliable_tool": most_unreliable_tool,
        "tools": [
            {
                "tool_name": name,
                **entry,
                "failure_rate_pct": round(100 * entry["failed"] / entry["calls"], 1) if entry["calls"] else 0.0,
            }
            for name, entry in sorted(tool_stats.items(), key=lambda item: -item[1]["calls"])
        ],
        "by_model": [
            {"model": name, **entry} for name, entry in sorted(by_model.items(), key=lambda item: -item[1]["tasks"])
        ],
        "by_task_type": [
            {"task_type": name, **entry}
            for name, entry in sorted(by_task_type.items(), key=lambda item: -item[1]["tasks"])
        ],
    }
