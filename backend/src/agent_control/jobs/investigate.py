"""Working out why something does not work, as a job: look first, keep what was seen, test the likely causes one at a time, then offer ways forward that rest on it.

"My TV will not take a cast" and "the printer stopped" are not requests with an answer to look up. A technician reads the machine and the network, forms a short list of
likely causes, runs the one check that would tell them apart, reads what it says, and only then reaches for a search engine or a fix. The assistant had none of that: it opened
browser pages it had guessed and ended the task when a method failed (11/16). This is the loop, in small saved units like the research job so that it survives a restart,
can be paused, and keeps the model to one small question at a time (jobs/research.py has the same shape and the same `Unit`):

    plan      the checks to run first (from the menu in diagnose.py) and a short list of likely causes
    baseline  one check per unit, each result kept
    analyze   what the results show (findings, each naming the check that showed it), which causes are supported or ruled out, and the one more check that would settle an open one
    probe     that check
    research  a search for what is known about the causes still open, read page by page with every note tied to its quotation
    options   the ways forward, each resting on a finding or a note (an option nothing supports is dropped), each saying whether it changes anything and how to undo it
    finish    the report, and what the person is told

Nothing here changes the computer: the checks only read, and a way forward that changes something is an option the person chooses, which then runs as a task through the usual
approvals. A cause is "supported" only when a result shows it; what the results do not show is reported as not known.
"""

from __future__ import annotations

import re
from typing import Annotated, Any

from pydantic import BaseModel, Field

from agent_control.diagnose import PROBE_HELP, PROBES
from agent_control.jobs.research import NOTES_SYSTEM, PAGE_CHARS, ResearchIO, Unit, _Notes, verified_facts

MAX_ROUNDS = 5  # analysis rounds, each of which may ask for one more check
MIN_BASELINE, MAX_BASELINE = 2, 6
MAX_HYPOTHESES = 6
MAX_FINDINGS = 12
MAX_QUERIES = 2
PAGES_PER_QUERY = 3
MAX_OPTIONS = 4
SUMMARY_CHARS = 1_400  # of one check's result that the model reads


# Each field below has a size for the grammar the model writes under (docs/DESIGN.md, D6): an open field is where a small model writes on to the token cap.
_Name = Annotated[str, Field(json_schema_extra={"maxLength": 40})]
_Sentence = Annotated[str, Field(json_schema_extra={"maxLength": 300})]


class _Plan(BaseModel):
    probes: list[_Name] = Field(default_factory=list, description="the checks to run first, in order, by name", json_schema_extra={"maxItems": 12})
    hypotheses: list[_Sentence] = Field(default_factory=list, description="3 to 6 likely causes, one sentence each", json_schema_extra={"maxItems": MAX_HYPOTHESES})


class _Finding(BaseModel):
    text: str = Field(description="one thing the results show, with the numbers or names in them", json_schema_extra={"maxLength": 300})
    probe: str = Field(description="the name of the check whose result shows it", json_schema_extra={"maxLength": 40})


class _Hypothesis(BaseModel):
    id: str = Field(description="H1, H2... as listed", json_schema_extra={"maxLength": 8})
    status: str = Field(description="supported, refuted or open", json_schema_extra={"maxLength": 12})


class _Next(BaseModel):
    op: str = Field(default="", description="the one more check that would settle an open cause, by name; empty for none", json_schema_extra={"maxLength": 40})
    host: str = Field(default="", description="for host_check: the address, as it appears in the results", json_schema_extra={"maxLength": 120})
    word: str = Field(default="", description="for event_log: a word the messages must mention", json_schema_extra={"maxLength": 60})


class _Analysis(BaseModel):
    findings: list[_Finding] = Field(default_factory=list, json_schema_extra={"maxItems": MAX_FINDINGS})
    hypotheses: list[_Hypothesis] = Field(default_factory=list, json_schema_extra={"maxItems": MAX_HYPOTHESES})
    next: _Next = Field(default_factory=_Next)
    enough: bool = Field(default=False, description="true when the results explain the problem, or no listed check would add anything")


class _Queries(BaseModel):
    searches: list[Annotated[str, Field(json_schema_extra={"maxLength": 160})]] = Field(
        default_factory=list, description="up to 2 web searches for what is known about the causes still open", json_schema_extra={"maxItems": MAX_QUERIES}
    )


class _Option(BaseModel):
    title: str = Field(description="what to do, in a few words", json_schema_extra={"maxLength": 120})
    why: str = Field(description="why it should help, in one sentence", json_schema_extra={"maxLength": 300})
    how: str = Field(description="the steps, in plain words", json_schema_extra={"maxLength": 1000})
    changes_something: bool = Field(default=False, description="true when it changes a setting, installs something or moves files")
    undo: str = Field(default="", description="how to undo it, when it changes something", json_schema_extra={"maxLength": 300})
    evidence: list[Annotated[str, Field(json_schema_extra={"maxLength": 8})]] = Field(
        default_factory=list, description="the ids of the findings (F1...) and notes (W1...) it rests on", json_schema_extra={"maxItems": 8}
    )


class _Options(BaseModel):
    options: list[_Option] = Field(default_factory=list, json_schema_extra={"maxItems": MAX_OPTIONS})


def _menu() -> str:
    return "\n".join(f"- {name}: {PROBE_HELP[name]}" for name in PROBES)


PLAN_SYSTEM = (
    "You work out why something on a person's computer does not work. You are given the problem and the checks you may run; each check reads this computer or the home "
    "network and changes nothing. Choose the checks that would show the most, between {low} and {high}, in the order to run them, by their names. Then write 3 to 6 likely "
    "causes, one sentence each, the ones a technician would look at first.\n\nThe checks:\n{menu}"
)
ANALYZE_SYSTEM = (
    "You read the results of checks run on a person's computer to find out why their problem happens. Write findings: things the results show, each with the name of the "
    "check whose result shows it. Never write a finding the results do not show; a guess is not a finding. Mark each likely cause supported (a result shows it), refuted (a "
    "result shows it is not the cause) or open. If one more check would settle an open cause, name it; for host_check give an address that appears in the results. Set enough "
    "to true when the results explain the problem or no listed check would add anything. If the problem is about one particular device or program, write a finding that says "
    "whether it appears in the results (by its name, brand or model) and which devices or programs do appear; when it does not appear, say so plainly.\n\nThe checks:\n{menu}"
)
QUERY_SYSTEM = (
    "You write web searches for what is known about the possible causes of a computer problem that the checks could not settle. Write at most 2, each specific (the product, "
    "the symptom, the error words), in different words from each other."
)
OPTIONS_SYSTEM = (
    "You suggest ways forward for a person's computer problem, from what was found. Each option has a title, one sentence on why it should help, the steps in plain words, "
    "whether it changes anything (a setting, an install, files), and how to undo it if so. Every option must list the ids of the findings (F1...) or notes (W1...) it rests on; "
    "an option nothing supports is not written. At most 4, the most likely to work first. Do not suggest what was already tried or ruled out."
)


# --- state -------------------------------------------------------------------------------------------------------------------------------------


def new_state(max_pages: int = 0) -> dict[str, Any]:
    return {
        "kind": "investigate", "stage": "plan", "topic": "", "queue": [], "observations": [], "findings": [], "hypotheses": [], "rounds": 0,
        "queries": [], "queries_done": 0, "web_notes": [], "options": [], "summary": "", "errors_in_a_row": 0, "noun": "investigation",
    }


def progress_text(state: dict[str, Any]) -> str:
    stage = state.get("stage")
    seen = len(state.get("observations") or [])
    if stage == "plan":
        return "I am deciding which checks to run."
    if stage in ("baseline", "probe"):
        return f"Checking this computer and the network: {seen} checks done, {len(state.get('queue') or [])} to go."
    if stage == "analyze":
        return f"Reading what the {seen} checks show."
    if stage == "research":
        return f"Looking up what is known: {state.get('queries_done', 0)} of {len(state.get('queries') or [])} searches done."
    if stage == "options":
        return "Working out the ways forward."
    return "Finishing the report."


async def advance(goal: str, state: dict[str, Any], io: ResearchIO) -> Unit:
    state = {
        **state, "queue": list(state.get("queue") or []), "observations": [dict(item) for item in state.get("observations") or []], "findings": list(state.get("findings") or []),
        "hypotheses": [dict(item) for item in state.get("hypotheses") or []], "queries": list(state.get("queries") or []), "web_notes": list(state.get("web_notes") or []),
        "options": list(state.get("options") or []),
    }
    if io.probe is None:
        raise RuntimeError("this computer's checks are not available here")
    stage = state["stage"]
    if stage == "plan":
        return await _plan(goal, state, io)
    if stage in ("baseline", "probe"):
        return await _run_probe(state, io)
    if stage == "analyze":
        return await _analyze(goal, state, io)
    if stage == "research":
        return await _research(goal, state, io)
    if stage == "options":
        return await _options(goal, state, io)
    return _finish(goal, state)


# --- units -------------------------------------------------------------------------------------------------------------------------------------


async def _plan(goal: str, state: dict[str, Any], io: ResearchIO) -> Unit:
    system = PLAN_SYSTEM.format(low=MIN_BASELINE, high=MAX_BASELINE, menu=_menu())
    answer = await io.think(system, f"Problem: {goal}", _Plan)
    chosen = [name.strip() for name in getattr(answer, "probes", []) if name.strip() in PROBES]
    chosen = list(dict.fromkeys(chosen))[:MAX_BASELINE]
    if len(chosen) < MIN_BASELINE:
        # A model that names too few checks still gets the ones every problem of this kind starts with, from the menu's own order.
        chosen = list(dict.fromkeys([*chosen, *PROBES]))[:MIN_BASELINE + 1]
    hypotheses = [" ".join(str(item).split()) for item in getattr(answer, "hypotheses", []) if str(item).strip()][:MAX_HYPOTHESES]
    state["queue"] = [{"op": name, "args": {}} for name in chosen]
    state["hypotheses"] = [{"id": f"H{number}", "text": text, "status": "open"} for number, text in enumerate(hypotheses, 1)]
    state["stage"] = "baseline"
    return Unit(state, say=f"I will look into this properly: first I check {', '.join(item['op'].replace('_', ' ') for item in state['queue'])}, and I will tell you what I find.", urgent=True)


async def _run_probe(state: dict[str, Any], io: ResearchIO) -> Unit:
    if not state["queue"]:
        state["stage"] = "analyze"
        return Unit(state)
    item = state["queue"].pop(0)
    result = await io.probe(item["op"], dict(item.get("args") or {}))
    state["observations"].append({"op": item["op"], "args": dict(item.get("args") or {}), "ok": bool(result.ok), "summary": str(result.summary)[:SUMMARY_CHARS]})
    if not state["queue"]:
        state["stage"] = "analyze"
    return Unit(state)


def _seen_addresses(state: dict[str, Any]) -> set[str]:
    return set(re.findall(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", " ".join(item["summary"] for item in state["observations"])))


def _observed_text(state: dict[str, Any]) -> str:
    return "\n\n".join(f"[{item['op']}{' ' + str(item['args']) if item['args'] else ''}]{'' if item['ok'] else ' (did not run)'}\n{item['summary']}" for item in state["observations"][-8:])


async def _analyze(goal: str, state: dict[str, Any], io: ResearchIO) -> Unit:
    listed = "\n".join(f"{item['id']}: {item['text']} [{item['status']}]" for item in state["hypotheses"]) or "(none yet)"
    known = "\n".join(f"{finding['id']}: {finding['text']}" for finding in state["findings"]) or "(none yet)"
    prompt = f"Problem: {goal}\n\nLikely causes:\n{listed}\n\nFindings so far:\n{known}\n\nResults of the checks:\n{_observed_text(state)}"
    answer = await io.think(ANALYZE_SYSTEM.format(menu=_menu()), prompt, _Analysis)
    ran = {item["op"] for item in state["observations"]}
    for finding in getattr(answer, "findings", []):
        text, probe = " ".join(str(finding.text).split()), str(finding.probe).strip()
        if text and probe in ran and text not in [item["text"] for item in state["findings"]] and len(state["findings"]) < MAX_FINDINGS:
            state["findings"].append({"id": f"F{len(state['findings']) + 1}", "text": text, "probe": probe})
    by_id = {item["id"]: item for item in state["hypotheses"]}
    for update in getattr(answer, "hypotheses", []):
        target = by_id.get(str(update.id).strip().upper())
        if target is not None and str(update.status).strip().lower() in ("supported", "refuted", "open"):
            target["status"] = str(update.status).strip().lower()
    state["rounds"] += 1
    wanted = getattr(answer, "next", None)
    op = str(getattr(wanted, "op", "") or "").strip()
    args: dict[str, Any] = {}
    if op == "host_check":
        host = str(getattr(wanted, "host", "") or "").strip()
        op = op if host in _seen_addresses(state) else ""  # a check may only be pointed at an address the results showed: a model's invented address is not scanned
        args = {"host": host}
    elif op == "event_log":
        args = {"word": str(getattr(wanted, "word", "") or "").strip()[:40]}
    already = any(item["op"] == op and item["args"] == args for item in state["observations"])
    if op in PROBES and not already and state["rounds"] < MAX_ROUNDS and not getattr(answer, "enough", False):
        state["queue"].append({"op": op, "args": args})
        state["stage"] = "probe"
        return Unit(state)
    open_causes = [item for item in state["hypotheses"] if item["status"] == "open"]
    if open_causes or not state["findings"]:
        state["stage"] = "research"
        return Unit(state, say=f"The checks show {len(state['findings'])} thing(s). I am looking up what is known about the rest.")
    state["stage"] = "options"
    return Unit(state)


async def _research(goal: str, state: dict[str, Any], io: ResearchIO) -> Unit:
    if not state["queries"] and not state["queries_done"]:
        findings = "\n".join(f"- {finding['text']}" for finding in state["findings"]) or "(nothing settled)"
        open_causes = "\n".join(f"- {item['text']}" for item in state["hypotheses"] if item["status"] == "open") or "(none)"
        answer = await io.think(QUERY_SYSTEM, f"Problem: {goal}\n\nWhat the checks showed:\n{findings}\n\nCauses still open:\n{open_causes}", _Queries)
        state["queries"] = [" ".join(str(item).split()) for item in getattr(answer, "searches", []) if str(item).strip()][:MAX_QUERIES]
        if not state["queries"]:
            state["stage"] = "options"
        return Unit(state)
    if state["queries_done"] >= len(state["queries"]):
        state["stage"] = "options"
        return Unit(state)
    query = state["queries"][state["queries_done"]]
    state["queries_done"] += 1
    try:
        results = await io.search(query, PAGES_PER_QUERY + 2)
    except Exception:  # noqa: BLE001 - what the web says is a help to the options, not a need: the checks stand without it
        return Unit(state)
    seen = {note["url"] for note in state["web_notes"]}
    for result in results:
        url = str(result.get("url") or "")
        if not url.startswith(("http://", "https://")) or url in seen or sum(1 for note in state["web_notes"] if note["query"] == query) >= PAGES_PER_QUERY * 2:
            continue
        page = await io.fetch(url)
        if page is None or len(page.text.strip()) < 200:
            continue
        seen.add(url)
        text = page.text[:PAGE_CHARS]
        answer = await io.think(NOTES_SYSTEM, f"Goal: {goal}\nQuestion: {query}\n\nPage title: {page.title}\nPage text:\n{text}", _Notes)
        for fact in verified_facts(getattr(answer, "facts", []), text)[:2]:
            state["web_notes"].append({"id": f"W{len(state['web_notes']) + 1}", "claim": fact["claim"], "quote": fact["quote"], "url": url, "title": page.title[:120], "query": query})
        if sum(1 for note in state["web_notes"] if note["query"] == query) >= PAGES_PER_QUERY:
            break
    return Unit(state)


async def _options(goal: str, state: dict[str, Any], io: ResearchIO) -> Unit:
    findings = "\n".join(f"{finding['id']}: {finding['text']} (from {finding['probe']})" for finding in state["findings"]) or "(the checks settled nothing)"
    notes = "\n".join(f"{note['id']}: {note['claim']} ({note['title']})" for note in state["web_notes"]) or "(nothing found on the web)"
    refuted = "\n".join(f"- {item['text']}" for item in state["hypotheses"] if item["status"] == "refuted") or "(none)"
    prompt = f"Problem: {goal}\n\nFindings:\n{findings}\n\nWhat the web says:\n{notes}\n\nRuled out:\n{refuted}"
    answer = await io.think(OPTIONS_SYSTEM, prompt, _Options)
    known = {finding["id"] for finding in state["findings"]} | {note["id"] for note in state["web_notes"]}
    options = []
    for option in getattr(answer, "options", []):
        cited = [ident.strip().upper() for ident in option.evidence if ident.strip().upper() in known]
        if not any(ident.startswith("F") for ident in cited):
            cited += _supporting_findings(option, state["findings"])  # the model left the evidence out or named nothing real (it does, now and then): code finds what in the findings the option is about
        # A way forward must rest on something found on this computer (an F). What the web says about someone else's machine supports a finding; it does not stand in for one:
        # on the first live run an option about a firewall port came only from forum notes about a different problem, and no check of this computer had looked at the firewall.
        if any(ident.startswith("F") for ident in cited) and option.title.strip() and option.how.strip():
            options.append({
                "title": " ".join(option.title.split())[:120], "why": " ".join(option.why.split())[:300], "how": " ".join(option.how.split())[:600],
                "changes_something": bool(option.changes_something), "undo": " ".join(option.undo.split())[:300], "evidence": cited,
            })
    state["options"] = options[:MAX_OPTIONS]
    state["stage"] = "finish"
    return Unit(state)


# --- the end -----------------------------------------------------------------------------------------------------------------------------------


_WORD = re.compile(r"[a-z0-9]{4,}")
_PLAIN = frozenset({"that", "this", "with", "from", "have", "will", "your", "when", "then", "into", "them", "they", "been", "does", "more", "than", "which", "should", "must", "also", "only", "open", "click"})


def _supporting_findings(option: _Option, findings: list[dict[str, Any]]) -> list[str]:
    """The findings an option is about, by the words they share (two or more that are not everyday words): the ids to cite when the model did not."""
    wanted = set(_WORD.findall(f"{option.title} {option.why} {option.how}".lower())) - _PLAIN
    scored = []
    for finding in findings:
        shared = wanted & (set(_WORD.findall(finding["text"].lower())) - _PLAIN)
        if len(shared) >= 2:
            scored.append((len(shared), finding["id"]))
    return [ident for _count, ident in sorted(scored, reverse=True)[:2]]


def _finish(goal: str, state: dict[str, Any]) -> Unit:
    state["summary"], state["stage"] = render_summary(state), "done"
    return Unit(state, report=render_report(goal, state), urgent=True)


def render_summary(state: dict[str, Any]) -> str:
    """What the person is told: what was found (with the check that showed it), what could not be told, and the ways forward."""
    lines = []
    if state["findings"]:
        lines.append("What I found:")
        lines += [f"- {finding['text']}" for finding in state["findings"]]
    else:
        lines.append("The checks did not settle anything.")
    open_causes = [item["text"].rstrip(". ") for item in state["hypotheses"] if item["status"] == "open"]
    if open_causes:
        lines += ["", "No check settled these, so I do not know whether they are true:"] + [f"- {cause}" for cause in open_causes[:3]]
    if state["options"]:
        lines += ["", "Ways forward:"]
        for number, option in enumerate(state["options"], 1):
            change = " It changes something on this computer, so I will ask you first." + (f" To undo it: {option['undo']}" if option["undo"] else "") if option["changes_something"] else ""
            lines.append(f"{number}. {option['title']}: {option['why']}{change}")
        lines += ["", "Tell me which one to try (or 'you choose'), and I will do it."]
    else:
        lines += ["", "I have no way forward that the evidence supports yet. Tell me more about the problem, or what you have already tried, and I will look again."]
    return "\n".join(lines)


def render_report(goal: str, state: dict[str, Any]) -> str:
    lines = [f"# {goal}", "", "## Summary", "", state["summary"], "", "## What each check showed", ""]
    for item in state["observations"]:
        lines += [f"### {item['op'].replace('_', ' ')}" + (f" {item['args']}" if item["args"] else ""), "", item["summary"], ""]
    if state["web_notes"]:
        lines += ["## What the web says", ""]
        lines += [f"- [{note['id']}] {note['claim']} ({note['title']}: {note['url']})" for note in state["web_notes"]]
    return "\n".join(lines)


def goal_evidence(state: dict[str, Any]) -> list[str]:
    """What the goal keeps from this job: each finding, and each way forward, as one short line the next attempt is shown."""
    lines = [f"Found: {finding['text']}" for finding in state.get("findings") or []]
    lines += [f"Option {number}: {option['title']} - {option['how']}" for number, option in enumerate(state.get("options") or [], 1)]
    return lines
