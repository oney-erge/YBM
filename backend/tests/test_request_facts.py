"""What a request says is read in one place (orchestration/request_facts.py), each fact from the text it always came from."""

from __future__ import annotations

import pytest

from agent_control.orchestration.request_facts import RequestFacts, own_and_objective, own_words
from agent_control.orchestration.call_rewrites import _filesystem_append_when_asked, _filesystem_listing_input_with_dates_intent, _filesystem_overwrite_when_asked_to_change, _is_simulated_delete, _named_coding_providers
from agent_control.orchestration.asking import user_asked_to_be_asked_first, users_own_words
from agent_control.schemas import TaskRecord


def _task(text: str, objective: str | None = None) -> TaskRecord:
    return TaskRecord(objective=objective or text, metadata={"original_message_text": text})


@pytest.mark.parametrize("text", [
    "Delete all the files in the junk folder", "please erase the old logs", "wipe the folder", "Get rid of the scrap files",
    "throw away the temp files", "remove all the drafts", "destroy the backups", "trash these",
])
def test_the_ways_of_saying_delete_that_were_recognised_still_are(text):
    facts = RequestFacts.from_text(text)
    assert facts.deletes and facts.only_deletes


@pytest.mark.parametrize("text", [
    "Delete the junk and move the rest into archive", "Organize the folder and get rid of the clutter", "rename and delete old ones",
])
def test_a_request_that_also_moves_or_renames_is_not_only_a_delete(text):
    facts = RequestFacts.from_text(text)
    assert facts.deletes and facts.moves and not facts.only_deletes


@pytest.mark.parametrize("text", ["What is in the folder?", "Sort my downloads by type", "Copy it to D:"])
def test_a_request_without_a_delete_never_says_it_deletes(text):
    assert RequestFacts.from_text(text).deletes is False


@pytest.mark.parametrize("text, appends", [
    ("Add the word pears on a new line", True), ("Add cherries at the end of fruit.txt", True), ("append this to the log", True),
    ("put it underneath", True), ("add one more item to the list", True), ("Write hello into a.txt", False), ("Make a new file", False),
])
def test_what_counts_as_adding_to_a_file(text, appends):
    assert RequestFacts.from_text(text).appends is appends


@pytest.mark.parametrize("text, changes", [
    ("Change it to say goodbye", True), ("update the file", True), ("replace the text", True), ("overwrite greeting.txt", True),
    ("Create a file called a.txt", False), ("Read the file", False),
])
def test_what_counts_as_changing_a_file(text, changes):
    assert RequestFacts.from_text(text).changes is changes


@pytest.mark.parametrize("text, recency", [
    ("Which file is the newest?", True), ("which memo was changed most recently", True), ("what time was a.txt last modified", True),
    ("latest report", True), ("How many files are there?", False),
])
def test_what_counts_as_asking_for_the_newest_or_oldest(text, recency):
    assert RequestFacts.from_text(text).wants_recency is recency


@pytest.mark.parametrize("text, ask_first", [
    ("Ask me before changing anything", True), ("check with me first", True), ("I want your approval before you move them", True),
    ("let me decide", True), ("only after my ok", True), ("Move the files", False),
])
def test_what_counts_as_asking_to_be_asked_first(text, ask_first):
    assert RequestFacts.from_text(text).asks_to_be_asked_first is ask_first


def test_a_named_coding_agent_is_found_by_provider_or_by_description():
    assert RequestFacts.from_text("Use Codex to write it").providers == ("codex",)
    assert RequestFacts.from_text("have Claude Code and Copilot compare").providers == ("claude_code", "github_copilot")
    assert RequestFacts.from_text("use an external coding agent").names_a_coding_agent
    assert RequestFacts.from_text("write a script").names_a_coding_agent is False


def test_what_asks_to_understand_a_document_and_the_urls_in_a_request():
    assert RequestFacts.from_text("Summarize the pdf").asks_to_understand_a_document
    assert RequestFacts.from_text("what is this about").asks_to_understand_a_document
    assert RequestFacts.from_text("Read me the pdf").asks_to_understand_a_document is False
    assert RequestFacts.from_text("Check https://a.example.org/x, then (https://b.example.org).").urls == (
        "https://a.example.org/x", "https://b.example.org",
    )


# --- each fact reads the text it always read ----------------------------------------------------------------------------------------------


def test_a_delete_is_read_from_what_the_user_wrote_not_from_a_question_the_model_added_to_the_objective():
    task = TaskRecord(
        objective="Delete the files. Question to the user: do you want me to move them? Answer: yes move them",
        metadata={"original_message_text": "Delete the files"},
    )
    assert RequestFacts.from_task(task).only_deletes is True
    assert own_words(task) == users_own_words(task) == "Delete the files"


def test_recency_and_ask_first_may_be_stated_by_the_objective_the_concierge_made():
    task = TaskRecord(objective="Find the newest file; ask me before acting", metadata={"original_message_text": "what changed lately?"})
    facts = RequestFacts.from_task(task)
    assert facts.wants_recency and facts.asks_to_be_asked_first
    assert not RequestFacts.from_task(TaskRecord(objective="x", metadata={"original_message_text": "hello"})).wants_recency
    assert own_and_objective(task) == "what changed lately? Find the newest file; ask me before acting"


def test_a_task_with_no_original_message_falls_back_to_its_objective():
    task = TaskRecord(objective="Delete everything in junk", metadata={})
    assert RequestFacts.from_task(task).only_deletes and own_words(task) == "Delete everything in junk"


# --- the guards in the worker ask the facts and behave as before --------------------------------------------------------------------------


def test_the_worker_guards_still_decide_the_same_way(tmp_path):
    existing = tmp_path / "a.txt"
    existing.write_text("old", encoding="utf-8")
    write = {"operation": "write_text_file", "path": str(existing), "content": "x"}
    assert _filesystem_append_when_asked(_task("Add pears on a new line"), "filesystem.manage", write).get("append") is True
    assert "append" not in _filesystem_append_when_asked(_task("Write pears"), "filesystem.manage", write)
    assert _filesystem_overwrite_when_asked_to_change(_task("Change it to say goodbye"), "filesystem.manage", write).get("overwrite") is True
    assert "overwrite" not in _filesystem_overwrite_when_asked_to_change(_task("Create a file"), "filesystem.manage", write)
    listing = {"operation": "inspect_folder", "root": str(tmp_path)}
    assert _filesystem_listing_input_with_dates_intent(_task("which is newest?"), "filesystem.manage", listing)["with_dates"] is True
    assert "with_dates" not in _filesystem_listing_input_with_dates_intent(_task("list it"), "filesystem.manage", listing)
    organize = {"operation": "organize_plan", "root": str(tmp_path)}
    assert _is_simulated_delete(_task("delete all the files"), "filesystem.manage", organize) is True
    assert _is_simulated_delete(_task("delete the junk and organize the rest"), "filesystem.manage", organize) is False
    assert user_asked_to_be_asked_first(_task("ask me before changing anything")) is True
    assert _named_coding_providers(_task("use claude code")) == ["claude_code"]


def test_counting_is_a_fact_only_the_decision_model_can_raise() -> None:
    """The words of "How many times does the word error appear across the log files?" match nothing, and the model added the counts in its head."""
    from agent_control.orchestration.request_facts import ADDED_FACT_QUESTIONS, ALL_FACT_QUESTIONS, CHECKS_KEY

    task = _task("How many times does the word error appear across the log files?")
    assert not RequestFacts.from_task(task).counts_things  # no pattern, no model answer: False, as before
    task.metadata[CHECKS_KEY] = {"counts_things": True}
    assert RequestFacts.from_task(task).counts_things
    task.metadata[CHECKS_KEY] = {"counts_things": False}
    assert not RequestFacts.from_task(task).counts_things
    assert set(ADDED_FACT_QUESTIONS) <= set(ALL_FACT_QUESTIONS) and "tallies" in ALL_FACT_QUESTIONS


def test_the_assistants_own_offer_is_not_the_user_naming_a_coding_agent() -> None:
    """Live 11/15: offered a hand-over to Claude Code, told no, and the task was handed over anyway because "Claude Code" in the offer counted as the user's."""
    from agent_control.clarification import objective_with_answer
    from agent_control.orchestration.call_rewrites import _coding_agent_requested
    from agent_control.orchestration.request_facts import RequestFacts
    from agent_control.schemas import TaskRecord

    offer = "I tried several ways to do this on my own and could not finish it. Do you want me to hand it to Claude Code on this computer? Reply yes to hand it over, or no and I will tell you what I found."
    request = "Please research which houseplants are safe for cats."
    objective = objective_with_answer(request, offer, "no, just tell me what you found")
    task = TaskRecord(objective=objective, metadata={"original_message_text": request, "claude_code_offer": {"question": offer, "state": "declined"}})
    facts = RequestFacts.from_task(task)
    assert not facts.names_a_coding_agent and facts.providers == ()
    assert not _coding_agent_requested(task, {"operation": "start", "provider": "claude_code"})
    # the user's own answer is still the user's: "use Claude Code" names it
    named = TaskRecord(objective=objective_with_answer(request, "Which tool should I use?", "use Claude Code please"), metadata={"original_message_text": request})
    assert RequestFacts.from_task(named).providers == ("claude_code",) and _coding_agent_requested(named, {"operation": "start", "provider": "claude_code"})
    # and a yes to the offer is still a yes
    accepted = TaskRecord(objective=objective, metadata={"original_message_text": request, "claude_code_offer": {"question": offer, "state": "accepted"}})
    assert _coding_agent_requested(accepted, {"operation": "start", "provider": "claude_code"})
    # a message that names it itself, and a question with a bracket inside it
    assert RequestFacts.from_task(TaskRecord(objective="Ask Claude Code to fix it", metadata={"original_message_text": "Ask Claude Code to fix it"})).providers == ("claude_code",)
    odd = objective_with_answer(request, "Which of [a] or [b]? Say Codex or Claude Code.", "a")
    assert RequestFacts.from_task(TaskRecord(objective=odd, metadata={"original_message_text": request})).providers == ()


def test_a_request_about_a_relative_time_gets_todays_date_and_no_other_does() -> None:
    """Live 11/15: "which files were modified in the last week?" was answered "What is today's date?" because the Operator was never told."""
    from datetime import datetime, timezone

    from agent_control.orchestration.request_facts import ALL_FACT_QUESTIONS, CHECKS_KEY, FACT_CONFIRM_AT, FACT_CONFIRM_AT_BY_NAME, today_note

    assert "depends_on_today" in ALL_FACT_QUESTIONS
    assert FACT_CONFIRM_AT_BY_NAME["depends_on_today"] < FACT_CONFIRM_AT  # a line of date is cheap: read at a lower mark than the facts that change what runs
    now = datetime(2026, 11, 15, 15, 56, tzinfo=timezone.utc)
    note = today_note({"depends_on_today": True}, now)
    assert "Today is Sunday, November 15, 2026" in note and "15:56" in note and "do not ask the person for the date" in note
    # every other request: no change to its prompt at all
    assert today_note({"depends_on_today": False}, now) == ""
    assert today_note({"counts_things": True}, now) == ""
    assert today_note(None, now) == "" and today_note("yes", now) == ""
    assert CHECKS_KEY == "fact_checks"
