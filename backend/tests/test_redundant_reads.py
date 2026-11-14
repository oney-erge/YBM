"""A model that asks again for a read it already has is pointed at the result, then the task finishes with it."""

from __future__ import annotations

import pytest
from test_worker_operator_loop import QueueOperator, _executor_with_adapter, _settings

from agent_control.orchestration import StaticToolAdapter, TaskWorker
from agent_control.orchestration.call_rewrites import ALREADY_DONE_STATUS
from agent_control.orchestration.repeats import already_done_summary, redundant_read
from agent_control.schemas import OperatorAction, OperatorDecision, RiskLevel, TaskStatus
from helpers import make_repos

READ = {"operation": "read_file", "path": "C:/notes/a.txt"}


def _entry(tool: str = "filesystem.manage", tool_input: dict | None = None, status: str = "succeeded", summary: str = "the notes") -> dict:
    return {"tool_name": tool, "input": tool_input or READ, "status": status, "output_summary": summary}


def test_the_same_read_again_is_redundant() -> None:
    found = redundant_read([_entry()], "filesystem.manage", dict(READ))
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
    assert redundant_read(history, tool, tool_input) is None


def test_a_write_since_the_read_makes_a_new_read_worthwhile() -> None:
    written = _entry(tool_input={"operation": "write_text_file", "path": "C:/notes/a.txt", "content": "x"}, summary="wrote it")
    assert redundant_read([_entry(), written], "filesystem.manage", dict(READ)) is None
    assert redundant_read([_entry(), _entry(status="failed")], "filesystem.manage", dict(READ)) is not None  # a failed step changes nothing


def test_the_nudge_says_to_finish_from_the_result() -> None:
    text = already_done_summary(2, _entry(summary="the notes say hello"), read=True)
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
    worker = TaskWorker(repos, audit, executor=executor, operator=QueueOperator([_read_call()] * 4), operator_max_steps=8)

    result = task
    for _ in range(4):
        result = await worker.process_task(result.id)

    assert len(adapter.requests) == 1  # read once
    history = result.metadata["operator_history"]
    # Shown the result twice, the model is first offered the chance to answer in its own words; it asks to read again,
    # so the task finishes with the result it already has.
    assert [entry["status"] for entry in history] == ["succeeded", ALREADY_DONE_STATUS, ALREADY_DONE_STATUS, "stuck", ALREADY_DONE_STATUS]
    assert "finish with action done" in history[1]["output_summary"]
    assert result.status == TaskStatus.COMPLETED
    assert result.metadata["synthesized_answer"] == "The notes say hello."


@pytest.mark.asyncio
async def test_a_model_that_keeps_re_reading_is_offered_its_own_answer_first(tmp_path) -> None:
    """Live: "What is the total on the receipt?" (it has none) got the whole file back, because the model re-read it."""
    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("What is the total amount on receipt.pdf")
    adapter = StaticToolAdapter({"summary": "Receipt from Corner Cafe. Items: one coffee, one bagel."})
    executor = _executor_with_adapter(_settings(), audit, repos, adapter, tool_name="filesystem.manage")
    operator = QueueOperator([
        _read_call(), _read_call(), _read_call(),
        OperatorDecision(action=OperatorAction.DONE, final_answer="The receipt does not show a total, only a coffee and a bagel."),
    ])
    worker = TaskWorker(repos, audit, executor=executor, operator=operator, operator_max_steps=8)

    result = task
    for _ in range(4):
        result = await worker.process_task(result.id)

    assert result.status == TaskStatus.COMPLETED
    assert result.metadata["synthesized_answer"].startswith("The receipt does not show a total, only a coffee and a bagel.")


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


# ---- a finish written as a tool call -------------------------------------------------------------------------------
# Live: after reading a spreadsheet the model answered with {"action": "call_tool", "tool_name": "done", ...} twice and
# was told "unregistered tool: done" with the correct total already written in the input.


def _finish_as_tool(name: str = "done", **tool_input) -> OperatorDecision:
    return OperatorDecision(action=OperatorAction.CALL_TOOL, tool_name=name, tool_input=tool_input, risk_level=RiskLevel.LOW)


@pytest.mark.parametrize("name", ["done", "Done", "final_answer", "finish"])
def test_a_finish_named_as_a_tool_is_the_done_action(name) -> None:
    from agent_control.orchestration.finishing import finish_named_as_a_tool

    repaired = finish_named_as_a_tool(_finish_as_tool(name, final_answer="  The total is 611.  "))
    assert repaired.action == OperatorAction.DONE and repaired.final_answer == "The total is 611."
    assert repaired.tool_name is None and repaired.tool_input == {}


@pytest.mark.parametrize(
    "decision",
    [
        _finish_as_tool("done"),  # no answer written: nothing to repair
        _finish_as_tool("done", final_answer="  "),
        _finish_as_tool("filesystem.manage", final_answer="not a finish"),  # a real tool keeps being a tool
        OperatorDecision(action=OperatorAction.DONE, final_answer="already done"),
    ],
)
def test_other_decisions_are_left_alone(decision) -> None:
    from agent_control.orchestration.finishing import finish_named_as_a_tool

    assert finish_named_as_a_tool(decision) == decision


@pytest.mark.asyncio
async def test_a_task_finishes_when_the_model_writes_done_as_a_tool(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("What is two plus two")
    executor = _executor_with_adapter(_settings(), audit, repos, StaticToolAdapter({"summary": "x"}), tool_name="llm")
    worker = TaskWorker(repos, audit, executor=executor, operator=QueueOperator([_finish_as_tool("done", final_answer="Four.")]))

    result = await worker.process_task(task.id)

    assert result.status == TaskStatus.COMPLETED and result.metadata["synthesized_answer"] == "Four."
