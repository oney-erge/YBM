"""The scorecard shows the same case across runs, so a score is only ever compared with itself (scripts/e2e_scorecard.py)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _scorecard():
    spec = importlib.util.spec_from_file_location("e2e_scorecard", ROOT / "scripts" / "e2e_scorecard.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _run(results: Path, name: str, stages: list[dict] | str) -> None:
    folder = results / f"run_{name}"
    folder.mkdir(parents=True)
    (folder / "summary.json").write_text(stages if isinstance(stages, str) else json.dumps(stages), encoding="utf-8")


def _stage(case_id: str, passed: bool, seconds: float = 10.0, skipped: str | None = None) -> dict:
    return {"case_id": case_id, "passed": passed, "total_duration_s": seconds, "skipped": skipped}


def test_each_case_keeps_its_own_history_oldest_first_and_skipped_runs_do_not_count(tmp_path) -> None:
    card = _scorecard()
    _run(tmp_path, "20261113_100000", [_stage("v_steam", True, 15), _stage("v_hidden", True, 36)])
    _run(tmp_path, "20261114_100000", [_stage("v_steam", True, 16), _stage("v_hidden", False, 670, skipped="needs a fixture")])
    _run(tmp_path, "20261114_150000", [_stage("v_steam", False, 104), _stage("v_hidden", True, 57)])
    _run(tmp_path, "20261114_160000", "not json")  # a half-written run is ignored, not fatal

    history = card.load_history(tmp_path)

    assert [(item.run, item.result) for item in history["v_steam"].outcomes] == [("20261113_100000", "P"), ("20261114_100000", "P"), ("20261114_150000", "F")]
    assert history["v_steam"].latest == "F"
    assert [item.result for item in history["v_hidden"].recent(5)] == ["P", "P"]  # the skipped run is not a result
    assert history["v_steam"].flaky() and not history["v_hidden"].flaky()


def test_a_suite_is_rendered_with_what_it_is_for_and_what_never_ran(tmp_path) -> None:
    card = _scorecard()
    cases = tmp_path / "cases"
    cases.mkdir()
    (cases / "vision_cases.json").write_text(json.dumps([{"id": "v_a"}, {"id": "v_b"}, {"id": "v_c"}]), encoding="utf-8")
    _run(tmp_path / "results", "20261114_100000", [_stage("v_a", True), _stage("v_b", False)])

    text = card.render(card.load_history(tmp_path / "results"), ["vision"], cases_dir=cases)

    assert "## vision (regression check): latest run of each case 1 of 2 passing, 3 cases in the suite" in text
    assert "| `v_a` | P 10s | P |  |" in text and "| `v_b` | F 10s | F |  |" in text and "| `v_c` | never run | - | - |" in text
    failing = card.render(card.load_history(tmp_path / "results"), ["vision"], failing_only=True, cases_dir=cases)
    assert "`v_a`" not in failing and "`v_b`" in failing


def test_the_runner_records_when_the_answer_reached_the_person(tmp_path) -> None:
    """A chat reply showed 67 s in the summaries: the harness waits that long to see whether a task starts. The answer went out in seconds."""
    import test_e2e_runner as runner_tests

    runner = runner_tests._runner_module()
    turn = runner.TurnResult(label="initial", message="hi", task_id=None, final_status="chat", duration_s=67.0)
    events = [
        {"time": "2026-11-14T20:00:02+00:00", "payload": {"kind": "text", "text": "On it - I'll send the result here when it's done."}},
        {"time": "2026-11-14T20:00:03+00:00", "payload": {"kind": "edit", "text": "Working on it"}},
        {"time": "2026-11-14T20:00:06+00:00", "payload": {"kind": "text", "text": "The answer."}},
    ]
    runner.fetch_message_sent_events = lambda _start: events
    runner._capture_sent_messages(turn, "2026-11-14T20:00:00+00:00")

    assert turn.first_sign_s == 2.0 and turn.answer_s == 6.0  # the edit at 3 s is progress, the text at 6 s is the answer
