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
PAGE_CHARS = 6_000  # the part of a page the model reads: about 1,500 tokens, so one unit is seconds, not minutes
MAX_FACTS_PER_PAGE = 4
MAX_FACTS_PER_SECTION = 14
DEFAULT_MAX_PAGES = 60
HARD_MAX_PAGES = 150
PROGRESS_EVERY = 10  # pages read between two messages to the person


class _Questions(BaseModel):
    questions: list[str] = Field(default_factory=list, description="the sub-questions that, answered, answer the goal; each a full question")


class _Fact(BaseModel):
    claim: str = Field(description="one fact from the page, in your own short words")
    quote: str = Field(description="the exact words from the page that say it, copied, not reworded")


class _Notes(BaseModel):
    relevant: bool = Field(default=False, description="true only if the page says something that answers the question")
    facts: list[_Fact] = Field(default_factory=list)


PLAN_SYSTEM = (
    "You plan research. Given a goal, write the sub-questions whose answers, put together, answer it: concrete, one thing each, in the order "
    "someone would need them (what the options are, what each costs, what it needs, what goes wrong, what experts or owners say). "
    f"Between {MIN_QUESTIONS} and {MAX_QUESTIONS} questions. Each is a complete question that a web search could answer."
)
NOTES_SYSTEM = (
    "You read one web page to help answer a question. Say whether the page answers it. If it does, list up to "
    f"{MAX_FACTS_PER_PAGE} facts it states. Each fact has a quote: the exact words from the page that say it, copied character for character. "
    "Never write a fact the page does not state. If the page is about something else, say it is not relevant and list no facts."
)
SECTION_SYSTEM = (
    "You write one section of a research report from numbered notes. Use only the notes: every sentence that states a fact ends with the numbers "
    "of the notes it comes from in square brackets, like [3] or [3][7]. Do not add anything from memory. If the notes disagree, say that they "
    "disagree and give both. If they do not answer the question, say so in one sentence. At most 150 words, plain prose, no headings."
)
SUMMARY_SYSTEM = (
    "You summarise a research report for a person who will read only this. From the sections, write the answer in at most 90 words, plain prose: "
    "the conclusion first, then the one or two things that matter most. Keep the citation numbers in square brackets where you use a fact. "
    "Say plainly which parts the sources did not answer."
)

_CITATION = re.compile(r"\[(\d+)\]")


# --- state -------------------------------------------------------------------------------------------------------------------------------------


def new_state(max_pages: int = DEFAULT_MAX_PAGES) -> dict[str, Any]:
    return {
        "stage": "plan",
        "max_pages": max(5, min(int(max_pages), HARD_MAX_PAGES)),
        "questions": [],
        "queries_done": 0,
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
        share = max(2, -(-state["max_pages"] // len(questions)))  # rounded up, so the limit is reached and not stopped just short of it
        found = 0
        for result in await io.search(questions[index], RESULTS_PER_QUERY):
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
        return Unit(state)
    if not state["sources"]:
        raise ValueError("the searches found no pages")
    state["stage"] = "read"
    return Unit(state, say=f"I found {len(state['sources'])} pages to read. Reading them one at a time now.")


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
    source["facts"] = verified_facts(getattr(answer, "facts", []), text) if getattr(answer, "relevant", False) else []
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


def only_given_citations(text: str, allowed: set[int]) -> str:
    """Take out a citation to a note number that was not given: a source the model made up."""
    return re.sub(r"\[(\d+)\]", lambda match: match.group(0) if int(match.group(1)) in allowed else "", text)


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
