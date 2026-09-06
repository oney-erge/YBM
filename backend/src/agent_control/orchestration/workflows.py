"""Verified workflows (docs/ROADMAP.md "reusable verified workflows"): save
a completed task's own replay plan (worker.build_replay_plan) as a named,
parameterized template, then run it again later against different values
through the exact same approval/retry/verification pipeline replay already
provides.

Deliberately thin, matching the same design choice replay itself made: no
new execution engine, no hand-authored plan format - a workflow's `plan`
is always a real recorded run's own successful tool calls, with specific
literal values swapped for ``{{name}}`` placeholders after the fact. There
is no workflow designer here and none is planned; the only editing surface
is "type a name for this value" at save time and "fill in this value" at
run time.
"""

from __future__ import annotations

import re
from typing import Any

from agent_control.orchestration.worker import CHECK_ENTRY_NAMES

_PLACEHOLDER_RE = re.compile(r"^\{\{([a-zA-Z_][a-zA-Z0-9_]*)\}\}$")


def replay_plan_gaps(history: list[dict[str, Any]]) -> list[str]:
    """Human-readable names for every succeeded step build_replay_plan
    would silently drop - a delegated sub-task, or a member of a parallel
    batch. A workflow saved from a plan with gaps would silently replay a
    partial version of what the source task actually did, which is worse
    than not offering the feature: refuse instead (admin.py's
    save-workflow endpoint), and say exactly what could not be captured
    rather than leaving the user to notice steps are missing later.
    """
    gaps: list[str] = []
    for entry in history:
        if entry.get("tool_name") in CHECK_ENTRY_NAMES:
            continue
        if entry.get("status") != "succeeded":
            continue
        tool_name = entry.get("tool_name")
        if tool_name == "delegate":
            objective = ""
            input_value = entry.get("input")
            if isinstance(input_value, dict):
                objective = str(input_value.get("objective") or "")
            gaps.append(f"delegated sub-task: {objective}" if objective else "a delegated sub-task")
        elif entry.get("origin"):
            gaps.append(f"{tool_name} (ran as part of a parallel batch)")
    return gaps


def _walk_strings(value: Any, replace: Any) -> Any:
    if isinstance(value, str):
        return replace(value)
    if isinstance(value, dict):
        return {key: _walk_strings(item, replace) for key, item in value.items()}
    if isinstance(value, list):
        return [_walk_strings(item, replace) for item in value]
    return value


def parameterize_plan(
    plan: list[dict[str, Any]], substitutions: dict[str, str]
) -> tuple[list[dict[str, Any]], list[str]]:
    """Replace literal values with ``{{name}}`` placeholders wherever they
    appear in `plan`'s tool_input values, recursively (a manifest entry's
    nested source/destination included). `substitutions` maps the exact
    literal value to replace -> the parameter name to give it, e.g.
    ``{"C:/Users/sam/Downloads": "folder"}``.

    Returns the parameterized plan plus the distinct parameter names
    actually found, in first-seen order - only names that matched
    something are returned, so a substitution offered but never present in
    the plan does not produce a run-time field nobody's plan ever reads.
    """
    seen_order: list[str] = []

    def replace(value: str) -> str:
        name = substitutions.get(value)
        if name is None:
            return value
        if name not in seen_order:
            seen_order.append(name)
        return f"{{{{{name}}}}}"

    new_plan = [
        {"tool_name": step.get("tool_name"), "tool_input": _walk_strings(step.get("tool_input") or {}, replace)}
        for step in plan
    ]
    return new_plan, seen_order


def instantiate_plan(plan_template: list[dict[str, Any]], values: dict[str, str]) -> list[dict[str, Any]]:
    """The reverse of parameterize_plan: replace every ``{{name}}``
    placeholder in `plan_template`'s tool_input values with `values[name]`.
    Raises ValueError naming every missing parameter at once (not just the
    first one hit) rather than letting a partially-substituted plan reach
    replay with a literal ``{{name}}`` string as a real tool argument.
    """
    missing: set[str] = set()

    def replace(value: str) -> str:
        match = _PLACEHOLDER_RE.match(value)
        if not match:
            return value
        name = match.group(1)
        if name not in values:
            missing.add(name)
            return value
        return values[name]

    new_plan = [
        {"tool_name": step.get("tool_name"), "tool_input": _walk_strings(step.get("tool_input") or {}, replace)}
        for step in plan_template
    ]
    if missing:
        raise ValueError(f"missing required parameter(s): {', '.join(sorted(missing))}")
    return new_plan
