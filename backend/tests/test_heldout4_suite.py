"""The fourth held-out suite: fresh requests written before the round-5 work, run once at the end."""

from __future__ import annotations

import csv
import hashlib
import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CASES_PATH = ROOT / "e2e" / "heldout4_cases.json"
FROZEN_SHA256 = ROOT / "e2e" / "heldout4_cases.sha256"

sys.path.insert(0, str(ROOT / "e2e"))
import gap_fixtures  # noqa: E402
import heldout4_fixtures as fx  # noqa: E402


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
    return fx.prepare_heldout4_fixtures(tmp_path_factory.mktemp("heldout4"), gap_fixtures.remove_tree)


def test_the_suite_is_frozen() -> None:
    """Changing a case or its files after the suite has been run makes the run meaningless: update heldout4_cases.sha256 in the same change
    and say why. A case edited to match what YBM does today stops measuring anything."""
    material = CASES_PATH.read_bytes() + (ROOT / "e2e" / "heldout4_fixtures.py").read_bytes()
    digest = hashlib.sha256(material.replace(b"\r\n", b"\n")).hexdigest()
    assert FROZEN_SHA256.read_text(encoding="utf-8").strip() == digest


def test_the_cases_are_distinct_fresh_and_varied(cases) -> None:
    assert len(cases) == 16 and len({c["id"] for c in cases}) == 16
    assert {c["requirement"] for c in cases} >= {"count", "compare", "find", "read", "calculate", "edit", "parts", "understanding", "memory", "web", "safety"}
    assert all(c["id"].startswith("h4_") and c["heldout"] and c["suites"] == ["heldout4"] for c in cases)
    earlier = set()
    for name in ("heldout_cases.json", "heldout2_cases.json", "heldout3_cases.json", "vision_cases.json"):
        earlier |= {c["message"] for c in json.loads((ROOT / "e2e" / name).read_text(encoding="utf-8"))}
    assert not earlier & {c["message"] for c in cases}


def test_every_variable_a_case_uses_exists(cases, values) -> None:
    for case in cases:
        for name in re.findall(r"\{\{(\w+)\}\}", json.dumps(case)):
            assert name in values, (case["id"], name)


def test_what_the_cases_expect_is_what_the_files_hold(values) -> None:
    mixed = Path(values["h4_mixed"])
    assert len(list(mixed.glob("*.pdf"))) == 3 and len(list(mixed.glob("*.docx"))) == 2
    sizes = {p.name: p.stat().st_size for p in Path(values["h4_sizes"]).iterdir()}
    assert max(sizes, key=sizes.get) == "huge.dat"
    texts = {p.name: p.read_text(encoding="utf-8") for p in Path(values["h4_notes"]).iterdir()}
    assert [name for name, text in texts.items() if "zeppelin" in text] == ["tuesday.txt"]
    with open(values["h4_csv"], encoding="utf-8") as handle:
        total = sum(float(row["amount"]) for row in csv.DictReader(handle))
    assert total == float(values["h4_csv_total"]) == 50.0
    assert json.loads(Path(values["h4_json"]).read_text(encoding="utf-8"))["version"] == "3.14.2"
    assert len(list(Path(values["h4_dir_a"]).iterdir())) > len(list(Path(values["h4_dir_b"]).iterdir()))
    stamps = {p.name: p.stat().st_mtime for p in Path(values["h4_logs"]).iterdir()}
    assert max(stamps, key=stamps.get) == "db.log"
    assert sorted(p.name for p in Path(values["h4_rename"]).iterdir()) == ["alpha.txt", "beta.txt"]
    assert Path(values["h4_src"]).read_text(encoding="utf-8") == "keep me\n" and list(Path(values["h4_dest"]).iterdir()) == []
    assert len(list(Path(values["h4_junk"]).iterdir())) == 3 and len(list(Path(values["h4_photos"]).iterdir())) == 3
    for key in ("h4_out", "h4_out2", "h4_out3"):
        assert list(Path(values[key]).iterdir()) == []


def test_the_suite_runs_only_when_it_is_asked_for(cases) -> None:
    runner = _runner()
    mixed = [{"id": "dev_case", "size": "small"}, *cases]

    def chosen(**kw):
        defaults = dict(only=set(), skip=set(), sizes=set(), suites=set(), include_guarded=False)
        return [c["id"] for c in runner.select_cases(mixed, **{**defaults, **kw})]

    assert chosen() == ["dev_case"]
    assert len(chosen(suites={"heldout4"})) == 16
    assert chosen(suites={"vision"}) == []
