"""A live case leaves nothing of its own in the owner's memory (e2e/residue.py)."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "e2e"))
import residue  # noqa: E402  (the e2e folder is not a package)


def _database(path: Path) -> Path:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT);
        CREATE TABLE project_notes (id TEXT PRIMARY KEY, project_id TEXT, text TEXT);
        CREATE TABLE project_plans (id TEXT PRIMARY KEY, goal TEXT);
        CREATE TABLE project_plan_steps (id TEXT PRIMARY KEY, plan_id TEXT, title TEXT);
        INSERT INTO projects VALUES ('p_real', 'my own project');
        INSERT INTO project_notes VALUES ('n_real', 'p_real', 'a real note');
        INSERT INTO project_plans VALUES ('pl_real', 'a real plan');
        INSERT INTO project_plan_steps VALUES ('s_real', 'pl_real', 'a real step');
        """
    )
    conn.commit()
    conn.close()
    return path


def _count(db: Path, table: str) -> int:
    conn = sqlite3.connect(db)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


def test_what_a_case_made_is_removed_and_what_was_there_stays(tmp_path):
    db = _database(tmp_path / "state.db")
    skills = tmp_path / "skills"
    skills.mkdir()
    (skills / "real-skill.md").write_text("kept", encoding="utf-8")
    before = residue.snapshot(db, skills)

    conn = sqlite3.connect(db)
    conn.executescript(
        """
        INSERT INTO projects VALUES ('p_test', 'pool');
        INSERT INTO project_notes VALUES ('n_test', 'p_test', 'Decided to build a pool.');
        INSERT INTO project_plans VALUES ('pl_test', 'plan for a pool');
        INSERT INTO project_plan_steps VALUES ('s_test', 'pl_test', 'dig');
        """
    )
    conn.commit()
    conn.close()
    (skills / "helper-made-by-a-test.md").write_text("learned from a test", encoding="utf-8")

    removed = residue.remove_new(db, skills, before)

    assert {_count(db, t) for t in ("projects", "project_notes", "project_plans", "project_plan_steps")} == {1}
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT id FROM projects").fetchall() == [("p_real",)]
    assert conn.execute("SELECT id FROM project_plan_steps").fetchall() == [("s_real",)]
    conn.close()
    assert (skills / "real-skill.md").read_text(encoding="utf-8") == "kept"
    assert not (skills / "helper-made-by-a-test.md").exists()
    assert (skills / residue.QUARANTINE / "helper-made-by-a-test.md").read_text(encoding="utf-8") == "learned from a test"  # moved, never deleted
    assert any("projects" in item for item in removed) and any("helper-made-by-a-test.md" in item for item in removed)


def test_nothing_new_means_nothing_removed(tmp_path):
    db = _database(tmp_path / "state.db")
    skills = tmp_path / "skills"
    before = residue.snapshot(db, skills)  # no skills folder yet
    assert residue.remove_new(db, skills, before) == []
    assert _count(db, "projects") == 1


def test_a_missing_database_is_not_an_error(tmp_path):
    before = residue.snapshot(tmp_path / "missing" / "state.db", tmp_path / "skills")
    assert before["projects"] == set()
    assert residue.remove_new(tmp_path / "missing" / "state.db", tmp_path / "skills", before) == []


def test_a_second_skill_with_the_same_name_does_not_overwrite_the_first(tmp_path):
    db = _database(tmp_path / "state.db")
    skills = tmp_path / "skills"
    skills.mkdir()
    before = residue.snapshot(db, skills)
    for text in ("first", "second"):
        (skills / "helper.md").write_text(text, encoding="utf-8")
        residue.remove_new(db, skills, before)
    kept = sorted(p.read_text(encoding="utf-8") for p in (skills / residue.QUARANTINE).iterdir())
    assert kept == ["first", "second"]
