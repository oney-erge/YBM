"""A research job reads one page at a time, keeps a quotation behind every claim, and never invents a source (jobs/research.py)."""

from __future__ import annotations

import pytest

from agent_control.jobs import research
from agent_control.jobs.research import Page, ResearchIO, advance, new_state

PAGE_A = "A heat pump moves heat instead of making it. " * 6 + "Air-source units cost between 4,000 and 8,000 dollars installed in the Chicago area. " + "More text about other things. " * 8
PAGE_B = "Ground-source systems cost more to install but run cheaper over twenty years, according to the survey of owners. " * 4 + "Filler. " * 40
PAGE_EMPTY = "too short"


class _Fake:
    """A model, a search engine and a web in one: scripted, and it records every call so a test can say what was asked."""

    def __init__(self, *, questions=None, pages=None, facts_for=None, rotate=False) -> None:
        self.rotate, self._searches = rotate, 0
        self.questions = questions if questions is not None else ["What do heat pumps cost?", "What do owners say?", "What goes wrong?"]
        self.pages = pages if pages is not None else {"https://a.example/hp": Page("Costs", PAGE_A), "https://b.example/gs": Page("Owners", PAGE_B)}
        self.facts_for = facts_for or {}
        self.calls: list[tuple[str, str]] = []

    async def think(self, system, user, schema):
        self.calls.append(("think", user[:60]))
        if schema is research._Questions:
            return research._Questions(questions=self.questions)
        title = user.split("Page title: ")[1].split("\n")[0]
        facts = self.facts_for.get(title, [])
        return research._Notes(facts=[research._Fact(claim=c, quote=q) for c, q in facts])

    async def write(self, system, user):
        self.calls.append(("write", user[:60]))
        if "Sections:" in user:
            return "Air-source costs 4,000 to 8,000 dollars [1]. A made-up source says otherwise [99]."
        return "Air-source units cost 4,000 to 8,000 dollars installed [1]. Another claim from nowhere [42]."

    async def search(self, query, count):
        self.calls.append(("search", query))
        urls = list(self.pages)
        if self.rotate:  # a real engine answers each question with different pages
            start, self._searches = self._searches * count, self._searches + 1
            urls = urls[start:]
        return [{"title": url.split("/")[2], "url": url, "snippet": ""} for url in urls][:count]

    async def fetch(self, url):
        self.calls.append(("fetch", url))
        return self.pages.get(url)

    @property
    def io(self) -> research.ResearchIO:
        return ResearchIO(think=self.think, write=self.write, search=self.search, fetch=self.fetch)


async def _run(fake: _Fake, goal: str = "heat pumps in Chicago", max_pages: int = 20):
    state, said, report = new_state(max_pages), [], None
    for _ in range(200):  # one unit at a time, as the runner does
        unit = await advance(goal, state, fake.io)
        state = unit.state
        if unit.say:
            said.append(unit.say)
        if unit.report is not None:
            report = unit.report
            break
    return state, said, report


@pytest.mark.asyncio
async def test_a_job_goes_from_questions_through_pages_and_notes_to_a_cited_report() -> None:
    fake = _Fake(facts_for={"Costs": [("Air-source units cost 4,000 to 8,000 dollars installed.", "Air-source units cost between 4,000 and 8,000 dollars installed in the Chicago area.")]})
    state, said, report = await _run(fake)

    assert state["stage"] == "done" and len(state["questions"]) == 3 and state["pages_read"] == 2
    assert said[0].startswith("I am researching this") and "1. What do heat pumps cost?" in said[0]
    assert any("pages to read" in line for line in said) and any("Writing the report now" in line for line in said)
    assert report is not None and report.startswith("# heat pumps in Chicago") and "## Summary" in report and "## Sources" in report
    assert "[1] a.example: https://a.example/hp" in report  # the cited source is listed with its number
    assert "[99]" not in report and "[42]" not in report  # a citation to a note that was not given is taken out
    assert "No page I could read answered this question." in report  # the questions no source answered say so


@pytest.mark.asyncio
async def test_a_fact_whose_quotation_is_not_in_the_page_is_dropped() -> None:
    """The model cannot invent a source: a note is a claim and the exact words that say it, and the words are checked against the page."""
    real = "Air-source units cost between 4,000 and 8,000 dollars installed in the Chicago area."
    fake = _Fake(facts_for={"Costs": [("Real fact.", real), ("Made-up fact.", "Heat pumps are free for everyone in Illinois, says the governor."), ("Too short.", "cost")]})
    state, _said, _report = await _run(fake)
    facts = [fact for source in state["sources"] for fact in source["facts"]]
    assert facts == [{"claim": "Real fact.", "quote": real}]  # the invented quotation and the one-word quotation are gone


@pytest.mark.asyncio
async def test_a_page_that_cannot_be_read_is_skipped_not_a_failure_of_the_job() -> None:
    fake = _Fake(pages={"https://gone.example/x": None, "https://tiny.example/y": Page("Tiny", PAGE_EMPTY), "https://a.example/hp": Page("Costs", PAGE_A)})
    state, _said, report = await _run(fake)
    statuses = {source["url"]: source["status"] for source in state["sources"]}
    assert statuses == {"https://gone.example/x": "failed", "https://tiny.example/y": "failed", "https://a.example/hp": "read"}
    assert report is not None and "1 pages read of 3 found" in report


@pytest.mark.asyncio
async def test_the_same_page_found_by_two_questions_is_read_once_and_the_page_limit_holds() -> None:
    fake = _Fake()
    state, _said, _report = await _run(fake, max_pages=5)
    urls = [source["url"] for source in state["sources"]]
    assert len(urls) == len(set(urls)) == 2  # three questions found the same two pages
    assert [call for call in fake.calls if call[0] == "fetch"] == [("fetch", "https://a.example/hp"), ("fetch", "https://b.example/gs")]
    big = _Fake(pages={f"https://s{n}.example/p": Page(f"S{n}", PAGE_A) for n in range(40)}, rotate=True)
    state, _said, _report = await _run(big, max_pages=12)
    assert len(state["sources"]) == 12 and state["pages_read"] == 12


@pytest.mark.asyncio
async def test_progress_is_told_every_ten_pages_and_never_between() -> None:
    big = _Fake(pages={f"https://s{n}.example/p": Page(f"S{n}", PAGE_A) for n in range(24)}, rotate=True)
    state, said, _report = await _run(big, max_pages=24)
    progress = [line for line in said if line.startswith("Read ")]
    assert [line.split(" of ")[0] for line in progress] == ["Read 10", "Read 20"]
    assert state["pages_read"] == 24


@pytest.mark.asyncio
async def test_too_few_questions_or_no_pages_raise_so_the_runner_can_count_the_failure() -> None:
    with pytest.raises(ValueError, match="usable question"):
        await advance("goal", new_state(), _Fake(questions=["Only one?"]).io)
    state = {**new_state(), "stage": "search", "questions": ["a?", "b?", "c?"], "queries_done": 3}
    with pytest.raises(ValueError, match="found no pages"):
        await advance("goal", state, _Fake(pages={}).io)


@pytest.mark.asyncio
async def test_a_unit_never_changes_the_state_it_was_given() -> None:
    """The runner saves the state a unit returns; a unit that raises must leave the saved one as it was, so a retry starts from the same place."""
    state = new_state()
    before = {**state, "sources": list(state["sources"])}
    await advance("goal", state, _Fake().io)
    assert state == before


def test_progress_is_said_in_words_for_every_stage() -> None:
    state = {**new_state(), "questions": ["a?", "b?"], "sources": [{"id": 1, "status": "read", "facts": []}, {"id": 2, "status": "new", "facts": []}]}
    texts = {stage: research.progress_text({**state, "stage": stage}) for stage in ("plan", "search", "read", "write", "finish")}
    assert "planning" in texts["plan"] and "0 of 2 questions searched" in texts["search"]
    assert "1 of 2 pages read (1 to go)" in texts["read"] and "0 of 2 sections written" in texts["write"] and texts["finish"] == "Finishing the report."


def test_a_sentence_no_note_supports_is_removed_and_one_that_says_what_the_sources_do_not_say_stays() -> None:
    """The first live report said "the only suitable choice" and "owners report significant savings": neither was in any note."""
    text = (
        "Air-source units cost 4,000 to 8,000 dollars [1]. They are the only suitable choice for every home. "
        "Ground-source costs more [2][99]. Owners report significant savings. The sources do not say what insulation is needed. No page covered permits."
    )
    kept = research.only_given_citations(text, {1, 2})
    assert kept == "Air-source units cost 4,000 to 8,000 dollars [1]. Ground-source costs more [2]. The sources do not say what insulation is needed. No page covered permits."
    assert research.only_given_citations("It is the best.", {1}) == ""  # nothing supported: nothing said
    assert research.only_given_citations("Costs 5 dollars [7].", {1}) == ""  # a citation to a note that was not given leaves nothing to stand on
