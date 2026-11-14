"""A request to buy or order something: the model is told once, as soon as it starts browsing for it, that it cannot pay (orchestration/ladder.py)."""

from __future__ import annotations

import pytest

from agent_control.config import LLMConfig
from agent_control.orchestration import ladder
from agent_control.orchestration.request_facts import RequestFacts

ORDER = "Order me a large pizza from the shop on the corner."
SEARCH = {"tool_name": "browser.open", "input": {"operation": "search"}, "status": "succeeded", "output_summary": "Title: pizza near me - Bing | Results ..."}


@pytest.mark.parametrize(
    "text",
    [
        ORDER,
        "Book a flight to Berlin for next Friday",
        "buy me a new laptop on amazon",
        "reserve a table for two tonight",
        "pay for my parking",
        "Can you order takeout?",
    ],
)
def test_these_requests_spend_money(text):
    assert RequestFacts.from_text(text).spends_money


@pytest.mark.parametrize(
    "text",
    [
        "Put the files in order of date",
        "In order to plan the trip, list my downloads",
        "order the rows by name",
        "what is the largest file in Downloads",
        "write a script that reads a book list",
        "open https://shop.example/order-pizza and read the menu",
    ],
)
def test_these_requests_do_not(text):
    assert not RequestFacts.from_text(text).spends_money


def test_the_hint_comes_once_the_model_starts_to_browse():
    assert ladder.money_hint(ORDER, [], set()) is None
    hint = ladder.money_hint(ORDER, [SEARCH], set())
    assert hint is not None and hint.id == "money" and "cannot" in hint.text and "action done" in hint.text
    assert ladder.money_hint(ORDER, [SEARCH], {"money"}) is None


def test_other_requests_and_other_tools_get_nothing():
    assert ladder.money_hint("what is the weather", [SEARCH], set()) is None
    assert ladder.money_hint(ORDER, [{**SEARCH, "tool_name": "filesystem.manage"}], set()) is None


def test_next_hint_gives_the_money_advice_first():
    hint = ladder.next_hint(ORDER, [SEARCH], set(), code_enabled=False)
    assert hint is not None and hint.id == "money"


def test_the_summary_after_the_reply_is_off_unless_asked_for():
    assert LLMConfig().summarize_memory_in_background is False
