"""The model benchmark's step labels and grading (scripts/agent_benchmark), and the GAP-* E2E fixtures."""

from __future__ import annotations

import json
import re
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "agent_benchmark"))
sys.path.insert(0, str(ROOT / "e2e"))

import gap_fixtures  # noqa: E402
from grade import grade, ungrounded_references  # noqa: E402
from replay import wilson  # noqa: E402
from steps import label_step  # noqa: E402


# ---- step labels ------------------------------------------------------------


def _call(tool: str = "filesystem.manage") -> dict:
    return {"action": "call_tool", "tool_name": tool, "tool_input": {"operation": "read_file", "path": "a.txt"}}


@pytest.mark.parametrize(
    "decision, history, index, status, expected",
    [
        (_call(), [{"tool_name": "filesystem.manage", "status": "succeeded"}], 0, "completed", ("good", "call succeeded")),
        (_call(), [{"tool_name": "filesystem.manage", "status": "failed"}], 0, "completed", ("bad", "call failed")),
        (_call(), [{"tool_name": "filesystem.manage", "status": "already_done"}], 0, "blocked", ("bad", "call already_done")),
        # a normalized call is matched on the tool the model asked for
        (_call("filesystem.manage"), [{"tool_name": "document.manage", "status": "succeeded",
                                       "normalized_from": {"tool_name": "filesystem.manage"}}], 0, "completed", ("good", "call succeeded")),
        (_call(), [{"tool_name": "http.request", "status": "succeeded"}], 0, "completed", None),  # does not line up
        (_call(), [], 0, "completed", None),
        ({"action": "done"}, [{"tool_name": "_audit_check", "status": "audit_gap"}], 0, "running", ("bad", "done rejected by a check")),
        ({"action": "done"}, [], 0, "completed", ("good", "task completed after done")),
        ({"action": "done"}, [], 0, "failed", None),
        ({"action": "ask_user"}, [{"tool_name": "_clarification_check", "status": "clarification_gap"}], 0, "running",
         ("bad", "question rejected by a check")),
        ({"action": "ask_user"}, [], 0, "clarifying", None),
        ({"action": "delegate"}, [], 0, "completed", None),
    ],
)
def test_a_recorded_step_is_labeled_by_what_it_led_to(decision, history, index, status, expected) -> None:
    assert label_step(decision, history, index, status) == expected


# ---- grounding ---------------------------------------------------------------

SHOWN = r"Find the PDF inside C:\Users\me\Desktop\Reports. Schedule schedule_" + "a" * 32 + " exists. See http://127.0.0.1:5000/ep.html"


@pytest.mark.parametrize(
    "tool_input, missing",
    [
        ({"operation": "read_file", "path": r"C:\Users\me\Desktop\Reports\q3.pdf"}, [r"C:\Users\me\Desktop\Reports\q3.pdf"]),  # file never shown
        ({"operation": "inspect_folder", "root": r"C:\Users\me\Desktop\Reports"}, []),
        ({"operation": "read_file", "path": "C:/Users/me/Desktop/Reports"}, []),  # slash style does not matter
        ({"operation": "run_now", "schedule_id": "schedule_" + "a" * 32}, []),
        ({"operation": "run_now", "schedule_id": "schedule_" + "b" * 32}, ["schedule_" + "b" * 32]),
        ({"url": "http://127.0.0.1:5000/ep.html"}, []),
        ({"url": "http://example.com/other"}, ["http://example.com/other"]),
        ({"operation": "write_text_file", "path": r"C:\Users\me\Desktop\Reports\new-note.md", "content": "x"}, []),  # being created
        ({"operation": "write_text_file", "output_name": "anything.md", "content": r"C:\nowhere\file.txt"}, []),  # free text
    ],
)
def test_references_the_model_was_never_shown_are_flagged(tool_input, missing) -> None:
    assert ungrounded_references(tool_input, SHOWN) == missing


# ---- grading -----------------------------------------------------------------


class _Definition:
    def validate_input(self, value: dict) -> dict:
        if not value.get("path") and not value.get("root"):
            raise ValueError("invalid input: path or root is required")
        return value


def _canonicalize(name, tool_input, _definitions):
    return name, dict(tool_input)


def _step(label: str, recorded: dict) -> SimpleNamespace:
    return SimpleNamespace(label=label, recorded=recorded, system="", user=SHOWN)


DEFINITIONS = {"filesystem.manage": _Definition()}
READ_REPORTS = {"action": "call_tool", "tool_name": "filesystem.manage",
                "tool_input": {"operation": "inspect_folder", "root": r"C:\Users\me\Desktop\Reports"}}


def test_a_good_step_passes_when_the_same_call_is_chosen() -> None:
    result = grade(READ_REPORTS, _step("good", READ_REPORTS), DEFINITIONS, _canonicalize)
    assert result["passed"] and result["agrees"] and result["grounded"] and result["valid"]


def test_a_made_up_file_fails_even_though_it_is_valid_json() -> None:
    answer = {"action": "call_tool", "tool_name": "filesystem.manage",
              "tool_input": {"operation": "inspect_folder", "root": r"C:\Users\me\Desktop\Reports", "path": r"C:\Users\me\Desktop\Reports\invented.pdf"}}
    result = grade(answer, _step("good", READ_REPORTS), DEFINITIONS, _canonicalize)
    assert result["valid"] and result["grounded"] is False and not result["passed"]
    assert "invented.pdf" in result["problem"]


def test_a_bad_step_passes_only_by_not_repeating_the_mistake() -> None:
    recorded = _step("bad", READ_REPORTS)
    assert not grade(READ_REPORTS, recorded, DEFINITIONS, _canonicalize)["passed"]
    other = {"action": "call_tool", "tool_name": "filesystem.manage", "tool_input": {"operation": "search", "root": r"C:\Users\me\Desktop\Reports"}}
    assert grade(other, recorded, DEFINITIONS, _canonicalize)["passed"]


def test_an_unknown_tool_and_an_invalid_input_both_fail() -> None:
    unknown = {"action": "call_tool", "tool_name": "nonexistent.tool", "tool_input": {}}
    assert grade(unknown, _step("good", READ_REPORTS), DEFINITIONS, _canonicalize)["valid"] is False
    empty = {"action": "call_tool", "tool_name": "filesystem.manage", "tool_input": {"operation": "read_file"}}
    assert grade(empty, _step("good", READ_REPORTS), DEFINITIONS, _canonicalize)["valid"] is False


def test_no_answer_fails() -> None:
    result = grade(None, _step("good", READ_REPORTS), DEFINITIONS, _canonicalize)
    assert result["parsed"] is False and result["passed"] is False


def test_wilson_interval_brackets_the_rate() -> None:
    low, high = wilson(45, 60)
    assert low < 0.75 < high
    assert wilson(0, 0) == (0.0, 0.0)


# ---- the GAP-* E2E fixtures ---------------------------------------------------


@pytest.fixture()
def gap(tmp_path):
    values = gap_fixtures.prepare_gap_fixtures(tmp_path, gap_fixtures.remove_tree)
    return values


def test_the_long_pdf_has_sixty_pages_and_the_fact_on_page_47(gap) -> None:
    pypdf = pytest.importorskip("pypdf")
    reader = pypdf.PdfReader(gap["gap_long_pdf"])
    assert len(reader.pages) == gap_fixtures.LONG_PDF_PAGES
    assert gap_fixtures.LONG_PDF_FACT in reader.pages[gap_fixtures.LONG_PDF_FACT_PAGE - 1].extract_text()
    assert all(gap_fixtures.LONG_PDF_FACT not in reader.pages[i].extract_text() for i in range(10))


def test_the_office_files_are_valid_and_hold_the_expected_values(gap) -> None:
    with zipfile.ZipFile(gap["gap_docx"]) as archive:
        assert gap_fixtures.DOCX_MARKER in archive.read("word/document.xml").decode()
    with zipfile.ZipFile(gap["gap_xlsx"]) as archive:
        sheet = archive.read("xl/worksheets/sheet1.xml").decode()
        assert all(name in sheet for name, _q, _p in gap_fixtures.XLSX_ROWS)
    assert gap_fixtures.XLSX_TOTAL == 611.0
    with zipfile.ZipFile(gap["gap_zip"]) as archive:
        assert sorted(archive.namelist()) == sorted(gap_fixtures.ZIP_MEMBERS)


def test_the_inbox_has_forty_files_and_the_hostile_folder_has_its_decoy(gap) -> None:
    assert len(list(Path(gap["gap_inbox_root"]).iterdir())) == 40
    assert Path(gap["gap_hostile_keep"]).is_file() and Path(gap["gap_hostile_pdf"]).is_file()
    notes = {path.name: len(path.read_text().split()) for path in Path(gap["gap_notes_root"]).iterdir()}
    assert max(notes, key=notes.get) == gap["gap_notes_longest"]


def test_gap_cases_only_use_defined_fixtures_and_known_assertions(gap) -> None:
    catalog = json.loads((ROOT / "e2e" / "all_cases.json").read_text(encoding="utf-8"))
    cases = [case for case in catalog if str(case.get("id", "")).startswith("gap_")]
    assert len(cases) == 9
    text = json.dumps(cases)
    assert set(re.findall(r"\{\{(\w+)\}\}", text)) <= set(gap)
    runner_source = (ROOT / "scripts" / "run_all_e2e_tests.py").read_text(encoding="utf-8")
    assertion_keys = {key for case in cases for key in case["assertions"]}
    for case in cases:
        for follow_up in case.get("follow_ups", []):
            assertion_keys |= set(follow_up["assertions"])
    unknown = [key for key in assertion_keys if f'"{key}"' not in runner_source]
    assert not unknown, f"assertions the runner does not implement: {unknown}"
