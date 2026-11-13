"""The third held-out suite: open-ended requests about the computer, editing, memory, workflows and honesty, written once and run once."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CASES_PATH = ROOT / "e2e" / "heldout3_cases.json"
FROZEN_SHA256 = ROOT / "e2e" / "heldout3_cases.sha256"

sys.path.insert(0, str(ROOT / "e2e"))
import gap_fixtures  # noqa: E402
import heldout3_fixtures as fx  # noqa: E402


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
    return fx.prepare_heldout3_fixtures(tmp_path_factory.mktemp("heldout3"), gap_fixtures.remove_tree)


def test_the_suite_is_frozen() -> None:
    """Changing a case or its files after the suite has been run makes the run meaningless: update heldout3_cases.sha256 in the same
    change and say why. A case edited to match what YBM does today stops measuring anything."""
    material = CASES_PATH.read_bytes() + (ROOT / "e2e" / "heldout3_fixtures.py").read_bytes()
    digest = hashlib.sha256(material.replace(b"\r\n", b"\n")).hexdigest()
    assert FROZEN_SHA256.read_text(encoding="utf-8").strip() == digest


def test_the_cases_are_distinct_fresh_and_cover_every_kind_of_request(cases) -> None:
    assert len(cases) == 16 and len({c["id"] for c in cases}) == 16
    assert {c["requirement"] for c in cases} == {"computer", "files", "editing", "memory", "workflow", "safety", "build"}
    assert all(c["id"].startswith("h3_") and c["heldout"] and c["suites"] == ["heldout3"] for c in cases)
    earlier = set()
    for name in ("heldout_cases.json", "heldout2_cases.json"):
        earlier |= {c["message"] for c in json.loads((ROOT / "e2e" / name).read_text(encoding="utf-8"))}
    assert not earlier & {c["message"] for c in cases}


def test_every_variable_a_case_uses_exists(cases, values) -> None:
    for case in cases:
        for name in re.findall(r"\{\{(\w+)\}\}", json.dumps(case)):
            assert name in values, (case["id"], name)


def test_every_file_a_case_names_is_there(values) -> None:
    for name, path in values.items():
        if name in {"h3_find_name", "h3_drive", "h3_steam_count", "h3_steam_installed"} or name.startswith("h3_free_"):
            continue
        if name.startswith("h3_") and name != "h3_work":
            assert Path(path).exists(), name


def test_what_the_cases_expect_is_what_the_files_hold(values) -> None:
    from agent_control.tools.office_text import office_text

    assert "tide" in office_text(Path(values["h3_docx"]))
    assert Path(values["h3_big"]).stat().st_size > Path(values["h3_small"]).stat().st_size
    stamps = {p.name: p.stat().st_mtime for p in Path(values["h3_dated"]).iterdir()}
    assert max(stamps, key=stamps.get) == "memo-2024.txt"
    assert len(Path(values["h3_words"]).read_text(encoding="utf-8").split()) == 13
    assert Path(values["h3_fruit"]).read_text(encoding="utf-8") == "apples\nbananas\n"
    assert sorted(p.name for p in Path(values["h3_imgs"]).iterdir()) == ["keep.txt", "shot-1.png", "shot-2.png", "shot-3.png"]
    assert len(list(Path(values["h3_wf_dir"]).iterdir())) == 4
    assert len(list(Path(values["h3_junk"]).iterdir())) == 3
    assert (Path(values["h3_root"]) / "deep" / "er" / "folder" / values["h3_find_name"]).exists()


def test_the_answers_that_depend_on_this_computer_are_computed_not_typed(values) -> None:
    count = fx.steam_game_count()
    assert values["h3_steam_count"] == (str(count) if count is not None else "0")
    free = fx.free_disk_gb(Path(values["h3_root"]).anchor or "/")
    assert int(values["h3_free_a"]) + 1 == int(values["h3_free_b"]) == int(values["h3_free_c"]) - 1
    assert abs(int(values["h3_free_b"]) - int(free)) <= 1


def test_each_held_out_suite_runs_only_when_it_is_asked_for(cases) -> None:
    runner = _runner()
    mixed = [{"id": "dev_case", "size": "small"}, *cases]

    def chosen(**kw):
        defaults = dict(only=set(), skip=set(), sizes=set(), suites=set(), include_guarded=False)
        return [c["id"] for c in runner.select_cases(mixed, **{**defaults, **kw})]

    assert chosen() == ["dev_case"]
    assert len(chosen(suites={"heldout3"})) == 16
    assert chosen(suites={"heldout2"}) == []
