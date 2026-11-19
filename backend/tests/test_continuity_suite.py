"""The continuity suite (e2e/continuity_cases.json): the mechanisms behind the 2026-11-16 chain, in other topics."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "e2e"))
import continuity_fixtures as fx  # noqa: E402
import gap_fixtures  # noqa: E402


@pytest.fixture(scope="module")
def cases() -> list[dict]:
    return json.loads((ROOT / "e2e" / "continuity_cases.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def values(tmp_path_factory) -> dict[str, str]:
    return fx.prepare_continuity_fixtures(tmp_path_factory.mktemp("continuity"), gap_fixtures.remove_tree)


def test_the_cases_are_chains_in_the_suite_and_use_variables_that_exist(cases, values) -> None:
    assert len(cases) == 7 and len({c["id"] for c in cases}) == 7
    assert all(c["id"].startswith("cont_") and c["suites"] == ["continuity"] and c["follow_ups"] and c["rubric"] for c in cases)
    for case in cases:
        for name in re.findall(r"\{\{(\w+)\}\}", json.dumps(case)):
            assert name in values, (case["id"], name)


def test_the_word_count_the_case_expects_is_what_the_file_holds(values) -> None:
    assert len((Path(values["c_notes"]) / "draft.txt").read_text(encoding="utf-8").split()) == 31
    assert sorted(p.name for p in Path(values["c_mess"]).iterdir())[:2] == ["minutes.doc.txt", "photo-a.jpg.txt"]


def test_the_runner_knows_the_suite() -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location("run_all_e2e_tests", ROOT / "scripts" / "run_all_e2e_tests.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    assert sum(1 for case in module.load_cases() if "continuity" in (case.get("suites") or [])) == 7
