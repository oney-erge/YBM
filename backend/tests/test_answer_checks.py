"""The checks on an answer before it is delivered (orchestration/answer_checks.py): the order, and how often each may speak."""

from __future__ import annotations

from agent_control.orchestration import answer_checks
from agent_control.orchestration.answer_checks import Answer, check_answer
from agent_control.orchestration.check_entries import CHECK_ENTRY_NAMES, CHECK_ENTRY_NUMBER, CHECK_ENTRY_STRATEGY
from agent_control.schemas import TaskRecord


def _answer(text: str, *, request: str = "tell me", history: list | None = None, metadata: dict | None = None, addresses=(), numbers=()) -> Answer:
    history = history if history is not None else []
    task = TaskRecord(objective=request, metadata={"original_message_text": request, **(metadata or {})})
    real = [e for e in history if isinstance(e, dict) and e.get("tool_name") not in CHECK_ENTRY_NAMES]
    return Answer(task, history, text, request, real, "step-1", tuple(addresses), tuple(numbers))


def test_an_answer_with_nothing_wrong_goes_on():
    assert check_answer(_answer("There are 3 files in the folder.")) is None


def test_a_value_no_tool_produced_sends_the_answer_back_and_is_remembered_in_the_metadata():
    pushback = check_answer(_answer("The total is 52.", numbers=("52",)))
    assert pushback is not None
    assert pushback.entry["tool_name"] == CHECK_ENTRY_NUMBER and pushback.entry["status"] == "number_gap"
    assert "52 in your answer did not come from any tool result" in pushback.entry["error"]
    assert pushback.metadata == {"operator_number_gap_count": 1}


def test_an_address_and_a_number_are_given_in_one_reason():
    pushback = check_answer(_answer("x", addresses=("a@b.example",), numbers=("7",)))
    assert pushback is not None
    assert "a@b.example in your answer is not in any tool result" in pushback.entry["error"] and " Also, 7 in your answer" in pushback.entry["error"]


def test_a_sum_that_does_not_add_up_is_pointed_out_once():
    wrong = "Totals: 10 + 20 + 30 = 50"
    first = check_answer(_answer(wrong))
    assert first is not None and first.entry["hint_id"] == "sum" and first.entry["tool_name"] == CHECK_ENTRY_NUMBER
    assert "give the correct total (60)" in first.entry["error"]
    assert check_answer(_answer(wrong, history=[first.entry])) is None


def test_a_quotation_nothing_contains_is_pointed_out_once():
    answer = 'The note says "meet me at the old bridge at dawn" and nothing else.'
    first = check_answer(_answer(answer, request="what does the note say"))
    assert first is not None and first.entry["hint_id"] == "quotes" and first.entry["tool_name"] == CHECK_ENTRY_STRATEGY
    assert check_answer(_answer(answer, request="what does the note say", history=[first.entry])) is None


def test_a_quotation_the_user_gave_or_a_tool_returned_is_not_invented():
    history = [{"tool_name": "filesystem.manage", "status": "ok", "output_summary": 'the file holds "meet me at the old bridge at dawn"'}]
    assert check_answer(_answer('It says "meet me at the old bridge at dawn".', request="what does the note say", history=history)) is None


def test_the_checks_run_in_the_order_they_are_listed():
    assert [label for label, _ in answer_checks.CHECKS] == [
        "a value no tool produced", "a sum that does not add up", "a quotation nothing contains", "a receipt that does not match the request",
        "something said to be done that nothing shows",
    ]
    # an answer that fails the first and the second is sent back for the first
    pushback = check_answer(_answer("10 + 20 + 30 = 50", numbers=("50",)))
    assert pushback is not None and pushback.entry["status"] == "number_gap" and "hint_id" not in pushback.entry


def test_a_receipt_that_says_less_than_the_request_asked_is_pointed_out_at_most_twice(monkeypatch):
    monkeypatch.setattr(answer_checks.ladder, "write_receipt_gap", lambda request, paths: "the file was not written")
    monkeypatch.setattr(answer_checks.ladder, "move_scope_gap", lambda request, moves: None)
    first = check_answer(_answer("Done."))
    assert first is not None and first.entry["hint_id"] == "receipt1" and first.entry["error"] == "Declared done, but the file was not written"
    second = check_answer(_answer("Done.", history=[first.entry]))
    assert second is not None and second.entry["hint_id"] == "receipt2"
    assert check_answer(_answer("Done.", history=[first.entry, second.entry])) is None
