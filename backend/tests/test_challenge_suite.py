"""The challenge suite: requests written to find where YBM stops being good. Open: a failure is a finding, the fix goes in the product."""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CASES_PATH = ROOT / "e2e" / "challenge_cases.json"

sys.path.insert(0, str(ROOT / "e2e"))
import challenge_fixtures as fx  # noqa: E402
import gap_fixtures  # noqa: E402


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
    return fx.prepare_challenge_fixtures(tmp_path_factory.mktemp("challenge"), gap_fixtures.remove_tree)


def test_every_case_says_how_to_judge_it(cases) -> None:
    assert len(cases) == 39 and len({c["id"] for c in cases}) == 39
    assert {c["requirement"] for c in cases} == {"acquire", "plan", "honesty", "invent", "conversation", "chaos", "self"}
    for case in cases:
        assert case["id"].startswith("c_") and case["heldout"] and case["suites"] == ["challenge"] and case["claims"]
        assert case["rubric"], case["id"]  # a person judges what a script cannot
        assert case["assertions"]["final_status_in"], case["id"]
        if case.get("manual"):
            assert len(case["manual"]) > 20, case["id"]  # a case that is not run says why


def test_every_variable_a_case_uses_exists(cases, values) -> None:
    for case in cases:
        for name in re.findall(r"\{\{(\w+)\}\}", json.dumps(case)):
            assert name in values, (case["id"], name)


def test_what_the_cases_expect_is_what_the_files_hold(values) -> None:
    from PIL import Image, ImageFilter, ImageStat

    def sharpness(path: str) -> float:
        return float(ImageStat.Stat(Image.open(path).convert("L").filter(ImageFilter.FIND_EDGES)).var[0])

    pics = Path(values["c_pictures"])
    scores = {p.stem: sharpness(str(p)) for p in pics.iterdir()}
    blurry = {name for name, score in scores.items() if score < 0.2 * max(scores.values())}
    assert blurry == {"IMG_0102", "IMG_0105", "IMG_0106"}
    downloads = Path(values["c_downloads"])
    by_content: dict[bytes, list[str]] = {}
    for p in downloads.iterdir():
        by_content.setdefault(p.read_bytes(), []).append(p.name)
    assert sorted(len(v) for v in by_content.values()) == [1, 2, 3]
    latest = max(Path(values["c_invoices"]).glob("invoice-*.txt"), key=lambda p: p.stat().st_mtime)
    assert values["c_latest_total"] in latest.read_text(encoding="utf-8")
    names = sorted(p.name for p in Path(values["c_documents"]).iterdir())
    assert names == ["resume.docx", "resume.pdf"]
    assert sum(1 for p in Path(values["c_temp_area"]).iterdir()) == 4


def test_the_runner_asks_for_the_suite_only_by_name(cases) -> None:
    runner = _runner()
    mixed = [{"id": "dev_case", "size": "small"}, *cases]

    def chosen(**kw):
        defaults = dict(only=set(), skip=set(), sizes=set(), suites=set(), include_guarded=False)
        return [c["id"] for c in runner.select_cases(mixed, **{**defaults, **kw})]

    assert chosen() == ["dev_case"]
    assert len(chosen(suites={"challenge"})) == 39
