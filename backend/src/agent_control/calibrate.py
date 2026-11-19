"""Calibration: do the decision model's yes/no questions still separate what they are meant to, with the model that is running now?

Every skill with a `when:` question is put in front of the Operator only when the decision model says yes to it at the skill's `when_at` mark. Those marks were measured on one
model (docs/ULTIMATE_ASSISTANT_PLAN.md, M7): requests that should say yes scored above the mark, requests that should not scored below it. A different model, or a retrained one, scores
differently, and the marks silently stop meaning what they did. This is the check that says so, and where the marks should move, before a model swap ships:

    python -m agent_control.cli calibrate        # the table, exit 1 when any mark no longer separates

The corpus is `e2e/calibration_cases.json`: for each skill file, requests that must say yes and requests that must say no. It is written from the measurements, not from the skill
text, and kept next to the sealed exams, so the numbers are the same every time.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_control.tools.skills import _parse_skill_file

Ask = Callable[[str, dict[str, str]], Awaitable[dict[str, float]]]  # request, {name: question} -> {name: probability of yes}


@dataclass
class SkillResult:
    skill: str
    mark: float
    lowest_yes: float
    highest_no: float
    suggested: float | None
    ok: bool
    detail: str


async def _scores(ask: Ask, question: str, requests: list[str]) -> list[float]:
    scores = []
    for request in requests:
        answer = await ask(request, {"q": question})
        scores.append(float(answer.get("q", 0.0)))
    return scores


async def calibrate(skills_dir: str | Path, corpus_path: str | Path, ask: Ask) -> list[SkillResult]:
    corpus: dict[str, Any] = json.loads(Path(corpus_path).read_text(encoding="utf-8"))
    results: list[SkillResult] = []
    for file_name, cases in sorted(corpus.items()):
        skill = _parse_skill_file(Path(skills_dir) / file_name)
        if skill is None or not skill["when"]:
            results.append(SkillResult(file_name, 0.0, 0.0, 0.0, None, False, "the skill file or its question is missing"))
            continue
        yes = await _scores(ask, skill["when"], list(cases.get("yes") or []))
        no = await _scores(ask, skill["when"], list(cases.get("no") or []))
        low, high, mark = (min(yes) if yes else 1.0), (max(no) if no else 0.0), float(skill["when_at"] or 0.5)
        ok = low >= mark > high
        suggested = round((low + high) / 2, 2) if low > high else None
        detail = "separates" if ok else (f"move the mark to about {suggested}" if suggested is not None else "no mark separates: the question needs rewording")
        results.append(SkillResult(file_name, mark, round(low, 2), round(high, 2), suggested, ok, detail))
    return results


def render(results: list[SkillResult]) -> str:
    lines = [f"{'skill':34} {'mark':>5} {'lowest yes':>11} {'highest no':>11}  result"]
    for r in results:
        lines.append(f"{r.skill:34} {r.mark:>5.2f} {r.lowest_yes:>11.2f} {r.highest_no:>11.2f}  {'ok' if r.ok else 'CHECK'}: {r.detail}")
    bad = [r for r in results if not r.ok]
    lines.append("")
    lines.append("All marks still separate." if not bad else f"{len(bad)} of {len(results)} skill(s) need a look before this model is used.")
    return "\n".join(lines)
