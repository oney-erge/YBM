"""What a live case leaves behind in the owner's own memory, and taking it out again.

The live cases run on the real bot, in the real chat. A case that says "I want to build a pool" makes a project, notes and a plan; a case that does a task makes a
skill. None of it is the owner's. On 11/19 the live memory held projects and notes made by tests (each note four times over), and two test-made skills, so "which
projects are active?" would have answered with the tests'. This module takes a snapshot before a case and removes what is new after it. Skill files are moved
into a folder next to them, never deleted.
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

#: A table, and the table whose rows belong to it (removed with it).
TABLES = ("projects", "project_notes", "project_plans")
CHILDREN = {"project_plans": ("project_plan_steps", "plan_id")}
QUARANTINE = "removed_by_tests"


def snapshot(db_path: Path, skills_dir: Path) -> dict[str, set[str]]:
    """The ids and skill files that exist now."""
    found: dict[str, set[str]] = {table: set() for table in TABLES}
    try:
        conn = sqlite3.connect(db_path)
        for table in TABLES:
            try:
                found[table] = {str(row[0]) for row in conn.execute(f"SELECT id FROM {table}")}
            except sqlite3.Error:
                found[table] = set()
        conn.close()
    except sqlite3.Error:
        pass
    found["skill_files"] = {p.name for p in skills_dir.glob("*.md")} if skills_dir.is_dir() else set()
    return found


def remove_new(db_path: Path, skills_dir: Path, before: dict[str, set[str]]) -> list[str]:
    """Remove the rows made since `before`, and move the new skill files aside. Returns what was removed, for the log."""
    removed: list[str] = []
    try:
        conn = sqlite3.connect(db_path)
        for table in TABLES:
            try:
                current = {str(row[0]) for row in conn.execute(f"SELECT id FROM {table}")}
            except sqlite3.Error:
                continue
            new = sorted(current - before.get(table, set()))
            if not new:
                continue
            marks = ",".join("?" for _ in new)
            child = CHILDREN.get(table)
            if child:
                conn.execute(f"DELETE FROM {child[0]} WHERE {child[1]} IN ({marks})", new)
            conn.execute(f"DELETE FROM {table} WHERE id IN ({marks})", new)
            removed.append(f"{len(new)} {table}")
        conn.commit()
        conn.close()
    except sqlite3.Error:
        pass
    if skills_dir.is_dir():
        aside = skills_dir / QUARANTINE
        for path in sorted(skills_dir.glob("*.md")):
            if path.name in before.get("skill_files", set()):
                continue
            aside.mkdir(exist_ok=True)
            target = aside / path.name
            if target.exists():
                target = aside / f"{path.stem}-{int(path.stat().st_mtime)}{path.suffix}"
            shutil.move(str(path), str(target))
            removed.append(f"skill {path.name}")
    return removed
