"""Rebuild the full Operator prompt for each labeled step from the task's stored history.

YBM stores each model call's prompt cut to its first 8,000 characters (llm/call_log.py). The Operator prompt is longer
than that, so a replay of the stored text is a replay of a truncated prompt: the tool list cut off in the older prompts,
and in the current layout (tool list first) the objective and history, the parts that decide the answer, missing
altogether. A benchmark built on those scored every model near 50% for a reason that had nothing to do with the models.

This rebuilds the prompt the way the worker does, from what the task keeps: the objective, the operator history up to the
step, the conversation memory, and the current tool catalog and system prompt. A step whose recorded decision cannot be
made against today's tools (a tool since removed) is dropped, because its label no longer means anything.

    python rebuild.py --db .agent_control/agent_control.db --out steps_full.json [--count 120]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any

from grade import call_is_valid
from steps import Step, load_steps, sample, save

# Tasks whose objective was extended by a clarification answer: the objective as it was at each step is not stored.
_FOLDED = ("\n[Question to the user:", "\n[User clarification:")


def original_objective(objective: str) -> str:
    cut = [objective.find(marker) for marker in _FOLDED if marker in objective]
    return objective[: min(cut)] if cut else objective


def rebuild(steps: list[Step], db_path: Path) -> tuple[list[Step], dict[str, int]]:
    from agent_control.cli import _worker_config_context
    from agent_control.config import load_settings
    from agent_control.orchestration.operator import OPERATOR_SYSTEM_PROMPT, OperatorLoopService
    from agent_control.orchestration.worker import _canonical_operator_tool_call
    from agent_control.tools.registry import build_tool_registry

    settings = load_settings()
    registry = build_tool_registry(settings, "http://127.0.0.1:8765")
    definitions = {definition.name: definition for definition in registry.definitions}
    config_context = _worker_config_context(registry, settings)

    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    tasks: dict[str, tuple[str, dict[str, Any]]] = {
        row[0]: (row[1], json.loads(row[2] or "{}"))
        for row in connection.execute("SELECT id, objective, metadata_json FROM tasks")
    }
    indexes = {row[0]: row[1] for row in connection.execute("SELECT id, step_index FROM llm_calls")}
    connection.close()

    kept: list[Step] = []
    dropped = {"no step index": 0, "clarified task": 0, "no longer expressible": 0}
    for step in steps:
        objective, metadata = tasks[step.task_id]
        index = indexes.get(step.id)
        if index is None:
            dropped["no step index"] += 1
            continue
        if metadata.get("clarification_answers"):
            dropped["clarified task"] += 1
            continue
        if step.recorded.get("action") == "call_tool":
            valid, _ = call_is_valid(step.recorded, definitions, _canonical_operator_tool_call)
            if not valid:
                dropped["no longer expressible"] += 1
                continue
        history = list((metadata.get("operator_history") or [])[: int(index)])
        user = OperatorLoopService._prompt(
            original_objective(objective), config_context, history, str(metadata.get("memory_context") or "")
        )
        kept.append(replace(step, system=OPERATOR_SYSTEM_PROMPT, user=user))
    return kept, dropped


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", type=Path, default=Path(".agent_control/agent_control.db"))
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--count", type=int, default=0, help="0 keeps every step that can be rebuilt")
    arguments = parser.parse_args()
    found = load_steps(arguments.db)
    rebuilt, dropped = rebuild(found, arguments.db)
    chosen = sample(rebuilt, arguments.count) if arguments.count else rebuilt
    save(chosen, arguments.out)
    lengths = sorted(len(step.system) + len(step.user) for step in chosen)
    labels = {label: sum(1 for step in chosen if step.label == label) for label in ("good", "bad")}
    print(f"{len(found)} labeled -> {len(rebuilt)} rebuilt (dropped {dropped}); saved {len(chosen)} {labels}")
    print(f"prompt chars: median {lengths[len(lengths) // 2]}, max {lengths[-1]} (a stored prompt was capped at about 13,300)")
