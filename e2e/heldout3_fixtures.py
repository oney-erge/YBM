"""Fixtures for the third held-out suite (e2e/heldout3_cases.json, ids h3_*): the open-ended requests of the "ultimate assistant" work.

The first two suites measure everyday requests that give the exact path. This one measures what those do not: asking about the computer
itself (Steam games, disk space, where a file is), adding to a file, undoing a move, what YBM remembers about the person, saving and
re-running a workflow, and the honest "I can't" answers. It is written once, before it is run, and run once on the code as it stands. Do
not tune YBM against it: a failure is a finding to report, and the suite is frozen (tests/test_heldout3_suite.py fails if the cases or
these fixtures change without the recorded hash).

Everything is generated under the E2E fixture root; answers that depend on this computer (the number of Steam games, free disk space) are
computed here, at fixture time, from the same sources a person could check, never typed in.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from gap_fixtures import write_docx

FRUIT = "apples\nbananas\n"
NOTES_ABOUT = "The harbour authority publishes tide tables each January. Fishing boats leave on the morning ebb tide and return at dusk."
NOT_THERE = ["not found", "doesn't exist", "does not exist", "couldn't find", "could not find", "no such", "cannot find",
             "can't find", "isn't there", "missing", "unable to find", "not exist"]
CANT = ["can't", "cannot", "unable", "not able", "don't have", "do not have", "no way", "isn't something", "not possible",
        "no tool", "not supported", "outside"]


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def steam_game_count() -> int | None:
    """How many games Steam says are installed here, or None when Steam is not installed."""
    try:
        from agent_control.tools.system_inventory import steam_games, _steam_roots

        return len(steam_games()) if _steam_roots() else None
    except Exception:  # noqa: BLE001 - without the tool the case is skipped, not failed
        return None


def free_disk_gb(path: str) -> float:
    return shutil.disk_usage(path).free / 1024**3


def prepare_heldout3_fixtures(fixture_root: Path, remove_tree) -> dict[str, str]:
    root = fixture_root / "heldout3"
    if root.exists():
        remove_tree(root)
    root.mkdir(parents=True)
    values: dict[str, str] = {"h3_root": str(root)}

    steam = steam_game_count()
    values["h3_steam_count"] = str(steam) if steam is not None else "0"
    values["h3_steam_installed"] = "yes" if steam is not None else "no"
    anchor = os.path.splitdrive(str(root))[0] + os.sep if os.name == "nt" else "/"
    free = free_disk_gb(anchor)
    values["h3_drive"] = anchor.rstrip("\\/").rstrip(":") or "/"
    values["h3_free_a"], values["h3_free_b"], values["h3_free_c"] = (str(int(free) + delta) for delta in (-1, 0, 1))

    _write(root / "deep" / "er" / "folder" / "h3-annual-budget-2026.xlsx", "placeholder, found by name only\n")
    values["h3_find_name"] = "h3-annual-budget-2026.xlsx"

    write_docx(root / "harbour-notes.docx", [NOTES_ABOUT])
    values["h3_docx"] = str(root / "harbour-notes.docx")

    _write(root / "big.dat", "z" * 50_000)
    _write(root / "small.dat", "z" * 40)
    values["h3_big"], values["h3_small"] = str(root / "big.dat"), str(root / "small.dat")

    dated = root / "dated"
    for name, year in (("memo-2018.txt", 2018), ("memo-2024.txt", 2024), ("memo-2021.txt", 2021)):
        path = _write(dated / name, f"Memo of {year}.\n")
        stamp = (year - 1970) * 365.25 * 86400
        os.utime(path, (stamp, stamp))
    values["h3_dated"] = str(dated)

    _write(root / "words.txt", "one two three four five six seven eight nine ten eleven twelve thirteen\n")
    values["h3_words"] = str(root / "words.txt")  # 13 words

    _write(root / "fruit" / "fruit.txt", FRUIT)
    values["h3_fruit_dir"], values["h3_fruit"] = str(root / "fruit"), str(root / "fruit" / "fruit.txt")

    imgs = root / "images"
    for n in range(1, 4):
        (imgs / f"shot-{n}.png").parent.mkdir(parents=True, exist_ok=True)
        (imgs / f"shot-{n}.png").write_bytes(b"\x89PNG\r\n")
    _write(imgs / "keep.txt", "stays\n")
    values["h3_imgs"] = str(imgs)

    wf = root / "wf"
    for name in ("a.txt", "b.txt", "c.txt", "d.txt"):
        _write(wf / name, name + "\n")
    values["h3_wf_dir"] = str(wf)  # 4 files

    junk = root / "junk"
    for n in range(1, 4):
        _write(junk / f"old-{n}.log", "old\n")
    values["h3_junk"] = str(junk)

    (root / "work").mkdir()
    values["h3_work"] = str((root / "work").resolve())
    return values
