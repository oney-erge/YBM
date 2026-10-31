"""Turn the Operator calls YBM has already made into labeled replay cases.

Every Operator call is stored with its exact prompt and its answer (llm_calls), and the task keeps what each
call led to (operator_history). That is enough to say, without hand-labeling, whether a recorded decision was
good or bad for the state it was made in:

  call_tool  -> the call succeeded                          good
                the call failed, was denied or was repeated bad   (a later model must not repeat it)
  done       -> the Auditor or fulfillment check rejected it   bad
                the task completed after it                    good
  ask_user   -> the clarification check rejected it            bad

Anything else (approvals, delegation, parallel calls, calls whose outcome cannot be matched) is left out
rather than guessed.

The prompts contain the user's own messages and file names. They stay in the local database and in the
scratch files this script writes; only scores, never prompts, belong in anything published.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

CHECK_TOOLS = {"_audit_check", "_fulfillment_check", "_clarification_check"}
BAD_CALL_STATUSES = {"failed", "denied", "already_done"}


@dataclass
class Step:
    id: str
    task_id: str
    recorded_model: str
    created_at: str
    system: str
    user: str
    recorded: dict[str, Any]
    label: str  # "good" | "bad"
    why: str


def _decision(response_text: str | None) -> dict[str, Any] | None:
    try:
        decision = json.loads(response_text or "")
    except json.JSONDecodeError:
        return None
    return decision if isinstance(decision, dict) and decision.get("action") else None


def label_step(decision: dict[str, Any], history: list[dict[str, Any]], index: int, task_status: str) -> tuple[str, str] | None:
    """(label, why) for the decision made when `history` had `index` entries, or None when it cannot be told."""
    outcome = history[index] if 0 <= index < len(history) and isinstance(history[index], dict) else None
    action = decision.get("action")
    if action == "call_tool":
        if outcome is None or outcome.get("tool_name") in CHECK_TOOLS:
            return None
        requested = (outcome.get("normalized_from") or {}).get("tool_name") or outcome.get("tool_name")
        if decision.get("tool_name") not in {requested, outcome.get("tool_name")}:
            return None  # history and call do not line up (a delegated sub-task, or a retried call)
        status = str(outcome.get("status") or "")
        if status == "succeeded":
            return "good", "call succeeded"
        if status in BAD_CALL_STATUSES:
            return "bad", f"call {status}"
        return None
    if action == "done":
        if outcome is not None and outcome.get("tool_name") in {"_audit_check", "_fulfillment_check"}:
            return "bad", "done rejected by a check"
        if outcome is None and task_status == "completed":
            return "good", "task completed after done"
        return None
    if action == "ask_user":
        if outcome is not None and outcome.get("tool_name") == "_clarification_check":
            return "bad", "question rejected by a check"
        return None
    return None


def load_steps(db_path: Path, *, since: str | None = None) -> list[Step]:
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    tasks = {
        row["id"]: (row["status"], json.loads(row["metadata_json"] or "{}"))
        for row in connection.execute("SELECT id, status, metadata_json FROM tasks")
    }
    query = (
        "SELECT id, task_id, model, step_index, messages_json, response_text, created_at FROM llm_calls "
        "WHERE source = 'operator' AND task_id IS NOT NULL"
    )
    params: tuple[Any, ...] = ()
    if since:
        query += " AND created_at >= ?"
        params = (since,)
    steps: list[Step] = []
    for row in connection.execute(query + " ORDER BY created_at, rowid", params):
        if row["task_id"] not in tasks:
            continue
        status, metadata = tasks[row["task_id"]]
        decision = _decision(row["response_text"])
        try:
            messages = json.loads(row["messages_json"] or "[]")
        except json.JSONDecodeError:
            continue
        if decision is None or len(messages) < 2 or row["step_index"] is None:
            continue
        labeled = label_step(decision, metadata.get("operator_history") or [], int(row["step_index"]), status)
        if labeled is None:
            continue
        steps.append(
            Step(
                id=row["id"], task_id=row["task_id"], recorded_model=row["model"], created_at=row["created_at"],
                system=str(messages[0].get("content") or ""), user=str(messages[1].get("content") or ""),
                recorded=decision, label=labeled[0], why=labeled[1],
            )
        )
    connection.close()
    return steps


def sample(steps: list[Step], count: int, seed: int = 7) -> list[Step]:
    """All of the bad steps first (they are the failures worth testing), then good ones, mixed across tasks."""
    import random

    rng = random.Random(seed)
    bad = [step for step in steps if step.label == "bad"]
    good = [step for step in steps if step.label == "good"]
    rng.shuffle(bad)
    rng.shuffle(good)
    take_bad = bad[: max(1, count * 2 // 5)]
    chosen = take_bad + good[: count - len(take_bad)]
    return sorted(chosen, key=lambda step: (step.created_at, step.id))


def save(steps: list[Step], path: Path) -> None:
    path.write_text(json.dumps([asdict(step) for step in steps], ensure_ascii=False), encoding="utf-8")


def load(path: Path) -> list[Step]:
    return [Step(**item) for item in json.loads(path.read_text(encoding="utf-8"))]


if __name__ == "__main__":
    import argparse
    from collections import Counter

    parser = argparse.ArgumentParser(description="Extract labeled Operator steps from a YBM database")
    parser.add_argument("--db", type=Path, default=Path(".agent_control/agent_control.db"))
    parser.add_argument("--count", type=int, default=120, help="0 keeps every labeled step")
    parser.add_argument("--out", type=Path, required=True)
    arguments = parser.parse_args()
    found = load_steps(arguments.db)
    print(f"{len(found)} labeled steps:", dict(Counter((s.recorded_model, s.label) for s in found)))
    chosen = sample(found, arguments.count) if arguments.count else sorted(found, key=lambda step: (step.created_at, step.id))
    save(chosen, arguments.out)
    print(f"saved {len(chosen)} to {arguments.out}", dict(Counter(s.label for s in chosen)))
