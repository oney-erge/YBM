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


# ---- an address nobody read ---------------------------------------------------------------------------------------------
# Live: asked for Grace Hopper's email in a file that does not list her, the 9B answered "grace.hopper@navy.example".

from agent_control.orchestration.number_check import ungrounded_addresses  # noqa: E402

CONTACTS = [{"tool_name": "filesystem.manage", "input": {"operation": "read_file"}, "status": "succeeded",
             "output_summary": "Contacts. Ada Lovelace - ada@analytical.example. T. Okafor - t.okafor@northwind.example."}]


def test_an_invented_email_is_flagged() -> None:
    answer = "Grace Hopper's email address is grace.hopper@navy.example"
    assert ungrounded_addresses("What is Grace Hopper's email address in contacts.txt?", answer, CONTACTS) == ["grace.hopper@navy.example"]


def test_an_email_the_tool_returned_is_fine_in_any_case_and_with_a_full_stop() -> None:
    assert ungrounded_addresses("Okafor's email?", "It is T.Okafor@Northwind.example.", CONTACTS) == []


def test_an_address_in_the_request_or_in_memory_is_grounded() -> None:
    assert ungrounded_addresses("Send a note to bob@home.example", "Sent it to bob@home.example.", []) == []
    assert ungrounded_addresses("Email my sister", "Emailed marisol@family.example.", [], "my sister: marisol@family.example") == []


def test_a_web_link_nothing_returned_is_flagged_but_one_visited_is_not() -> None:
    visited = [{"tool_name": "browser.open", "input": {"url": "http://127.0.0.1:5000/episode.html"}, "status": "succeeded", "output_summary": "Episode 4"}]
    assert ungrounded_addresses("What is on the page?", "See http://127.0.0.1:5000/episode.html.", visited) == []
    assert ungrounded_addresses("Where is the manual?", "It is at https://docs.example.com/manual.", visited) == ["https://docs.example.com/manual"]


def test_an_answer_with_no_address_is_never_flagged() -> None:
    assert ungrounded_addresses("What is in the file?", "It lists two people and no addresses.", CONTACTS) == []


@pytest.mark.asyncio
async def test_the_worker_sends_the_model_back_once_for_an_invented_email(tmp_path) -> None:
    repos, worker = _worker(
        tmp_path, [_read(), _done("Grace Hopper's email is grace.hopper@navy.example"), _done("There is no email for Grace Hopper in the file.")]
    )
    task = repos.tasks.create("What is Grace Hopper's email address in contacts.txt?")

    result = task
    for _ in range(3):
        result = await worker.process_task(result.id)

    sent_back = [e for e in result.metadata["operator_history"] if e["tool_name"] == "_number_check"]
    assert len(sent_back) == 1 and "grace.hopper@navy.example" in sent_back[0]["error"] and "Do not make up values" in sent_back[0]["error"]
    assert result.status == TaskStatus.COMPLETED
    assert result.metadata["synthesized_answer"] == "There is no email for Grace Hopper in the file."


# --- an answer that shows its sum and gets it wrong -----------------------------------------------------------------------------------------

from agent_control.orchestration.number_check import inconsistent_sum  # noqa: E402


def test_the_suite_answer_that_listed_4_5_and_6_and_said_5_is_caught_with_the_true_total():
    answer = "5 lines are there in total in all the .txt files (4 lines in a.txt + 5 lines in b.txt + 6 lines in c.txt)."
    assert inconsistent_sum(answer) == (["4", "5", "6"], "15")


@pytest.mark.parametrize("answer", [
    "15 lines in total (4 in a.txt + 5 in b.txt + 6 in c.txt).",
    "4 + 5 + 6 = 15",
    "The total is 15: 4 + 5 + 6.",
    "That comes to 1,500 (1,000 + 500).",
    "12.5 + 7.5 is 20",
    "There are 15 lines in total.",
    "Nothing to add here",
    "Use C++ and 3 threads",
    "",
    None,
])
def test_a_sum_that_is_stated_correctly_or_no_sum_at_all_is_left_alone(answer):
    assert inconsistent_sum(answer) is None


def test_only_a_written_out_addition_counts_not_numbers_that_are_merely_near_a_plus():
    assert inconsistent_sum("Version 3 + 4 new files were added, see the notes.") == (["3", "4"], "7")
    assert inconsistent_sum("a=3\nb=+4") is None
    assert inconsistent_sum("3 apples, 4 pears and a long sentence between them + then 9 plums") is None


def test_decimals_and_thousands_separators_are_added_exactly():
    assert inconsistent_sum("It is 20 (0.1 + 0.2 + 12.5).") == (["0.1", "0.2", "12.5"], "12.8")
    assert inconsistent_sum("It is 3 (1,000 + 2,500)") == (["1000", "2500"], "3500")


# --- a total stated first with its parts listed under it ---------------------------------------------------------------------------------------

LISTED = "There are 12 lines in total across all .txt files. The files contain:\n- a.txt: 4 lines\n- sub/b.txt: 5 lines\n- sub/deeper/c.txt: 6 lines"


def test_a_total_that_the_listed_parts_do_not_add_up_to_is_caught():
    assert inconsistent_sum(LISTED) == (["4", "5", "6"], "15")
    assert inconsistent_sum("- a.txt: 4 lines\n- b.txt: 5 lines\nTotal: 12 lines") == (["4", "5"], "9")


def test_a_listed_total_that_is_right_or_that_counts_something_else_is_left_alone():
    assert inconsistent_sum(LISTED.replace("12 lines", "15 lines")) is None
    assert inconsistent_sum("There are 3 files in total:\n- a.txt: 4 lines\n- b.txt: 5 lines\n- c.txt: 6 lines") is None
    assert inconsistent_sum("There are 12 lines in total.\n- a.txt: 12 lines") is None  # one part is not an addition
    assert inconsistent_sum("12 lines in total. Biggest first:\n- a.txt: 4 lines and b.txt: 5 lines") is None  # two numbers in one item
    assert inconsistent_sum("Total: 12 lines\n- Total: 12 lines") is None


# --- the Auditor may reword an answer but not make a value in it wrong ---------------------------------------------------------------------

def test_an_auditors_wording_with_a_wrong_sum_is_dropped(tmp_path) -> None:
    repos, worker = _worker(tmp_path, [])
    task = repos.tasks.create("How many lines are there in total in all the .txt files in that folder?")
    fine = "15 lines in total (4 lines in a.txt + 5 lines in b.txt + 6 lines in c.txt)."
    wrong = "5 lines in total (4 lines in a.txt + 5 lines in b.txt + 6 lines in c.txt)."
    assert worker._audit_rewrite_adds_no_wrong_value(task, [], fine, "There are 15 lines altogether (4 + 5 + 6).") is True
    assert worker._audit_rewrite_adds_no_wrong_value(task, [], fine, wrong) is False
    # An original that was already wrong is not made a reason to refuse every wording.
    assert worker._audit_rewrite_adds_no_wrong_value(task, [], wrong, wrong) is True


def test_an_auditors_wording_with_an_address_nothing_produced_is_dropped(tmp_path) -> None:
    repos, worker = _worker(tmp_path, [])
    task = repos.tasks.create("Who owns the project?")
    assert worker._audit_rewrite_adds_no_wrong_value(task, [], "Grace owns it.", "Grace owns it: grace@navy.example") is False
    assert worker._audit_rewrite_adds_no_wrong_value(task, [], "Grace owns it.", "Grace is the owner.") is True
