"""Fixtures for the fourth held-out suite (e2e/heldout4_cases.json, ids h4_*): fresh requests, written BEFORE the round-5 work.

The first three suites and the vision suite are used up: fixes were made after each of them ran. This one was written and frozen before the
understanding, policy and verifier work of round 5 began, and is run once, at the end, to measure that work on requests it was not tuned
against: counting and comparing, finding by content, renaming, copying, a total over a table, reading a JSON field, two synonyms for
"delete" and an "ask me first" instruction (to measure whether the request is understood rather than keyword-matched), remembering and
then using a fact, several files in one request, and the honest "I can't".

Not to be tuned against: a failure is a finding to report, and the suite is frozen (tests/test_heldout4_suite.py fails if the cases or these
fixtures change without the recorded hash). Everything is generated under the E2E fixture root and every expected answer is computed here.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from gap_fixtures import write_docx

NOTE_TEXT = "The balloon festival opens on Saturday. The zeppelin hangar needs two more volunteers."
NOT_THERE = ["not found", "doesn't exist", "does not exist", "couldn't find", "could not find", "no such", "cannot find",
             "can't find", "isn't there", "missing", "unable to find", "not exist"]
CANT = ["can't", "cannot", "unable", "not able", "don't have", "do not have", "no way", "isn't something", "not possible",
        "no tool", "not supported", "outside"]


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def prepare_heldout4_fixtures(fixture_root: Path, remove_tree) -> dict[str, str]:
    root = fixture_root / "heldout4"
    if root.exists():
        remove_tree(root)
    root.mkdir(parents=True)
    values: dict[str, str] = {"h4_root": str(root)}

    # Counting by kind: 3 PDF and 2 Word files among other things.
    mixed = root / "mixed"
    for name in ("a.pdf", "b.pdf", "c.pdf", "x.txt", "y.csv"):
        _write(mixed / name, "x")
    write_docx(mixed / "d.docx", ["one"])
    write_docx(mixed / "e.docx", ["two"])
    values["h4_mixed"] = str(mixed)

    # The largest file.
    sizes = root / "sizes"
    _write(sizes / "tiny.dat", "z" * 10)
    _write(sizes / "medium.dat", "z" * 4_000)
    _write(sizes / "huge.dat", "z" * 90_000)
    values["h4_sizes"] = str(sizes)

    # Finding by content.
    notes = root / "notes"
    _write(notes / "monday.txt", "Buy bread and check the mail.")
    _write(notes / "tuesday.txt", NOTE_TEXT)
    _write(notes / "wednesday.txt", "Call the plumber about the sink.")
    values["h4_notes"] = str(notes)

    write_docx(root / "festival.docx", [NOTE_TEXT])
    values["h4_docx"] = str(root / "festival.docx")

    # Renaming and copying.
    rename = root / "rename"
    for name in ("alpha.txt", "beta.txt"):
        _write(rename / name, name)
    values["h4_rename"] = str(rename)
    _write(root / "copy-src" / "original.txt", "keep me\n")
    (root / "copy-dest").mkdir()
    values["h4_src"], values["h4_dest"] = str(root / "copy-src" / "original.txt"), str(root / "copy-dest")
    values["h4_src_dir"] = str(root / "copy-src")

    # A total over a table: 12.50 + 7.25 + 30.00 + 0.25 = 50.00.
    _write(root / "expenses.csv", "item,amount\ncoffee,12.50\nlunch,7.25\ntrain,30.00\ngum,0.25\n")
    values["h4_csv"], values["h4_csv_total"] = str(root / "expenses.csv"), "50"

    _write(root / "package.json", json.dumps({"name": "demo", "version": "3.14.2", "private": True}, indent=2))
    values["h4_json"] = str(root / "package.json")

    junk = root / "junk"
    for n in range(1, 4):
        _write(junk / f"scrap-{n}.tmp", "scrap\n")
    values["h4_junk"] = str(junk)

    photos = root / "photos"
    for n in range(1, 4):
        _write(photos / f"p{n}.jpg", "jpg")
    values["h4_photos"] = str(photos)

    # Which folder has more files: 4 against 2.
    for name, count in (("dir-many", 4), ("dir-few", 2)):
        for n in range(count):
            _write(root / name / f"f{n}.txt", "x")
    values["h4_dir_a"], values["h4_dir_b"] = str(root / "dir-many"), str(root / "dir-few")

    # The most recently changed log.
    logs = root / "logs"
    for name, year in (("app.log", 2019), ("db.log", 2025), ("web.log", 2022)):
        path = _write(logs / name, f"log of {year}\n")
        stamp = (year - 1970) * 365.25 * 86400
        os.utime(path, (stamp, stamp))
    values["h4_logs"] = str(logs)

    for name in ("out", "out2", "out3"):
        (root / name).mkdir()
        values[f"h4_{name}"] = str((root / name).resolve())
    return values
