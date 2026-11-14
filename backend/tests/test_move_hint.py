"""A request to move some files into a folder the user names: the model is told once not to sort everything by type (orchestration/ladder.py)."""

from __future__ import annotations

import pytest

from agent_control.orchestration import ladder
from agent_control.orchestration.request_facts import RequestFacts

REQUEST = "Move the .png files in C:/data/photos into a subfolder called pictures. Leave everything else where it is."
LISTING = {"tool_name": "filesystem.manage", "input": {"operation": "inspect_folder"}, "status": "succeeded", "output_summary": "Found 7 item(s)."}
MANIFEST = {"tool_name": "filesystem.manage", "input": {"operation": "apply_manifest"}, "status": "succeeded", "output_summary": "Moved 3."}


@pytest.mark.parametrize(
    "text",
    [
        REQUEST,
        "move all my invoices into a folder named Taxes",
        "Please move the pdfs to a new directory called archive",
    ],
)
def test_these_requests_move_files_into_a_folder_they_name(text):
    assert RequestFacts.from_text(text).moves_into_a_named_folder


@pytest.mark.parametrize(
    "text",
    [
        "Organize my downloads into folders by type",
        "move the report to my desktop",
        "create a folder called notes",
        "open https://example.com/folder-called-x and move on",
    ],
)
def test_these_do_not(text):
    assert not RequestFacts.from_text(text).moves_into_a_named_folder


def test_the_hint_comes_after_the_first_look_at_files_and_once():
    assert ladder.move_hint(REQUEST, [], set()) is None
    hint = ladder.move_hint(REQUEST, [LISTING], set())
    assert hint is not None and hint.id == "move" and "apply_manifest" in hint.text and "organize_plan" in hint.text
    assert ladder.move_hint(REQUEST, [LISTING], {"move"}) is None


def test_nothing_is_said_when_the_files_were_already_moved_or_the_tool_is_another():
    assert ladder.move_hint(REQUEST, [LISTING, MANIFEST], set()) is None
    assert ladder.move_hint(REQUEST, [{**LISTING, "tool_name": "browser.open"}], set()) is None
    assert ladder.move_hint("Organize my downloads by type", [LISTING], set()) is None


def test_next_hint_gives_it():
    hint = ladder.next_hint(REQUEST, [LISTING], set(), code_enabled=False)
    assert hint is not None and hint.id == "move"
