"""One table of the same cases across runs: what each case did every time it ran, whether it is flaky, and what each suite scores now.

A score is only comparable with itself. On 11/14 numbers from three different suites, graded two different ways, read as "accuracy dropped"
(16/16, 11/21, 7/13) when they were different tests. This prints each case's own history, so the question "did it get better?" is answered by
the same case before and after, and a regression in a frozen suite shows up as one row that used to pass.

Usage:
    python scripts/e2e_scorecard.py                          # every suite, the last 4 runs of each case
    python scripts/e2e_scorecard.py --suite vision,heldout4  # only these suites (e2e/<name>_cases.json)
    python scripts/e2e_scorecard.py --last 6 --failing       # only cases whose latest run failed
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RESULTS = ROOT / ".agent_control" / "e2e_results"
DEFAULT_CASES = ROOT / "e2e"
SUITE_FILES = {
    "vision": "vision_cases.json", "heldout3": "heldout3_cases.json", "heldout4": "heldout4_cases.json",
    "challenge": "challenge_cases.json", "verdict": "verdict_cases.json", "verdict2": "verdict2_cases.json",
}
#: A frozen suite exists to answer "did anything that worked break?"; the others to find where it breaks. The scorecard says which it is.
PURPOSE = {
    "vision": "regression check", "heldout3": "regression check", "heldout4": "regression check",
    "challenge": "hard cases, open", "verdict": "sealed exam (already run once)", "verdict2": "sealed exam",
}


@dataclass
class Outcome:
    run: str
    result: str  # "P", "F" or "S" (skipped)
    seconds: float | None = None


@dataclass
class CaseHistory:
    case_id: str
    outcomes: list[Outcome] = field(default_factory=list)

    def recent(self, count: int) -> list[Outcome]:
        return [item for item in self.outcomes if item.result != "S"][-count:]

    @property
    def latest(self) -> str:
        ran = self.recent(1)
        return ran[0].result if ran else "-"

    def flaky(self, count: int = 4) -> bool:
        """Both a pass and a fail in the last runs: the case is not settled either way, so one run proves nothing."""
        results = {item.result for item in self.recent(count)}
        return {"P", "F"} <= results


def load_history(results_dir: Path = DEFAULT_RESULTS) -> dict[str, CaseHistory]:
    """Every case's outcomes, oldest run first. A run folder without a readable summary.json is ignored."""
    history: dict[str, CaseHistory] = {}
    for run in sorted(path for path in results_dir.glob("run_*") if path.is_dir()):
        try:
            stages = json.loads((run / "summary.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(stages, list):
            continue
        for stage in stages:
            if not isinstance(stage, dict) or not stage.get("case_id"):
                continue
            result = "S" if stage.get("skipped") else ("P" if stage.get("passed") else "F")
            seconds = stage.get("total_duration_s")
            entry = history.setdefault(str(stage["case_id"]), CaseHistory(str(stage["case_id"])))
            entry.outcomes.append(Outcome(run.name.removeprefix("run_"), result, float(seconds) if seconds is not None else None))
    return history


def suite_ids(name: str, cases_dir: Path = DEFAULT_CASES) -> list[str]:
    path = cases_dir / SUITE_FILES[name]
    try:
        return [str(case["id"]) for case in json.loads(path.read_text(encoding="utf-8"))]
    except (OSError, ValueError, KeyError, TypeError):
        return []


def _cell(item: Outcome) -> str:
    return f"{item.result}{'' if item.seconds is None else f' {item.seconds:.0f}s'}"


def render(history: dict[str, CaseHistory], suites: list[str], *, last: int = 4, failing_only: bool = False, cases_dir: Path = DEFAULT_CASES) -> str:
    out: list[str] = []
    for name in suites:
        ids = suite_ids(name, cases_dir)
        if not ids:
            continue
        rows, passing, ran = [], 0, 0
        for case_id in ids:
            entry = history.get(case_id)
            if entry is None or not entry.recent(1):
                if not failing_only:
                    rows.append(f"| `{case_id}` | never run | - | - |")
                continue
            ran += 1
            passing += entry.latest == "P"
            if failing_only and entry.latest == "P":
                continue
            cells = " ".join(_cell(item) for item in entry.recent(last))
            rows.append(f"| `{case_id}` | {cells} | {entry.latest} | {'flaky' if entry.flaky(last) else ''} |")
        out += [
            f"## {name} ({PURPOSE.get(name, '')}): latest run of each case {passing} of {ran} passing, {len(ids)} cases in the suite",
            "",
            f"| Case | Last {last} runs, oldest first | Latest | Note |",
            "|---|---|---|---|",
            *rows,
            "",
        ]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--suite", default=",".join(SUITE_FILES), help="comma-separated suites")
    parser.add_argument("--last", type=int, default=4, help="how many recent runs to show per case")
    parser.add_argument("--failing", action="store_true", help="only cases whose latest run failed")
    parser.add_argument("--results", default=str(DEFAULT_RESULTS))
    args = parser.parse_args(argv)
    suites = [name for name in args.suite.split(",") if name in SUITE_FILES]
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    print(render(load_history(Path(args.results)), suites, last=args.last, failing_only=args.failing))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
