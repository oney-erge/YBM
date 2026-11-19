"""Projects the assistant remembers: a pool, a robot, a daycare search. Not open or closed: picked up again weeks later, with what was decided and why.

A project (the `projects` table, which the console already uses for folders and instructions) now also has *hooks*, the words and names that point to it, and a
*notebook* (`project_notes`): dated entries of decisions, facts, open questions, purchases, steps and results, written as they come up in any chat
(channels/memory_learning.py proposes them; the code here applies them). When a message may be about a project, the project's last entries go into the context of
the answer, so "back to the pool, where were we?" and "what did we decide about the filter?" are answered from the notebook.

Hooks only produce *candidates*. A word in a message never starts or continues a project by itself (docs/ROADMAP.md, "Not doing", records what keyword triggers cost this
repository once); the decision model is asked whether the message really is about the project, and only a yes puts the notebook in front of the model.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from agent_control.schemas import ProjectRecord, utc_now
from agent_control.storage.repositories import Repositories

NOTE_KINDS = ("decision", "fact", "question", "purchase", "step", "result")
MAX_HOOKS = 8
MAX_NOTES_SHOWN = 8
MAX_PROJECTS_SHOWN = 2
CONFIRM_AT = 0.5
_STOP = frozenset({
    "the", "and", "for", "with", "that", "this", "from", "have", "has", "was", "were", "are", "our", "your", "their", "about", "into", "some", "what", "when", "where",
    "which", "will", "would", "could", "should", "project", "back", "again", "going", "want", "need", "like", "just", "also", "then", "than", "them", "they", "been",
})


def words_of(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(w) > 2 and w not in _STOP}


def _stem(word: str) -> str:
    for suffix in ("ing", "es", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)]
    return word


def _stems(text: str) -> set[str]:
    return {_stem(w) for w in words_of(text)}


def find_project(repositories: Repositories, name: str, hooks: list[str] | None = None) -> ProjectRecord | None:
    """The project a name (and hooks) point to: the same name, or a project whose name and hooks share most of the words."""
    wanted = " ".join(re.findall(r"[a-z0-9]+", (name or "").lower()))
    mine = _stems(name) | {_stem(h.lower()) for h in hooks or [] if len(h) > 2}
    best: tuple[float, ProjectRecord] | None = None
    for project in repositories.projects.list_all():
        if " ".join(re.findall(r"[a-z0-9]+", project.name.lower())) == wanted:
            return project
        theirs = _stems(project.name) | {_stem(h.lower()) for h in project.hooks}
        if not mine or not theirs:
            continue
        share = len(mine & theirs) / min(len(mine), len(theirs))
        if share >= 0.67 and (best is None or share > best[0]):
            best = (share, project)
    return best[1] if best else None


def note_to_project(
    repositories: Repositories, name: str, hooks: list[str], kind: str, text: str, *, conversation_id: str | None = None, task_id: str | None = None,
) -> ProjectRecord | None:
    """Write one notebook entry, making the project when it is new. A project is made only with a name and an entry."""
    name = " ".join((name or "").split())[:80]
    text = " ".join((text or "").split())[:400]
    if not name or not text:
        return None
    kind = kind if kind in NOTE_KINDS else "fact"
    clean_hooks = [h for h in dict.fromkeys(" ".join(h.lower().split())[:30] for h in hooks if h and len(h.strip()) > 2)][:MAX_HOOKS]
    clean_hooks = clean_hooks or derive_hooks(name, text)  # the small model often gives none; the words of the name still point to the project
    project = find_project(repositories, name, clean_hooks)
    if project is None:
        project = repositories.projects.create(ProjectRecord(name=name, hooks=clean_hooks))
    else:
        merged = list(dict.fromkeys([*project.hooks, *clean_hooks]))[:MAX_HOOKS]
        if merged != project.hooks:
            repositories.projects.set_memory(project.id, hooks=merged)
    repositories.project_notes.add(project.id, kind, text, task_id=task_id)
    repositories.episodes.add("note", f"[{project.name}] {kind}: {text}", conversation_id=conversation_id, task_id=None, project_id=project.id)
    return project


def candidates(repositories: Repositories, text: str, *, limit: int = 3) -> list[tuple[int, ProjectRecord]]:
    """Projects whose name or hooks share words with the message, best first, with the number of words shared. Only candidates: nothing is decided here."""
    mine = _stems(text)
    found: list[tuple[int, ProjectRecord]] = []
    for project in repositories.projects.list_all():
        if project.status == "done":
            continue
        theirs = _stems(project.name) | {_stem(h.lower()) for h in project.hooks}
        shared = len(mine & theirs)
        if shared:
            found.append((shared, project))
    found.sort(key=lambda item: (-item[0], item[1].updated_at), reverse=False)
    return found[:limit]


async def confirmed(repositories: Repositories, text: str, decision: Any) -> list[ProjectRecord]:
    """The candidates the message is really about. With the decision model, it is asked; without it, a project is taken only when two of its words are in the message."""
    found = candidates(repositories, text)
    if not found:
        return []
    mode = getattr(decision, "mode", "off")
    if decision is None or mode not in {"cascade", "decision", "shadow"}:
        return _without_a_model(found, text)
    from agent_control.llm.decision import DecisionModelClient

    questions = {
        f"p{index}": f"Is the message about the project '{project.name}' ({', '.join(project.hooks[:4])}), something the person has been working on?"
        for index, (_, project) in enumerate(found)
    }
    try:
        answers = await DecisionModelClient(decision).yes_no(text, questions)
    except Exception:  # noqa: BLE001 - help, not a gate: without an answer the rule below applies
        return _without_a_model(found, text)
    return [project for index, (_, project) in enumerate(found) if answers.get(f"p{index}", 0.0) >= CONFIRM_AT][:MAX_PROJECTS_SHOWN]


def _without_a_model(found: list[tuple[int, ProjectRecord]], text: str) -> list[ProjectRecord]:
    """No model to ask: a project is taken when the message shares two of its words or a word of its own name. That is generous (a few lines of notes may be offered that were
    not needed), which is cheaper than a project the owner named being forgotten; with the decision model on, this is not used."""
    mine = _stems(text)
    return [project for shared, project in found if shared >= 2 or (_stems(project.name) & mine)][:MAX_PROJECTS_SHOWN]


def _ago(moment: datetime, now: datetime | None = None) -> str:
    now = now or utc_now()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    days = max(0, (now - moment).days)
    return "today" if days == 0 else "yesterday" if days == 1 else f"{days} days ago" if days < 14 else f"{days // 7} weeks ago"


def context_text(repositories: Repositories, projects: list[ProjectRecord]) -> str:
    """The block the answer is written with: for each project, when it was last touched and its latest notebook entries, oldest first."""
    blocks = []
    for project in projects[:MAX_PROJECTS_SHOWN]:
        notes = repositories.project_notes.list(project.id, limit=MAX_NOTES_SHOWN)
        if not notes:
            continue
        touched = _ago(max(note["at"] for note in notes))
        lines = "\n".join(f"  - {str(note['at'])[:10]} {note['kind']}: {note['text']}" for note in reversed(notes))
        blocks.append(f"- {project.name} (last touched {touched}); the notebook, latest entries:\n{lines}")
    if not blocks:
        return ""
    return (
        "\n\nProjects the person has been working on that this message may be about (from earlier chats; use them to say where things stand and what was decided, and "
        "offer to pick the project up again if it was left a while ago):\n" + "\n".join(blocks)
    )


async def project_context(repositories: Repositories, text: str, decision: Any = None) -> str:
    """The notebook of every project the message is about, or nothing."""
    try:
        return context_text(repositories, await confirmed(repositories, text, decision))
    except Exception:  # noqa: BLE001 - recall is help, never a reason to fail a message
        return ""


def project_context_sync(repositories: Repositories, text: str) -> str:
    """The same without a model, for the places that cannot wait on one (the chat reply's context)."""
    try:
        return context_text(repositories, _without_a_model(candidates(repositories, text), text))
    except Exception:  # noqa: BLE001
        return ""


# --- what the person can ask: which projects, and where each one stands -------------------------------------------------------------------

#: A project not touched for this long is parked: still held, with its decisions, and picked up again when it is mentioned. The person decides done and dropped.
PARK_AFTER = timedelta(days=14)
#: Where the readable copy of the memory is kept: the owner can open, move or back up these files; the database holds the same notes for search.
MEMORY_DIR = Path(".agent_control") / "memory"
_MAX_INDEX_LINES = 8
_KIND_TITLES = {
    "decision": "Decisions", "question": "Open questions", "fact": "Facts", "purchase": "Bought or to buy", "step": "Steps", "result": "Results",
}


def derive_hooks(name: str, text: str = "") -> list[str]:
    """Words that point to a project when the model gave none: the words of its name, then the longer words of what was said. Candidates only; a model confirms."""
    own = [w for w in re.findall(r"[a-z0-9]+", (name or "").lower()) if len(w) > 2 and w not in _STOP]
    said = [w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(w) > 4 and w not in _STOP]
    return list(dict.fromkeys([*own, *said]))[:6]


def last_touched(repositories: Repositories, project: ProjectRecord) -> datetime:
    notes = repositories.project_notes.list(project.id, limit=1)
    stamps = [project.updated_at, *([notes[0]["at"]] if notes else [])]
    return max(stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc) for stamp in stamps)


def standing(project: ProjectRecord, touched: datetime, now: datetime | None = None) -> str:
    """active, parked, done or dropped. The first two follow the calendar and are never stored; the last two are the person's word."""
    if project.status in ("done", "dropped"):
        return project.status
    return "active" if (now or utc_now()) - touched < PARK_AFTER else "parked"


def listing(repositories: Repositories, now: datetime | None = None) -> list[tuple[ProjectRecord, str, datetime]]:
    """Every project with where it stands, the most recently touched first."""
    now = now or utc_now()
    rows = [(project, last_touched(repositories, project)) for project in repositories.projects.list_all()]
    rows.sort(key=lambda row: row[1], reverse=True)
    return [(project, standing(project, touched, now), touched) for project, touched in rows]


def index_text(repositories: Repositories, now: datetime | None = None) -> str:
    """One line per active project and the names of the parked ones: what the model always sees, so "which projects are active?" and "back to the pool" need no guess."""
    try:
        rows = listing(repositories, now)
    except Exception:  # noqa: BLE001 - help, never a reason to fail a message
        return ""
    if not rows:
        return ""
    now = now or utc_now()
    lines = ["\n\n## Projects you are holding for this person (read one's notes with memory.manage project, by name)"]
    active = [row for row in rows if row[1] == "active"]
    for project, _, touched in active[:_MAX_INDEX_LINES]:
        lines.append(f"- {project.name}: active, last touched {_ago(touched, now)}")
    parked = [f"{project.name} ({_ago(touched, now)})" for project, state, touched in rows if state == "parked"]
    if parked:
        lines.append("- Parked: " + ", ".join(parked[:_MAX_INDEX_LINES]) + (f" and {len(parked) - _MAX_INDEX_LINES} more" if len(parked) > _MAX_INDEX_LINES else ""))
    finished = sum(1 for row in rows if row[1] in ("done", "dropped"))
    if finished:
        lines.append(f"- {finished} finished or dropped, not listed.")
    return "\n".join(lines)


def _slug(name: str) -> str:
    return "-".join(re.findall(r"[a-z0-9]+", name.lower()))[:60] or "project"


def render_project(repositories: Repositories, project: ProjectRecord, now: datetime | None = None) -> str:
    """The project's own page: where it stands, the words that point to it, and its notebook by kind, newest first. What the owner can read and what the assistant reads."""
    now = now or utc_now()
    touched = last_touched(repositories, project)
    notes = repositories.project_notes.list(project.id, limit=200)
    lines = [f"# {project.name}", "", f"Status: {standing(project, touched, now)} (last touched {_ago(touched, now)})."]
    if project.hooks:
        lines.append("Words that point to it: " + ", ".join(project.hooks) + ".")
    if project.instructions.strip():
        lines += ["", "## What it is", project.instructions.strip()]
    latest = {}
    for note in notes:
        latest.setdefault(note["kind"], note)
    where = [f"- {_KIND_TITLES[kind][:-1] if kind != 'fact' else 'Fact'}: {latest[kind]['text']}" for kind in ("decision", "question", "step") if kind in latest]
    if where:
        lines += ["", "## Where we are", *where]
    for kind in NOTE_KINDS:
        entries = [note for note in notes if note["kind"] == kind]
        if entries:
            lines += ["", f"## {_KIND_TITLES[kind]}", *[f"- {str(note['at'])[:10]}: {note['text']}" for note in entries]]
    if not notes:
        lines += ["", "Nothing has been written down about it yet."]
    return "\n".join(lines) + "\n"


def describe(repositories: Repositories, name: str) -> tuple[ProjectRecord, str] | None:
    """The project a name points to, and its page, or None."""
    project = find_project(repositories, name, derive_hooks(name))
    if project is None:
        found = candidates(repositories, name, limit=1)
        project = found[0][1] if found else None
    return None if project is None else (project, render_project(repositories, project))


def render_about_you(repositories: Repositories) -> str:
    """What is kept about the person, one line each with its date; what stopped being true is below, with the day it stopped."""
    facts = repositories.memory_facts.list_all(include_closed=True)
    lines = ["# About you", ""]
    current = [fact for fact in facts if fact.is_current]
    lines += [f"- {fact.content}" + (f" ({str(fact.valid_from)[:10]})" if getattr(fact, "valid_from", None) else "") for fact in current] or ["Nothing yet."]
    ended = [fact for fact in facts if not fact.is_current]
    if ended:
        lines += ["", "## No longer so", *[f"- {fact.content} (until {str(fact.valid_to)[:10]})" for fact in ended]]
    return "\n".join(lines) + "\n"


def write_files(repositories: Repositories, root: Path | None = None, now: datetime | None = None) -> list[Path]:
    """Write the readable copy: INDEX.md, about-you.md and one folder per project with its PROJECT.md. The notes stay in the database too; these files are regenerated, so
    an edit by hand is not kept. Never raises: a copy that could not be written is not worth a failed task."""
    root = root or MEMORY_DIR
    written: list[Path] = []
    try:
        rows = listing(repositories, now)
        index = ["# Projects", ""] + [f"- [{project.name}](projects/{_slug(project.name)}/PROJECT.md): {state}, last touched {_ago(touched, now)}" for project, state, touched in rows]
        targets = {
            root / "INDEX.md": "\n".join(index if rows else ["# Projects", "", "None yet."]) + "\n",
            root / "about-you.md": render_about_you(repositories),
        }
        for project, _, _ in rows:
            targets[root / "projects" / _slug(project.name) / "PROJECT.md"] = render_project(repositories, project, now)
        for path, text in targets.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8", newline="\n")
            written.append(path)
    except Exception:  # noqa: BLE001
        return written
    return written


def tidy(repositories: Repositories, root: Path | None = None, now: datetime | None = None) -> dict[str, int]:
    """The nightly chore: notes that say the same thing twice are kept once, and the readable copy is rewritten."""
    removed = repositories.project_notes.remove_duplicates()
    return {"duplicates_removed": removed, "files_written": len(write_files(repositories, root, now))}
