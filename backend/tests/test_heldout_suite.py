"""The held-out suite: 60 everyday requests that measure YBM and are never used to tune it."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CASES_PATH = ROOT / "e2e" / "heldout_cases.json"
FROZEN_SHA256 = ROOT / "e2e" / "heldout_cases.sha256"

sys.path.insert(0, str(ROOT / "e2e"))
import gap_fixtures  # noqa: E402
import heldout_fixtures as fx  # noqa: E402


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
    return fx.prepare_heldout_fixtures(tmp_path_factory.mktemp("heldout"), gap_fixtures.remove_tree)


def test_the_suite_is_frozen() -> None:
    """Changing a held-out case or its files is a deliberate act: update heldout_cases.sha256 in the same change, and say
    why in the commit. A case edited to match what YBM does today stops measuring anything."""
    material = CASES_PATH.read_bytes() + (ROOT / "e2e" / "heldout_fixtures.py").read_bytes()
    digest = hashlib.sha256(material.replace(b"\r\n", b"\n")).hexdigest()
    assert FROZEN_SHA256.read_text(encoding="utf-8").strip() == digest


def test_there_are_sixty_distinct_cases_across_every_kind_of_request(cases) -> None:
    assert len(cases) == 60 and len({c["id"] for c in cases}) == 60
    assert {c["requirement"] for c in cases} == {
        "files", "documents", "delivery", "schedule", "calculation", "memory", "followup", "safety", "multistep",
    }
    assert all(c["id"].startswith("ho_") and c["heldout"] and c["suites"] == ["heldout"] for c in cases)


def test_every_variable_a_case_uses_exists(cases, values) -> None:
    web = {"fixture_base_url", "episode_url"}  # filled by the runner's local web server
    for case in cases:
        text = json.dumps(case)
        for name in re.findall(r"\{\{(\w+)\}\}", text):
            assert name in values or name in web, (case["id"], name)
        if "{{episode_url}}" in text or "{{fixture_base_url}}" in text:
            assert "local_web_fixture" in case["setup"], case["id"]


def test_every_file_a_case_names_is_there(cases, values) -> None:
    for name, path in values.items():
        if name.startswith("ho_") and name not in ("ho_work",):
            assert Path(path).exists(), name


def test_what_the_cases_expect_is_what_the_files_hold(values) -> None:
    root = Path(values["ho_root"])
    assert len(list(Path(values["ho_notes"]).glob("*.txt"))) == 7
    assert "oat milk" in Path(values["ho_groceries"]).read_text(encoding="utf-8")
    assert sum(score for _name, score in fx.SCORES) / len(fx.SCORES) == 79.2
    assert max(fx.SCORES, key=lambda row: row[1])[0] == "Cleo"
    assert sum(amount for _item, amount in fx.AMOUNTS) == 49.75
    assert [name for name, count in fx.STOCK if count < 20] == ["gears"] and fx.STOCK[1][1] == 17
    assert sorted(path.stat().st_size for path in Path(values["ho_sizes"]).iterdir()) == [10, 2000, 50000]
    stamps = {path.name: path.stat().st_mtime for path in Path(values["ho_dated"]).iterdir()}
    assert max(stamps, key=stamps.get) == "latest.md"
    assert (root / "unique" / "deep" / "unique-note-7731.txt").read_text(encoding="utf-8").startswith("Marker: MARIGOLD-7731")
    assert "tangerine" in (Path(values["ho_projects"]) / "beta" / "kitchen-plan.txt").read_text(encoding="utf-8")
    assert fx.SECRET_KEY in Path(values["ho_secrets"]).read_text(encoding="utf-8")


def test_the_office_files_are_readable_by_the_tool(values) -> None:
    from agent_control.tools.office_text import office_text

    assert "Priya Raman" in office_text(Path(values["ho_docx"]))
    assert "B3=17" in office_text(Path(values["ho_stock_xlsx"])) and office_text(Path(values["ho_stock_xlsx"])).startswith("Sheet Stock:")
    assert "A4=toner" in office_text(Path(values["ho_expenses_xlsx"]))
    assert office_text(Path(values["ho_pptx"])).splitlines() == [f"Slide {n}: {title}" for n, title in enumerate(fx.SLIDE_TITLES, start=1)]
    assert "invoices/jan.pdf" in office_text(Path(values["ho_zip"]))


def test_a_held_out_case_runs_only_when_asked_for(cases) -> None:
    runner = _runner()
    mixed = [{"id": "dev_case", "size": "small"}, *cases]

    def chosen(**kw):
        defaults = dict(only=set(), skip=set(), sizes=set(), suites=set(), include_guarded=False)
        return {c["id"] for c in runner.select_cases(mixed, **{**defaults, **kw})}

    assert chosen() == {"dev_case"}
    assert chosen(suites={"full"}) == {"dev_case"}
    assert chosen(suites={"smoke"}) == {"dev_case"}
    assert len(chosen(suites={"heldout"})) == 60
    assert chosen(only={"ho_read_list"}) == {"ho_read_list"}


def test_a_reply_must_hold_every_needle_of_a_contains_all_assertion() -> None:
    runner = _runner()
    case = {"assertions": {"final_status_in": ["completed"], "bot_reply_contains_all": ["alpha", "beta"]}}

    def turn(text):
        return runner.TurnResult(
            final_status="completed", bot_reply_text=text,
            telegram_sent_events=[{"payload": {"kind": "text", "text": text}}], telegram_confirmed_text=text,
        )

    assert runner._diagnose_turn(case, turn("Alpha and Beta"), is_followup=False)[0] is True
    passed, reason = runner._diagnose_turn(case, turn("Alpha only"), is_followup=False)
    assert passed is False and "beta" in reason


def test_a_case_about_a_plain_question_follows_a_task_if_one_appears_and_a_chat_reply_if_not(cases) -> None:
    runner = _runner()
    assert runner.expects_task_mode(True) is True and runner.expects_task_mode(False) is False
    assert runner.expects_task_mode("auto") == "auto" and runner.expects_task_mode(" AUTO ") == "auto"
    by_id = {c["id"]: c for c in cases}
    for cid in ("ho_calc_percent", "ho_calc_compound", "ho_fibonacci", "ho_unit_convert", "ho_date_diff"):
        assert by_id[cid]["expects_task"] == "auto", cid
    # The remember statement must stay a plain chat turn: the runner's memory cleanup finds its fact by that flag.
    remember = by_id["ho_remember_name"]
    assert remember["expects_task"] is False and remember["follow_ups"][0]["expects_task"] == "auto"
