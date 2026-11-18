"""A goal: what the person wants, kept apart from any one attempt at it.

A task is one bounded attempt. When it ends without the result (a method declined, a budget reached, a blocker found) the person's objective has not
ended, but until now nothing else held it: the next message ("write me a plan for it", "you choose", "no, you do it") was read on its own words and
became new work (live 11/16: a TV that would not take a cast, and a plan titled "Ok give me write me an optimum plan for it").

A goal holds the objective in resolved words and the person's own, what it is waiting on, the methods the person declined, and a short record of each
attempt (a task, a plan or a job) with how it ended. Intake shows the open goals of a chat to the one call that reads the message (the Concierge), which
says which goal the message belongs to and what it does to it (channels/base.py, orchestration/goal_routing.py); the worker updates a goal when its
attempt ends (orchestration/reply.py). Plans, jobs and tasks stay what they are: attempts of a goal, not owners of it.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from pydantic import Field

from agent_control.schemas import StrictBaseModel, new_id, utc_now


class GoalStatus(StrEnum):
    OPEN = "open"  # being worked on, or ready to be
    WAITING = "waiting"  # waiting for the person (a question, an approval)
    BLOCKED = "blocked"  # the last attempt ended without the result; the goal is still wanted
    DONE = "done"
    DROPPED = "dropped"  # the person said to forget it


OPEN_STATUSES = (GoalStatus.OPEN, GoalStatus.WAITING, GoalStatus.BLOCKED)
#: A goal nobody has touched for this long is no longer offered as what a message might mean.
STALE_AFTER = timedelta(days=14)
MAX_ATTEMPTS = 12
MAX_SHOWN = 4

#: What a message can do to a goal, as the Concierge names it (schemas.MessageClassification.act).
ACTS = ("new", "answer", "delegate", "decline", "continue", "plan", "control", "chat")
#: The acts that refer to something already under way: with no goal to refer to they cannot be carried out as they stand.
FOLLOW_UP_ACTS = ("answer", "delegate", "decline", "continue", "plan")


class Goal(StrictBaseModel):
    id: str = Field(default_factory=lambda: new_id("goal"))
    conversation_id: str
    #: The objective in resolved words ("get the living-room Samsung TV to take casts from this PC"), not the sentence that started it.
    objective: str
    #: The person's own words, as they first said it.
    words: str = ""
    status: GoalStatus = GoalStatus.OPEN
    #: What the goal waits on: {"task_id", "question"} while a task asks the person something.
    waiting: dict[str, Any] = Field(default_factory=dict)
    #: Methods the person said no to for this goal (for example "claude_code"). They are not offered again, and the goal does not end.
    declined: list[str] = Field(default_factory=list)
    #: One entry per attempt: {"kind": "task"|"plan"|"job", "id", "at", "outcome", "note"}.
    attempts: list[dict[str, Any]] = Field(default_factory=list)
    #: Short statements of what was found, each one a fact the attempts observed.
    evidence: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    revision: int = 0

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_STATUSES

    def is_fresh(self, now: datetime | None = None) -> bool:
        return (now or utc_now()) - self.updated_at <= STALE_AFTER


def _short(value: object, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def goals_context(goals: list[Goal]) -> tuple[str, dict[str, Goal]]:
    """The "Open goals" block that goes to the Concierge with the message, and the label (G1, G2...) of each goal in it.

    Empty when the chat has no open goal, so the prompt of every chat without one is exactly what it was.
    """
    shown = [goal for goal in goals if goal.is_open][:MAX_SHOWN]
    if not shown:
        return "", {}
    labels = {f"G{number}": goal for number, goal in enumerate(shown, 1)}
    lines = ["Open goals in this chat (the person may be continuing one of them):"]
    for label, goal in labels.items():
        notes = [goal.status.value]
        if goal.waiting.get("question"):
            notes.append("waiting for the person to answer: " + _short(goal.waiting["question"], 140))
        tried = [f"{attempt.get('kind', 'task')} {attempt.get('outcome', 'started')}" + (f" ({_short(attempt['note'], 80)})" if attempt.get("note") else "") for attempt in goal.attempts[-3:]]
        if tried:
            notes.append("tried: " + "; ".join(tried))
        if goal.declined:
            notes.append("the person declined: " + ", ".join(goal.declined))
        lines.append(f"{label}: {_short(goal.objective, 200)} [{' | '.join(notes)}]")
    lines.append(
        "If this message answers, continues, plans for, or declines something in one of these goals, set goal_ref to its label and act to one of: "
        "answer (it answers the goal's question), delegate (the person hands the choice to you: 'you choose', 'whatever you think'), decline (refuses the "
        "method that was offered), continue ('do it yourself', 'keep trying', 'try again'), plan (asks for a plan for the goal), control (pause, stop, "
        "or status of a job), chat. Then normalized_objective is that goal's objective with whatever the message adds. If it is a new request, goal_ref "
        "is null and act is new. If more than one goal fits the message equally well (nothing in it points to one), leave goal_ref null: the person will be asked which."
    )
    return "\n".join(lines), labels


def attempt_entry(kind: str, ident: str, *, outcome: str = "started", note: str = "", at: datetime | None = None) -> dict[str, Any]:
    return {"kind": kind, "id": ident, "at": (at or utc_now()).isoformat(), "outcome": outcome, "note": _short(note, 200)}


def goal_notes_for_operator(goal: Goal) -> str:
    """What the next attempt is told about the goal it belongs to: what was tried and how it ended, what was declined, what is known."""
    lines = [f"\n\nThis continues a goal the person still wants: {_short(goal.objective, 300)}"]
    for attempt in goal.attempts[-4:]:
        lines.append(f"- An earlier {attempt.get('kind', 'task')} {attempt.get('outcome', 'started')}" + (f": {_short(attempt['note'], 160)}" if attempt.get("note") else ""))
    for fact in goal.evidence[-5:]:
        lines.append(f"- Known: {_short(fact, 160)}")
    if goal.declined:
        lines.append("- The person declined: " + ", ".join(goal.declined) + ". Do not offer it again; find another way.")
    lines.append("Do not repeat a method that already failed. Do not end the goal because one method failed: try a different one or say exactly what blocks it and what would unblock it.")
    return "\n".join(lines)
