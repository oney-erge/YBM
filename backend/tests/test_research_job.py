"""A research job reads one page at a time, keeps a quotation behind every claim, and never invents a source (jobs/research.py)."""

from __future__ import annotations

import re

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


def test_quote_matching_preserves_punctuation_signs_and_units() -> None:
    page = "The 4.0 kW unit costs $1,200; the change is -5 percent."
    facts = [research._Fact(claim="reported", quote=quote) for quote in (
        "4.0 kW unit costs $1,200", "4 0 kW unit costs $1,200", "change is 5 percent", "change is -5 percent",
    )]
    kept = research.verified_facts(facts, page)
    assert [item["quote"] for item in kept] == ["4.0 kW unit costs $1,200", "change is -5 percent"]


def test_report_distinguishes_found_attempted_read_useful_and_cited() -> None:
    state = new_state()
    state.update(sources=[
        {"id": 1, "status": "read", "facts": [{"quote": "Evidence"}], "title": "One", "url": "https://one.example"},
        {"id": 2, "status": "read", "facts": [], "title": "Two", "url": "https://two.example"},
        {"id": 3, "status": "failed", "facts": [], "title": "Three", "url": "https://three.example"},
        {"id": 4, "status": "new", "facts": [], "title": "Four", "url": "https://four.example"},
    ], sections=[{"question": "What?", "text": "Evidence [1]."}])
    report = research.render_report("Coverage", state)
    assert "4 pages found; 3 attempted; 2 read; 1 with recorded quotations; 1 cited" in report


@pytest.mark.asyncio
async def test_section_writer_receives_source_quote_not_a_misleading_paraphrase() -> None:
    state = new_state()
    state.update(stage="write", questions=["What does it cost?"], sources=[{
        "id": 1, "question": 0, "title": "GPU prices", "status": "read",
        "facts": [{"claim": "Offloading costs $463.", "quote": "RTX 3080 10 GB Partial offload - $463 used"}],
    }])
    seen = []
    fake = _Fake()

    async def write(system, user):
        seen.append(user)
        return "The source lists a used GPU price [1]."

    io = ResearchIO(think=fake.think, write=write, search=fake.search, fetch=fake.fetch)
    await advance("GPU question", state, io)
    assert "RTX 3080 10 GB Partial offload - $463 used" in seen[0]
    assert "Offloading costs $463" not in seen[0]


@pytest.mark.asyncio
async def test_a_page_that_cannot_be_read_is_skipped_not_a_failure_of_the_job() -> None:
    fake = _Fake(pages={"https://gone.example/x": None, "https://tiny.example/y": Page("Tiny", PAGE_EMPTY), "https://a.example/hp": Page("Costs", PAGE_A)})
    state, _said, report = await _run(fake)
    statuses = {source["url"]: source["status"] for source in state["sources"]}
    assert statuses == {"https://gone.example/x": "failed", "https://tiny.example/y": "failed", "https://a.example/hp": "read"}
    assert report is not None and "3 pages found; 3 attempted; 1 read" in report


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


class _Wide:
    """An engine with plenty to say: each distinct search gets its own pages (the same search the same ones again), and a model that rewrites searches."""

    def __init__(self, *, per_search=8, rewrite=True, rewrite_error=False, same_pages=False) -> None:
        self.per_search, self.rewrite, self.rewrite_error, self.same_pages = per_search, rewrite, rewrite_error, same_pages
        self.searched: list[str] = []

    async def think(self, system, user, schema):
        if schema is research._Questions:
            return research._Questions(questions=[f"Question {n}?" for n in range(1, 8)])
        if schema is research._Phrasings:
            if self.rewrite_error:
                raise TimeoutError("slow")
            return research._Phrasings(searches=[f"another way to ask {n}" for n in range(1, 8)] if self.rewrite else [])
        return research._Notes(facts=[])

    async def write(self, system, user):
        return "text [1]"

    async def search(self, query, count):
        self.searched.append(query)
        slug = "everything" if self.same_pages else query.replace(" ", "-").replace("?", "")  # same_pages: every search gets the same pages
        return [{"title": f"{slug} {i}", "url": f"https://site{i}.example/{slug}", "snippet": ""} for i in range(count)]

    async def fetch(self, url):
        return Page("page", "A sentence long enough to count as the text of a page. " * 6)

    @property
    def io(self) -> ResearchIO:
        return ResearchIO(think=self.think, write=self.write, search=self.search, fetch=self.fetch)


async def _search_only(wide: _Wide, max_pages: int):
    state, said = new_state(max_pages), []
    for _ in range(200):
        unit = await advance("a goal", state, wide.io)
        state = unit.state
        if unit.say:
            said.append(unit.say)
        if state["stage"] == "read":
            break
    return state, said


@pytest.mark.asyncio
async def test_a_request_for_more_pages_than_one_search_per_question_finds_searches_again_in_other_words() -> None:
    """Live 11/14: 100 pages asked for, 7 questions of 8 results, 36 pages found and the job went on to read them."""
    wide = _Wide()
    state, said = await _search_only(wide, 100)
    assert len(state["sources"]) == 100 and state["round"] == 1 and state["stage"] == "read"  # 56 in the first round, the rest in the second
    assert wide.searched[:7] == [f"Question {n}?" for n in range(1, 8)]  # the questions as written first
    assert wide.searched[7:9] == ["another way to ask 1", "another way to ask 2"]  # then each in other words
    assert any("Searching again in different words" in line for line in said) and said[-1].startswith("I found 100 pages")
    assert len({source["url"] for source in state["sources"]}) == 100  # no page twice


@pytest.mark.asyncio
async def test_no_second_round_when_the_first_found_enough_or_when_a_round_adds_nothing_or_the_model_cannot_rewrite() -> None:
    enough = await _search_only(_Wide(), 30)
    assert len(enough[0]["sources"]) == 30 and enough[0]["round"] == 0
    # An engine that answers every search with the same eight pages: the second round finds nothing new, and the job reads what it has
    state, _ = await _search_only(_Wide(same_pages=True), 100)
    assert state["stage"] == "read" and len(state["sources"]) == 8 and state["round"] == 1
    # A model that cannot rewrite the searches, or writes none: the pages found are read, not an error
    for broken in (_Wide(rewrite_error=True), _Wide(rewrite=False)):
        state, said = await _search_only(broken, 100)
        assert state["stage"] == "read" and state["round"] == 0 and len(state["sources"]) == 56
        assert not any("different words" in line for line in said)


@pytest.mark.asyncio
async def test_the_rounds_never_go_past_the_limit_and_an_older_saved_job_without_them_still_advances() -> None:
    state = new_state(100)
    for key in ("round", "phrasings", "added_this_round"):
        del state[key]  # a job saved before rounds existed
    wide = _Wide()
    for _ in range(200):
        unit = await advance("a goal", state, wide.io)
        state = unit.state
        if state["stage"] == "read":
            break
    assert state["round"] < research.MAX_SEARCH_ROUNDS and len(state["sources"]) <= 100 and state["stage"] == "read"


def test_an_abbreviation_does_not_end_a_sentence_so_a_cited_claim_is_not_cut_in_half() -> None:
    """Live 11/14: "Law No. 5403 is required [1]." was split after "No." and the report said "5403 is required [1]."."""
    text = "On farmland, a non-agricultural use permission under Law No. 5403 is required [1]. Building inspection is covered by Law No. 4708 [2][4]. 4,000 dollars is typical [3]."
    kept = research.only_given_citations(text, {1, 2, 3, 4})
    assert kept == text  # all three sentences whole, the one that starts with a number too
    assert "Law No. 5403 is required [1]." in kept and "Law No. 4708 [2][4]." in kept
    assert research.only_given_citations("A claim from memory. Art. 21 of the Zoning Law requires a permit [1]. Dr. Smith agrees.", {1}) == "Art. 21 of the Zoning Law requires a permit [1]."


class _Big(_Fake):
    """A job of many pages: the model plans chapters, then different questions for each chapter, and every page is its own."""

    def __init__(self) -> None:
        super().__init__()
        self.chapter_requests = 0

    async def think(self, system, user, schema):
        if schema is research._Chapters:
            self.chapter_requests += 1
            return research._Chapters(chapters=[f"Area {n}" for n in range(1, 12)])  # more than asked: the extra are cut
        if schema is research._Questions:
            number = user.split("chapter ")[1].split(" of")[0]
            return research._Questions(questions=[f"Chapter {number} question {n}?" for n in range(1, 4)])
        if schema is research._Phrasings:
            return research._Phrasings(searches=[])
        return research._Notes(facts=[research._Fact(claim="A fact the page states.", quote="A sentence long enough to count as the text of a page.")])

    async def write(self, system, user):
        number = re.search(r"\[(\d+)\]", user).group(1)  # cite the first note (or the first citation of what it is given), as a model asked to would
        if "Sections:" in user and "Chapter:" in user:
            return f"The chapter says one thing [{number}]."
        if "Chapters:" in user:
            return f"Across the chapters, one thing [{number}]."
        return f"This section says one thing [{number}]."

    async def search(self, query, count):
        slug = query.replace(" ", "-").replace("?", "")
        return [{"title": f"{slug} {i}", "url": f"https://site{i}.example/{slug}", "snippet": ""} for i in range(count)]

    async def fetch(self, url):
        return Page("page", "A sentence long enough to count as the text of a page. " * 6)


@pytest.mark.asyncio
async def test_a_job_of_many_pages_is_planned_and_written_in_chapters_and_its_summary_comes_from_the_overviews() -> None:
    fake = _Big()
    state, said, report = await _run_many(fake, max_pages=300)
    assert research.chapter_count(300) == 3 and research.chapter_count(100) == 0 and research.chapter_count(5000) == research.MAX_CHAPTERS
    assert state["stage"] == "done" and [chapter["title"] for chapter in state["chapters"]] == ["Area 1", "Area 2", "Area 3"]  # the model's extra chapters are cut
    assert fake.chapter_requests == 1  # the chapters are planned once; each chapter's questions in a unit of its own
    assert [(chapter["first"], chapter["end"]) for chapter in state["chapters"]] == [(0, 3), (3, 6), (6, 9)] and len(state["questions"]) == 9
    assert said[0].startswith("I am researching this in 3 chapters, up to 300 pages in all:") and "1. Area 1 (3 questions)" in said[0]
    assert all(chapter["overview"].startswith("The chapter says one thing [") for chapter in state["chapters"])
    assert report is not None and "## 1. Area 1" in report and "### Chapter 1 question 1?" in report and "## 3. Area 3" in report
    assert "The chapter says one thing [" in report and state["summary"].startswith("Across the chapters, one thing [")
    assert "Chapter 2 of 3 written: Area 2." in said


async def _run_many(fake: _Fake, goal: str = "a big question", max_pages: int = 300):
    state, said, report = new_state(max_pages), [], None
    for _ in range(3000):
        unit = await advance(goal, state, fake.io)
        state = unit.state
        if unit.say:
            said.append(unit.say)
        if unit.report is not None:
            report = unit.report
            break
    return state, said, report


@pytest.mark.asyncio
async def test_a_big_job_says_how_far_it_is_and_tells_progress_less_often_and_a_small_job_is_unchanged() -> None:
    fake = _Big()
    state, said, _report = await _run_many(fake, max_pages=300)
    progress = [line for line in said if line.startswith("Read ")]
    assert 1 <= len(progress) <= 12  # about every tenth of the pages, not every ten
    assert research.progress_text({**new_state(300), "stage": "plan", "chapter_titles": ["a", "b", "c"], "chapters": [{"title": "a"}]}) == "I am planning the questions to research: 1 of 3 chapters planned."
    assert research.progress_text({**state, "stage": "chapters"}).startswith("Writing the chapter overviews: 3 of 3 done")
    small, _said, small_report = await _run(_Fake(facts_for={"Costs": []}))
    assert small["chapters"] == [] and "## 1." not in (small_report or "")  # 100 pages or fewer: the flow it always had


def test_the_time_a_job_takes_is_said_as_a_range_on_the_card() -> None:
    assert research.time_estimate(10) == "about 5 to 10 minutes"
    assert research.time_estimate(60) == "about 10 to 15 minutes"
    assert research.time_estimate(600) == "about 1.5 to 2.5 hours"
    assert research.time_estimate(1000) == "about 2.5 to 4 hours"
    from agent_control.human_text import describe_action

    card = describe_action("job.manage", {"operation": "start_research", "goal": "heat pumps", "max_pages": 600})
    assert "up to 600 pages (about 1.5 to 2.5 hours; actual time varies)" in card and "heat pumps" in card


@pytest.mark.asyncio
async def test_search_rephrasing_is_batched_and_survives_saved_state() -> None:
    import json

    state = new_state(600)
    state["questions"] = [f"Question {index}?" for index in range(21)]
    state["sources"] = [{"url": "https://example.com", "status": "new", "facts": []}]
    calls = []

    async def think(system, user, schema):
        lines = user.splitlines()[1:]
        calls.append(len(lines))
        return schema(searches=[f"Alternative {line}" for line in lines])

    fake = _Fake()
    io = research.ResearchIO(think=think, write=fake.write, search=fake.search, fetch=fake.fetch)
    for _ in range(3):
        result = await research._more_searches(state, io)
        state = json.loads(json.dumps(result.state))

    assert calls == [10, 10, 1]
    assert len(state["phrasings"]) == 21 and state["round"] == 1
    assert "pending_phrasings" not in state


@pytest.mark.asyncio
async def test_synthetic_600_page_job_keeps_search_history_across_restarts() -> None:
    import json

    class WideBig(_Big):
        def __init__(self):
            super().__init__()
            self.rewrite_calls = 0
            self.rewrite_prompts = []

        async def think(self, system, user, schema):
            if schema is research._Phrasings:
                self.rewrite_calls += 1
                self.rewrite_prompts.append(user)
                round_number = (self.rewrite_calls - 1) // 2 + 1
                lines = user.splitlines()[1:]
                return schema(searches=[f"round {round_number} {line.split(' (previous searches:', 1)[0]}" for line in lines])
            return await super().think(system, user, schema)

    fake = WideBig()
    state, report = new_state(600), None
    for step in range(3000):
        unit = await advance("a large question", state, fake.io)
        state = unit.state
        if step % 25 == 0:
            state = json.loads(json.dumps(state))  # persisted state after restart
        if unit.report is not None:
            report = unit.report
            break
    assert report is not None and state["stage"] == "done"
    assert len(state["sources"]) == 600 and state["pages_read"] == 600
    assert len({source["url"] for source in state["sources"]}) == 600
    assert len(state["chapters"]) == 6 and len(state["search_history"]) >= 2
    assert "previous searches:" in fake.rewrite_prompts[2]
    assert "600 pages found; 600 attempted; 600 read" in report
