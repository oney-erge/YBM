"""A survey finds every one of something in a place, with a source for every cell, and stops when a measure says so (jobs/survey.py)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agent_control.jobs import engines, survey
from agent_control.jobs.research import Page, ResearchIO

PAGE_A = (
    "Licensed child care in Pearland. Little Sprouts Academy, 123 Main St, Pearland. Monthly tuition is $1,200 for infants. Licensed by the state. "
    "Bright Path Daycare, 45 Oak Ave, Pearland. Monthly tuition is $1,050 for infants. Open 6:30 am to 6:00 pm."
)
PAGE_B = "Parents' directory. LITTLE SPROUTS ACADEMY at 123 Main St offers hours 6:30 am to 6:00 pm. Oak Tree Kids, 9 Elm Rd, Pearland. Hours 7 am to 5 pm."
PAGES = {"https://reg.example/pearland": PAGE_A, "https://dir.example/pearland": PAGE_B, "https://sprouts.example/": "Little Sprouts Academy. Licensed by the state, no violations in 2025."}


def _rows_for(text: str) -> survey._Rows:
    rows = []
    if "Little Sprouts" in text or "LITTLE SPROUTS" in text:
        cells = []
        if "$1,200" in text:
            cells.append(survey._Cell(criterion="monthly cost", value="$1,200", quote="Monthly tuition is $1,200 for infants"))
            cells.append(survey._Cell(criterion="reviews", value="excellent", quote="rated excellent by every parent"))  # not on the page: dropped
        if "hours 6:30" in text:
            cells.append(survey._Cell(criterion="hours", value="6:30 to 6:00", quote="hours 6:30 am to 6:00 pm"))
        rows.append(survey._Row(name="LITTLE SPROUTS ACADEMY" if "LITTLE" in text else "Little Sprouts Academy", address="123 Main St" if "123 Main" in text else "", cells=cells))
    if "Bright Path" in text:
        rows.append(survey._Row(name="Bright Path Daycare", address="45 Oak Ave, Pearland", cells=[survey._Cell(criterion="monthly cost", value="$1,050", quote="Monthly tuition is $1,050 for infants")]))
    if "Oak Tree" in text:
        rows.append(survey._Row(name="Oak Tree Kids", address="9 Elm Rd, Pearland", cells=[survey._Cell(criterion="hours", value="7 to 5", quote="Hours 7 am to 5 pm")]))
    return survey._Rows(rows=rows)


class _Io:
    def __init__(self, *, more_searches: int = 2) -> None:
        self.more_left = more_searches
        self.searched: list[str] = []

    async def think(self, system, user, schema):
        if schema is survey._Plan:
            return survey._Plan(entity="daycare", area="Pearland, Texas", criteria=["monthly cost", "hours", "license status", "reviews"], searches=["Texas licensed child care search Pearland", "Pearland daycare directory"])
        if schema is survey._Rows:
            return _rows_for(user)
        if schema is survey._More:
            self.more_left -= 1
            return survey._More(searches=[f"daycares near Pearland round {self.more_left}"] if self.more_left >= 0 else [])
        if schema is survey._Rating:
            return survey._Rating(score=8 if "Little Sprouts" in user else 5, reason="meets the criteria")
        if schema is survey._Summary:
            return survey._Summary(text="Three daycares were found; Little Sprouts looks best on cost and hours.")
        raise AssertionError(schema)

    async def write(self, system, user):
        return ""

    async def search(self, query, count):
        self.searched.append(query)
        if "directory" in query:
            return [{"title": "Directory", "url": "https://dir.example/pearland", "snippet": ""}]
        if "Little Sprouts" in query:
            return [{"title": "Sprouts", "url": "https://sprouts.example/", "snippet": ""}]
        return [{"title": "Registry", "url": "https://reg.example/pearland", "snippet": ""}]

    async def fetch(self, url):
        text = PAGES.get(url)
        return Page("page", text) if text else None

    @property
    def io(self) -> ResearchIO:
        return ResearchIO(think=self.think, write=self.write, search=self.search, fetch=self.fetch)


async def _run(io: _Io, goal: str = "Find every daycare in Pearland and compare them", limit: int = 400):
    state = survey.new_state()
    stages = []
    for _ in range(limit):
        unit = await survey.advance(goal, state, io.io)
        state = unit.state
        stages.append(state["stage"])
        if unit.report is not None:
            return state, unit, stages
    raise AssertionError("the survey did not finish")


@pytest.mark.asyncio
async def test_a_survey_plans_searches_reads_pages_and_keeps_only_cells_whose_words_are_on_the_page() -> None:
    io = _Io()
    state = survey.new_state()
    unit = await survey.advance("Find every daycare in Pearland", state, io.io)
    assert unit.urgent and "every daycare" in unit.say and "monthly cost" in unit.say and "three days" in unit.say  # the estimate and the stop rule are said at once
    state = unit.state
    assert state["stage"] == "search" and state["criteria"][0] == "monthly cost"
    while state["stage"] == "search":
        state = (await survey.advance("g", state, io.io)).state
    assert state["stage"] == "harvest" and len(state["pages"]) == 2
    while state["stage"] == "harvest":
        state = (await survey.advance("g", state, io.io)).state
    names = {row["name"].lower() for row in state["rows"]}
    assert names == {"little sprouts academy", "bright path daycare", "oak tree kids"}  # the same daycare on two pages, in two spellings, is one row
    sprouts = next(r for r in state["rows"] if "sprouts" in r["name"].lower())
    assert set(sprouts["cells"]) == {"monthly cost", "hours"}  # "reviews" had no words on the page: dropped
    assert sprouts["cells"]["monthly cost"]["url"] == "https://reg.example/pearland" and len(sprouts["sources"]) == 2
    assert state["new_this_round"] == 3


@pytest.mark.asyncio
async def test_a_survey_goes_round_again_until_new_searches_stop_finding_anything_and_then_reports_with_sources() -> None:
    io = _Io(more_searches=4)
    state, unit, stages = await _run(io)
    assert "judge" in stages and "deepen" in stages and "rate" in stages
    assert state["stop_reason"] in ("three rounds of new searches found almost nothing new", "no new way to search was left")
    assert state["round"] >= 2 and len(state["rows"]) == 3
    report = unit.report
    assert "# Survey: Find every daycare in Pearland" in report and "## The best" in report and "## Everyone found" in report
    assert "[Little Sprouts Academy](" in report and "[source](https://reg.example/pearland)" in report
    assert "not found" in report  # a cell nobody could fill is said, not filled in
    assert "three daycares were found" in report.lower()
    ranked = [r for r in state["rows"] if r["score"] is not None]
    assert ranked and max(r["score"] for r in ranked) == 8


def test_the_stop_measure_is_a_share_of_all_rows_over_the_last_three_rounds() -> None:
    base = {"rows": [{}] * 100}
    assert not survey.steady({**base, "round_new": [40, 9]})  # fewer than three rounds is never enough
    assert not survey.steady({**base, "round_new": [40, 9, 1]})  # the last three rounds added 50
    assert not survey.steady({**base, "round_new": [60, 10, 1, 1, 0]})  # 2 of 100 is 2 percent: not under
    assert survey.steady({"rows": [{}] * 101, "round_new": [60, 10, 1, 0, 0]})  # 1 of 101
    assert not survey.steady({"rows": [{}] * 300, "round_new": [200, 3, 2, 1]})  # 6 of 300 is 2 percent: not under
    assert survey.steady({"rows": [{}] * 400, "round_new": [300, 3, 2, 1]})  # 6 of 400 is 1.5 percent
    assert not survey.steady({"rows": [], "round_new": [0, 0, 0]})  # nothing found is not a finished survey


def test_a_small_table_is_steady_when_the_last_three_rounds_found_under_two_rows() -> None:
    """11/19, the real daycare survey: 23 rows, rounds of [4, 14, 0, 0, 1, 4, 0, 0]. 2 percent of 23 is under one row, so one stray find in three rounds kept it searching until the round cap."""
    small = {"rows": [{}] * 23}
    assert survey.steady({**small, "round_new": [4, 14, 0, 0, 1]})  # 1 new in the last three rounds
    assert not survey.steady({**small, "round_new": [4, 14, 0, 1, 1]})  # 2 are not under two
    assert not survey.steady({**small, "round_new": [4, 14, 0, 0, 1, 4]})  # 5 in the last three
    assert survey.MAX_ROUNDS >= 30  # the cap is a backstop; the measure and the hours decide


@pytest.mark.asyncio
async def test_a_gap_is_looked_for_on_a_different_page_each_time_never_the_same_page_twice() -> None:
    """The real survey read the first search result for every gap, so a second look read the same page and found the same nothing."""
    class _Tries(_Io):
        def __init__(self) -> None:
            super().__init__()
            self.fetched: list[str] = []

        async def search(self, query, count):
            return [{"title": "one", "url": "https://one.example/a", "snippet": ""}, {"title": "two", "url": "https://two.example/b", "snippet": ""}, {"title": "three", "url": "https://three.example/c", "snippet": ""}]

        async def fetch(self, url):
            self.fetched.append(url)
            return Page("page", "A page that says nothing about this daycare.")

    io = _Tries()
    state = survey.new_state()
    state.update({"stage": "deepen", "criteria": ["hours"], "entity": "daycare", "area": "Pearland"})
    state["rows"] = [{"id": 1, "name": "Oak Tree Kids", "address": "9 Elm Rd", "url": "", "cells": {}, "sources": [], "tries": {}, "score": None, "reason": ""}]
    state["deepen_queue"] = [1]
    for _ in range(6):
        if state["stage"] != "deepen":
            break
        state = (await survey.advance("g", state, io.io)).state
    assert io.fetched == ["https://one.example/a", "https://two.example/b", "https://three.example/c"]  # three different pages, one each time
    assert state["rows"][0]["tries"]["hours"] == survey.MAX_TRIES_PER_CELL and state["stage"] == "rate"


@pytest.mark.asyncio
async def test_the_ceiling_stops_the_search_and_a_job_that_found_nothing_says_so() -> None:
    io = _Io()
    state = survey.new_state()
    state["started_at"] = (datetime.now(timezone.utc) - timedelta(hours=100)).isoformat()
    state["stage"] = "judge"
    state["round_new"] = [5]
    state["rows"] = [{"id": 1, "name": "A", "address": "", "url": "", "cells": {}, "sources": [], "tries": {}, "score": None, "reason": ""}]
    state["criteria"] = ["hours"]
    state["entity"], state["area"] = "daycare", "Pearland"
    unit = await survey.advance("g", state, io.io)
    assert "limit was reached" in unit.state["stop_reason"] and unit.state["stage"] in ("deepen", "rate")


def test_a_survey_is_a_job_kind_with_progress_text_and_a_new_state() -> None:
    assert "survey" in engines.KINDS and engines.engine_for("survey") is survey
    assert engines.new_state_for("survey")["kind"] == "survey"
    state = {**survey.new_state(), "stage": "harvest", "entity": "daycare", "phrasings": ["a", "b"], "queries_done": 2, "pages": [{"status": "read"}, {"status": "new"}], "rows": [{}]}
    assert "every daycare" in survey.progress_text(state) and "1 of 2 pages read" in survey.progress_text(state)
