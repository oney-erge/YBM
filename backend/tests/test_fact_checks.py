"""A fact the request's words matched is believed once the decision model agrees (request_facts.py, planning.py, cli._fact_checker)."""

from __future__ import annotations

import pytest

from agent_control.cli import _fact_checker
from agent_control.config import AppSettings, DecisionConfig
from agent_control.orchestration import TaskWorker
from agent_control.orchestration.request_facts import CHECKS_KEY, FACT_QUESTIONS, RequestFacts
from helpers import make_repos


class _Task:
    def __init__(self, text: str, metadata: dict | None = None) -> None:
        self.objective = text
        self.metadata = {"original_message_text": text, **(metadata or {})}


def test_a_word_alone_is_still_a_fact_when_the_model_was_not_asked() -> None:
    facts = RequestFacts.from_text("Sort the files into folders. Keep every file; do not delete anything.")
    assert facts.deletes and facts.moves  # the pattern's word, as before this existed


def test_a_no_from_the_model_cancels_a_match_and_a_yes_keeps_it() -> None:
    text = "Sort the files into folders. Keep every file; do not delete anything."
    cancelled = RequestFacts(text, text, {"deletes": False, "moves": True})
    assert not cancelled.deletes and cancelled.moves
    assert RequestFacts(text, text, {"deletes": True}).deletes


def test_a_fact_with_no_word_match_is_never_made_by_the_model() -> None:
    text = "Hello there."
    assert not RequestFacts(text, text, {"deletes": True, "tallies": True}).deletes
    assert not RequestFacts(text, text, {"tallies": True}).tallies


def test_only_the_facts_the_words_matched_are_asked_about() -> None:
    assert RequestFacts.from_text("Delete notes.txt").candidates() == ("deletes",)
    assert RequestFacts.from_text("How many lines in total? Sort them.").candidates() == ("moves", "tallies")
    assert RequestFacts.from_text("Hello there.").candidates() == ()


def test_facts_the_model_does_not_confirm_stay_the_patterns() -> None:
    facts = RequestFacts.from_text("Move the .png files into a subfolder called pictures.")
    assert facts.moves and facts.moves_into_a_named_folder and set(FACT_QUESTIONS) == {"deletes", "moves", "changes", "asks_to_understand_a_document", "tallies"}


def test_a_task_carries_the_answers_to_every_rule_that_reads_it() -> None:
    task = _Task("Keep every file; do not delete anything.", {CHECKS_KEY: {"deletes": False}})
    assert not RequestFacts.from_task(task).deletes
    assert RequestFacts.from_task(_Task("Keep every file; do not delete anything.")).deletes
    assert not RequestFacts.from_task(_Task("x", {CHECKS_KEY: "not a dict"})).deletes


# --- the worker ----------------------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_worker_asks_once_before_the_first_step_and_keeps_the_answers(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    asked: list[tuple[str, tuple[str, ...]]] = []

    async def checks(request: str, names: tuple[str, ...]) -> dict[str, bool]:
        asked.append((request, names))
        return {"deletes": False, "moves": True}

    worker = TaskWorker(repos, audit, fact_checks=checks)
    task = repos.tasks.create("Sort the files. Do not delete anything.", metadata={"original_message_text": "Sort the files. Do not delete anything."})
    task = await worker._with_request_facts(task)
    assert task.metadata[CHECKS_KEY] == {"deletes": False, "moves": True}
    assert asked == [("Sort the files. Do not delete anything.", ("deletes", "moves"))]
    await worker._with_request_facts(task)
    assert len(asked) == 1  # not again
    assert not RequestFacts.from_task(task).deletes


@pytest.mark.asyncio
async def test_a_request_no_pattern_matched_costs_no_question(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    asked: list = []

    async def checks(request: str, names: tuple[str, ...]) -> dict[str, bool]:
        asked.append(names)
        return {}

    task = repos.tasks.create("Hello there.", metadata={"original_message_text": "Hello there."})
    task = await TaskWorker(repos, audit, fact_checks=checks)._with_request_facts(task)
    assert asked == [] and CHECKS_KEY not in task.metadata


@pytest.mark.asyncio
async def test_a_model_that_fails_leaves_the_patterns_words_and_a_worker_without_it_is_unchanged(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)

    async def broken(request: str, names: tuple[str, ...]) -> dict[str, bool]:
        raise RuntimeError("model down")

    text = "Delete the old files."
    task = repos.tasks.create(text, metadata={"original_message_text": text})
    failed = await TaskWorker(repos, audit, fact_checks=broken)._with_request_facts(task)
    assert CHECKS_KEY not in failed.metadata and RequestFacts.from_task(failed).deletes
    plain = await TaskWorker(repos, audit)._with_request_facts(task)
    assert CHECKS_KEY not in plain.metadata


# --- the cli's checker ---------------------------------------------------------------------------------------------------------------------


def test_there_is_no_checker_when_the_decision_model_is_off() -> None:
    assert _fact_checker(AppSettings(_env_file=None, decision=DecisionConfig(mode="off"))) is None


@pytest.mark.asyncio
async def test_the_checker_asks_one_call_and_confirms_at_one_half(monkeypatch) -> None:
    sent: list[dict] = []

    async def fake_yes_no(self, text: str, questions: dict[str, str]) -> dict[str, float]:
        sent.append({"text": text, "questions": dict(questions)})
        return {"deletes": 0.04, "moves": 0.5}

    monkeypatch.setattr("agent_control.llm.decision.DecisionModelClient.yes_no", fake_yes_no)
    checker = _fact_checker(AppSettings(_env_file=None, decision=DecisionConfig(mode="decision")))
    assert checker is not None
    assert await checker("Sort them; do not delete anything.", ("deletes", "moves")) == {"deletes": False, "moves": True}
    assert sent == [{"text": "Sort them; do not delete anything.", "questions": {name: FACT_QUESTIONS[name] for name in ("deletes", "moves")}}]
