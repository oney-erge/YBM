"""Deep research as a job: many sources read one at a time, every claim kept with the words that support it, a cited report at the end.

A 9B model on a laptop cannot read a hundred pages in one prompt and cannot hold a long plan in its head, but it can read one page well. So the
job never asks it to. The big picture lives in the job's saved state (the questions, the sources, the notes, the sections); a unit of work
shows the model one small thing and writes down what it said. Nothing is trusted that can be checked:

    * a note is a claim and a quotation, and the quotation must be in the page text or the claim is dropped (the model cannot invent a source);
    * a section is written from the numbered notes alone, and a citation to a number that was not given is removed;
    * a question no source answered is reported as unanswered, never filled in.

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


@dataclass(frozen=True)
class ResearchIO:
    think: Think
    write: Write
    search: Search
    fetch: Fetch


# --- the limits that keep one unit small enough for a small model -------------------------------------------------------------------------------

MIN_QUESTIONS, MAX_QUESTIONS = 3, 10
RESULTS_PER_QUERY = 8
#: One search per question finds about half of what a request for 100 pages needs (7 questions of 8 results, less the pages two questions share: 36 live on 11/14).
#: When the pages found are short of the limit the person set, the questions are searched again in different words, up to this many rounds in all.
MAX_SEARCH_ROUNDS = 3
PAGE_CHARS = 6_000  # the part of a page the model reads: about 1,500 tokens, so one unit is seconds, not minutes
MAX_FACTS_PER_PAGE = 4
MAX_FACTS_PER_SECTION = 14
DEFAULT_MAX_PAGES = 60
HARD_MAX_PAGES = 150
PROGRESS_EVERY = 10  # pages read between two messages to the person


class _Questions(BaseModel):
    questions: list[str] = Field(default_factory=list, description="the sub-questions that, answered, answer the goal; each a full question")


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
NOTES_SYSTEM = (
    "You read part of one web page to help answer a question. Copy out up to "
    f"{MAX_FACTS_PER_PAGE} facts from the page that help answer it: prices, ranges, numbers, named options, comparisons, what people report. Each fact "
    "has a quote: the exact words from the page that say it, copied character for character, a sentence or a phrase of at least eight words. If nothing "
    "on the page helps, return an empty list. Never write a fact the page does not state."
)
PHRASE_SYSTEM = (
    "You rewrite web searches so that other websites answer them. You are given research questions. For each one write a different search: other key words and "
    "another angle (a guide, a price list, an official page, owners' or customers' experiences, a forum), not the same words in another order. One search per "
    "question, in the same order."
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
        "queries_done": 0,
        "round": 0,  # 0 searches the questions as written; each later round searches them in other words (`phrasings`)
        "phrasings": [],
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
    """What one unit of work did: the new state, what to tell the person (if anything), and the report when the job is finished."""

    state: dict[str, Any]
    say: str | None = None
    report: str | None = None


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
        return "I am planning the questions to research."
    if stage == "search":
        return f"Searching: {state.get('queries_done', 0)} of {total} questions searched, {len(sources)} pages found so far."
    if stage == "read":
        left = sum(1 for source in sources if source["status"] == "new")
        return f"Reading: {read} of {len(sources)} pages read ({left} to go)."
    if stage == "write":
        return f"Writing: {len(state.get('sections') or [])} of {total} sections written, from {read} pages."
    return "Finishing the report."


# --- one unit ------------------------------------------------------------------------------------------------------------------------------------


async def advance(goal: str, state: dict[str, Any], io: ResearchIO) -> Unit:
    """Do the next unit of work and return the state after it. The caller saves it; an exception leaves the saved state as it was."""
    state = {**state, "sources": [dict(source) for source in state["sources"]], "sections": list(state["sections"])}
    stage = state["stage"]
    if stage == "plan":
        return await _plan(goal, state, io)
    if stage == "search":
        return await _search(state, io)
    if stage == "read":
        return await _read(goal, state, io)
    if stage == "write":
        return await _write(state, io)
    return await _finish(goal, state, io)


async def _plan(goal: str, state: dict[str, Any], io: ResearchIO) -> Unit:
    answer = await io.think(PLAN_SYSTEM, f"Goal: {goal}", _Questions)
    questions = [" ".join(str(item).split()) for item in getattr(answer, "questions", []) if str(item).strip()]
    questions = list(dict.fromkeys(questions))[:MAX_QUESTIONS]
    if len(questions) < MIN_QUESTIONS:
        raise ValueError(f"the model wrote {len(questions)} usable question(s); at least {MIN_QUESTIONS} are needed")
    state["questions"], state["stage"] = questions, "search"
    listed = "\n".join(f"{n}. {question}" for n, question in enumerate(questions, 1))
    return Unit(state, say=f"I am researching this. These are the questions I will answer (up to {state['max_pages']} pages):\n{listed}\nI will tell you as I go.")


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
    if len(state["sources"]) < state["max_pages"] and state.get("round", 0) + 1 < MAX_SEARCH_ROUNDS and state.get("added_this_round", 0) > 0:
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
    listed = "\n".join(f"{n}. {question}" for n, question in enumerate(questions, 1))
    try:
        answer = await io.think(PHRASE_SYSTEM, f"Questions:\n{listed}", _Phrasings)
    except Exception:  # noqa: BLE001 - see above
        return None
    searches = [" ".join(str(item).split()) for item in getattr(answer, "searches", [])]
    if not any(searches):
        return None
    state["phrasings"] = [searches[i] if i < len(searches) and searches[i] else questions[i] for i in range(len(questions))]
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
    if state["pages_read"] - state["told_at_pages"] >= PROGRESS_EVERY:
        state["told_at_pages"] = state["pages_read"]
        with_facts = sum(1 for item in state["sources"] if item["facts"])
        say = f"Read {state['pages_read']} of {len(state['sources'])} pages so far; {with_facts} of them had something that answers a question."
    return Unit(state, say=say)


def verified_facts(facts: list[Any], page_text: str) -> list[dict[str, str]]:
    """The facts whose quotation really is in the page. A quotation the page does not contain means the model made the fact up."""
    haystack = _squash(page_text)
    kept = []
    for fact in facts[:MAX_FACTS_PER_PAGE]:
        claim, quote = " ".join(str(getattr(fact, "claim", "")).split()), " ".join(str(getattr(fact, "quote", "")).split())
        if claim and len(quote) >= 12 and _squash(quote) in haystack:
            kept.append({"claim": claim, "quote": quote})
    return kept


def _squash(text: str) -> str:
    return re.sub(r"[\W_]+", " ", text.casefold()).strip()


async def _write(state: dict[str, Any], io: ResearchIO) -> Unit:
    index = len(state["sections"])
    if index >= len(state["questions"]):
        state["stage"] = "finish"
        return Unit(state)
    notes = numbered_notes(state, index)
    question = state["questions"][index]
    if not notes:
        text = "No page I could read answered this question."
    else:
        listed = "\n".join(f"[{number}] {fact['claim']}  (from {source['title']})" for number, source, fact in notes)
        written = await io.write(SECTION_SYSTEM, f"Question: {question}\n\nNotes:\n{listed}")
        text = only_given_citations(written.strip(), {number for number, _s, _f in notes})
    state["sections"].append({"question": question, "text": text})
    return Unit(state, say=f"Writing: section {index + 1} of {len(state['questions'])} done." if (index + 1) % 3 == 0 else None)


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
_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9$\"'(])")


def only_given_citations(text: str, allowed: set[int]) -> str:
    """Keep only what a note supports: a citation to a number that was not given is a source the model made up and is removed, and a sentence
    that cites nothing is removed, because a claim with no note behind it is the model speaking from memory ("the only suitable choice",
    "owners report significant savings": neither was in any note of the first live report). A sentence that says what the sources do not say stays."""
    kept = []
    for sentence in _SENTENCE.split(" ".join(text.split())):
        cleaned = re.sub(r"\[(\d+)\]", lambda match: match.group(0) if int(match.group(1)) in allowed else "", sentence).strip()
        if _CITATION.search(cleaned) or _NOT_SAID.match(cleaned):
            kept.append(re.sub(r"\s+([.,;])", r"\1", cleaned))
    return " ".join(kept)


async def _finish(goal: str, state: dict[str, Any], io: ResearchIO) -> Unit:
    answered = [section for section in state["sections"] if not section["text"].startswith("No page I could read")]
    if answered:
        body = "\n\n".join(f"{section['question']}\n{section['text']}" for section in answered)
        used = {int(n) for section in answered for n in _CITATION.findall(section["text"])}
        summary = only_given_citations((await io.write(SUMMARY_SYSTEM, f"Goal: {goal}\n\nSections:\n{body}")).strip(), used)
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
    for section in state["sections"]:
        lines += [f"## {section['question']}", "", section["text"], ""]
    lines += ["## Sources", ""]
    for source in state["sources"]:
        if source["id"] in cited:
            lines.append(f"[{source['id']}] {source['title']}: {source['url']}")
    read = sum(1 for source in state["sources"] if source["status"] == "read")
    lines += ["", f"_{read} pages read of {len(state['sources'])} found; {len(cited)} cited. Every claim above has a quotation from its page behind it in the job record._"]
    return "\n".join(lines)
