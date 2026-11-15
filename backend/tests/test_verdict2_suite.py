"""The second verdict suite: the exam for the finished assistant. Written once, frozen, run once on the final code (see e2e/verdict2_fixtures.py)."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CASES_PATH = ROOT / "e2e" / "verdict2_cases.json"
FROZEN_SHA256 = ROOT / "e2e" / "verdict2_cases.sha256"

sys.path.insert(0, str(ROOT / "e2e"))
import gap_fixtures  # noqa: E402
import verdict2_fixtures as fx  # noqa: E402


def _runner():
    spec = importlib.util.spec_from_file_location("run_all_e2e_tests", ROOT / "scripts" / "run_all_e2e_tests.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def cases() -> list[dict]:
    return json.loads(CASES_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def values(tmp_path_factory) -> dict[str, str]:
    return fx.prepare_verdict2_fixtures(tmp_path_factory.mktemp("verdict2"), gap_fixtures.remove_tree)


def test_the_suite_is_frozen() -> None:
    """Changing a case or its files after a run makes that run meaningless: update verdict2_cases.sha256 in the same change and say why.
    A case edited to match what YBM does today stops measuring anything."""
    material = b"".join((ROOT / "e2e" / name).read_bytes() for name in ("verdict2_cases.json", "verdict2_fixtures.py", "media_fixtures.py"))
    digest = hashlib.sha256(material.replace(b"\r\n", b"\n")).hexdigest()
    assert FROZEN_SHA256.read_text(encoding="utf-8").strip() == digest


def test_the_cases_are_distinct_and_cover_the_kinds_of_work(cases) -> None:
    assert len(cases) == 20 and len({c["id"] for c in cases}) == 20
    assert {c["requirement"] for c in cases} == {"acquire", "plan", "research", "self", "honesty", "invent", "conversation", "safety"}
    assert all(c["id"].startswith("fin2_") and c["heldout"] and c["suites"] == ["verdict2"] and c["claims"] and c["rubric"] for c in cases)
    assert sum(1 for c in cases if c.get("safety")) >= 3
    earlier = set()
    for name in ("heldout_cases.json", "heldout2_cases.json", "heldout3_cases.json", "heldout4_cases.json", "vision_cases.json", "challenge_cases.json", "verdict_cases.json"):
        path = ROOT / "e2e" / name
        if path.exists():
            earlier |= {c["message"] for c in json.loads(path.read_text(encoding="utf-8"))}
    assert not earlier & {c["message"] for c in cases}


def test_every_variable_a_case_uses_exists(cases, values) -> None:
    for case in cases:
        for name in re.findall(r"\{\{(\w+)\}\}", json.dumps(case)):
            assert name in values, (case["id"], name)


def test_what_the_cases_expect_is_what_the_files_hold(values) -> None:
    amounts = [float(re.search(r"EUR ([\d.]+)", p.read_text(encoding="utf-8")).group(1)) for p in Path(values["y_invoices"]).iterdir()]
    assert len(amounts) == 4 and round(sum(amounts), 2) == 566.00

    sizes = {p.name: p.stat().st_size // 1024 for p in Path(values["y_tree"]).rglob("*.bin")}
    top = sorted(sizes, key=sizes.get, reverse=True)[:3]
    assert top == ["boulder.bin", "glacier.bin", "stone.bin"] and [sizes[n] for n in top] == [75, 40, 9]

    rows = [line.split(",") for line in Path(values["y_table"]).read_text(encoding="utf-8").splitlines()[1:]]
    minutes = {name: int(value) for name, value in rows}
    assert sum(minutes.values()) / len(minutes) == 36 and max(minutes, key=minutes.get) == "Cy"

    import time

    recent = sorted(p.name for p in Path(values["y_dated"]).iterdir() if time.time() - p.stat().st_mtime < 3 * 86400)
    assert recent == ["ferry-times.txt", "orchard-map.txt"]

    words = re.findall(r"[A-Za-z']+", Path(values["y_story"]).read_text(encoding="utf-8"))
    assert len(words) == 35 and max(words, key=len) == "extraordinarily"

    assert sorted(p.name for p in Path(values["y_letters"]).iterdir()) == ["council-tax-letter.txt", "revenue-tax-letter.txt"]
    assert not Path(values["y_broken"]).read_bytes().startswith(b"%PDF")
    assert sorted(p.name for p in Path(values["y_rename"]).iterdir()) == ["map-links.txt", "packing.txt", "tickets.txt", "trip-notes.txt"]
    biggest = max(Path(values["y_cache"]).iterdir(), key=lambda p: p.stat().st_size)
    assert biggest.name == "render-cache.tmp" and biggest.stat().st_size == 60 * 1024
    assert list(Path(values["y_out"]).iterdir()) == []


def test_the_runner_asks_for_the_suite_only_by_name(cases) -> None:
    runner = _runner()
    mixed = [{"id": "dev_case", "size": "small"}, *cases]

    def chosen(**kw):
        defaults = dict(only=set(), skip=set(), sizes=set(), suites=set(), include_guarded=False)
        return [c["id"] for c in runner.select_cases(mixed, **{**defaults, **kw})]

    assert chosen() == ["dev_case"]
    assert len(chosen(suites={"verdict2"})) == 20
    assert chosen(suites={"verdict"}) == []
