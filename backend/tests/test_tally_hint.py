"""A request to add up what several files hold: after two reads the model is told to let code do the arithmetic (orchestration/ladder.py)."""

from __future__ import annotations

import pytest

from agent_control.orchestration import ladder
from agent_control.orchestration.request_facts import RequestFacts

REQUEST = "How many lines are there in total in all the .txt files under C:/data/texts? Count every subfolder too."


def _read(name: str, status: str = "succeeded") -> dict:
    return {"tool_name": "filesystem.manage", "input": {"operation": "read_file", "path": name}, "status": status, "output_summary": "1 2 3"}


@pytest.mark.parametrize(
    "text",
    [REQUEST, "what is the sum of the amounts in these receipts", "add up the invoices", "the average of the three scores", "what do they cost altogether"],
)
def test_these_requests_add_things_up(text):
    assert RequestFacts.from_text(text).tallies


@pytest.mark.parametrize("text", ["what is the difference between a.txt and b.txt", "summarize the report", "open https://example.com/total and read it"])
def test_these_do_not(text):
    assert not RequestFacts.from_text(text).tallies


def test_the_hint_is_due_after_two_reads_only():
    assert not ladder.tally_hint_due(REQUEST, [_read("a.txt")])
    assert not ladder.tally_hint_due(REQUEST, [_read("a.txt"), _read("b.txt", status="failed")])
    assert ladder.tally_hint_due(REQUEST, [_read("a.txt"), _read("b.txt")])
    assert not ladder.tally_hint_due("compare a.txt and b.txt", [_read("a.txt"), _read("b.txt")])


def test_next_hint_gives_it_once_and_only_when_code_can_run_and_has_not():
    steps = [_read("a.txt"), _read("b.txt")]
    hint = ladder.next_hint(REQUEST, steps, set(), code_enabled=True)
    assert hint is not None and hint.id == "tally" and "code.interpreter" in hint.text
    assert ladder.next_hint(REQUEST, steps, {"tally"}, code_enabled=True) is None
    assert ladder.next_hint(REQUEST, steps, set(), code_enabled=False) is None
    ran = {"tool_name": "code.interpreter", "input": {"operation": "run_python"}, "status": "succeeded", "output_summary": "15"}
    assert ladder.next_hint(REQUEST, [*steps, ran], set(), code_enabled=True) is None
