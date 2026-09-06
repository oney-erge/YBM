from __future__ import annotations

from agent_control.analytics import build_reliability_dashboard
from agent_control.schemas import (
    Capability,
    TaskStatus,
    ToolCallRequest,
    ToolCallResult,
    ToolResultStatus,
    ToolVerification,
)
from helpers import make_repos


def _tool_call(repos, task_id: str, tool_name: str, status: ToolResultStatus, *, verification: ToolVerification | None = None) -> None:
    request = ToolCallRequest(task_id=task_id, tool_name=tool_name, capability=Capability.FILESYSTEM_WRITE, input={})
    repos.tool_invocations.create(request)
    repos.tool_invocations.complete(
        ToolCallResult(request_id=request.id, status=status, output={}, verification=verification)
    )


def test_dashboard_counts_statuses_and_computes_percentages(tmp_path) -> None:
    repos, _audit = make_repos(tmp_path)
    done = repos.tasks.create("sort downloads")
    repos.tasks.update_metadata(done.id, {**done.metadata, "task_type": "file_management"}, TaskStatus.COMPLETED)
    failed = repos.tasks.create("broken task")
    repos.tasks.update_metadata(failed.id, failed.metadata, TaskStatus.FAILED)
    repos.tasks.create("still running")  # stays RECEIVED - neither completed nor failed

    dashboard = build_reliability_dashboard(repos, window_days=7)

    assert dashboard["tasks_attempted"] == 3
    assert dashboard["completed"] == 1
    assert dashboard["failed"] == 1
    assert dashboard["completed_pct"] == round(100 / 3, 1)
    assert dashboard["by_task_type"] == [{"task_type": "file_management", "tasks": 1, "completed": 1, "failed": 0}]


def test_dashboard_only_counts_a_completed_task_as_verified_when_nothing_is_missing(tmp_path) -> None:
    repos, _audit = make_repos(tmp_path)

    fully_verified = repos.tasks.create("move files cleanly")
    _tool_call(repos, fully_verified.id, "filesystem.manage", ToolResultStatus.SUCCEEDED,
               verification=ToolVerification(checked=3, verified=3, missing=[]))
    repos.tasks.update_metadata(fully_verified.id, fully_verified.metadata, TaskStatus.COMPLETED)

    partially_verified = repos.tasks.create("move files, one went missing")
    _tool_call(repos, partially_verified.id, "filesystem.manage", ToolResultStatus.SUCCEEDED,
               verification=ToolVerification(checked=3, verified=2, missing=["destination not found: c"]))
    repos.tasks.update_metadata(partially_verified.id, partially_verified.metadata, TaskStatus.COMPLETED)

    unverified = repos.tasks.create("read a file")
    _tool_call(repos, unverified.id, "filesystem.manage", ToolResultStatus.SUCCEEDED)  # no verify() coverage
    repos.tasks.update_metadata(unverified.id, unverified.metadata, TaskStatus.COMPLETED)

    dashboard = build_reliability_dashboard(repos, window_days=7)

    assert dashboard["completed"] == 3
    assert dashboard["verified_completed"] == 1
    assert dashboard["verified_completed_pct"] == round(100 / 3, 1)
    # checked_completed counts both fully_verified and partially_verified -
    # a mechanical check ran on each, one just came back clean and the
    # other didn't. Only `unverified` (no verify() coverage at all) is
    # outside that denominator, which is the distinction
    # verification_coverage_pct exists to surface: "verified_completed_pct"
    # alone can't say whether a low number means "checked and wrong" or
    # "never checked".
    assert dashboard["checked_completed"] == 2
    assert dashboard["checked_completed_pct"] == round(200 / 3, 1)
    assert dashboard["verification_coverage_pct"] == round(100 * 2 / 3, 1)


def test_dashboard_reports_zero_coverage_when_nothing_was_ever_checked(tmp_path) -> None:
    repos, _audit = make_repos(tmp_path)
    unverified = repos.tasks.create("read a file")
    _tool_call(repos, unverified.id, "filesystem.manage", ToolResultStatus.SUCCEEDED)
    repos.tasks.update_metadata(unverified.id, unverified.metadata, TaskStatus.COMPLETED)

    dashboard = build_reliability_dashboard(repos, window_days=7)

    assert dashboard["completed"] == 1
    assert dashboard["checked_completed"] == 0
    assert dashboard["verification_coverage_pct"] == 0.0
    assert dashboard["verified_completed_pct"] == 0.0


def test_dashboard_ranks_the_tool_with_the_worst_failure_rate_above_the_minimum_call_count(tmp_path) -> None:
    repos, _audit = make_repos(tmp_path)
    task = repos.tasks.create("t")

    # 1 failure out of 1 call - should NOT count (below _MIN_CALLS_FOR_RELIABILITY_RANKING).
    _tool_call(repos, task.id, "flaky_once", ToolResultStatus.FAILED)

    # 2 failures out of 4 calls (50%) - enough calls to count, and the worst rate.
    for _ in range(2):
        _tool_call(repos, task.id, "browser.control", ToolResultStatus.SUCCEEDED)
    for _ in range(2):
        _tool_call(repos, task.id, "browser.control", ToolResultStatus.FAILED)

    # 4 calls, all succeeded.
    for _ in range(4):
        _tool_call(repos, task.id, "filesystem.manage", ToolResultStatus.SUCCEEDED)

    dashboard = build_reliability_dashboard(repos, window_days=7)

    assert dashboard["most_unreliable_tool"] == "browser.control"
    by_name = {entry["tool_name"]: entry for entry in dashboard["tools"]}
    assert by_name["browser.control"]["failure_rate_pct"] == 50.0
    assert by_name["filesystem.manage"]["failure_rate_pct"] == 0.0
    assert by_name["flaky_once"]["calls"] == 1  # still reported, just not eligible for the ranking
    # None of these calls declared a verification, but only succeeded ones
    # count as "not_checked" - a failed call was never eligible to be
    # checked in the first place, so it shouldn't inflate this count.
    assert by_name["browser.control"]["not_checked"] == 2
    assert by_name["filesystem.manage"]["not_checked"] == 4
    assert by_name["flaky_once"]["not_checked"] == 0


def test_dashboard_aggregates_retries_fallback_and_token_cost(tmp_path) -> None:
    repos, _audit = make_repos(tmp_path)
    task = repos.tasks.create("t")
    repos.tasks.update_metadata(
        task.id,
        {
            "operator_retry_count": 2,
            "token_usage": {"calls": 3, "total_tokens": 450, "last_model": "gpt-5-mini", "fallback_used": True},
        },
        TaskStatus.COMPLETED,
    )

    dashboard = build_reliability_dashboard(repos, window_days=7)

    assert dashboard["tasks_with_retries"] == 1
    assert dashboard["mean_retries"] == 2.0
    assert dashboard["fallback_tasks"] == 1
    assert dashboard["total_tokens"] == 450
    assert dashboard["by_model"] == [{"model": "gpt-5-mini", "tasks": 1, "completed": 1, "total_tokens": 450}]


def test_dashboard_excludes_tasks_created_before_the_window(tmp_path) -> None:
    from datetime import timedelta

    from agent_control.schemas import utc_now

    repos, _audit = make_repos(tmp_path)
    old_task = repos.tasks.create("ancient task")
    # Backdate directly in storage - there is no public API to fabricate an
    # old created_at, and the window boundary is exactly what this test
    # needs to exercise.
    with repos.tasks.database.connect() as connection:
        connection.execute(
            "UPDATE tasks SET created_at = ? WHERE id = ?",
            ((utc_now() - timedelta(days=30)).isoformat(), old_task.id),
        )
    repos.tasks.create("recent task")

    dashboard = build_reliability_dashboard(repos, window_days=7)

    assert dashboard["tasks_attempted"] == 1


def test_dashboard_reports_no_average_duration_when_nothing_completed(tmp_path) -> None:
    repos, _audit = make_repos(tmp_path)
    repos.tasks.create("still running")

    dashboard = build_reliability_dashboard(repos, window_days=7)

    assert dashboard["avg_task_duration_seconds"] is None
    assert dashboard["most_unreliable_tool"] is None
    assert dashboard["tools"] == []
