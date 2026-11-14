"""What is checked in the answer before it is delivered, in order, each warning given at most as often as it is useful.

The model says `done` and the answer is read by script before anything else is spent on it. A script can tell four kinds of mistake, and
each sends the model back with a reason it can act on:

    1. a number of a calculation, or an address, that no tool produced (and the request asks for a calculation)
    2. a sum that does not add up to the total the answer states
    3. a quotation of something that nothing read contains
    4. a write or a move the request describes that the receipt shows did not happen as described
    5. something the answer says was done to the world (deleted, sent to someone else, installed, paid for) that the record does not show

The first is said once per task (the worker only asks for the values when it has not asked before), the second, third and fifth once, the
fourth twice. That policy used to be four copies of "look in the history for my hint id, else append and return", inside the worker's DONE
branch. It is here once: `check_answer` returns the first check that fails as a `Pushback`, and the worker appends the entry and puts the
task back to RUNNING. Nothing here touches storage or a model. What follows (the Auditor, then fulfillment) stays in the worker, which
owns the repositories and the model calls.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from agent_control.orchestration import claims, ladder
from agent_control.orchestration.check_entries import CHECK_ENTRY_NUMBER, CHECK_ENTRY_STRATEGY
from agent_control.orchestration.number_check import inconsistent_sum
from agent_control.schemas import TaskRecord


@dataclass(frozen=True)
class Pushback:
    """The answer goes back to the model with `entry` appended to the history; `metadata` is merged into the task's metadata."""

    entry: dict[str, Any]
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Answer:
    """What the checks look at: the answer and what it is measured against."""

    task: TaskRecord
    history: list[dict[str, Any]]
    text: str | None
    request: str  # the user's own words
    real_steps: list[dict[str, Any]]  # the history without check entries
    step_id: str
    unseen_addresses: tuple[str, ...] = ()  # in the answer, produced by no tool
    unseen_numbers: tuple[str, ...] = ()


def _already_said(history: list[dict[str, Any]], hint_id: str) -> bool:
    return any(isinstance(entry, dict) and entry.get("hint_id") == hint_id for entry in history)


def _values_no_tool_produced(answer: Answer) -> Pushback | None:
    if not (answer.unseen_addresses or answer.unseen_numbers):
        return None
    reasons = []
    if answer.unseen_addresses:
        reasons.append(
            f"{', '.join(answer.unseen_addresses)} in your answer is not in any tool result or in the request. Do not "
            "make up values: answer only from what the tools returned, and say plainly when something is not there."
        )
    if answer.unseen_numbers:
        reasons.append(
            f"{', '.join(answer.unseen_numbers)} in your answer did not come from any tool result, and the request asks for a "
            "calculation. Work it out with code.interpreter (run_python, in the `code` field) from the data you "
            "already have, then answer with the number the code printed."
        )
    entry = {
        "tool_name": CHECK_ENTRY_NUMBER, "input": None, "status": "number_gap",
        "error": "Declared done, but " + " Also, ".join(reasons), "step_id": answer.step_id,
    }
    return Pushback(entry, {"operator_number_gap_count": 1})


def _sum_that_does_not_add_up(answer: Answer) -> Pushback | None:
    if _already_said(answer.history, "sum"):
        return None
    wrong_sum = inconsistent_sum(answer.text)
    if not wrong_sum:
        return None
    terms, total = wrong_sum
    return Pushback({
        "tool_name": CHECK_ENTRY_NUMBER, "input": None, "status": "number_gap", "hint_id": "sum", "step_id": answer.step_id,
        "error": (
            f"Declared done, but your answer adds {' + '.join(terms)}, which is {total}, and never says {total}. "
            f"Check the arithmetic and give the correct total ({total})."
        ),
    })


def _quotes_nothing_contains(answer: Answer) -> Pushback | None:
    if _already_said(answer.history, "quotes"):
        return None
    invented = ladder.ungrounded_quotes(answer.text, answer.request, answer.real_steps, str(answer.task.metadata.get("memory_context") or ""))
    if not invented:
        return None
    return Pushback({
        "tool_name": CHECK_ENTRY_STRATEGY, "input": None, "status": "strategy", "hint_id": "quotes", "step_id": answer.step_id,
        "error": ladder.UNGROUNDED_QUOTES_TEXT.format(quotes=", ".join(f'"{q}"' for q in invented[:3])),
    })


def _receipt_that_does_not_match(answer: Answer) -> Pushback | None:
    gap = (
        ladder.write_receipt_gap(answer.request, [str(path) for path in answer.task.metadata.get("changed_paths") or []])
        or ladder.move_scope_gap(answer.request, answer.task.metadata.get("undo_moves"))
    )
    rounds = sum(1 for entry in answer.history if isinstance(entry, dict) and str(entry.get("hint_id") or "").startswith("receipt"))
    if not gap or rounds >= 2:
        return None
    return Pushback({
        "tool_name": CHECK_ENTRY_STRATEGY, "input": None, "status": "strategy", "hint_id": f"receipt{rounds + 1}",
        "error": f"Declared done, but {gap}", "step_id": answer.step_id,
    })


def _things_done_that_nothing_shows(answer: Answer) -> Pushback | None:
    if _already_said(answer.history, "claim"):
        return None
    found = [c for c in claims.unbacked_claims(answer.text, answer.history, answer.task.metadata) if c.kind in claims.WORLD_CLAIMS]
    if not found:
        return None
    said = "; ".join(f'"{c.said}"' for c in found[:3])
    return Pushback({
        "tool_name": CHECK_ENTRY_STRATEGY, "input": None, "status": "strategy", "hint_id": "claim", "step_id": answer.step_id,
        "error": (
            f"Declared done, but your answer says something was done ({said}) and there is no record of it in this task. Say plainly what did "
            "not happen and what the person can do instead, or do it with a tool first. Never say you paid for, ordered, deleted, installed or "
            "sent something unless a tool result shows it."
        ),
    })


#: The checks, in the order they run. The first that fails wins; the later ones are looked at on the next `done`.
CHECKS: tuple[tuple[str, Callable[[Answer], Pushback | None]], ...] = (
    ("a value no tool produced", _values_no_tool_produced),
    ("a sum that does not add up", _sum_that_does_not_add_up),
    ("a quotation nothing contains", _quotes_nothing_contains),
    ("a receipt that does not match the request", _receipt_that_does_not_match),
    ("something said to be done that nothing shows", _things_done_that_nothing_shows),
)


def check_answer(answer: Answer) -> Pushback | None:
    """The first reason to send the answer back to the model, or None when the script finds nothing wrong."""
    for _label, check in CHECKS:
        pushback = check(answer)
        if pushback is not None:
            return pushback
    return None
