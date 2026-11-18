"""Deep research as a job: sources read one at a time, with checked quotations and a cited report.

A 9B model on a laptop cannot read a hundred pages in one prompt and cannot hold a long plan in its head, but it can read one page well. So the
job never asks it to. The big picture lives in the job's saved state (the questions, the sources, the notes, the sections); a unit of work
shows the model one small thing and writes down what it said. Nothing is trusted that can be checked:

    * a note is a claim and a quotation, and the quotation must be in the page text or the note is dropped;
    * a section is written from the numbered notes alone, and a citation to a number that was not given is removed;
    * a question with no retained notes is reported as unanswered.

These checks do not establish that a quotation supports a generated claim. Claims and their original context still need review.

`advance` does exactly one unit, so a restart, a pause or a person's question in between loses nothing. The model, the search and the page fetch
are passed in (`ResearchIO`), so the engine is the same in a test and on the laptop.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse, urlunparse

from pydantic import BaseModel, Field

# --- what a unit may ask of the outside world ---------------------------------------------------------------------------------------------------

Think = Callable[[str, str, type[BaseModel]], Awaitable[BaseModel]]  # system, user, schema -> the parsed answer
Write = Callable[[str, str], Awaitable[str]]  # system, user -> text
Search = Callable[[str, int], Awaitable[list[dict[str, str]]]]  # query, how many -> [{title, url, snippet}]


@dataclass(frozen=True)
class Page:
    title: str
    text: str


Fetch = Callable[[str], Awaitable[Page | None]]  # url -> the page's visible text, or None when it cannot be read


#: One read-only check of this computer or its network (diagnose.py): name and arguments in, a result with `ok` and `summary` out. Only a job that investigates has one.
ProbeRun = Callable[[str, dict[str, Any]], Awaitable[Any]]


@dataclass(frozen=True)
class ResearchIO:
    think: Think
    write: Write
    search: Search
    fetch: Fetch
    probe: ProbeRun | None = None


# --- the limits that keep one unit small enough for a small model -------------------------------------------------------------------------------

MIN_QUESTIONS, MAX_QUESTIONS = 3, 10
RESULTS_PER_QUERY = 8
#: One search per question finds about half of what a request for 100 pages needs (7 questions of 8 results, less the pages two questions share: 36 live on 11/14).
#: When the pages found are short of the limit the person set, the questions are searched again in different words, up to this many rounds in all.
MAX_SEARCH_ROUNDS = 3
PAGE_CHARS = 6_000  # the part of a page the model reads: about 1,500 tokens, so one unit is seconds, not minutes
MAX_FACTS_PER_PAGE = 4
#: A section is written from at most this many notes. It was 14, and with a hundred pages per question that threw away most of what was read.
MAX_FACTS_PER_SECTION = 20
DEFAULT_MAX_PAGES = 60
HARD_MAX_PAGES = 1000
PROGRESS_EVERY = 10  # pages read between two messages to the person (for a big job, a tenth of its pages)
#: A job of more pages than this is written in chapters: the goal is cut into areas, each area gets its own questions (so about this many pages), its own sections
#: and a short overview, and the summary is written from the overviews. The model reads at most one page or ten sections at a time.
#: Jobs remain bounded by HARD_MAX_PAGES and the configured limit. Existing jobs without chapters retain their original flow.
PAGES_PER_CHAPTER = 100
MAX_CHAPTERS = 10
#: Searches get more rounds when the person asked for more than the usual maximum of pages.
EXTRA_SEARCH_ROUNDS = 6
USUAL_MAX_PAGES = 150


class _Questions(BaseModel):
    questions: list[str] = Field(default_factory=list, description="the sub-questions that, answered, answer the goal; each a full question")


class _Chapters(BaseModel):
    chapters: list[str] = Field(default_factory=list, description="the chapters of the report, each a short title; together they cover the goal, each a different area")


class _Supported(BaseModel):
    supported: bool = Field(description="true only if the quotations say what the sentence says: the same subject, the same numbers, the same units and conditions")
    reason: str = Field(default="", description="one short phrase: what matches, or what differs")


class _Phrasings(BaseModel):
    searches: list[str] = Field(default_factory=list, description="one web search for each question, in the same order, in different words")


class _Fact(BaseModel):
    claim: str = Field(description="what the page says, in one short sentence with its numbers")
    quote: str = Field(description="the exact words from the page that say it, copied character for character")


class _Notes(BaseModel):
    # No "is this page relevant" flag: asked that first, the 9B said "not relevant" about a page of monthly running costs by system size and season.
    # Asked only for facts with their quotations, it found four on that page and none on a page about something else; no verified fact means not relevant.
    facts: list[_Fact] = Field(default_factory=list, description="up to 4 facts that help answer the question; empty if the page has none")


PLAN_SYSTEM = (
    "You plan research. Given a goal, write the sub-questions whose answers, put together, answer it: concrete, one thing each, in the order "
    "someone would need them (what the options are, what each costs, what it needs, what goes wrong, what experts or owners say). "
    f"Between {MIN_QUESTIONS} and {MAX_QUESTIONS} questions. Each is a complete question that a web search could answer."
)
CHAPTERS_SYSTEM = (
    "You plan a long research report. Given a goal, write the chapters whose findings, put together, cover it: distinct areas, each a short title, "
    "in the order someone would need them (what the options are, what each costs, what it needs, what goes wrong, what experts or owners say). "
    "Write exactly the number of chapters you are asked for."
)
CHAPTER_SYSTEM = (
    "You write the overview of one chapter of a research report from its sections. At most 120 words, plain prose: what the sections together say, "
    "the most important numbers first. Every sentence that states a fact ends with the citation numbers in square brackets that the sections gave for it, like "
    "[4] or [3][5]; a sentence with no number is removed. Do not add anything the sections do not say. Say plainly what the sources did not answer."
)
CLAIM_SYSTEM = (
    "You check one sentence of a research report against the exact quotations from the pages it cites. The sentence is supported only if the quotations say what it "
    "says: about the same subject (the same product, model, place or person; a price of a thing is not the cost of using it), with the same numbers and the same units "
    "and conditions. A number in the sentence must be in the quotations. If the quotations are about something else, say something weaker, or say the opposite, it is not supported."
)
NOTES_SYSTEM = (
    "You read part of one web page to help answer a question. Copy out up to "
    f"{MAX_FACTS_PER_PAGE} facts from the page that help answer it: prices, ranges, numbers, named options, comparisons, what people report. Each fact "
    "has a quote: the exact words from the page that say it, copied character for character, a sentence or a phrase of at least eight words. If nothing "
    "on the page helps, return an empty list. Never write a fact the page does not state."
)
PHRASE_SYSTEM = (
    "You rewrite web searches so that other websites answer them. You are given research questions. For each one write a different search: other key words and "
    "another angle (a guide, a price list, an official page, owners' or customers' experiences, a forum), not the same words in another order. One search per "
    "question, in the same order. Do not repeat any previous search listed for that question."
)
SECTION_SYSTEM = (
    "You write one section of a research report from numbered notes. Use only the notes: every sentence that states a fact ends with the numbers "
    "of the notes it comes from in square brackets, like [3] or [3][7]. Do not add anything from memory. If the notes disagree, say that they "
    "disagree and give both. If they do not answer the question, say so in one sentence. At most 150 words, plain prose, no headings."
)
SUMMARY_SYSTEM = (
    "You summarise a research report for a person who will read only this. From the sections, write the answer in at most 90 words, plain prose: "
    "the conclusion first, then the one or two things that matter most. Every sentence that states a fact must end with the citation numbers in "
    "square brackets that the section gave for it, like [4] or [3][5]; a sentence with no number is removed. Do not add anything the sections do not "
    "say. Say plainly which parts the sources did not answer."
)

_CITATION = re.compile(r"\[(\d+)\]")


# --- state -------------------------------------------------------------------------------------------------------------------------------------


def new_state(max_pages: int = DEFAULT_MAX_PAGES) -> dict[str, Any]:
    return {
        "stage": "plan",
        "max_pages": max(5, min(int(max_pages), HARD_MAX_PAGES)),
        "questions": [],
        "chapter_titles": [],  # only a job of more than PAGES_PER_CHAPTER pages: planned first, then each chapter's questions in its own unit
        "chapters": [],  # {"title", "first", "end" (its questions are questions[first:end]), "overview"}
        "queries_done": 0,
        "round": 0,  # 0 searches the questions as written; each later round searches them in other words (`phrasings`)
        "phrasings": [],
        "search_history": [],  # completed rewrite rounds, aligned with questions
        "added_this_round": 0,
        "sources": [],  # {"id": n, "url", "title", "question": index, "status": new|read|skipped|failed, "facts": [{claim, quote}]}
        "sections": [],  # {"question", "text"}
        "summary": "",
        "pages_read": 0,
        "told_at_pages": 0,
        "errors_in_a_row": 0,
    }


@dataclass
class Unit:
    """What one unit of work did: the new state, what to tell the person (if anything), and the report when the job is finished.

    `urgent` is for what the person is waiting to hear (the questions, right after they asked): it is sent at any hour. Progress lines are not urgent and are
    held back at night (jobs/runner.py).
    """

    state: dict[str, Any]
    say: str | None = None
    report: str | None = None
    urgent: bool = False


def normalized_url(url: str) -> str:
    parsed = urlparse(url.strip())
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower().removeprefix("www."), parsed.path.rstrip("/"), "", parsed.query, ""))


def progress_text(state: dict[str, Any]) -> str:
    """Where the job is, in words, for "how far are you?"."""
    sources = state.get("sources") or []
    read = sum(1 for source in sources if source["status"] == "read")
    stage = state.get("stage")
    total = len(state.get("questions") or [])
    if stage == "plan":
        if state.get("chapter_titles"):
            return f"I am planning the questions to research: {len(state.get('chapters') or [])} of {len(state['chapter_titles'])} chapters planned."
        return "I am planning the questions to research."
    if stage == "search":
        return f"Searching: {state.get('queries_done', 0)} of {total} questions searched, {len(sources)} pages found so far."
    if stage == "read":
        left = sum(1 for source in sources if source["status"] == "new")
        return f"Reading: {read} of {len(sources)} pages read ({left} to go)."
    if stage == "write":
        return f"Writing: {len(state.get('sections') or [])} of {total} sections written, from {read} pages."
    if stage == "chapters":
        done = sum(1 for chapter in state.get("chapters") or [] if chapter.get("overview") is not None)
        return f"Writing the chapter overviews: {done} of {len(state.get('chapters') or [])} done."
    return "Finishing the report."


# --- one unit ------------------------------------------------------------------------------------------------------------------------------------


async def advance(goal: str, state: dict[str, Any], io: ResearchIO) -> Unit:
    """Do the next unit of work and return the state after it. The caller saves it; an exception leaves the saved state as it was."""
    state = {
        **state, "sources": [dict(source) for source in state["sources"]], "sections": list(state["sections"]),
        "chapters": [dict(chapter) for chapter in state.get("chapters") or []], "chapter_titles": list(state.get("chapter_titles") or []),
    }
    stage = state["stage"]
    if stage == "plan":
        return await _plan(goal, state, io)
    if stage == "search":
        return await _search(state, io)
    if stage == "read":
        return await _read(goal, state, io)
    if stage == "write":
        return await _write(state, io)
    if stage == "chapters":
        return await _chapter_overview(state, io)
    return await _finish(goal, state, io)


#: Seconds per page, all in: searching, reading it, taking notes, writing (measured on the 9B in a 150-page job: 101 pages read in about 14 minutes, plus the rest).
SECONDS_PER_PAGE = 11


def time_estimate(pages: int) -> str:
    """How long a job of this many pages takes, as a range in words ("about 1.5 to 2.5 hours"), for the card the person approves."""
    minutes = pages * SECONDS_PER_PAGE / 60
    low, high = minutes * 0.8, minutes * 1.3
    if high < 90:
        return f"about {max(5, round(low / 5) * 5)} to {max(10, round(high / 5) * 5)} minutes"

    def hours(value: float) -> str:
        halves = round(value / 60 * 2) / 2
        return f"{halves:g}"

    return f"about {hours(low)} to {hours(high)} hours"


def chapter_count(max_pages: int) -> int:
    """How many chapters a job of this many pages is written in: 0 for one that is not (a hundred pages or fewer)."""
    return 0 if max_pages <= PAGES_PER_CHAPTER else min(MAX_CHAPTERS, -(-max_pages // PAGES_PER_CHAPTER))


async def _plan(goal: str, state: dict[str, Any], io: ResearchIO) -> Unit:
    wanted = chapter_count(state["max_pages"])
    if wanted:
        return await _plan_chapters(goal, state, io, wanted)
    answer = await io.think(PLAN_SYSTEM, f"Goal: {goal}", _Questions)
    questions = _usable_questions(answer, [])
    state["questions"], state["stage"] = questions, "search"
    listed = "\n".join(f"{n}. {question}" for n, question in enumerate(questions, 1))
    return Unit(state, say=f"I am researching this. These are the questions I will answer (up to {state['max_pages']} pages):\n{listed}\nI will tell you as I go.", urgent=True)


def _usable_questions(answer: Any, already: list[str]) -> list[str]:
    questions = [" ".join(str(item).split()) for item in getattr(answer, "questions", []) if str(item).strip()]
    questions = [question for question in dict.fromkeys(questions) if question not in already][:MAX_QUESTIONS]
    if len(questions) < MIN_QUESTIONS:
        raise ValueError(f"the model wrote {len(questions)} usable question(s); at least {MIN_QUESTIONS} are needed")
    return questions


async def _plan_chapters(goal: str, state: dict[str, Any], io: ResearchIO, wanted: int) -> Unit:
    """A big job is planned in small units too: the chapters first, then the questions of one chapter at a time."""
    if not state["chapter_titles"]:
        answer = await io.think(CHAPTERS_SYSTEM, f"Goal: {goal}\nWrite {wanted} chapters.", _Chapters)
        titles = list(dict.fromkeys(" ".join(str(item).split()) for item in getattr(answer, "chapters", []) if str(item).strip()))[:wanted]
        if len(titles) < 2:
            raise ValueError(f"the model wrote {len(titles)} usable chapter(s); at least 2 are needed")
        state["chapter_titles"] = titles
        return Unit(state)
    number = len(state["chapters"])
    title = state["chapter_titles"][number]
    answer = await io.think(
        PLAN_SYSTEM, f"Goal: {goal}\nThis is chapter {number + 1} of {len(state['chapter_titles'])}: {title}. Write the questions for this chapter only.", _Questions
    )
    questions = _usable_questions(answer, state["questions"])
    first = len(state["questions"])
    state["questions"] = [*state["questions"], *questions]
    state["chapters"].append({"title": title, "first": first, "end": len(state["questions"]), "overview": None})
    if len(state["chapters"]) < len(state["chapter_titles"]):
        return Unit(state)
    state["stage"] = "search"
    listed = "\n".join(f"{n}. {chapter['title']} ({chapter['end'] - chapter['first']} questions)" for n, chapter in enumerate(state["chapters"], 1))
    return Unit(
        state,
        say=f"I am researching this in {len(state['chapters'])} chapters, up to {state['max_pages']} pages in all:\n{listed}\nThis takes hours. I will tell you as I go, and you can ask how far I am, pause or stop me at any time.",
        urgent=True,
    )


async def _search(state: dict[str, Any], io: ResearchIO) -> Unit:
    index = state["queries_done"]
    questions = state["questions"]
    seen = {normalized_url(source["url"]) for source in state["sources"]}
    if index < len(questions) and len(state["sources"]) < state["max_pages"]:
        # The pages are shared out across the questions, so the last question is not left with what the first ones did not use.
        # Rounded up, so the limit is reached and not stopped just short of it; in a later round, shared out over what is still missing.
        missing = state["max_pages"] - len(state["sources"]) if state.get("round") else state["max_pages"]
        share = max(2, -(-missing // (len(questions) - index if state.get("round") else len(questions))))
        found = 0
        queries = state.get("phrasings") or questions
        for result in await io.search(queries[index], RESULTS_PER_QUERY):
            url = str(result.get("url") or "").strip()
            if not url.startswith(("http://", "https://")) or normalized_url(url) in seen or found >= share:
                continue
            seen.add(normalized_url(url))
            state["sources"].append({
                "id": len(state["sources"]) + 1, "url": url, "title": str(result.get("title") or url)[:160],
                "question": index, "status": "new", "facts": [],
            })
            found += 1
            if len(state["sources"]) >= state["max_pages"]:
                break
        state["queries_done"] = index + 1
        state["added_this_round"] = state.get("added_this_round", 0) + found
        return Unit(state)
    if not state["sources"]:
        raise ValueError("the searches found no pages")
    rounds = EXTRA_SEARCH_ROUNDS if state["max_pages"] > USUAL_MAX_PAGES else MAX_SEARCH_ROUNDS
    if len(state["sources"]) < state["max_pages"] and state.get("round", 0) + 1 < rounds and state.get("added_this_round", 0) > 0:
        more = await _more_searches(state, io)
        if more is not None:
            return more
    state["stage"] = "read"
    return Unit(state, say=f"I found {len(state['sources'])} pages to read. Reading them one at a time now.")


async def _more_searches(state: dict[str, Any], io: ResearchIO) -> Unit | None:
    """The pages found are fewer than the person asked for: write each question's search again in other words, once more round. None when the model cannot.

    More pages are a bonus, not a need, so a model that fails here leaves the job to read what it has instead of failing it.
    """
    questions = state["questions"]
    pending = list(state.get("pending_phrasings") or [])
    offset = len(pending)
    batch = questions[offset:offset + MAX_QUESTIONS]
    history = state.get("search_history") or []
    # Older saved jobs have only the most recent completed round.
    if not history and state.get("phrasings"):
        history = [state["phrasings"]]
    listed = "\n".join(
        f"{n}. {question}" + (
            f" (previous searches: {'; '.join(str(items[offset + n - 1]) for items in history if len(items) > offset + n - 1)})"
            if any(len(items) > offset + n - 1 for items in history) else ""
        )
        for n, question in enumerate(batch, 1)
    )
    try:
        answer = await io.think(PHRASE_SYSTEM, f"Questions:\n{listed}", _Phrasings)
    except Exception:  # noqa: BLE001 - see above
        state.pop("pending_phrasings", None)
        return None
    searches = [" ".join(str(item).split()) for item in getattr(answer, "searches", [])]
    pending.extend(searches[i] if i < len(searches) and searches[i] else question for i, question in enumerate(batch))
    if len(pending) < len(questions):
        state["pending_phrasings"] = pending
        return Unit(state)
    state.pop("pending_phrasings", None)
    if not any(query != original and all(index >= len(items) or query != items[index] for items in history)
               for index, (query, original) in enumerate(zip(pending, questions, strict=True))):
        return None
    state["phrasings"] = pending
    state["search_history"] = [*history, pending]
    state["round"], state["queries_done"], state["added_this_round"] = state.get("round", 0) + 1, 0, 0
    return Unit(state, say=f"I have {len(state['sources'])} pages so far and you asked for up to {state['max_pages']}. Searching again in different words.")


async def _read(goal: str, state: dict[str, Any], io: ResearchIO) -> Unit:
    source = next((item for item in state["sources"] if item["status"] == "new"), None)
    if source is None:
        state["stage"] = "write"
        read = sum(1 for item in state["sources"] if item["status"] == "read")
        return Unit(state, say=f"I have read {read} pages. Writing the report now.")
    page = await io.fetch(source["url"])
    if page is None or len(page.text.strip()) < 200:
        source["status"] = "failed"  # a page that cannot be read, or has nothing in it, is skipped: it is not an error of the job
        return Unit(state)
    text = page.text[:PAGE_CHARS]
    question = state["questions"][source["question"]]
    answer = await io.think(NOTES_SYSTEM, f"Goal: {goal}\nQuestion: {question}\n\nPage title: {page.title}\nPage text:\n{text}", _Notes)
    source["facts"] = verified_facts(getattr(answer, "facts", []), text)
    source["status"] = "read"
    state["pages_read"] += 1
    say = None
    if state["pages_read"] - state["told_at_pages"] >= max(PROGRESS_EVERY, state["max_pages"] // 10):
        state["told_at_pages"] = state["pages_read"]
        with_facts = sum(1 for item in state["sources"] if item["facts"])
        say = f"Read {state['pages_read']} of {len(state['sources'])} pages so far; {with_facts} of them had something that answers a question."
    return Unit(state, say=say)


def verified_facts(facts: list[Any], page_text: str) -> list[dict[str, str]]:
    """The facts whose quotation really is in the page. A quotation the page does not contain means the model made the fact up."""
    haystack = " ".join(page_text.split())
    kept = []
    for fact in facts[:MAX_FACTS_PER_PAGE]:
        claim, quote = " ".join(str(getattr(fact, "claim", "")).split()), " ".join(str(getattr(fact, "quote", "")).split())
        if claim and len(quote) >= 12 and quote in haystack:
            kept.append({"claim": claim, "quote": quote})
    return kept


async def _write(state: dict[str, Any], io: ResearchIO) -> Unit:
    index = len(state["sections"])
    if index >= len(state["questions"]):
        state["stage"] = "chapters" if state["chapters"] else "finish"
        return Unit(state)
    notes = numbered_notes(state, index)
    question = state["questions"][index]
    if not notes:
        text = "No page I could read answered this question."
    else:
        # A quotation can match the source while the model's paraphrased claim
        # changes its meaning. Give the writer the checked words, not that claim.
        listed = "\n".join(f"[{number}] {fact['quote']}  (from {source['title']})" for number, source, fact in notes)
        written = await io.write(SECTION_SYSTEM, f"Question: {question}\n\nNotes:\n{listed}")
        text = only_given_citations(written.strip(), {number for number, _s, _f in notes})
        text = await _checked(state, text, io)
        text = text or "No page I could read answered this question."  # every sentence failed its check: nothing the pages said supports a claim here
    state["sections"].append({"question": question, "text": text})
    return Unit(state, say=f"Writing: section {index + 1} of {len(state['questions'])} done." if (index + 1) % 3 == 0 else None)


_NUMBER = re.compile(r"(?<![\w.])\d[\d,. ]*\d|(?<![\w.])\d")


def _numbers(text: str) -> set[str]:
    """The numbers a sentence states, without citation marks and thousands separators: "4,000" and "4000" are the same number."""
    cleaned = _CITATION.sub(" ", text)
    return {re.sub(r"[,\s]", "", found).rstrip(".") for found in _NUMBER.findall(cleaned)} - {""}


def number_missing(sentence: str, quotes: list[str]) -> bool:
    """A number in the sentence that none of the cited quotations contain: the claim has a figure no source gave. Checked without a model."""
    stated = _numbers(sentence)
    if not stated:
        return False
    given = _numbers(" ".join(quotes))
    return bool(stated - given)


async def check_claims(text: str, quotes_by_number: dict[int, list[str]], io: ResearchIO) -> tuple[str, int, int, int]:
    """Keep the sentences whose cited quotations say what they say. (the text, sentences checked, sentences removed, sentences left unchecked).

    A quotation can really be in a page while the sentence built on it says something else: a used graphics card's price became "the cost of offloading" in the first
    live report (11/15). Citing is not support. Each sentence with a citation is checked against the quotations of the notes it cites: first, without a model, that every
    number it states is in them; then by the model, whether they say the same thing about the same subject. A sentence that fails is removed. When the model cannot check
    (it fails or times out), the sentence stays and is counted as unchecked, so the report can say so instead of passing it off as checked.
    """
    kept: list[str] = []
    checked = removed = unchecked = 0
    for sentence in _SENTENCE.split(" ".join(text.split())):
        numbers = [int(n) for n in _CITATION.findall(sentence)]
        if not numbers or _NOT_SAID.match(sentence):
            kept.append(sentence)
            continue
        quotes = [quote for number in dict.fromkeys(numbers) for quote in quotes_by_number.get(number, [])]
        if not quotes:
            removed += 1
            checked += 1
            continue
        checked += 1
        if number_missing(sentence, quotes):
            removed += 1
            continue
        listed = "\n".join(f"- {quote}" for quote in quotes[:6])
        try:
            answer = await io.think(CLAIM_SYSTEM, f"Sentence: {_CITATION.sub('', sentence).strip()}\n\nQuotations:\n{listed}", _Supported)
        except Exception:  # noqa: BLE001 - a check that cannot run is reported as not run, it does not decide
            checked -= 1
            unchecked += 1
            kept.append(sentence)
            continue
        if getattr(answer, "supported", False):
            kept.append(sentence)
        else:
            removed += 1
    return " ".join(kept), checked, removed, unchecked


def _quotes_by_number(state: dict[str, Any]) -> dict[int, list[str]]:
    return {int(source["id"]): [fact["quote"] for fact in source.get("facts") or []] for source in state["sources"]}


async def _checked(state: dict[str, Any], text: str, io: ResearchIO) -> str:
    text, checked, removed, unchecked = await check_claims(text, _quotes_by_number(state), io)
    state["claims_checked"] = int(state.get("claims_checked", 0)) + checked
    state["claims_removed"] = int(state.get("claims_removed", 0)) + removed
    state["claims_unchecked"] = int(state.get("claims_unchecked", 0)) + unchecked
    return text


def numbered_notes(state: dict[str, Any], question_index: int) -> list[tuple[int, dict[str, Any], dict[str, str]]]:
    """The facts for one question, each with the number of its source: what a section may be written from."""
    notes = []
    for source in state["sources"]:
        if source["question"] != question_index:
            continue
        for fact in source["facts"]:
            notes.append((source["id"], source, fact))
    return notes[:MAX_FACTS_PER_SECTION]


#: A sentence that says what the sources do not say needs no citation: it claims nothing.
_NOT_SAID = re.compile(r"(?i)^(?:the\s+)?(?:provided\s+|available\s+)?(?:notes|sources|pages|reports?)\s+(?:do|does|did)\s+(?:not|n't)\b|^no\s+(?:page|source|note)s?\b|^i\s+could\s+not\b")
#: A sentence ends at . ! ? and a space, unless the period closes an abbreviation: "Law No. 5403 is required [1]." is one sentence (live 11/14: it was cut after "No." and
#: the half that held the citation was kept as "5403 is required [1].").
_ABBREVIATIONS = ("No", "Nos", "Art", "Arts", "Sec", "Secs", "Dr", "Mr", "Mrs", "Ms", "St", "vs", "approx", "est", "Inc", "Ltd", "Co", "Corp", "etc", "Fig", "Vol", "pp", "p")
_SENTENCE = re.compile("".join(rf"(?<!\b{word}\.)" for word in _ABBREVIATIONS) + r"(?<=[.!?])\s+(?=[A-Z0-9$\"'(])")


def only_given_citations(text: str, allowed: set[int]) -> str:
    """Require known citation numbers; this does not establish semantic support. A citation to a number that was not given is removed, and a sentence
    that cites nothing is removed, because a claim with no note behind it is the model speaking from memory ("the only suitable choice",
    "owners report significant savings": neither was in any note of the first live report). A sentence that says what the sources do not say stays."""
    kept = []
    for sentence in _SENTENCE.split(" ".join(text.split())):
        cleaned = re.sub(r"\[(\d+)\]", lambda match: match.group(0) if int(match.group(1)) in allowed else "", sentence).strip()
        if _CITATION.search(cleaned) or _NOT_SAID.match(cleaned):
            kept.append(re.sub(r"\s+([.,;])", r"\1", cleaned))
    return " ".join(kept)


def _answered(sections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [section for section in sections if not section["text"].startswith("No page I could read")]


async def _chapter_overview(state: dict[str, Any], io: ResearchIO) -> Unit:
    """One chapter's overview from its sections (at most ten, so a small model reads them in one go). A chapter no page answered has none."""
    index = next((n for n, chapter in enumerate(state["chapters"]) if chapter.get("overview") is None), None)
    if index is None:
        state["stage"] = "finish"
        return Unit(state)
    chapter = state["chapters"][index]
    answered = _answered(state["sections"][chapter["first"]:chapter["end"]])
    overview = ""
    if answered:
        body = "\n\n".join(f"{section['question']}\n{section['text']}" for section in answered)
        used = {int(n) for section in answered for n in _CITATION.findall(section["text"])}
        overview = only_given_citations((await io.write(CHAPTER_SYSTEM, f"Chapter: {chapter['title']}\n\nSections:\n{body}")).strip(), used)
        overview = await _checked(state, overview, io)
    chapter["overview"] = overview
    return Unit(state, say=f"Chapter {index + 1} of {len(state['chapters'])} written: {chapter['title']}.")


async def _finish(goal: str, state: dict[str, Any], io: ResearchIO) -> Unit:
    answered = _answered(state["sections"])
    if answered and state["chapters"]:
        overviews = [chapter for chapter in state["chapters"] if chapter.get("overview")]
        body = "\n\n".join(f"{chapter['title']}\n{chapter['overview']}" for chapter in overviews)
        used = {int(n) for chapter in overviews for n in _CITATION.findall(chapter["overview"])}
        summary = only_given_citations((await io.write(SUMMARY_SYSTEM, f"Goal: {goal}\n\nChapters:\n{body}")).strip(), used) if overviews else ""
        summary = await _checked(state, summary, io) if summary else summary
        summary = summary or "I could not write a summary from the chapters, but each chapter below has what the sources said."
    elif answered:
        body = "\n\n".join(f"{section['question']}\n{section['text']}" for section in answered)
        used = {int(n) for section in answered for n in _CITATION.findall(section["text"])}
        summary = only_given_citations((await io.write(SUMMARY_SYSTEM, f"Goal: {goal}\n\nSections:\n{body}")).strip(), used)
        summary = await _checked(state, summary, io)
    else:
        summary = "I could not find pages that answer this. The sources I read did not say anything I could check."
    state["summary"], state["stage"] = summary, "done"
    return Unit(state, say=None, report=render_report(goal, state))


def render_report(goal: str, state: dict[str, Any]) -> str:
    """The report as Markdown: the answer, a section per question, and the list of sources, each with its number."""
    cited = {int(n) for section in state["sections"] for n in _CITATION.findall(section["text"])} | {
        int(n) for n in _CITATION.findall(state.get("summary", ""))
    }
    lines = [f"# {goal}", "", "## Summary", "", state.get("summary") or "(none)", ""]
    chapters = state.get("chapters") or []
    if chapters:
        for number, chapter in enumerate(chapters, 1):
            lines += [f"## {number}. {chapter['title']}", ""]
            if chapter.get("overview"):
                lines += [chapter["overview"], ""]
            for section in state["sections"][chapter["first"]:chapter["end"]]:
                lines += [f"### {section['question']}", "", section["text"], ""]
    else:
        for section in state["sections"]:
            lines += [f"## {section['question']}", "", section["text"], ""]
    lines += ["## Sources", ""]
    for source in state["sources"]:
        if source["id"] in cited:
            lines.append(f"[{source['id']}] {source['title']}: {source['url']}")
    read = sum(1 for source in state["sources"] if source["status"] == "read")
    attempted = sum(1 for source in state["sources"] if source["status"] in ("read", "failed"))
    useful = sum(1 for source in state["sources"] if source["status"] == "read" and source.get("facts"))
    lines += ["", f"_{len(state['sources'])} pages found; {attempted} attempted; {read} read; {useful} with recorded quotations; {len(cited)} cited._"]
    checked = int(state.get("claims_checked", 0))
    if checked or state.get("claims_unchecked"):
        lines += [
            f"_Every cited sentence was checked against the quotations it cites: {checked} checked, {int(state.get('claims_removed', 0))} removed because the quotations did not say it"
            + (f", {int(state['claims_unchecked'])} could not be checked and are kept" if state.get("claims_unchecked") else "") + ". A check by a small model is not a proof: read the sources behind a claim you rely on._"
        ]
    return "\n".join(lines)
