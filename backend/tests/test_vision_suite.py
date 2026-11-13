"""The vision suite: Oney's own examples of what YBM must do, written once, frozen, and run live on the real bot before a release."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CASES_PATH = ROOT / "e2e" / "vision_cases.json"
FROZEN_SHA256 = ROOT / "e2e" / "vision_cases.sha256"

sys.path.insert(0, str(ROOT / "e2e"))
import gap_fixtures  # noqa: E402
import vision_fixtures as fx  # noqa: E402


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
    return fx.prepare_vision_fixtures(tmp_path_factory.mktemp("vision"), gap_fixtures.remove_tree)


def test_the_suite_is_frozen() -> None:
    """Changing a case or its files after a run makes that run meaningless: update vision_cases.sha256 in the same change and say why.
    A case edited to match what YBM does today stops measuring anything."""
    material = CASES_PATH.read_bytes() + (ROOT / "e2e" / "vision_fixtures.py").read_bytes()
    digest = hashlib.sha256(material.replace(b"\r\n", b"\n")).hexdigest()
    assert FROZEN_SHA256.read_text(encoding="utf-8").strip() == digest


def test_the_cases_are_distinct_and_cover_what_was_asked_for(cases) -> None:
    assert len(cases) == 16 and len({c["id"] for c in cases}) == 16
    assert {c["requirement"] for c in cases} == {"computer", "files", "code", "parts", "web", "build", "editing", "memory", "coding_agent", "safety"}
    assert all(c["id"].startswith("v_") and c["heldout"] and c["suites"] == ["vision"] for c in cases)
    earlier = set()
    for name in ("heldout_cases.json", "heldout2_cases.json", "heldout3_cases.json"):
        earlier |= {c["message"] for c in json.loads((ROOT / "e2e" / name).read_text(encoding="utf-8"))}
    assert not earlier & {c["message"] for c in cases}


def test_every_variable_a_case_uses_exists(cases, values) -> None:
    for case in cases:
        for name in re.findall(r"\{\{(\w+)\}\}", json.dumps(case)):
            assert name in values, (case["id"], name)


def test_what_the_cases_expect_is_what_the_files_hold(values) -> None:
    from agent_control.tools.office_text import office_text

    assert "lighthouse" in office_text(Path(values["v_docx"]))
    total = sum(len(p.read_text(encoding="utf-8").splitlines()) for p in Path(values["v_texts"]).rglob("*.txt"))
    assert str(total) == values["v_text_lines"] == "15"
    short, long_ = (len(Path(values[k]).read_text(encoding="utf-8").split()) for k in ("v_c1", "v_c2"))
    assert long_ > short
    firsts = {p.stem: p.read_text(encoding="utf-8").splitlines()[0] for p in Path(values["v_reports"]).iterdir()}
    assert max(firsts, key=lambda name: len(firsts[name])) == "south"
    assert Path(values["v_todo"]).read_text(encoding="utf-8") == "buy milk\nbuy eggs\n"
    assert sorted(p.name for p in Path(values["v_photos"]).iterdir()) == ["keep.txt", "pic-1.png", "pic-2.png", "pic-3.png"]
    assert len(list(Path(values["v_junk"]).iterdir())) == 3
    assert (Path(values["v_root"]) / ".cache-data" / "archive" / "2026" / values["v_find_name"]).exists()
    assert list(Path(values["v_out"]).iterdir()) == [] and list(Path(values["v_agent_out"]).iterdir()) == []


def test_the_steam_answer_is_computed_here_not_typed(values) -> None:
    count = fx.steam_game_count()
    assert values["v_steam_count"] == (str(count) if count is not None else "0")


def test_the_suite_runs_only_when_it_is_asked_for(cases) -> None:
    runner = _runner()
    mixed = [{"id": "dev_case", "size": "small"}, *cases]

    def chosen(**kw):
        defaults = dict(only=set(), skip=set(), sizes=set(), suites=set(), include_guarded=False)
        return [c["id"] for c in runner.select_cases(mixed, **{**defaults, **kw})]

    assert chosen() == ["dev_case"]
    assert len(chosen(suites={"vision"})) == 16
    assert chosen(suites={"heldout3"}) == []
