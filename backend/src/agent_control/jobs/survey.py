"""A survey: every one of something in a place, checked against criteria, with a source for every cell, and a stop that is measured.

"Find all the daycares around me and compare them" and "find the best pastel de nata within 40 miles, take your time" are not questions with an answer to look up. They ask
for a table. The job builds it the way a careful person would, in small saved units like the research job (so it survives a restart, can be paused, and keeps the model to
one small question at a time):

    plan      what a row is, the place, the criteria (columns), and web searches that start from official lists and registries, then directories, then maps and reviews
    search    one search per unit; the pages found are queued
    harvest   one page per unit: the rows on it, with each cell's value and the exact words that say it (a cell whose words are not on the page is dropped)
    judge     when the queue is empty: how many rows, how many were new in the last rounds, how many cells are filled. Then the next move:
              broaden (more searches, in different words), deepen (visit each row with a gap and look for it), or stop
    deepen    one row with a gap per unit: a search for that row and the missing cell, then the page it names
    rate      one row per unit: a score from 0 to 10 for how well it meets the criteria, from its cells alone
    finish    the report: the table with a link for every cell, the ranking, what could not be found

"Good enough" is measured, not felt. The survey stops when the rows found in the last three rounds are under 2 percent of all rows, and the cells are filled to 95 percent or
every empty cell has been looked for several times, or when it reaches its ceiling (rounds and hours). What it could not find is said as not found, never filled in.
`advance` does exactly one unit; the model, the search and the page fetch come in as `ResearchIO`.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

from pydantic import BaseModel, Field

from agent_control.jobs.research import PAGE_CHARS, ResearchIO, Unit, normalized_url

RESULTS_PER_QUERY = 8
MAX_PHRASINGS_PER_ROUND = 8
MAX_ROUNDS = 40  # a backstop: the stop measure below and the hours decide, as the person is told
STOP_NEW_RATE = 0.02  # rows found in the last three rounds, as a share of all rows (and under STOP_NEW_FLOOR rows at the least: with 23 rows 2% is under one row, so a single stray find would keep the search going)
STOP_NEW_FLOOR = 2
STOP_FILLED = 0.95
STEADY_ROUNDS = 3
MAX_TRIES_PER_CELL = 3
MAX_ROWS_RATED = 40
MAX_ROWS_PER_PAGE = 25
MAX_CRITERIA = 8
DEFAULT_CEILING_HOURS = 72
SUMMARY_ROWS = 5


class _Plan(BaseModel):
    entity: str = Field(description="what one row is, as a short singular noun (daycare, bakery that serves pastel de nata, pool builder)")
    area: str = Field(default="", description="the place, as the person said or as it can be taken from what they said (a city, a county, 'within 40 miles of Pearland, Texas')")
    criteria: list[str] = Field(default_factory=list, description="3 to 8 things to find out about each one, short (monthly cost, license status, hours, reviews)")
    searches: list[str] = Field(default_factory=list, description="web searches: the first two look for an official list or registry, then directories, then maps and reviews")


class _Cell(BaseModel):
    criterion: str = Field(description="one of the criteria, as listed")
    value: str = Field(description="what the page says, short")
    quote: str = Field(description="the exact words from the page that say it, copied character for character")


class _Row(BaseModel):
    name: str = Field(description="the name of the one")
    address: str = Field(default="", description="its street address or town, as the page gives it")
    url: str = Field(default="", description="its own web address if the page gives one")
    cells: list[_Cell] = Field(default_factory=list)


class _Rows(BaseModel):
    rows: list[_Row] = Field(default_factory=list, description="every one on the page that is in or near the place; empty when the page has none")


class _More(BaseModel):
    searches: list[str] = Field(default_factory=list, description="new web searches, in different words from those already made")


class _Rating(BaseModel):
    score: int = Field(description="0 to 10: how well it meets the criteria, judged from the cells given")
    reason: str = Field(description="one short sentence")


class _Summary(BaseModel):
    text: str = Field(description="three to five sentences: what was found, which are the best and why, what could not be found")


PLAN_SYSTEM = (
    "You plan a survey: finding every one of something in a place and comparing them. From the person's request write: what one row is (a short singular noun), the place, "
    f"3 to {MAX_CRITERIA} criteria to find out about each one (what the person said matters, then what a careful person would check), and web searches. The first two "
    "searches look for an official list or registry (a licensing or inspection database, a city or state list); the rest look for directories, maps and review sites, in "
    "different words. Between 5 and 8 searches."
)
ROWS_SYSTEM = (
    "You read one web page for a survey. List every {entity} the page names that is in or near {area}. For each give its name, its address or town, its own web address if the "
    "page gives one, and for each of these criteria that the page says something about, the value and the exact words from the page that say it, copied character for character: "
    "{criteria}. Never write a value the page does not say. If the page lists none, return an empty list."
)
DEEPEN_SYSTEM = (
    "You read one web page for a survey, looking for specific facts about one {entity}: {name}{address}. For each of these criteria the page says something about for that one, "
    "give the value and the exact words from the page that say it, copied character for character: {missing}. Never write a value the page does not say about that one."
)
MORE_SYSTEM = (
    "You write web searches for a survey that has not found everything yet. Given what it is looking for and the searches already made, write up to {n} new ones in different "
    "words: other towns and neighbourhoods around the place, other kinds of source (county or state lists, association members, maps, review sites), and the names of the "
    "kinds of place in other words."
)
RATE_SYSTEM = (
    "You rate one {entity} for a survey, from the cells given only. 10 means it meets the person's criteria very well and the cells say so; 0 means it fails them. "
    "A criterion with no cell is unknown: it neither raises nor lowers the score much. Give the score and one sentence."
)
SUMMARY_SYSTEM = (
    "You write the summary of a finished survey in three to five plain sentences: how many were found and how sure that is, which are the best and why (from the cells), and "
    "what could not be found. Never state a fact that is not in the rows given."
)


# --- state -------------------------------------------------------------------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def new_state(max_pages: int = 0) -> dict[str, Any]:
    return {
        "kind": "survey", "stage": "plan", "goal": "", "entity": "", "area": "", "criteria": [], "phrasings": [], "queries_done": 0, "round": 0,
        "pages": [], "rows": [], "round_new": [], "new_this_round": 0, "rows_at_round_start": 0, "rated": 0, "deepen_queue": [], "summary": "",
        "started_at": _now().isoformat(), "ceiling_hours": DEFAULT_CEILING_HOURS, "stop_reason": "", "errors_in_a_row": 0, "noun": "survey",
    }


def filled_share(state: dict[str, Any]) -> float:
    rows, criteria = state.get("rows") or [], state.get("criteria") or []
    if not rows or not criteria:
        return 0.0
    cells = sum(1 for row in rows for criterion in criteria if criterion in (row.get("cells") or {}))
    return cells / (len(rows) * len(criteria))


def steady(state: dict[str, Any]) -> bool:
    """New rows in the last three rounds are a tiny share of all rows: searching more is no longer finding anything."""
    history = state.get("round_new") or []
    total = len(state.get("rows") or [])
    if len(history) < STEADY_ROUNDS or not total:
        return False
    return sum(history[-STEADY_ROUNDS:]) < max(STOP_NEW_FLOOR, STOP_NEW_RATE * total)


def progress_text(state: dict[str, Any]) -> str:
    stage = state.get("stage")
    rows = len(state.get("rows") or [])
    entity = state.get("entity") or "one"
    if stage == "plan":
        return "I am working out what to look for and where."
    if stage in ("search", "harvest"):
        pages = state.get("pages") or []
        read = sum(1 for page in pages if page["status"] != "new")
        return f"Looking for every {entity}: {state.get('queries_done', 0)} of {len(state.get('phrasings') or [])} searches done, {read} of {len(pages)} pages read, {rows} found so far."
    if stage == "deepen":
        return f"Checking the {rows} found against the criteria: {len(state.get('deepen_queue') or [])} left to look up."
    if stage == "rate":
        return f"Rating the {min(rows, MAX_ROWS_RATED)} best-documented: {state.get('rated', 0)} done."
    return "Writing the report."


def estimate_text(state: dict[str, Any] | None = None) -> str:
    return (
        "I will keep going until three rounds of new searches in a row find almost nothing new and the table is nearly full, or three days have passed. "
        "Probably several hours. I will tell you how it is going now and then and send a summary each morning."
    )


# --- one unit ------------------------------------------------------------------------------------------------------------------------------------


def _copy(state: dict[str, Any]) -> dict[str, Any]:
    return {
        **state, "criteria": list(state.get("criteria") or []), "phrasings": list(state.get("phrasings") or []), "pages": [dict(p) for p in state.get("pages") or []],
        "rows": [{**r, "cells": dict(r.get("cells") or {}), "sources": list(r.get("sources") or [])} for r in state.get("rows") or []],
        "round_new": list(state.get("round_new") or []), "deepen_queue": list(state.get("deepen_queue") or []),
    }


def _over_ceiling(state: dict[str, Any]) -> bool:
    try:
        started = datetime.fromisoformat(str(state.get("started_at")))
    except (TypeError, ValueError):
        return False
    return _now() - started > timedelta(hours=float(state.get("ceiling_hours") or DEFAULT_CEILING_HOURS))


async def advance(goal: str, state: dict[str, Any], io: ResearchIO) -> Unit:
    state = _copy(state)
    state["goal"] = state.get("goal") or goal
    stage = state["stage"]
    if stage == "plan":
        return await _plan(goal, state, io)
    if stage == "search":
        return await _search(state, io)
    if stage == "harvest":
        return await _harvest(state, io)
    if stage == "judge":
        return await _judge(state, io)
    if stage == "deepen":
        return await _deepen(state, io)
    if stage == "rate":
        return await _rate(state, io)
    return await _finish(state, io)


async def _plan(goal: str, state: dict[str, Any], io: ResearchIO) -> Unit:
    plan = await io.think(PLAN_SYSTEM, f"The person's request:\n{goal}", _Plan)
    if not isinstance(plan, _Plan) or not plan.searches:
        raise ValueError("the survey could not be planned")
    state["entity"] = " ".join(plan.entity.split())[:80] or "one"
    state["area"] = " ".join(plan.area.split())[:120]
    state["criteria"] = [" ".join(c.split())[:60] for c in plan.criteria if c.strip()][:MAX_CRITERIA] or ["address", "hours", "cost", "reviews"]
    state["phrasings"] = [" ".join(s.split())[:160] for s in plan.searches if s.strip()][:MAX_PHRASINGS_PER_ROUND]
    state["stage"] = "search"
    return Unit(
        state, urgent=True,
        say=(
            f"This is a survey: I will find every {state['entity']}{' in ' + state['area'] if state['area'] else ''} and compare them on {', '.join(state['criteria'])}. "
            f"{estimate_text(state)}"
        ),
    )


async def _search(state: dict[str, Any], io: ResearchIO) -> Unit:
    index = state["queries_done"]
    phrasings = state["phrasings"]
    if index >= len(phrasings):
        state["stage"] = "harvest"
        return Unit(state)
    seen = {normalized_url(page["url"]) for page in state["pages"]}
    for result in await io.search(phrasings[index], RESULTS_PER_QUERY):
        url = str(result.get("url") or "").strip()
        if url.startswith(("http://", "https://")) and normalized_url(url) not in seen:
            seen.add(normalized_url(url))
            state["pages"].append({"url": url, "title": str(result.get("title") or url)[:160], "status": "new", "query": index})
    state["queries_done"] = index + 1
    if state["queries_done"] >= len(phrasings):
        state["stage"] = "harvest"
    return Unit(state)


def _key(row: dict[str, Any] | _Row) -> str:
    name = row.get("name") if isinstance(row, dict) else row.name
    address = row.get("address") if isinstance(row, dict) else row.address
    words = re.sub(r"[^a-z0-9 ]+", " ", str(name or "").lower())
    words = re.sub(r"\b(inc|llc|ltd|the|of|and)\b", " ", words)
    street = re.sub(r"[^a-z0-9 ]+", " ", str(address or "").lower()).split()[:2]
    return " ".join(words.split()) + "|" + " ".join(street)


def merge_rows(state: dict[str, Any], rows: list[_Row], page: dict[str, Any], page_text: str) -> int:
    """Add the rows of one page to the table; a cell is kept only when its quotation is in the page. The number of rows that were new."""
    haystack = " ".join(page_text.split())
    by_key = {_key(row): row for row in state["rows"]}
    by_name = {" ".join(re.sub(r"[^a-z0-9 ]+", " ", str(row["name"]).lower()).split()): row for row in state["rows"]}
    new = 0
    for item in rows[:MAX_ROWS_PER_PAGE]:
        name = " ".join(item.name.split())[:120]
        if not name:
            continue
        key = _key(item)
        existing = by_key.get(key) or by_name.get(" ".join(re.sub(r"[^a-z0-9 ]+", " ", name.lower()).split()))
        if existing is None:
            existing = {"id": len(state["rows"]) + 1, "name": name, "address": " ".join(item.address.split())[:160], "url": item.url[:300], "cells": {}, "sources": [], "tries": {}, "score": None, "reason": ""}
            state["rows"].append(existing)
            by_key[key] = existing
            new += 1
        elif not existing.get("address") and item.address:
            existing["address"] = " ".join(item.address.split())[:160]
        if page["url"] not in existing["sources"]:
            existing["sources"].append(page["url"])
        for cell in item.cells:
            criterion = cell.criterion if cell.criterion in state["criteria"] else next((c for c in state["criteria"] if c.lower() == cell.criterion.lower()), None)
            quote = " ".join(cell.quote.split())
            if criterion and criterion not in existing["cells"] and len(quote) >= 8 and quote in haystack and cell.value.strip():
                existing["cells"][criterion] = {"value": " ".join(cell.value.split())[:200], "quote": quote[:300], "url": page["url"]}
    return new


async def _harvest(state: dict[str, Any], io: ResearchIO) -> Unit:
    page = next((p for p in state["pages"] if p["status"] == "new"), None)
    if page is None:
        state["stage"] = "judge"
        return Unit(state)
    fetched = await io.fetch(page["url"])
    if fetched is None or not fetched.text.strip():
        page["status"] = "failed"
        return Unit(state)
    text = fetched.text[:PAGE_CHARS]
    system = ROWS_SYSTEM.format(entity=state["entity"], area=state["area"] or "the place", criteria="; ".join(state["criteria"]))
    answer = await io.think(system, f"Page title: {fetched.title}\n\n{text}", _Rows)
    new = merge_rows(state, answer.rows if isinstance(answer, _Rows) else [], page, text)
    page["status"] = "read"
    state["new_this_round"] += new
    return Unit(state)


async def _judge(state: dict[str, Any], io: ResearchIO) -> Unit:
    """The round is over: record how many rows it added, then decide the next move."""
    state["round_new"].append(state["new_this_round"])
    state["new_this_round"] = 0
    state["round"] += 1
    rows = state["rows"]
    if _over_ceiling(state) or state["round"] >= MAX_ROUNDS:
        state["stop_reason"] = "the time or round limit was reached"
        return _to_deepen_or_rate(state)
    if not rows and state["round"] >= 2:
        state["stop_reason"] = "nothing was found"
        state["stage"] = "finish"
        return Unit(state)
    if steady(state):
        state["stop_reason"] = "three rounds of new searches found almost nothing new"
        return _to_deepen_or_rate(state)
    more = await io.think(MORE_SYSTEM.format(n=MAX_PHRASINGS_PER_ROUND), _more_prompt(state), _More)
    fresh = [" ".join(s.split())[:160] for s in (more.searches if isinstance(more, _More) else []) if s.strip() and s not in state["phrasings"]][:MAX_PHRASINGS_PER_ROUND]
    if not fresh:
        state["stop_reason"] = "no new way to search was left"
        return _to_deepen_or_rate(state)
    state["phrasings"].extend(fresh)
    state["stage"] = "search"
    return Unit(state, say=f"Round {state['round']} done: {len(rows)} {state['entity']} found so far ({state['round_new'][-1]} new in that round). Searching again in different words.")


def _more_prompt(state: dict[str, Any]) -> str:
    return (
        f"Looking for: every {state['entity']} in {state['area'] or 'the place'}. Goal: {state['goal']}\nFound so far: {len(state['rows'])}.\nSearches already made:\n"
        + "\n".join(f"- {p}" for p in state["phrasings"][-30:])
    )


def _to_deepen_or_rate(state: dict[str, Any]) -> Unit:
    """Fill the gaps first (rows are visited for what is missing), then rate."""
    queue = []
    for row in state["rows"]:
        missing = [c for c in state["criteria"] if c not in row["cells"] and row["tries"].get(c, 0) < MAX_TRIES_PER_CELL]
        if missing:
            queue.append(row["id"])
    state["deepen_queue"] = queue
    state["stage"] = "deepen" if queue and filled_share(state) < STOP_FILLED else "rate"
    return Unit(state, say=f"The search is done ({state['stop_reason']}): {len(state['rows'])} {state['entity']} found. Now checking each one against the criteria.")


async def _deepen(state: dict[str, Any], io: ResearchIO) -> Unit:
    if not state["deepen_queue"] or _over_ceiling(state):
        state["stage"] = "rate"
        return Unit(state)
    row_id = state["deepen_queue"].pop(0)
    row = next((r for r in state["rows"] if r["id"] == row_id), None)
    if row is None:
        return Unit(state)
    missing = [c for c in state["criteria"] if c not in row["cells"] and row["tries"].get(c, 0) < MAX_TRIES_PER_CELL]
    if not missing:
        return Unit(state)
    for criterion in missing:
        row["tries"][criterion] = row["tries"].get(criterion, 0) + 1
    tried = row.setdefault("tried_urls", [])
    url = next((u for u in (row.get("url") or "",) if u.startswith(("http://", "https://")) and normalized_url(u) not in tried), "")
    if not url:
        results = await io.search(f"{row['name']} {row['address'] or state['area']} {' '.join(missing[:3])}", 6)
        url = next((str(r.get("url") or "") for r in results if str(r.get("url") or "").startswith(("http://", "https://")) and normalized_url(str(r.get("url"))) not in tried), "")
    if url:
        tried.append(normalized_url(url))
        fetched = await io.fetch(url)
        if fetched is not None and fetched.text.strip():
            text = fetched.text[:PAGE_CHARS]
            system = DEEPEN_SYSTEM.format(entity=state["entity"], name=row["name"], address=f" ({row['address']})" if row["address"] else "", missing="; ".join(missing))
            answer = await io.think(system, f"Page title: {fetched.title}\n\n{text}", _Rows)
            haystack = " ".join(text.split())
            for item in (answer.rows if isinstance(answer, _Rows) else [])[:3]:
                for cell in item.cells:
                    criterion = next((c for c in missing if c.lower() == cell.criterion.lower()), None)
                    quote = " ".join(cell.quote.split())
                    if criterion and criterion not in row["cells"] and len(quote) >= 8 and quote in haystack and cell.value.strip():
                        row["cells"][criterion] = {"value": " ".join(cell.value.split())[:200], "quote": quote[:300], "url": url}
                if url not in row["sources"]:
                    row["sources"].append(url)
                break
    elif missing:
        for criterion in missing:
            row["tries"][criterion] = MAX_TRIES_PER_CELL  # no page left to try for this one
    if any(c not in row["cells"] and row["tries"].get(c, 0) < MAX_TRIES_PER_CELL for c in state["criteria"]):
        state["deepen_queue"].append(row_id)  # still missing something: another page, after the others have had their first
    if not state["deepen_queue"] or filled_share(state) >= STOP_FILLED:
        state["stage"] = "rate"
    return Unit(state)


def _best_documented(state: dict[str, Any]) -> list[dict[str, Any]]:
    rows = sorted(state["rows"], key=lambda r: (-len(r["cells"]), r["id"]))
    return rows[:MAX_ROWS_RATED]


async def _rate(state: dict[str, Any], io: ResearchIO) -> Unit:
    candidates = _best_documented(state)
    row = next((r for r in candidates if r.get("score") is None), None)
    if row is None:
        state["stage"] = "finish"
        return Unit(state)
    cells = "\n".join(f"- {c}: {v['value']}" for c, v in row["cells"].items()) or "(nothing found)"
    answer = await io.think(RATE_SYSTEM.format(entity=state["entity"]), f"What the person wants: {state['goal']}\n\n{row['name']} ({row['address']})\n{cells}", _Rating)
    if isinstance(answer, _Rating):
        row["score"] = max(0, min(10, int(answer.score)))
        row["reason"] = " ".join(answer.reason.split())[:200]
    else:
        row["score"] = 0
    state["rated"] += 1
    return Unit(state)


def _cell_text(row: dict[str, Any], criterion: str) -> str:
    cell = row["cells"].get(criterion)
    if not cell:
        return "not found"
    return f"{cell['value']} ([source]({cell['url']}))"


def _table(state: dict[str, Any], rows: list[dict[str, Any]]) -> str:
    header = ["#", state["entity"].capitalize(), "Address", *state["criteria"], "Score"]
    lines = ["| " + " | ".join(header) + " |", "| " + " | ".join("---" for _ in header) + " |"]
    for number, row in enumerate(rows, 1):
        score = "" if row.get("score") is None else str(row["score"])
        link = f"[{row['name']}]({row['sources'][0]})" if row["sources"] else row["name"]
        cells = [str(number), link, row["address"] or "not found", *(_cell_text(row, c).replace("|", "/") for c in state["criteria"]), score]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


async def _finish(state: dict[str, Any], io: ResearchIO) -> Unit:
    rows = state["rows"]
    ranked = sorted(rows, key=lambda r: (-(r["score"] if r["score"] is not None else -1), -len(r["cells"]), r["id"]))
    top = ranked[:SUMMARY_ROWS]
    summary = ""
    if rows:
        listing = "\n".join(f"{r['name']} ({r['address']}), score {r['score']}: " + "; ".join(f"{c}: {v['value']}" for c, v in r["cells"].items()) for r in top)
        answer = await io.think(SUMMARY_SYSTEM, f"Looking for: every {state['entity']} in {state['area']}. {len(rows)} found. Stopped because: {state['stop_reason']}.\nCells filled: {filled_share(state):.0%}.\nThe best:\n{listing}", _Summary)
        summary = " ".join(answer.text.split()) if isinstance(answer, _Summary) else ""
    state["summary"] = summary or f"{len(rows)} {state['entity']} were found."
    gaps = sum(1 for r in rows for c in state["criteria"] if c not in r["cells"])
    lines = [
        f"# Survey: {state['goal']}", "", "## Summary", "", state["summary"], "",
        f"_{len(rows)} found; stopped because {state['stop_reason'] or 'it finished'}; {filled_share(state):.0%} of the cells filled, {gaps} not found. Every value links to the page it came from._", "",
        "## The best", "", _table(state, ranked[:SUMMARY_ROWS * 2]), "", "## Everyone found", "", _table(state, ranked), "",
    ]
    state["stage"] = "done"
    return Unit(state, report="\n".join(lines))
