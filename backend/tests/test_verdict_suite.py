"""The verdict suite: the last check before a release. Written once, frozen, run once on the final code (see e2e/verdict_fixtures.py)."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CASES_PATH = ROOT / "e2e" / "verdict_cases.json"
FROZEN_SHA256 = ROOT / "e2e" / "verdict_cases.sha256"

sys.path.insert(0, str(ROOT / "e2e"))
import gap_fixtures  # noqa: E402
import verdict_fixtures as fx  # noqa: E402


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
    return fx.prepare_verdict_fixtures(tmp_path_factory.mktemp("verdict"), gap_fixtures.remove_tree)


def test_the_suite_is_frozen() -> None:
    """Changing a case or its files after a run makes that run meaningless: update verdict_cases.sha256 in the same change and say why.
    A case edited to match what YBM does today stops measuring anything."""
    material = b"".join((ROOT / "e2e" / name).read_bytes() for name in ("verdict_cases.json", "verdict_fixtures.py", "media_fixtures.py"))
    digest = hashlib.sha256(material.replace(b"\r\n", b"\n")).hexdigest()
    assert FROZEN_SHA256.read_text(encoding="utf-8").strip() == digest


def test_the_cases_are_distinct_and_cover_the_kinds_of_work(cases) -> None:
    assert len(cases) == 16 and len({c["id"] for c in cases}) == 16
    assert {c["requirement"] for c in cases} == {"acquire", "plan", "honesty", "invent", "conversation", "self"}
    assert all(c["id"].startswith("fin_") and c["heldout"] and c["suites"] == ["verdict"] and c["claims"] and c["rubric"] for c in cases)
    earlier = set()
    for name in ("heldout_cases.json", "heldout2_cases.json", "heldout3_cases.json", "heldout4_cases.json", "vision_cases.json", "challenge_cases.json"):
        path = ROOT / "e2e" / name
        if path.exists():
            earlier |= {c["message"] for c in json.loads(path.read_text(encoding="utf-8"))}
    assert not earlier & {c["message"] for c in cases}


def test_every_variable_a_case_uses_exists(cases, values) -> None:
    for case in cases:
        for name in re.findall(r"\{\{(\w+)\}\}", json.dumps(case)):
            assert name in values, (case["id"], name)


def test_what_the_cases_expect_is_what_the_files_hold(values) -> None:
    old = Path(values["x_temp"])
    assert sorted(p.name for p in old.iterdir()) == ["keep-a.dat", "keep-b.dat", "keep-c.dat", "old-1.dat", "old-2.dat"]
    from PIL import Image

    sizes = {p.stem: Image.open(p).size for p in Path(values["x_pictures"]).iterdir()}
    assert {name for name, (w, h) in sizes.items() if h > w} == {"market", "statue"}
    errors = {p.name: sum(line.lower().count("error") for line in p.read_text(encoding="utf-8").splitlines()) for p in Path(values["x_logs"]).iterdir()}
    assert errors == {"app.log": 5, "db.log": 2, "web.log": 9} and sum(errors.values()) == 16
    assert sorted(p.stat().st_size for p in Path(values["x_sizes"]).iterdir()) == [1024, 3072, 5120, 12288]
    ledger = next(Path(values["x_root"]).rglob("harbor-ledger-7.txt"))
    assert ledger.read_text(encoding="utf-8").splitlines()[-1] == "paid in full"


def test_the_runner_asks_for_the_suite_only_by_name(cases) -> None:
    runner = _runner()
    mixed = [{"id": "dev_case", "size": "small"}, *cases]

    def chosen(**kw):
        defaults = dict(only=set(), skip=set(), sizes=set(), suites=set(), include_guarded=False)
        return [c["id"] for c in runner.select_cases(mixed, **{**defaults, **kw})]

    assert chosen() == ["dev_case"]
    assert len(chosen(suites={"verdict"})) == 16
    assert chosen(suites={"vision"}) == []
