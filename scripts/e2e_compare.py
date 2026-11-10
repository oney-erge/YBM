"""Compare end-to-end runs case by case.

    python scripts/e2e_compare.py run_20261110_053706 run_20261110_062401 [--root .agent_control/e2e_results] [--group requirement]

Each argument is a run folder name under the results root (or a path). Prints, per case, a mark for each run (P pass, F fail, - not
run) with the failure reason of the last run, then the pass count of each run with its 95% Wilson interval and the exact two-sided
McNemar p-value for the first run against the last on the cases both ran. The interval is why 54 of 60 shows "above about 80%" and
not "90%": it is the honest reading of a pass count.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path


def wilson(passed: int, total: int, z: float = 1.96) -> tuple[float, float]:
    if total == 0:
        return (0.0, 0.0)
    p = passed / total
    denominator = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denominator
    margin = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denominator
    return (max(0.0, centre - margin), min(1.0, centre + margin))


def mcnemar_exact(only_first: int, only_last: int) -> float:
    """Two-sided exact p-value on the cases that passed in one run and failed in the other."""
    n = only_first + only_last
    if n == 0:
        return 1.0
    k = min(only_first, only_last)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / 2**n
    return min(1.0, 2 * tail)


def load(root: Path, name: str) -> dict[str, dict]:
    folder = Path(name) if Path(name).is_dir() else root / name
    rows = json.loads((folder / "summary.json").read_text(encoding="utf-8"))
    return {row["case_id"]: row for row in rows}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("runs", nargs="+")
    parser.add_argument("--root", default=".agent_control/e2e_results")
    args = parser.parse_args()
    root = Path(args.root)
    runs = [load(root, name) for name in args.runs]
    cases = sorted({case for run in runs for case in run})
    width = max(len(case) for case in cases)
    for case in cases:
        marks = "".join("-" if case not in run else ("P" if run[case].get("passed") else "F") for run in runs)
        last = runs[-1].get(case) or {}
        reason = "" if last.get("passed") or not last else str(last.get("failure_reason") or "")[:110]
        print(f"{case:<{width}}  {marks}  {reason}")
    print()
    for name, run in zip(args.runs, runs):
        passed = sum(1 for row in run.values() if row.get("passed"))
        low, high = wilson(passed, len(run))
        print(f"{name}: {passed}/{len(run)} = {passed / max(1, len(run)):.0%}  (95% interval {low:.0%} to {high:.0%})")
    if len(runs) >= 2:
        both = [case for case in runs[0] if case in runs[-1]]
        only_first = sum(1 for c in both if runs[0][c].get("passed") and not runs[-1][c].get("passed"))
        only_last = sum(1 for c in both if runs[-1][c].get("passed") and not runs[0][c].get("passed"))
        print(f"first vs last on {len(both)} shared cases: better {only_last}, worse {only_first}, exact McNemar p = {mcnemar_exact(only_first, only_last):.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
