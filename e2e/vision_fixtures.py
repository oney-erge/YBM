"""Fixtures for the vision suite (e2e/vision_cases.json, ids v_*): the things Oney said YBM must be able to do.

Each case is one of his own examples, in fresh wording: how many Steam games are installed, where is this file, what is it about, read
these websites and compare, build a small app and run it, work something out with a script, use Claude Code when told to, split a big
request into parts, remember what he says, and say honestly when it cannot do something. It is the permanent "does it still do what I
asked for" check, run live on the real Telegram bot before a release. Like the held-out suites it is written once and frozen
(tests/test_vision_suite.py fails if the cases or these fixtures change without the recorded hash); a failure is a finding to report,
and a case is never edited to match what YBM does today.

Everything is generated under the E2E fixture root. Answers that depend on this computer are computed here, never typed in.
"""

from __future__ import annotations

from pathlib import Path

from gap_fixtures import write_docx
from heldout3_fixtures import steam_game_count

NOTES_ABOUT = "The lighthouse keeper logs the weather every dawn. Supply boats call on Thursdays with fresh bread and lamp oil."


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def prepare_vision_fixtures(fixture_root: Path, remove_tree) -> dict[str, str]:
    root = fixture_root / "vision"
    if root.exists():
        remove_tree(root)
    root.mkdir(parents=True)
    values: dict[str, str] = {"v_root": str(root)}

    steam = steam_game_count()
    values["v_steam_count"] = str(steam) if steam is not None else "0"

    # Found by name only: nested, inside a hidden folder, and not in any search index.
    _write(root / ".cache-data" / "archive" / "2026" / "v-zebra-roadmap-q3.xlsx", "placeholder, found by name only\n")
    values["v_find_name"] = "v-zebra-roadmap-q3.xlsx"

    write_docx(root / "lighthouse-notes.docx", [NOTES_ABOUT])
    values["v_docx"] = str(root / "lighthouse-notes.docx")

    # Lines in all the .txt files below the folder, combined: 4 + 5 + 6 = 15 (the .md file must not count).
    texts = root / "texts"
    _write(texts / "a.txt", "1\n2\n3\n4\n")
    _write(texts / "sub" / "b.txt", "1\n2\n3\n4\n5\n")
    _write(texts / "sub" / "deeper" / "c.txt", "1\n2\n3\n4\n5\n6\n")
    _write(texts / "readme.md", "x\ny\nz\n")
    values["v_texts"] = str(texts)
    values["v_text_lines"] = "15"

    _write(root / "compare" / "short-note.txt", "one two three four five six seven\n")
    _write(root / "compare" / "long-note.txt", "one two three four five six seven eight nine ten eleven twelve\n")
    values["v_c1"], values["v_c2"] = str(root / "compare" / "short-note.txt"), str(root / "compare" / "long-note.txt")

    reports = root / "reports"
    _write(reports / "north.txt", "North region sold 12 units.\nMore detail follows.\n")
    _write(reports / "south.txt", "South region sold 7 units across all of its stores this quarter.\nMore detail follows.\n")
    _write(reports / "east.txt", "East: 3.\nMore detail follows.\n")
    values["v_reports"] = str(reports)

    _write(root / "list" / "todo.txt", "buy milk\nbuy eggs\n")
    values["v_todo"] = str(root / "list" / "todo.txt")
    values["v_todo_dir"] = str(root / "list")

    imgs = root / "photos"
    for n in range(1, 4):
        (imgs / f"pic-{n}.png").parent.mkdir(parents=True, exist_ok=True)
        (imgs / f"pic-{n}.png").write_bytes(b"\x89PNG\r\n")
    _write(imgs / "keep.txt", "stays where it is\n")
    values["v_photos"] = str(imgs)

    (root / "out").mkdir()
    values["v_out"] = str((root / "out").resolve())
    (root / "agent-out").mkdir()
    values["v_agent_out"] = str((root / "agent-out").resolve())

    junk = root / "junk"
    for n in range(1, 4):
        _write(junk / f"old-{n}.log", "old\n")
    values["v_junk"] = str(junk)
    return values
