"""Two gaps the fresh suite found: a newest-file question asked through a listing without dates, and a rename that was planned as a sort."""

from __future__ import annotations

from agent_control.orchestration import ladder
from agent_control.orchestration.call_rewrites import _filesystem_listing_input_with_dates_intent
from agent_control.orchestration.request_facts import RequestFacts
from agent_control.schemas import TaskRecord


def _task(text: str) -> TaskRecord:
    return TaskRecord(objective=text, metadata={"original_message_text": text})


NEWEST = "Which log file in C:/logs was changed most recently?"


def test_a_newest_file_question_asked_with_describe_folder_is_listed_with_dates_instead():
    for operation in ("describe_folder", "collect_folder_snapshot"):
        rewritten = _filesystem_listing_input_with_dates_intent(_task(NEWEST), "filesystem.manage", {"operation": operation, "root": "C:/logs"})
        assert rewritten == {"operation": "inspect_folder", "root": "C:/logs", "with_dates": True}, operation


def test_the_same_listings_are_left_alone_for_a_request_that_does_not_ask_about_dates():
    call = {"operation": "describe_folder", "root": "C:/logs"}
    assert _filesystem_listing_input_with_dates_intent(_task("What is in C:/logs?"), "filesystem.manage", call) == call
    assert _filesystem_listing_input_with_dates_intent(_task(NEWEST), "filesystem.manage", {"operation": "describe_folder"}) == {"operation": "describe_folder"}
    assert _filesystem_listing_input_with_dates_intent(_task(NEWEST), "web.search", call) == call


def test_inspect_folder_and_search_still_get_with_dates_as_before():
    for operation in ("inspect_folder", "search"):
        assert _filesystem_listing_input_with_dates_intent(_task(NEWEST), "filesystem.manage", {"operation": operation, "root": "C:/logs"})["with_dates"] is True


def _plan(status: str = "succeeded") -> dict:
    return {"tool_name": "filesystem.manage", "input": {"operation": "organize_plan", "root": "C:/r", "strategy": "by_type"}, "status": status}


RENAME = "Rename every .txt file in C:/r so that its name starts with old- and the rest of the name stays the same."


def test_what_counts_as_asking_to_rename():
    for text in (RENAME, "give them new names", "the names should start with old-"):
        assert RequestFacts.from_text(text).renames, text
    assert not RequestFacts.from_text("move the files into a folder").renames
    assert not RequestFacts.from_text("open https://example.com/rename-me").renames


def test_a_rename_planned_as_a_sort_is_pointed_to_apply_manifest_once():
    hint = ladder.rename_hint(RENAME, [_plan()], set())
    assert hint is not None and hint.id == "rename" and "apply_manifest" in hint.text and '"operation": "rename"' in hint.text
    assert ladder.rename_hint(RENAME, [_plan()], {"rename"}) is None
    assert ladder.rename_hint(RENAME, [_plan("failed")], set()) is None
    assert ladder.rename_hint("sort my downloads by type", [_plan()], set()) is None
    assert ladder.next_hint(RENAME, [_plan()], set(), code_enabled=False).id == "rename"
