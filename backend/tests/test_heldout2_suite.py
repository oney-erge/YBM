"""The second held-out suite: 40 fresh everyday requests, written once and run once on the code as it stood."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CASES_PATH = ROOT / "e2e" / "heldout2_cases.json"
FROZEN_SHA256 = ROOT / "e2e" / "heldout2_cases.sha256"

sys.path.insert(0, str(ROOT / "e2e"))
import gap_fixtures  # noqa: E402
import heldout2_fixtures as fx  # noqa: E402


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
    return fx.prepare_heldout2_fixtures(tmp_path_factory.mktemp("heldout2"), gap_fixtures.remove_tree)


def test_the_suite_is_frozen() -> None:
    """Changing a case or its files after the suite has been run makes the run meaningless: update heldout2_cases.sha256 in the same
    change and say why. A case edited to match what YBM does today stops measuring anything."""
    material = CASES_PATH.read_bytes() + (ROOT / "e2e" / "heldout2_fixtures.py").read_bytes()
    digest = hashlib.sha256(material.replace(b"\r\n", b"\n")).hexdigest()
    assert FROZEN_SHA256.read_text(encoding="utf-8").strip() == digest


def test_there_are_forty_distinct_fresh_cases_across_every_kind_of_request(cases) -> None:
    assert len(cases) == 40 and len({c["id"] for c in cases}) == 40
    assert {c["requirement"] for c in cases} == {
        "files", "documents", "delivery", "schedule", "calculation", "memory", "followup", "safety", "multistep",
    }
    assert all(c["id"].startswith("h2_") and c["heldout"] and c["suites"] == ["heldout2"] for c in cases)
    first = {c["id"] for c in json.loads((ROOT / "e2e" / "heldout_cases.json").read_text(encoding="utf-8"))}
    assert not first & {c["id"] for c in cases}
    first_messages = {c["message"] for c in json.loads((ROOT / "e2e" / "heldout_cases.json").read_text(encoding="utf-8"))}
    assert not first_messages & {c["message"] for c in cases}


def test_every_variable_a_case_uses_exists(cases, values) -> None:
    web = {"fixture_base_url", "episode_url", "form_url"}  # filled by the runner's local web server
    for case in cases:
        text = json.dumps(case)
        for name in re.findall(r"\{\{(\w+)\}\}", text):
            assert name in values or name in web, (case["id"], name)
        if "{{form_url}}" in text:
            assert "local_web_fixture" in case["setup"], case["id"]


def test_every_file_a_case_names_is_there(values) -> None:
    for name, path in values.items():
        if name.startswith("h2_") and name != "h2_work":
            assert Path(path).exists(), name


def test_what_the_cases_expect_is_what_the_files_hold(values) -> None:
    root = Path(values["h2_root"])
    assert "passport" in Path(values["h2_todo"]).read_text(encoding="utf-8")
    assert sorted(p.name for p in Path(values["h2_docs_md"]).glob("*.md")) == ["roadmap.md", "standup-notes.md"]
    assert sorted(p.stat().st_size for p in Path(values["h2_sizes"]).iterdir())[0] == 7
    stamps = {p.name: p.stat().st_mtime for p in Path(values["h2_dated"]).iterdir()}
    assert min(stamps, key=stamps.get) == "archive-2019.txt"
    assert "cardamom" in (Path(values["h2_projects"]) / "kitchen" / "spice-order.txt").read_text(encoding="utf-8")
    assert len(Path(values["h2_poem"]).read_text(encoding="utf-8").splitlines()) == 6
    assert "port: 8443" in Path(values["h2_yaml"]).read_text(encoding="utf-8")
    assert json.loads(Path(values["h2_json"]).read_text(encoding="utf-8"))["users"][1]["city"] == "Lisbon"
    assert max(fx.TEMPS, key=lambda row: row[1])[0] == "Cairo" and sum(h for _d, h in fx.HOURS) == 37.5
    assert sum(fx.MINUTES) / len(fx.MINUTES) == 37.5 and fx.SLIDES[1] == "Budget Overview"
    note = (root / "deep" / "inner" / "notes-tuesday.txt").read_text(encoding="utf-8")
    assert len(note.split()) == 12
    assert "Tr0ub4dor-NOTAREALKEY-55" in (Path(values["h2_env"])).read_text(encoding="utf-8")
    counts = sorted(len(p.read_text(encoding="utf-8").split()) for p in Path(values["h2_wc"]).iterdir())
    assert counts == [4, 9, 31]


def test_the_expected_answers_are_arithmetically_right() -> None:
    assert round(84 * 0.85, 2) == 71.4
    from datetime import date

    assert (date(2026, 4, 2) - date(2026, 1, 20)).days == 72
    assert round(5 * 1.609344, 2) == 8.05


def test_the_office_files_are_readable_by_the_tool(values) -> None:
    from agent_control.tools.office_text import office_text

    assert "October 14" in office_text(Path(values["h2_docx"]))
    assert "A3=Cairo  B3=36" in office_text(Path(values["h2_xlsx"])) and office_text(Path(values["h2_xlsx"])).startswith("Sheet Temps:")
    assert office_text(Path(values["h2_pptx"])).splitlines()[1] == "Slide 2: Budget Overview"
    assert "contract.pdf" in office_text(Path(values["h2_zip"]))


def test_each_held_out_suite_runs_only_when_it_is_asked_for(cases) -> None:
    runner = _runner()
    first = json.loads((ROOT / "e2e" / "heldout_cases.json").read_text(encoding="utf-8"))
    mixed = [{"id": "dev_case", "size": "small"}, *first, *cases]

    def chosen(**kw):
        defaults = dict(only=set(), skip=set(), sizes=set(), suites=set(), include_guarded=False)
        return {c["id"] for c in runner.select_cases(mixed, **{**defaults, **kw})}

    assert chosen() == {"dev_case"} and chosen(suites={"full"}) == {"dev_case"} and chosen(suites={"smoke"}) == {"dev_case"}
    assert chosen(suites={"heldout2"}) == {c["id"] for c in cases}
    assert len(chosen(suites={"heldout"})) == len(first)
    assert chosen(only={"h2_read_todo"}) == {"h2_read_todo"}
