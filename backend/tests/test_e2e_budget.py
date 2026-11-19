"""Choosing live cases in a time budget, the history that choice runs on, and one runner at a time (e2e/budget.py)."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "e2e"))
import budget  # noqa: E402  (the e2e folder is not a package)

AREAS = budget.load_areas()


def _case(case_id: str, *tags: str, timeout: int = 120, **extra) -> dict:
    return {"id": case_id, "tags": list(tags), "size": "small", "timeout_seconds": timeout, **extra}


def _history(**runs: list[tuple[bool, float]]) -> dict:
    history: dict = {"cases": {}}
    for case_id, outcomes in runs.items():
        for position, (ok, seconds) in enumerate(outcomes):
            budget.record(history, case_id, passed=ok, seconds=seconds, at=f"2026-11-1{position}T10:00:00")
    return history


def test_what_failed_last_time_goes_first_then_the_touched_areas_then_the_longest_wait() -> None:
    cases = [_case("quiet_files", "files"), _case("quiet_jobs", "ultimate"), _case("broken_browser", "hands"), _case("old_memory", "memory"), _case("new_one", "invent")]
    history = _history(
        quiet_files=[(True, 20)], quiet_jobs=[(True, 20)], broken_browser=[(False, 20)], old_memory=[(True, 20)],
    )
    history["cases"]["old_memory"]["runs"][0]["at"] = "2026-01-01T00:00:00"  # waited longest of those that have run
    ids = [c["id"] for c in budget.plan(cases, history, budget_seconds=600, touched={"files"}, areas=AREAS)]
    assert ids == ["broken_browser", "quiet_files", "new_one", "old_memory", "quiet_jobs"]  # failed, touched area, never run, then oldest first


def test_the_money_rule_leads_when_the_change_touches_code_it_covers_and_the_budget_is_kept() -> None:
    cases = [_case("hands_cart_stops_for_a_yes", "hands"), _case("a", "files"), _case("b", "files"), _case("c", "files")]
    history = _history(hands_cart_stops_for_a_yes=[(True, 60)], a=[(False, 50)], b=[(False, 50)], c=[(False, 50)])
    ids = [c["id"] for c in budget.plan(cases, history, budget_seconds=120, touched={"browser-and-computer"}, areas=AREAS)]
    assert ids == ["hands_cart_stops_for_a_yes", "a"]  # 60 + 50 fits in 120; a third would not
    # a change that does not touch it does not spend a quarter of the budget on it
    quiet = [c["id"] for c in budget.plan(cases, history, budget_seconds=120, touched={"files"}, areas=AREAS)]
    assert quiet[0] != "hands_cart_stops_for_a_yes"


def test_a_case_that_has_failed_many_runs_in_a_row_is_checked_in_rotation_not_first() -> None:
    cases = [_case("long_broken", "files"), _case("just_broke", "files"), _case("fine", "files")]
    history = _history(long_broken=[(False, 20)] * 3, just_broke=[(True, 20), (False, 20)], fine=[(True, 20)])
    assert budget.chronic(cases[0], history) and not budget.chronic(cases[1], history) and not budget.chronic(cases[2], history)
    ids = [c["id"] for c in budget.plan(cases, history, budget_seconds=600, areas=AREAS)]
    assert ids[0] == "just_broke"  # failed for the first time: look at once
    assert ids.index("long_broken") > ids.index("just_broke")  # known failing: not ahead of anything newly broken


def test_a_case_that_usually_takes_long_waits_for_the_nightly_slice_and_one_case_always_runs() -> None:
    slow = _case("slow", "files")
    history = _history(slow=[(True, 600), (True, 650)])
    assert budget.plan([slow], history, budget_seconds=300, areas=AREAS) == []  # over the cap: not part of a budgeted check
    only = _case("only", "files")
    assert [c["id"] for c in budget.plan([only], _history(only=[(True, 80)]), budget_seconds=10, areas=AREAS)] == ["only"]  # never fewer than one


def test_a_case_never_run_is_estimated_from_its_own_timeout_within_the_cap() -> None:
    assert budget.usual_seconds(_case("x", timeout=120), {"cases": {}}) == 30
    assert budget.usual_seconds(_case("x", timeout=3600), {"cases": {}}) == budget.DEFAULT_CAP_SECONDS
    assert budget.usual_seconds(_case("x"), _history(x=[(True, 10), (True, 12), (True, 400)])) == 12  # the median, so one slow run does not count


def test_the_history_keeps_the_last_runs_of_each_case_and_survives_a_missing_or_broken_file(tmp_path) -> None:
    history: dict = {"cases": {}}
    for number in range(budget.KEEP_RUNS + 4):
        budget.record(history, "x", passed=number % 2 == 0, seconds=number + 0.04, at=f"2026-11-19T10:{number:02d}:00")
    runs = history["cases"]["x"]["runs"]
    assert len(runs) == budget.KEEP_RUNS and runs[-1]["at"].endswith(f"{budget.KEEP_RUNS + 3:02d}:00")
    path = tmp_path / "state" / "history.json"
    budget.save_history(path, history)
    assert budget.load_history(path) == history
    assert budget.load_history(tmp_path / "missing.json") == {"cases": {}}
    path.write_text("not json", encoding="utf-8")
    assert budget.load_history(path) == {"cases": {}}


def test_an_empty_history_is_filled_from_the_runs_already_on_disk(tmp_path) -> None:
    for name, rows in {
        "run_1": [{"case_id": "a", "passed": True, "total_duration_s": 40.0, "started_at": "2026-11-18T09:00:00"}, {"case_id": "b", "passed": False, "total_duration_s": 9.0, "started_at": "2026-11-18T09:01:00", "skipped": "manual"}],
        "run_2": [{"case_id": "a", "passed": False, "total_duration_s": 30.0, "started_at": "2026-11-19T09:00:00"}],
    }.items():
        (tmp_path / name).mkdir()
        (tmp_path / name / "summary.json").write_text(json.dumps(rows), encoding="utf-8")
    (tmp_path / "run_3").mkdir()
    (tmp_path / "run_3" / "summary.json").write_text("{broken", encoding="utf-8")
    history: dict = {"cases": {}}
    assert budget.seed_from_results(history, tmp_path) == 2  # the skipped case and the broken file are not runs
    assert [run["ok"] for run in history["cases"]["a"]["runs"]] == [True, False] and "b" not in history["cases"]


def test_changed_files_map_to_areas_and_a_change_to_the_core_touches_all_of_them() -> None:
    assert budget.changed_areas(["backend/src/agent_control/tools/browser.py"], AREAS) == {"browser-and-computer"}
    assert budget.changed_areas(["backend\\src\\agent_control\\jobs\\survey.py", "docs/USING.md"], AREAS) == {"jobs"}
    assert budget.changed_areas(["README.md"], AREAS) == set()
    assert budget.changed_areas(["backend/src/agent_control/orchestration/worker.py"], AREAS) == set(AREAS["areas"])


def test_every_area_has_cases_and_every_case_tag_group_is_known() -> None:
    cases = []
    for path in sorted((ROOT / "e2e").glob("*_cases.json")) + [ROOT / "e2e" / "all_cases.json"]:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
        cases += [c for c in data if isinstance(c, dict) and c.get("id")]
    for name in AREAS["areas"]:
        assert any(name in budget.case_areas(c, AREAS) for c in cases), f"no live case belongs to the {name} area"


def test_the_nightly_slice_covers_every_case_once_a_week_and_never_changes_for_a_case() -> None:
    cases = [_case(f"case_{n}") for n in range(60)]
    week = [{c["id"] for c in budget.nightly_slice(cases, day)} for day in range(7)]
    assert set().union(*week) == {c["id"] for c in cases}
    assert sum(len(day) for day in week) == len(cases)  # no case in two slices
    assert week[3] == {c["id"] for c in budget.nightly_slice(cases, 10)}  # day 10 is the same seventh as day 3


def test_one_runner_at_a_time_uses_the_bot(tmp_path) -> None:
    lock = tmp_path / "e2e_runner.lock"
    assert budget.take_lock(lock) is None  # free: taken
    assert budget.take_lock(lock) is None  # this same process asking again is not another runner
    lock.write_text(json.dumps({"pid": os.getppid(), "started": "2026-11-19T10:00:00+00:00"}), encoding="utf-8")
    assert os.getppid() != os.getpid()
    busy = budget.take_lock(lock)
    assert busy is not None and "another live-check runner" in busy  # the parent process is alive and is not us
    lock.write_text(json.dumps({"pid": 2**22 + 12345, "started": "x"}), encoding="utf-8")
    assert budget.take_lock(lock) is None  # a dead runner's lock is taken over
    budget.drop_lock(lock)
    assert not lock.exists()
    lock.write_text(json.dumps({"pid": os.getppid()}), encoding="utf-8")
    budget.drop_lock(lock)
    assert lock.exists()  # a lock that is not ours is left alone


@pytest.mark.parametrize("pid", [0, -1])
def test_a_nonsense_process_id_is_not_alive(pid: int) -> None:
    assert budget._alive(pid) is False
