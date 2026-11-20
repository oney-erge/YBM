"""Fixtures for the continuity suite (e2e/continuity_cases.json, ids cont_*): the mechanisms behind the 2026-11-16 chain, in other topics."""

from __future__ import annotations

from pathlib import Path


def prepare_continuity_fixtures(fixture_root: Path, remove_tree) -> dict[str, str]:
    root = fixture_root / "continuity"
    if root.exists():
        remove_tree(root)
    root.mkdir(parents=True)
    values: dict[str, str] = {}

    values["c_work"] = str(root / "work")
    (root / "work").mkdir()

    notes = root / "notes"
    notes.mkdir()
    words = "The river was calm and the boats were tied up for the night while a few lights still burned in the windows of the old harbour houses nearby and the stars"
    assert len(words.split()) == 31
    (notes / "draft.txt").write_text(words + "\n", encoding="utf-8")
    values["c_notes"] = str(notes)

    mess = root / "mess"
    mess.mkdir()
    for name in ("receipt-1.txt", "receipt-2.txt", "photo-a.jpg.txt", "photo-b.jpg.txt", "minutes.doc.txt", "todo.txt"):
        (mess / name).write_text(f"{name}\n", encoding="utf-8")
    values["c_mess"] = str(mess)

    lease = root / "lease"
    lease.mkdir()
    (lease / "lease.txt").write_text(
        "Residential lease agreement for the flat at 14 Mill Lane.\nThe term of this lease ends on 30 June 2029.\nThe rent is 1450 dollars a month, due on the first.\n", encoding="utf-8"
    )
    values["c_lease"] = str(lease)
    return values
