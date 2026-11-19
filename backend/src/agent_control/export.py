"""Everything the assistant keeps that is yours, in open formats, in one folder: `ybm export`.

A backup (backup.py) is the way back: the database, the settings and the secrets, as they are. An export is the way out: what you said, what is known about you, your projects with
what was decided, your plans, goals, schedules and jobs, as text and JSON that any program or a person can read, with no database and no secrets in it. It is what keeps the assistant
from holding your life in a format only it can open.

Not included, on purpose: the secrets (`.env`, the vault, the logins it made for sites), the raw record of what each tool was given and returned (it can hold a password that was typed
into a form), logs, and the folders of files it made (artifacts, reports, workspaces), which are ordinary files in `.agent_control` and already readable.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent_control import projects_memory
from agent_control.storage.repositories import Repositories

#: file name -> (table, the columns that are exported, the order). Nothing else of a table leaves.
TABLES: dict[str, tuple[str, tuple[str, ...], str]] = {
    "messages.jsonl": ("messages", ("id", "conversation_id", "channel", "kind", "sender_id", "text", "received_at"), "received_at"),
    "tasks.jsonl": ("tasks", ("id", "objective", "status", "conversation_id", "created_at", "updated_at"), "created_at"),
    "facts.jsonl": ("memory_facts", ("id", "category", "content", "about", "source", "valid_from", "valid_to", "created_at"), "created_at"),
    "projects.jsonl": ("projects", ("id", "name", "hooks_json", "status", "created_at", "updated_at"), "created_at"),
    "project_notes.jsonl": ("project_notes", ("id", "project_id", "kind", "text", "at"), "at"),
    "plans.jsonl": ("project_plans", ("id", "goal", "summary", "status", "notes", "check_in_days", "created_at", "updated_at"), "created_at"),
    "plan_steps.jsonl": ("project_plan_steps", ("id", "plan_id", "position", "title", "detail", "owner", "status", "due", "result"), "plan_id, position"),
    "goals.jsonl": ("goals", ("id", "objective", "words", "status", "created_at", "updated_at"), "created_at"),
    "schedules.jsonl": ("schedules", ("id", "objective", "cadence", "timezone", "status", "next_run_at", "created_at"), "created_at"),
    "jobs.jsonl": ("background_jobs", ("id", "kind", "goal", "status", "created_at", "updated_at"), "created_at"),
}

README = """# YBM export, {stamp}

What is in this folder, in files any program or a person can read:

- `memory/`: `INDEX.md`, `about-you.md` and one `PROJECT.md` per project: what is known about you and where each project stands, as pages.
- `messages.jsonl`: everything said to and by the assistant, one message per line.
- `tasks.jsonl`: what was asked and how it ended (the answer is in `answers.jsonl`).
- `facts.jsonl`: what is remembered about you, with when it became true and, for what stopped being true, when it stopped.
- `projects.jsonl`, `project_notes.jsonl`: your projects and their notebooks (decisions, facts, open questions, purchases, steps, results).
- `plans.jsonl`, `plan_steps.jsonl`, `goals.jsonl`, `schedules.jsonl`, `jobs.jsonl`: plans and their steps, what you wanted, what runs on a schedule, background jobs.
- `skills/`: the procedures it learned, as markdown.

Not here: passwords and keys (`.env`, the vault, the logins it made for sites), the raw record of what each tool was given and returned, logs, and the files it made for you
(under `.agent_control/artifacts` and `.agent_control/workspaces`, which are ordinary files). {counts}
"""


def _rows(repositories: Repositories, table: str, columns: tuple[str, ...], order: str) -> list[dict[str, Any]]:
    with repositories.messages.database.connect() as connection:
        found = connection.execute(f"SELECT {', '.join(columns)} FROM {table} ORDER BY {order}").fetchall()  # noqa: S608 - names come from the table above
    return [{column: row[column] for column in columns} for row in found]


def _write_lines(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(row, ensure_ascii=False, default=str) + "\n" for row in rows), encoding="utf-8", newline="\n")


def export_all(repositories: Repositories, out_root: Path, *, skills_dir: Path | None = None, now: datetime | None = None) -> Path:
    """Write the export into a new folder under `out_root` and return it."""
    now = now or datetime.now(timezone.utc)
    stamp = now.strftime("%Y%m%d-%H%M%S")
    folder = out_root / f"ybm-export-{stamp}"
    folder.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    for name, (table, columns, order) in TABLES.items():
        rows = _rows(repositories, table, columns, order)
        _write_lines(folder / name, rows)
        counts[name] = len(rows)
    with repositories.tasks.database.connect() as connection:
        answers = connection.execute("SELECT id, metadata_json FROM tasks ORDER BY created_at").fetchall()
    said = []
    for row in answers:
        try:
            answer = json.loads(row["metadata_json"] or "{}").get("synthesized_answer")
        except ValueError:
            answer = None
        if answer:
            said.append({"task_id": row["id"], "answer": str(answer)})
    _write_lines(folder / "answers.jsonl", said)
    counts["answers.jsonl"] = len(said)
    projects_memory.write_files(repositories, folder / "memory", now)
    if skills_dir is not None and skills_dir.is_dir():
        (folder / "skills").mkdir(exist_ok=True)
        for path in sorted(skills_dir.glob("*.md")):
            shutil.copyfile(path, folder / "skills" / path.name)
    (folder / "README.md").write_text(
        README.format(stamp=stamp, counts="Counted: " + ", ".join(f"{name} {count}" for name, count in counts.items()) + "."), encoding="utf-8", newline="\n"
    )
    return folder


def run_export(out: str | None) -> int:
    from agent_control.cli import build_repositories

    repositories, _ = build_repositories()
    folder = export_all(repositories, Path(out) if out else Path(".agent_control") / "exports", skills_dir=Path(".agent_control") / "skills")
    print(f"Exported to {folder}")
    print("It holds what you said, what is known about you, your projects, plans, goals and schedules, as text and JSON; it holds no passwords or keys.")
    return 0
