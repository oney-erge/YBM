"""A task that stopped and was never answered is closed quietly after a day."""

from __future__ import annotations

from datetime import timedelta

import pytest

from agent_control.orchestration.signals import sweep_stale_blocked
from agent_control.orchestration.worker import TaskWorker
from agent_control.schemas import ChannelType, TaskStatus, utc_now
from agent_control.storage.repositories import _dt
from helpers import make_repos

DAY = timedelta(hours=24)


def _task(repos, status, age_hours, objective="a request"):
    task = repos.tasks.create(objective, conversation_id=repos.conversations.get_or_create(ChannelType.TELEGRAM, "42"))
    repos.tasks.update_metadata(task.id, {**task.metadata, "last_worker_error": "No tool could do it."}, status)
    with repos.tasks.database.connect() as connection:
        connection.execute("UPDATE tasks SET updated_at = ? WHERE id = ?", (_dt(utc_now() - timedelta(hours=age_hours)), task.id))
    return task.id


@pytest.fixture
def env(tmp_path):
    return make_repos(tmp_path)


def test_an_old_blocked_task_is_closed_and_says_why(env):
    repos, audit = env
    old = _task(repos, TaskStatus.BLOCKED, 30)
    assert sweep_stale_blocked(repos, audit, max_age=DAY) == [old]
    closed = repos.tasks.get(old)
    assert closed.status == TaskStatus.CANCELLED
    assert closed.metadata["auto_closed"] is True
    assert closed.metadata["auto_closed_reason"] == "Closed automatically: it stayed blocked for 24 hours with no reply."
    assert closed.metadata["cancelled_from_status"] == "blocked"


def test_a_recent_blocked_task_is_left_alone(env):
    repos, audit = env
    fresh = _task(repos, TaskStatus.BLOCKED, 3)
    assert sweep_stale_blocked(repos, audit, max_age=DAY) == []
    assert repos.tasks.get(fresh).status == TaskStatus.BLOCKED


def test_an_unanswered_question_is_closed_too(env):
    repos, audit = env
    asked = _task(repos, TaskStatus.CLARIFYING, 48)
    assert sweep_stale_blocked(repos, audit, max_age=DAY) == [asked]
    assert repos.tasks.get(asked).metadata["auto_closed_reason"].endswith("stayed clarifying for 24 hours with no reply.")


@pytest.mark.parametrize("status", [TaskStatus.RUNNING, TaskStatus.AWAITING_EXTERNAL, TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.QUEUED if hasattr(TaskStatus, "QUEUED") else TaskStatus.RECEIVED])
def test_nothing_else_is_touched_however_old(env, status):
    repos, audit = env
    other = _task(repos, status, 500)
    assert sweep_stale_blocked(repos, audit, max_age=DAY) == []
    assert repos.tasks.get(other).status == status


def test_the_sweep_is_idempotent(env):
    repos, audit = env
    _task(repos, TaskStatus.BLOCKED, 30)
    assert len(sweep_stale_blocked(repos, audit, max_age=DAY)) == 1
    assert sweep_stale_blocked(repos, audit, max_age=DAY) == []


def test_closing_is_recorded_as_a_system_cancel(env):
    repos, audit = env
    old = _task(repos, TaskStatus.BLOCKED, 30)
    sweep_stale_blocked(repos, audit, max_age=DAY)
    assert [s.actor for s in repos.task_signals.list_for_task(old)] == ["system:auto-close"]


def test_the_worker_sweeps_at_most_every_ten_minutes_and_not_when_disabled(env):
    repos, audit = env
    worker = TaskWorker(repos, audit, auto_close_blocked_hours=24)
    first = _task(repos, TaskStatus.BLOCKED, 30)
    assert worker._close_stale_tasks() == [first]
    second = _task(repos, TaskStatus.BLOCKED, 30)
    assert worker._close_stale_tasks() == []  # too soon
    assert repos.tasks.get(second).status == TaskStatus.BLOCKED
    off = TaskWorker(repos, audit, auto_close_blocked_hours=0)
    assert off._close_stale_tasks() == []
