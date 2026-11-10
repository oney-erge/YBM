"""The run-comparison tool states a pass count with its uncertainty."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _tool():
    spec = importlib.util.spec_from_file_location("e2e_compare", ROOT / "scripts" / "e2e_compare.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_ninety_percent_of_sixty_only_shows_above_about_eighty() -> None:
    low, high = _tool().wilson(54, 60)
    assert 0.79 < low < 0.81 and high > 0.94


def test_the_interval_narrows_with_more_cases() -> None:
    tool = _tool()
    low_small, _ = tool.wilson(9, 10)
    low_large, _ = tool.wilson(90, 100)
    assert low_large > low_small and tool.wilson(0, 0) == (0.0, 0.0)


def test_mcnemar_counts_only_the_cases_that_changed() -> None:
    tool = _tool()
    assert tool.mcnemar_exact(0, 0) == 1.0
    assert tool.mcnemar_exact(5, 5) == 1.0
    assert abs(tool.mcnemar_exact(0, 6) - 0.03125) < 1e-9  # six cases all one way


def test_two_runs_are_compared_case_by_case(tmp_path, capsys, monkeypatch) -> None:
    for name, rows in (
        ("run_a", [{"case_id": "x", "passed": False, "failure_reason": "blocked"}, {"case_id": "y", "passed": True}]),
        ("run_b", [{"case_id": "x", "passed": True}, {"case_id": "y", "passed": True}, {"case_id": "z", "passed": False, "failure_reason": "late"}]),
    ):
        (tmp_path / name).mkdir()
        (tmp_path / name / "summary.json").write_text(json.dumps(rows), encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["e2e_compare", "run_a", "run_b", "--root", str(tmp_path)])

    assert _tool().main() == 0
    out = capsys.readouterr().out
    assert "x  FP" in out and "y  PP" in out and "z  -F  late" in out
    assert "run_a: 1/2 = 50%" in out and "run_b: 2/3 = 67%" in out
    assert "better 1, worse 0" in out
