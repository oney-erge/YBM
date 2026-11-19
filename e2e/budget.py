"""Pick the live cases worth running in a time budget, remember how long each took and how it went, and let one runner at a time use the bot.

A live case costs real minutes on the real model, so a change is checked with a budget (five minutes), not with a suite: the cases that failed last time,
then the cases in the areas the change touched, then whichever cases have waited longest. Every case is therefore run again within a few changes, and the
nightly slice (`nightly_slice`) is a different seventh of all of them each night. Nothing here talks to the bot; `scripts/run_all_e2e_tests.py` uses it.
"""

from __future__ import annotations

import json
import os
import statistics
import subprocess
import sys
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

KEEP_RUNS = 8  # runs remembered per case
USUAL_RUNS = 5  # the usual time of a case is the median of its last runs
DEFAULT_CAP_SECONDS = 90.0  # a case is not part of a budgeted check when it usually takes longer than this: it waits for the nightly slice
MONEY_RULE = ("hands_cart_stops_for_a_yes", "ult_buy_asks")  # first in line whenever the change touches code they cover
CHRONIC_AFTER = 3  # a case that failed this many runs in a row is known to be failing: it is checked in rotation and does not take the front of the line
AREAS_PATH = Path(__file__).with_name("areas.json")


# --- history -------------------------------------------------------------------------------------------------------------------------------------------


def load_history(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"cases": {}}
    return data if isinstance(data, dict) and isinstance(data.get("cases"), dict) else {"cases": {}}


def save_history(path: Path, history: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(history, indent=1, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


def record(history: dict[str, Any], case_id: str, *, passed: bool, seconds: float, at: str) -> None:
    runs = history["cases"].setdefault(case_id, {"runs": []})["runs"]
    runs.append({"at": at, "ok": bool(passed), "seconds": round(float(seconds), 1)})
    del runs[:-KEEP_RUNS]


def seed_from_results(history: dict[str, Any], results_root: Path) -> int:
    """Fill an empty history from the summary.json of the runs already on disk (oldest first). Returns the number of case runs read."""
    read = 0
    for summary in sorted(results_root.glob("run_*/summary.json")):
        try:
            rows = json.loads(summary.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict) or not row.get("case_id") or row.get("skipped"):
                continue
            record(history, str(row["case_id"]), passed=bool(row.get("passed")), seconds=float(row.get("total_duration_s") or 0.0), at=str(row.get("started_at") or ""))
            read += 1
    return read


def usual_seconds(case: dict[str, Any], history: dict[str, Any], *, cap: float = DEFAULT_CAP_SECONDS) -> float:
    runs = [run["seconds"] for run in history["cases"].get(str(case.get("id")), {}).get("runs", [])[-USUAL_RUNS:] if run.get("seconds")]
    if runs:
        return float(statistics.median(runs))
    return min(float(case.get("timeout_seconds") or 360) / 4.0, cap)


def chronic(case: dict[str, Any], history: dict[str, Any]) -> bool:
    runs = history["cases"].get(str(case.get("id")), {}).get("runs") or []
    return len(runs) >= CHRONIC_AFTER and not any(run["ok"] for run in runs[-CHRONIC_AFTER:])


def last_run(case: dict[str, Any], history: dict[str, Any]) -> dict[str, Any] | None:
    runs = history["cases"].get(str(case.get("id")), {}).get("runs") or []
    return runs[-1] if runs else None


# --- areas ---------------------------------------------------------------------------------------------------------------------------------------------


def load_areas(path: Path = AREAS_PATH) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def changed_areas(paths: Iterable[str], areas: dict[str, Any]) -> set[str]:
    """The areas whose code the changed files belong to. A change to the shared core touches every area."""
    touched: set[str] = set()
    for raw in paths:
        path = raw.replace("\\", "/")
        if any(path.startswith(prefix) for prefix in areas.get("core", [])):
            return set(areas["areas"])
        for name, spec in areas["areas"].items():
            if any(path.startswith(prefix) for prefix in spec.get("paths", [])):
                touched.add(name)
    return touched


def case_areas(case: dict[str, Any], areas: dict[str, Any]) -> set[str]:
    tags = {str(tag) for tag in case.get("tags") or []} | {str(case.get("requirement") or "")}
    return {name for name, spec in areas["areas"].items() if tags & set(spec.get("tags", []))}


def git_changed_files(since: str, cwd: Path) -> list[str]:
    """Files changed from `since` (a ref) to the working tree, plus untracked ones."""
    def run(*args: str) -> list[str]:
        done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
        return [line for line in done.stdout.splitlines() if line.strip()]

    return sorted(set(run("diff", "--name-only", since)) | set(run("ls-files", "--others", "--exclude-standard")))


# --- choosing ------------------------------------------------------------------------------------------------------------------------------------------


def plan(
    cases: list[dict[str, Any]],
    history: dict[str, Any],
    *,
    budget_seconds: float,
    touched: set[str] | None = None,
    areas: dict[str, Any] | None = None,
    cap: float = DEFAULT_CAP_SECONDS,
) -> list[dict[str, Any]]:
    """The cases to run in the budget, in the order to run them. Always at least one."""
    areas = areas or load_areas()
    touched = touched or set()
    pool = [case for case in cases if usual_seconds(case, history, cap=cap) <= cap * 1.5]

    def rank(case: dict[str, Any]) -> tuple[int, int, int, str, str]:
        last = last_run(case, history)
        case_id = str(case.get("id"))
        in_touched_area = bool(touched & case_areas(case, areas))
        return (
            0 if case_id in MONEY_RULE and in_touched_area else 1,
            0 if last is not None and not last["ok"] and not chronic(case, history) else 1,  # failed last time (and not for a long time)
            0 if in_touched_area else 1,  # in a touched area
            str(last["at"]) if last else "",  # never run first, then the longest wait
            case_id,
        )

    chosen: list[dict[str, Any]] = []
    spent = 0.0
    for case in sorted(pool, key=rank):
        cost = usual_seconds(case, history, cap=cap)
        if chosen and spent + cost > budget_seconds:
            continue  # a cheaper one further down may still fit
        chosen.append(case)
        spent += cost
    return chosen


def nightly_slice(cases: list[dict[str, Any]], day: int, *, slices: int = 7) -> list[dict[str, Any]]:
    """A seventh of all the cases, a different seventh each day, so every case is run at least once a week. Stable: a case's slice is a function of its id."""
    return [case for case in cases if zlib.crc32(str(case.get("id")).encode("utf-8")) % slices == day % slices]


# --- one runner at a time --------------------------------------------------------------------------------------------------------------------------------


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        # os.kill(pid, 0) would end the process on Windows.
        done = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"], capture_output=True, text=True, check=False)
        return f'"{pid}"' in done.stdout
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def take_lock(path: Path) -> str | None:
    """Claim the bot for this runner. Returns None when claimed, or a sentence naming the runner that already has it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        held = json.loads(path.read_text(encoding="utf-8"))
        if _alive(int(held.get("pid", 0))) and int(held["pid"]) != os.getpid():
            return f"another live-check runner (pid {held['pid']}, started {held.get('started', '?')}) is using the bot; two runners cancel each other's tasks"
    except (OSError, ValueError, TypeError, KeyError):
        pass
    path.write_text(json.dumps({"pid": os.getpid(), "started": datetime.now(timezone.utc).isoformat(timespec="seconds")}), encoding="utf-8")
    return None


def drop_lock(path: Path) -> None:
    try:
        if json.loads(path.read_text(encoding="utf-8")).get("pid") == os.getpid():
            path.unlink()
    except (OSError, ValueError):
        pass
