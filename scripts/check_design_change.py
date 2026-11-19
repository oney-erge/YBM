#!/usr/bin/env python3
"""The change rule (docs/DESIGN.md): a change to the product's code is labelled Minimal or Design, and the label has to be true.

A pull request that touches `backend/src/agent_control/` ticks exactly one box in its description:

* Minimal: it fits a home that already exists (docs/DESIGN.md, section 2) and adds no module, table, config key, tool or tool operation, prompt rule or
  pattern that matches wording, and stays small.
* Design: it is anything else. It adds a short entry to docs/DESIGN.md, and what it replaces is deleted in the same series.

This script reads the pull request's description and its diff against the base branch and fails when the label is missing, when two are ticked, when a
Minimal change does what only a Design change may do, or when a Design change has no DESIGN.md entry. It also keeps two counts that may only go down
(docs/design_baseline.json): the patterns that match wording in the code that reads messages, and the lines of the layer that rewrites the model's tool calls.

    python scripts/check_design_change.py --base origin/main --body-file body.txt
    python scripts/check_design_change.py --update-baseline        # after a change that lowered a count
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PRODUCT = "backend/src/agent_control/"
BASELINE = ROOT / "docs" / "design_baseline.json"

#: Where a pattern that matches the person's (or the model's) wording is a symptom: the code that reads a message or a tool call.
PATTERN_HOMES = (
    PRODUCT + "orchestration/",
    PRODUCT + "channels/",
    PRODUCT + "plans/",
    PRODUCT + "clarification.py",
    PRODUCT + "goals.py",
    PRODUCT + "llm/classifier.py",
    PRODUCT + "llm/decision.py",
    PRODUCT + "tools/contracts.py",
)
REWRITE_LAYER = PRODUCT + "orchestration/call_rewrites.py"
#: A Minimal change is a few dozen lines, not a feature.
MINIMAL_LINES = 150

_BOX = re.compile(r"^\s*[-*]\s*\[(?P<mark>[ xX])\]\s*(?P<name>minimal|design)\b", re.IGNORECASE | re.MULTILINE)
_NEW_TABLE = re.compile(r"\bCREATE\s+TABLE\b|\bADD\s+COLUMN\b", re.IGNORECASE)
_NEW_FIELD = re.compile(r"^\s{4,8}[a-z_][a-z0-9_]*\s*:\s*[A-Za-z_][\w\[\], |.\"']*=")
_NEW_TOOL = re.compile(r"\bToolDefinition\(|\boperations\s*=\s*\(")
_NEW_PATTERN = re.compile(r"\bre\.compile\(")


@dataclass
class Change:
    path: str
    status: str = "M"  # A added, M modified, D deleted, R renamed
    added: list[str] = field(default_factory=list)


def labels_in(body: str) -> list[str]:
    """The boxes ticked in the pull request's description: 'minimal' and/or 'design'."""
    return [m.group("name").lower() for m in _BOX.finditer(body or "") if m.group("mark").lower() == "x"]


def product_changes(changes: list[Change]) -> list[Change]:
    """Every change to the product itself: code, prompts and deletions alike."""
    return [c for c in changes if c.path.startswith(PRODUCT) and "__pycache__" not in c.path]


def _code_lines(lines: list[str]) -> int:
    return sum(1 for line in lines if line.strip() and not line.strip().startswith("#"))


def minimal_violations(changes: list[Change]) -> list[str]:
    """What a change may not do and still call itself Minimal."""
    found: list[str] = []
    touched = [c for c in product_changes(changes) if c.path.endswith(".py") and c.status != "D"]
    for change in touched:
        name = change.path[len(PRODUCT):]
        if change.status in ("A", "R") and not name.endswith("__init__.py"):
            found.append(f"a new module: {change.path}")
        if name == "storage/migrations.py" and any(_NEW_TABLE.search(line) for line in change.added):
            found.append("a new table or column (storage/migrations.py)")
        if name == "config.py" and any(_NEW_FIELD.match(line) for line in change.added):
            found.append("a new config key (config.py)")
        if name.startswith("tools/") and any(_NEW_TOOL.search(line) for line in change.added):
            found.append(f"a new tool or tool operation ({change.path})")
        if name == "tools/contracts.py" and any("Literal[" in line for line in change.added):
            found.append("a new tool operation (tools/contracts.py)")
        if any(change.path.startswith(home) or change.path == home for home in PATTERN_HOMES) and any(_NEW_PATTERN.search(line) for line in change.added):
            found.append(f"a new pattern that matches wording ({change.path})")
    for change in changes:
        if change.path.startswith(PRODUCT + "prompts/") and change.status in ("A", "R"):
            found.append(f"a new prompt file: {change.path}")
    total = sum(_code_lines(c.added) for c in touched)
    if total > MINIMAL_LINES:
        found.append(f"{total} added lines of product code (a Minimal change is at most {MINIMAL_LINES})")
    return found


def counts(root: Path = ROOT) -> dict[str, int]:
    """The two numbers that may only go down."""
    patterns = 0
    for home in PATTERN_HOMES:
        target = root / home
        files = [target] if target.is_file() else sorted(target.rglob("*.py")) if target.is_dir() else []
        for path in files:
            if "__pycache__" in path.parts:
                continue
            patterns += len(_NEW_PATTERN.findall(path.read_text(encoding="utf-8")))
    rewrite = root / REWRITE_LAYER
    lines = len(rewrite.read_text(encoding="utf-8").splitlines()) if rewrite.is_file() else 0
    return {"wording_patterns": patterns, "call_rewrite_lines": lines}


def read_baseline(path: Path = BASELINE) -> dict[str, int]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: int(v) for k, v in data.items() if isinstance(v, int)}


def write_baseline(current: dict[str, int], path: Path = BASELINE) -> None:
    body = {"note": "Counts that may only go down (scripts/check_design_change.py, docs/DESIGN.md section 4). Lower them with --update-baseline.", **current}
    path.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")


def ratchet_problems(current: dict[str, int], baseline: dict[str, int], *, label: str | None, baseline_changed: bool) -> list[str]:
    """A count above the baseline needs a Design change that says so; a count below it has to be written down so it cannot creep back."""
    problems: list[str] = []
    for key, now in current.items():
        before = baseline.get(key)
        if before is None:
            problems.append(f"{key} has no baseline: run scripts/check_design_change.py --update-baseline")
        elif now > before and not (label == "design" and baseline_changed):
            problems.append(f"{key} went up ({before} to {now}); only a Design change may raise it, and it must also update docs/design_baseline.json")
        elif now < before and not baseline_changed:
            problems.append(f"{key} went down ({before} to {now}): good. Lock it in with scripts/check_design_change.py --update-baseline")
    return problems


def check(changes: list[Change], body: str, current: dict[str, int], baseline: dict[str, int]) -> list[str]:
    touched = product_changes(changes)
    paths = {c.path for c in changes}
    problems: list[str] = []
    ticked = labels_in(body)
    label = ticked[0] if len(set(ticked)) == 1 else None
    if touched:
        if not ticked:
            problems.append("this change touches backend/src/agent_control/ but its description ticks neither 'Minimal' nor 'Design' (see .github/PULL_REQUEST_TEMPLATE.md)")
        elif len(set(ticked)) > 1:
            problems.append("both 'Minimal' and 'Design' are ticked: pick the one that is true")
        elif label == "minimal":
            problems += [f"marked Minimal, but it adds {item}: that is a Design change (docs/DESIGN.md, section 3)" for item in minimal_violations(changes)]
        elif label == "design" and "docs/DESIGN.md" not in paths:
            problems.append("marked Design, but docs/DESIGN.md has no new entry (section 5: the problem, the home that changes, what it replaces and deletes)")
    problems += ratchet_problems(current, baseline, label=label, baseline_changed="docs/design_baseline.json" in paths)
    return problems


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, check=True, capture_output=True, text=True, encoding="utf-8").stdout


def changes_since(base: str) -> list[Change]:
    status = {}
    for line in _git("diff", "--name-status", "--find-renames", f"{base}...HEAD").splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            status[parts[-1]] = parts[0][0]
    found = {path: Change(path, kind) for path, kind in status.items()}
    current: Change | None = None
    for line in _git("diff", "-U0", "--no-color", f"{base}...HEAD").splitlines():
        if line.startswith("+++ "):
            current = found.get(line[6:]) if line.startswith("+++ b/") else None
        elif current is not None and line.startswith("+") and not line.startswith("+++"):
            current.added.append(line[1:])
    return list(found.values())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", default="origin/main", help="the branch the change will be merged into")
    parser.add_argument("--body-file", default="", help="a file with the pull request's description")
    parser.add_argument("--update-baseline", action="store_true", help="write today's counts to docs/design_baseline.json")
    args = parser.parse_args(argv)
    now = counts()
    if args.update_baseline:
        write_baseline(now)
        print(f"wrote {BASELINE.relative_to(ROOT)}: {now}")
        return 0
    body = Path(args.body_file).read_text(encoding="utf-8") if args.body_file else ""
    problems = check(changes_since(args.base), body, now, read_baseline())
    if problems:
        print("The change rule (docs/DESIGN.md) is not met:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print(f"The change rule is met. Counts: {now}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
