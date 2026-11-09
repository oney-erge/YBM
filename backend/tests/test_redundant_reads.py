"""A model that asks again for a read it already has is pointed at the result, then the task finishes with it."""

from __future__ import annotations

import pytest
from test_worker_operator_loop import QueueOperator, _executor_with_adapter, _settings

from agent_control.orchestration import StaticToolAdapter, TaskWorker
from agent_control.orchestration.worker import ALREADY_DONE_STATUS, _already_done_summary, _redundant_read
from agent_control.schemas import OperatorAction, OperatorDecision, RiskLevel, TaskStatus
from helpers import make_repos

READ = {"operation": "read_file", "path": "C:/notes/a.txt"}


def _entry(tool: str = "filesystem.manage", tool_input: dict | None = None, status: str = "succeeded", summary: str = "the notes") -> dict:
    return {"tool_name": tool, "input": tool_input or READ, "status": status, "output_summary": summary}


def test_the_same_read_again_is_redundant() -> None:
    found = _redundant_read([_entry()], "filesystem.manage", dict(READ))
    assert found is not None and found[0] == 1


@pytest.mark.parametrize(
    "history, tool, tool_input",
    [
        ([], "filesystem.manage", READ),  # never read
        ([_entry(status="failed")], "filesystem.manage", READ),  # the first attempt did not work
        ([_entry()], "filesystem.manage", {"operation": "read_file", "path": "C:/notes/b.txt"}),  # a different file
        ([_entry()], "filesystem.manage", {"operation": "write_text_file", "path": "C:/notes/a.txt", "content": "x"}),  # not a read
        ([_entry("browser.open", {"operation": "open", "url": "http://x"})], "browser.open", {"operation": "open", "url": "http://x"}),  # pages change
        ([_entry("http.request", {"url": "http://x"})], "http.request", {"url": "http://x"}),
        ([_entry("computer.use", {"operation": "observe"})], "computer.use", {"operation": "observe"}),  # screens change
    ],
)
def test_other_calls_are_not_redundant(history, tool, tool_input) -> None:
    assert _redundant_read(history, tool, tool_input) is None


def test_a_write_since_the_read_makes_a_new_read_worthwhile() -> None:
    written = _entry(tool_input={"operation": "write_text_file", "path": "C:/notes/a.txt", "content": "x"}, summary="wrote it")
    assert _redundant_read([_entry(), written], "filesystem.manage", dict(READ)) is None
    assert _redundant_read([_entry(), _entry(status="failed")], "filesystem.manage", dict(READ)) is not None  # a failed step changes nothing


def test_the_nudge_says_to_finish_from_the_result() -> None:
    text = _already_done_summary(2, _entry(summary="the notes say hello"), read=True)
    assert "already succeeded at step 2" in text and "the notes say hello" in text and "finish with action done" in text
    assert "do it twice" not in text  # that wording is for writes


def _read_call() -> OperatorDecision:
    return OperatorDecision(action=OperatorAction.CALL_TOOL, tool_name="filesystem.manage", tool_input=dict(READ), risk_level=RiskLevel.LOW)


@pytest.mark.asyncio
async def test_a_model_that_keeps_re_reading_is_answered_from_the_first_read(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("What is in a.txt")
    adapter = StaticToolAdapter({"summary": "The notes say hello."})
    executor = _executor_with_adapter(_settings(), audit, repos, adapter, tool_name="filesystem.manage")
    worker = TaskWorker(repos, audit, executor=executor, operator=QueueOperator([_read_call(), _read_call(), _read_call()]),
                        operator_max_steps=8)

    result = task
    for _ in range(3):
        result = await worker.process_task(result.id)

    assert len(adapter.requests) == 1  # read once
    history = result.metadata["operator_history"]
    assert [entry["status"] for entry in history] == ["succeeded", ALREADY_DONE_STATUS, ALREADY_DONE_STATUS]
    assert "finish with action done" in history[1]["output_summary"]
    assert result.status == TaskStatus.COMPLETED
    assert result.metadata["synthesized_answer"] == "The notes say hello."


@pytest.mark.asyncio
async def test_a_read_that_runs_again_after_a_write_is_not_blocked(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("What is in a.txt")
    adapter = StaticToolAdapter({"summary": "ok"})
    executor = _executor_with_adapter(_settings(), audit, repos, adapter, tool_name="filesystem.manage")
    write = OperatorDecision(action=OperatorAction.CALL_TOOL, tool_name="filesystem.manage", risk_level=RiskLevel.LOW,
                             tool_input={"operation": "write_text_file", "path": "C:/notes/a.txt", "content": "new"})
    worker = TaskWorker(repos, audit, executor=executor, operator=QueueOperator([_read_call(), write, _read_call()]),
                        operator_max_steps=8)

    result = task
    for _ in range(3):
        result = await worker.process_task(result.id)

    assert len(adapter.requests) == 3  # the second read ran: the file may have changed
