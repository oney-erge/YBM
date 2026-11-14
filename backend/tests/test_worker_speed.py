"""Three things that cost a task time without changing what it does: a progress line awaited, a slow poll, and an audit of a write."""

from __future__ import annotations

import asyncio

import pytest

from agent_control.cli import WORKER_POLL_SECONDS
from agent_control.orchestration import TaskWorker
from agent_control.orchestration.worker import _only_receipted_writes
from agent_control.schemas import TaskRecord, TaskStatus
from helpers import make_repos


def _step(tool: str, operation: str, status: str = "succeeded") -> dict:
    return {"tool_name": tool, "input": {"operation": operation}, "status": status, "output_summary": "ok"}


def test_an_answer_that_only_reports_writes_and_sends_is_not_audited():
    assert _only_receipted_writes([_step("filesystem.manage", "write_text_file")] * 3)
    assert _only_receipted_writes([_step("filesystem.manage", "apply_manifest")])
    assert _only_receipted_writes([_step("document.manage", "create_presentation"), _step("artifact.deliver", "send_file")])


def test_anything_that_reads_runs_a_script_browses_or_failed_keeps_the_audit():
    write = _step("filesystem.manage", "write_text_file")
    assert not _only_receipted_writes([])
    assert not _only_receipted_writes([_step("filesystem.manage", "inspect_folder"), write])
    assert not _only_receipted_writes([write, _step("filesystem.manage", "read_file")])
    assert not _only_receipted_writes([write, _step("code.interpreter", "run_python")])
    assert not _only_receipted_writes([write, _step("browser.control", "fill_form")])
    assert not _only_receipted_writes([write, _step("filesystem.manage", "write_text_file", status="failed")])
    assert not _only_receipted_writes([write, _step("filesystem.manage", "write_text_file", status="already_done")])
    assert not _only_receipted_writes([{"tool_name": "filesystem.manage", "input": None, "status": "succeeded"}])


def test_an_idle_worker_looks_for_work_every_second_not_every_three():
    assert WORKER_POLL_SECONDS == 1.0


class _Sink:
    def __init__(self, delays: list[float]) -> None:
        self.delays, self.started, self.finished = list(delays), [], []

    async def notify(self, task: TaskRecord) -> None:
        label = f"{task.status.value}:{task.metadata.get('n')}"
        self.started.append(label)
        await asyncio.sleep(self.delays.pop(0) if self.delays else 0)
        self.finished.append(label)


def _task(status: TaskStatus, n: int, task_id: str = "task_1") -> TaskRecord:
    return TaskRecord(id=task_id, objective="x", status=status, metadata={"n": n})


@pytest.mark.asyncio
async def test_progress_lines_do_not_hold_up_the_task_and_still_arrive_in_order(tmp_path):
    repos, audit = make_repos(tmp_path)
    sink = _Sink([0.2, 0.0, 0.0])
    worker = TaskWorker(repos, audit, notification_sink=sink)

    started = asyncio.get_running_loop().time()
    await worker._send_notification(_task(TaskStatus.RUNNING, 1))
    await worker._send_notification(_task(TaskStatus.RUNNING, 2))
    assert asyncio.get_running_loop().time() - started < 0.1, "the worker was held up by the first progress line"

    await worker._send_notification(_task(TaskStatus.COMPLETED, 3))  # waits for both progress lines, then sends the answer
    assert sink.finished == ["running:1", "running:2", "completed:3"]
    assert worker._progress_sends == {}


@pytest.mark.asyncio
async def test_a_failing_progress_line_is_not_an_error_and_does_not_stop_the_answer(tmp_path):
    repos, audit = make_repos(tmp_path)
    sent = []

    class Flaky:
        async def notify(self, task):
            if task.status == TaskStatus.RUNNING:
                raise RuntimeError("telegram is down")
            sent.append(task.status.value)

    worker = TaskWorker(repos, audit, notification_sink=Flaky())
    await worker._send_notification(_task(TaskStatus.RUNNING, 1))
    await worker._send_notification(_task(TaskStatus.COMPLETED, 2))
    assert sent == ["completed"] and worker._progress_sends == {}
