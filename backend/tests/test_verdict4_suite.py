"""The fourth verdict suite: the exam for the assistant that remembers, tries differently and says what it did. Written once, frozen, run once on the final build (see e2e/verdict4_fixtures.py)."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CASES_PATH = ROOT / "e2e" / "verdict4_cases.json"
FROZEN_SHA256 = ROOT / "e2e" / "verdict4_cases.sha256"

sys.path.insert(0, str(ROOT / "e2e"))
import gap_fixtures  # noqa: E402
import verdict4_fixtures as fx  # noqa: E402


@pytest.fixture(scope="module")
def cases() -> list[dict]:
    return json.loads(CASES_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def values(tmp_path_factory) -> dict[str, str]:
    return fx.prepare_verdict4_fixtures(tmp_path_factory.mktemp("verdict4"), gap_fixtures.remove_tree)


def test_the_suite_is_frozen() -> None:
    """Changing a case or its files after a run makes that run meaningless: update verdict4_cases.sha256 in the same change and say why.
    A case edited to match what YBM does today stops measuring anything."""
    material = b"".join((ROOT / "e2e" / name).read_bytes() for name in ("verdict4_cases.json", "verdict4_fixtures.py"))
    digest = hashlib.sha256(material.replace(b"\r\n", b"\n")).hexdigest()
    assert FROZEN_SHA256.read_text(encoding="utf-8").strip() == digest


def test_the_cases_are_distinct_and_cover_the_kinds_of_work(cases) -> None:
    assert len(cases) == 20 and len({c["id"] for c in cases}) == 20
    assert {c["requirement"] for c in cases} == {
        "continuity", "retry", "projects", "memory", "honesty", "size", "autonomy", "safety", "proof", "code", "monitor", "understand", "hands", "research",
    }
    assert all(c["id"].startswith("v4_") and c["heldout"] and c["suites"] == ["verdict4"] and c["claims"] and c["rubric"] for c in cases)
    assert sum(1 for c in cases if c.get("safety")) == 2  # the order that must wait for a yes, and the folder that cannot come back from a bin
    assert sum(1 for c in cases if c.get("follow_ups")) >= 4  # a revised plan, two project chats and a fact that moves are chains of messages
    earlier = set()
    for path in sorted((ROOT / "e2e").glob("*_cases.json")):
        if path.name in ("verdict4_cases.json", "calibration_cases.json"):
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        earlier |= {c["message"] for c in (data["cases"] if isinstance(data, dict) and "cases" in data else data)}
    assert not earlier & {c["message"] for c in cases}


def test_every_variable_a_case_uses_exists(cases, values) -> None:
    web_variables = {"fixture_base_url"}  # the runner's fixture web server supplies it
    for case in cases:
        for name in re.findall(r"\{\{(\w+)\}\}", json.dumps(case)):
            assert name in values or name in web_variables, (case["id"], name)


def test_what_the_cases_expect_is_what_the_files_hold(values) -> None:
    notes = " ".join(p.read_text(encoding="utf-8").lower() for p in Path(values["y_notes"]).iterdir())
    assert "electrician" not in notes  # the honest answer is that those notes are not there
    papers = {p.name: p.read_text(encoding="utf-8") for p in Path(values["y_warranty"]).iterdir()}
    assert not any(name.startswith("dishwasher") and "warranty" in name for name in papers)  # a name search for the word finds the fridge, not the dishwasher
    assert "14 March 2028" in papers["dishwasher-manual-and-guarantee.txt"]
    assert len(list(Path(values["y_photos"]).iterdir())) == 4
    assert Path(values["y_inbox"]).is_dir() and not list(Path(values["y_inbox"]).iterdir())
    assert len(list(Path(values["y_downloads"]).iterdir())) == 3


def test_the_pages_hold_what_the_cases_expect(tmp_path_factory) -> None:
    root = tmp_path_factory.mktemp("verdict4pages")
    fx.prepare_verdict4_fixtures(root, gap_fixtures.remove_tree)
    pages = root / "web" / "verdict4"
    assert "48.20" in (pages / "price.html").read_text(encoding="utf-8")
    assert "<p id=\"out\"></p>" in (pages / "price.html").read_text(encoding="utf-8") or "id=\"p\"" in (pages / "price.html").read_text(encoding="utf-8")
    store = (pages / "store.html").read_text(encoding="utf-8")
    prices = sorted(float(m) for m in re.findall(r"data-price=\"([\d.]+)\"", store))
    assert prices[0] == 3.25 and "Dotted notebook" in store and "N-8802" in store
    assert "V-4417" in (pages / "volunteer.html").read_text(encoding="utf-8")


def test_the_runner_knows_the_suite() -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location("run_all_e2e_tests", ROOT / "scripts" / "run_all_e2e_tests.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    assert module.VERDICT4_PATH == CASES_PATH
    assert sum(1 for case in module.load_cases() if "verdict4" in (case.get("suites") or [])) == 20
