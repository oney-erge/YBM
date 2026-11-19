"""The third verdict suite: the exam for the assistant that keeps hold of a goal. Written once, frozen, run once on the final build (see e2e/verdict3_fixtures.py)."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CASES_PATH = ROOT / "e2e" / "verdict3_cases.json"
FROZEN_SHA256 = ROOT / "e2e" / "verdict3_cases.sha256"

sys.path.insert(0, str(ROOT / "e2e"))
import gap_fixtures  # noqa: E402
import verdict3_fixtures as fx  # noqa: E402


@pytest.fixture(scope="module")
def cases() -> list[dict]:
    return json.loads(CASES_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def values(tmp_path_factory) -> dict[str, str]:
    return fx.prepare_verdict3_fixtures(tmp_path_factory.mktemp("verdict3"), gap_fixtures.remove_tree)


def test_the_suite_is_frozen() -> None:
    """Changing a case or its files after a run makes that run meaningless: update verdict3_cases.sha256 in the same change and say why.
    A case edited to match what YBM does today stops measuring anything."""
    material = b"".join((ROOT / "e2e" / name).read_bytes() for name in ("verdict3_cases.json", "verdict3_fixtures.py", "media_fixtures.py"))
    digest = hashlib.sha256(material.replace(b"\r\n", b"\n")).hexdigest()
    assert FROZEN_SHA256.read_text(encoding="utf-8").strip() == digest


def test_the_cases_are_distinct_and_cover_the_kinds_of_work(cases) -> None:
    assert len(cases) == 20 and len({c["id"] for c in cases}) == 20
    assert {c["requirement"] for c in cases} == {"continuity", "investigate", "honesty", "research", "accounts", "initiative", "safety", "acquire"}
    assert all(c["id"].startswith("fin3_") and c["heldout"] and c["suites"] == ["verdict3"] and c["claims"] and c["rubric"] for c in cases)
    assert sum(1 for c in cases if c.get("safety")) >= 4
    assert sum(1 for c in cases if c.get("manual")) == 3  # the 600-page run, the mailbox and the watch that fires: they need a night or an account, and still count in the 20
    assert sum(1 for c in cases if c.get("follow_ups")) >= 6  # the continuity cases are chains of messages
    earlier = set()
    for name in ("heldout_cases.json", "heldout2_cases.json", "heldout3_cases.json", "heldout4_cases.json", "vision_cases.json", "challenge_cases.json", "verdict_cases.json", "verdict2_cases.json"):
        path = ROOT / "e2e" / name
        if path.exists():
            earlier |= {c["message"] for c in json.loads(path.read_text(encoding="utf-8"))}
    assert not earlier & {c["message"] for c in cases}


def test_every_variable_a_case_uses_exists(cases, values) -> None:
    for case in cases:
        for name in re.findall(r"\{\{(\w+)\}\}", json.dumps(case)):
            assert name in values, (case["id"], name)


def test_what_the_cases_expect_is_what_the_files_hold(values) -> None:
    errors = sum(1 for p in Path(values["z_logs"]).iterdir() for line in p.read_text(encoding="utf-8").splitlines() if "ERROR" in line)
    assert errors == 8
    assert sorted(p.name for p in Path(values["z_scans"]).iterdir()) == ["Scan_3.txt", "scan 1.txt", "scan 10.txt", "scan 2.txt", "scan-11.txt"]
    assert sorted(p.name for p in Path(values["z_letters"]).iterdir()) == ["bank-notice.txt", "gas-bill.txt", "water-bill.txt"]
    import time

    old = sorted(p.name for p in Path(values["z_old"]).iterdir() if time.time() - p.stat().st_mtime > 30 * 86400)
    assert old == ["spring-notes.txt", "spring-plan.txt"]
    assert Path(values["z_inbox"]).is_dir() and not list(Path(values["z_inbox"]).iterdir())


def test_the_runner_knows_the_suite() -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location("run_all_e2e_tests", ROOT / "scripts" / "run_all_e2e_tests.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    assert module.VERDICT3_PATH == CASES_PATH
    assert sum(1 for case in module.load_cases() if "verdict3" in (case.get("suites") or [])) == 20
