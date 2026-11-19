"""Tell me when: a job that looks at one thing every so often, remembers what it saw last time, and speaks only when something changed.

"Tell me whenever a new file shows up in this folder" and "tell me if that page changes" are not tasks: a task that runs on a schedule and answers each time reports "nothing new" a
hundred times. A watch is a job (jobs/model.py) that is mostly asleep. Each time it wakes it looks once, compares with what it kept, and says something only when there is a change,
once for each change. What it kept is in its saved state, so a restart loses nothing and does not repeat a notice. A look that fails is not "nothing changed": after three failures in a row
it says it cannot look (once), and when looking works again it says so (once).

    folder   the names, sizes and modification times of the files in one folder (not its subfolders): new, changed and removed files
    page     the visible text of a web page: whether it changed, and a few of the changed lines

The first look only records what is there; nothing is announced for what was already there. The runner (jobs/runner.py) does not wake a sleeping watch before `sleep_until`.
"""

from __future__ import annotations

import difflib
import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any

from agent_control.jobs.research import ResearchIO, Unit

MIN_EVERY_MINUTES, MAX_EVERY_MINUTES, DEFAULT_EVERY_MINUTES = 1, 1440, 15
FAILURES_BEFORE_SAYING = 3
MAX_NAMES_SHOWN = 6
KEPT_PAGE_CHARS = 6_000


def new_state(target: str = "", every_minutes: int = DEFAULT_EVERY_MINUTES) -> dict[str, Any]:
    kind = "page" if target.lower().startswith(("http://", "https://")) else "folder"
    return {
        "kind": "watch", "target": target, "target_kind": kind, "every_minutes": max(MIN_EVERY_MINUTES, min(int(every_minutes), MAX_EVERY_MINUTES)),
        "baseline": False, "seen": {}, "page_text": "", "failures": 0, "said_failing": False, "changes_told": 0, "looks": 0, "sleep_until": "", "noun": "watch",
        "errors_in_a_row": 0, "stage": "watch",
    }


def progress_text(state: dict[str, Any]) -> str:
    what = state.get("target", "")
    if not state.get("baseline"):
        return f"I am about to look at {what} for the first time."
    return f"Watching {what} every {state.get('every_minutes')} minutes: looked {state.get('looks', 0)} times, told you about {state.get('changes_told', 0)} change(s)."


def _now(io: ResearchIO) -> datetime:
    return datetime.now(timezone.utc)


def asleep(state: dict[str, Any], now: datetime | None = None) -> bool:
    """Whether the watch is waiting for its next look: the runner leaves it alone until then."""
    until = state.get("sleep_until")
    if not until:
        return False
    try:
        return datetime.fromisoformat(str(until)) > (now or datetime.now(timezone.utc))
    except ValueError:
        return False


async def advance(goal: str, state: dict[str, Any], io: ResearchIO) -> Unit:
    state = {**state, "seen": dict(state.get("seen") or {})}
    target, kind = state["target"], state["target_kind"]
    state["looks"] = int(state.get("looks", 0)) + 1
    state["sleep_until"] = (datetime.now(timezone.utc) + timedelta(minutes=int(state["every_minutes"]))).isoformat()
    try:
        say = await (_look_at_folder(state, io) if kind == "folder" else _look_at_page(state, io))
    except Exception as exc:  # noqa: BLE001 - a look that fails is counted and said, never taken for "nothing changed"
        state["failures"] = int(state.get("failures", 0)) + 1
        if state["failures"] >= FAILURES_BEFORE_SAYING and not state.get("said_failing"):
            state["said_failing"] = True
            return Unit(state, say=f"I cannot look at {target} right now ({str(exc)[:160]}). I will keep trying and tell you when it works again.", urgent=True)
        return Unit(state)
    recovered = bool(state.get("said_failing"))
    state["failures"], state["said_failing"] = 0, False
    if say:
        state["changes_told"] = int(state.get("changes_told", 0)) + 1
    if recovered:
        say = f"I can look at {target} again." + (f" {say}" if say else "")
    return Unit(state, say=say or None, urgent=True)  # the person asked to be told: a night does not hold a change back


async def _look_at_folder(state: dict[str, Any], io: ResearchIO) -> str | None:
    if io.list_folder is None:
        raise RuntimeError("folders cannot be watched here")
    now = {name: f"{size}:{mtime}" for name, size, mtime in await io.list_folder(state["target"])}
    before, first = state["seen"], not state["baseline"]
    state["seen"], state["baseline"] = now, True
    if first:
        return None
    added = sorted(set(now) - set(before))
    removed = sorted(set(before) - set(now))
    changed = sorted(name for name in set(now) & set(before) if now[name] != before[name])
    if not (added or removed or changed):
        return None
    parts = []
    for label, names in (("New", added), ("Changed", changed), ("Removed", removed)):
        if names:
            shown = ", ".join(names[:MAX_NAMES_SHOWN]) + (f" and {len(names) - MAX_NAMES_SHOWN} more" if len(names) > MAX_NAMES_SHOWN else "")
            parts.append(f"{label}: {shown}.")
    return f"Something changed in {state['target']}. " + " ".join(parts)


async def _look_at_page(state: dict[str, Any], io: ResearchIO) -> str | None:
    page = await io.fetch(state["target"])
    if page is None or len(page.text.strip()) < 50:
        raise RuntimeError("the page did not open or had no text")
    text = page.text[:KEPT_PAGE_CHARS]
    digest = hashlib.sha256(" ".join(text.split()).encode("utf-8")).hexdigest()
    before, first = state["seen"].get("digest"), not state["baseline"]
    old_text = state.get("page_text", "")
    state["seen"], state["page_text"], state["baseline"] = {"digest": digest}, text, True
    if first or digest == before:
        return None
    new_lines = [line.strip() for line in difflib.unified_diff(old_text.splitlines(), text.splitlines(), lineterm="", n=0) if line.startswith("+") and not line.startswith("+++")]
    sample = " | ".join(line[1:].strip()[:140] for line in new_lines[:3])
    return f"The page {state['target']} changed." + (f" What is new: {sample}" if sample else "")
