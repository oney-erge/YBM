"""A calculated number must come from a tool result."""

from __future__ import annotations

import pytest

from agent_control.orchestration.number_check import asks_for_a_calculation, ungrounded_numbers

CELLS = [{"tool_name": "document.manage", "input": {"operation": "extract_text"}, "status": "succeeded",
          "output_summary": "Sheet Orders:\n  A2=keyboard  B2=3  C2=40.0\n  A3=monitor  B3=2  C3=215.5\n  A4=cable  B4=10  C4=6.0"}]
ASK = "Open orders.xlsx and tell me the total order value: quantity times unit price for every row, added together."


def test_a_wrong_mental_total_is_caught() -> None:
    assert ungrounded_numbers(ASK, "The total order value is 48076.25.", CELLS) == ["48076.25"]


def test_a_total_a_script_printed_is_grounded() -> None:
    history = [*CELLS, {"tool_name": "code.interpreter", "status": "succeeded",
                        "output_summary": "Python completed successfully and created 0 file(s). 611.0"}]
    assert ungrounded_numbers(ASK, "The total is 611.", history) == []  # 611 and 611.0 are the same number


def test_formatting_does_not_change_a_number() -> None:
    history = [{"tool_name": "code.interpreter", "status": "succeeded", "output_summary": "1628.894627\n1628.89"}]
    assert ungrounded_numbers("What do I have after ten years at 5 percent compounded?", "About $1,628.89.", history) == []


def test_numbers_the_user_wrote_and_small_counts_are_not_checked() -> None:
    history = [{"tool_name": "filesystem.manage", "status": "succeeded", "output_summary": "a.txt b.txt c.txt"}]
    answer = "18% of 2,450 needs 3 files and 2 steps."
    assert ungrounded_numbers("What is 18 percent of 2,450?", answer, history) == []


def test_a_request_that_asks_for_no_calculation_is_never_checked() -> None:
    answer = "The locker code is 4471 on page 22."
    assert asks_for_a_calculation("Find the locker code in the log.") is False
    assert ungrounded_numbers("Find the locker code in the log.", answer, []) == []


def test_check_entries_do_not_count_as_evidence() -> None:
    history = [{"tool_name": "_audit_check", "status": "audit_gap", "error": "you wrote 48076.25"}]
    assert ungrounded_numbers(ASK, "48076.25", history) == ["48076.25"]


@pytest.mark.parametrize("text", [
    "What is 18 percent of 2,450?", "Add up the Amount column", "What is the average score?", "How many days from March to July?",
    "Convert 72 km/h to m/s", "What is the 20th Fibonacci number", "Work out 15% of 80",
])
def test_requests_that_ask_for_a_calculation_are_recognised(text) -> None:
    assert asks_for_a_calculation(text) is True


@pytest.mark.parametrize("text", [
    "Read notes.txt and tell me what it says", "Who is the project owner?", "Find the invoice number in the PDF", "List the subfolders",
])
def test_ordinary_requests_are_not(text) -> None:
    assert asks_for_a_calculation(text) is False


# ---- in the worker ----------------------------------------------------------------------------------------------

from agent_control.config import AppSettings, CapabilityPolicy  # noqa: E402
from agent_control.orchestration.worker import TaskWorker  # noqa: E402
from agent_control.policy import PolicyEngine  # noqa: E402
from agent_control.schemas import Capability, OperatorAction, OperatorDecision, RiskLevel, TaskStatus  # noqa: E402
from agent_control.orchestration.executor import ToolExecutor  # noqa: E402
from agent_control.tools.spec import ToolDefinition  # noqa: E402
from helpers import make_repos  # noqa: E402
from test_worker_operator_loop import QueueOperator, StaticToolAdapter  # noqa: E402


def _worker(tmp_path, decisions, *, tools=("filesystem.manage", "code.interpreter"), verify=True):
    repos, audit = make_repos(tmp_path)
    settings = AppSettings(
        _env_file=None,
        capabilities={Capability.LLM_GENERATE: CapabilityPolicy(enabled=True, requires_approval=False, max_risk_level=RiskLevel.LOW)},
    )
    executor = ToolExecutor(
        PolicyEngine(settings, audit), repos, audit,
        adapters={name: StaticToolAdapter({"summary": "A2=keyboard  B2=3  C2=40.0  A3=monitor  B3=2  C3=215.5"}) for name in tools},
        tool_definitions={
            name: ToolDefinition(name=name, capability=Capability.LLM_GENERATE, enabled=True, description="test tool") for name in tools
        },
    )
    worker = TaskWorker(repos, audit, executor=executor, operator=QueueOperator(decisions), operator_max_steps=8, verify_numbers=verify)
    return repos, worker


def _read() -> OperatorDecision:
    return OperatorDecision(action=OperatorAction.CALL_TOOL, tool_name="filesystem.manage", tool_input={"operation": "read_file"}, risk_level=RiskLevel.LOW)


def _done(text: str) -> OperatorDecision:
    return OperatorDecision(action=OperatorAction.DONE, final_answer=text)


@pytest.mark.asyncio
async def test_a_calculated_number_no_tool_produced_sends_the_model_back_once(tmp_path) -> None:
    repos, worker = _worker(tmp_path, [_read(), _done("The total is 48076.25."), _done("The total is 48076.25.")])
    task = repos.tasks.create("Open orders.xlsx and tell me the total of quantity times unit price.")

    result = task
    for _ in range(3):
        result = await worker.process_task(result.id)

    sent_back = [e for e in result.metadata["operator_history"] if e["tool_name"] == "_number_check"]
    assert len(sent_back) == 1 and "48076.25" in sent_back[0]["error"] and "code.interpreter" in sent_back[0]["error"]
    # Once: the second time the same answer goes through rather than looping.
    assert result.status == TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_a_number_a_tool_produced_is_given_straight_away(tmp_path) -> None:
    repos, worker = _worker(tmp_path, [_read(), _done("The monitor line is 215.5.")])
    task = repos.tasks.create("Open orders.xlsx and tell me the total of quantity times unit price.")

    result = task
    for _ in range(2):
        result = await worker.process_task(result.id)

    assert result.status == TaskStatus.COMPLETED
    assert not [e for e in result.metadata["operator_history"] if e["tool_name"] == "_number_check"]


@pytest.mark.asyncio
async def test_without_a_code_tool_or_with_the_check_off_nothing_is_sent_back(tmp_path) -> None:
    for kwargs in ({"tools": ("filesystem.manage",)}, {"verify": False}):
        repos, worker = _worker(tmp_path / str(len(kwargs)), [_read(), _done("The total is 48076.25.")], **kwargs)
        task = repos.tasks.create("Open orders.xlsx and tell me the total of quantity times unit price.")
        result = task
        for _ in range(2):
            result = await worker.process_task(result.id)
        assert result.status == TaskStatus.COMPLETED, kwargs
