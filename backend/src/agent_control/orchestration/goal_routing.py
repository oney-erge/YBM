"""Carry out what the Concierge said a message does to a goal (goals.py), and keep goals up to date as their attempts end.

The Concierge reads the message together with the chat's open goals and returns `goal_ref` (which goal) and `act` (what the message does to it). This module is
what happens next, in code and the same for every channel:

* answer, delegate, decline: the message answers the question the goal waits on, so the waiting task is resumed with it (a delegation is the canonical
  "You choose.", a decline of a pending hand-over is "no"), and no new task is made;
* continue, plan: a new attempt of the same goal is made, with the goal's objective and what was already tried, so it does not start over or repeat a method;
* a follow-up that names no goal: with one open goal it is that goal, with several the person is asked once which, with none it is a new request;
* anything else: a new request, which gets a goal of its own.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from agent_control.clarification import resume_clarifying_task
from agent_control.goals import FOLLOW_UP_ACTS, Goal, GoalStatus, attempt_entry, goal_notes_for_operator, goals_context
from agent_control.schemas import InboundMessage, MessageClassification, TaskRecord, TaskStatus, new_id, utc_now

if TYPE_CHECKING:
    from agent_control.storage.audit import AuditLogger
    from agent_control.storage.repositories import Repositories

logger = logging.getLogger(__name__)

DELEGATED_ANSWER = "You choose."
#: Which methods a declined hand-over names, as the task records them.
CLAUDE_CODE = "claude_code"


@dataclass
class GoalRoute:
    goal: Goal | None
    act: str
    #: Several goals were open and the message did not say which one it means.
    ambiguous: bool = False


def context_for(repositories: Repositories, conversation_id: str) -> tuple[str, dict[str, Goal]]:
    """The "Open goals" block and the labels in it; empty when the chat has none (goal storage failing never stops a message)."""
    try:
        return goals_context(repositories.goals.list_open(conversation_id))
    except Exception:  # noqa: BLE001 - goals are help for reading a message, not a gate on it
        logger.warning("goals_unreadable", exc_info=True)
        return "", {}


def route_of(classification: MessageClassification, labels: dict[str, Goal]) -> GoalRoute:
    act = classification.act or ("new" if not classification.goal_ref else "continue")
    goal = labels.get(str(classification.goal_ref or "").strip().upper())
    if goal is None and act in FOLLOW_UP_ACTS:
        if len(labels) == 1:
            goal = next(iter(labels.values()))
        elif len(labels) > 1:
            return GoalRoute(None, act, ambiguous=True)
        else:
            act = "new"  # a follow-up with nothing to follow up is a request in its own right
    if goal is None and act not in ("new", "chat", "control"):
        act = "new"
    return GoalRoute(goal, act)


def ambiguity_question(labels: dict[str, Goal]) -> str:
    lines = [f"{number}. {goal.objective[:110]}" for number, goal in enumerate(labels.values(), 1)]
    return "Which one do you mean?\n" + "\n".join(lines) + f"\n{len(lines) + 1}. Something new\nReply with the number, or say it again with a few more words."


def resume_waiting_task(repositories: Repositories, audit: AuditLogger, route: GoalRoute, inbound: InboundMessage) -> str | None:
    """Give the person's answer to the task the goal waits on. The reply to send, or None when nothing is waiting (the message then continues the goal)."""
    goal = route.goal
    task_id = (goal.waiting or {}).get("task_id") if goal is not None else None
    task = repositories.tasks.get(str(task_id)) if task_id else None
    if task is None or task.status != TaskStatus.CLARIFYING:
        return None
    text = inbound.text or ""
    offer = task.metadata.get("claude_code_offer")
    offer_pending = isinstance(offer, dict) and offer.get("state") == "pending"
    if route.act == "delegate" and not offer_pending:
        text = DELEGATED_ANSWER  # to a hand-over offer the person's own "yes" or "no" is the answer, whatever the model made of the words
    elif route.act == "decline":
        text = "no" if offer_pending else (inbound.text or "no")
    result = resume_clarifying_task(
        repositories, audit, task, text=text, actor=f"{inbound.channel.value}:user:{inbound.sender_id}",
        message_id=inbound.id, received_at=inbound.received_at, correlation_id=inbound.correlation_id,
    )
    if not result.accepted:
        return "That question changed while I was reading your message. Please say it again."
    if route.act == "decline" and goal is not None:
        _remember_decline(repositories, goal.id, CLAUDE_CODE)
    if result.cancelled:
        return f"Cancelled: {result.task.objective[:200]}"
    if route.act == "delegate" or result.delegated:
        return "I'll make the choice and carry on."
    return "Got it, carrying on."


def _remember_decline(repositories: Repositories, goal_id: str, method: str) -> None:
    def change(goal: Goal) -> Goal:
        goal.declined = [*goal.declined, method]
        return goal

    try:
        repositories.goals.update(goal_id, change)
    except Exception:  # noqa: BLE001
        logger.warning("goal_decline_not_saved", exc_info=True)


def objective_for(route: GoalRoute, classification: MessageClassification, words: str) -> str:
    """The objective of the new attempt: for a message that continues a goal, the goal's own, with what the message adds."""
    said = (classification.normalized_objective or "").strip()
    if route.goal is not None and route.act in ("continue", "plan"):
        goal = route.goal.objective
        return said if said and (goal.casefold()[:40] in said.casefold() or len(said) > len(goal)) else (f"{goal} ({said})" if said and said.casefold() not in goal.casefold() else goal)
    return (said or words).strip()


def prepare_attempt(route: GoalRoute, conversation_id: str) -> tuple[str, dict[str, Any]]:
    """The goal id for the task about to be made, and the metadata that carries the goal to it: known before the task exists, so the worker never sees the task without it."""
    if route.goal is not None and route.act in ("continue", "plan"):
        return route.goal.id, {"goal_id": route.goal.id, "goal_act": route.act, "declined_methods": list(route.goal.declined), "goal_notes": goal_notes_for_operator(route.goal)}
    goal_id = new_id("goal")
    return goal_id, {"goal_id": goal_id}


def record_attempt(repositories: Repositories, task: TaskRecord, route: GoalRoute, goal_id: str, *, conversation_id: str, objective: str, words: str) -> None:
    """Save the task as an attempt of its goal: a new attempt on the goal it continues, a new goal for a new request."""
    try:
        if route.goal is not None and route.act in ("continue", "plan"):
            def attach(goal: Goal) -> Goal:
                goal.status, goal.waiting = GoalStatus.OPEN, {}
                goal.attempts = [*goal.attempts, attempt_entry("task", task.id)]
                return goal

            repositories.goals.update(goal_id, attach)
        else:
            repositories.goals.create(
                Goal(id=goal_id, conversation_id=conversation_id, objective=objective[:600], words=words[:600], attempts=[attempt_entry("task", task.id)])
            )
    except Exception:  # noqa: BLE001 - a goal that cannot be saved must not stop the work it describes
        logger.warning("goal_not_saved", exc_info=True)


def remember_choice(repositories: Repositories, conversation_id: str, inbound: InboundMessage, labels: dict[str, Goal], act: str) -> None:
    """Keep the message that could not be placed, so the person's "2" in reply to "Which one do you mean?" can be applied to it."""
    record = repositories.conversation_memory.get(conversation_id) or {}
    state = dict(record.get("facts") or {})
    state["pending_goal_choice"] = {"text": inbound.text, "act": act, "goal_ids": [goal.id for goal in labels.values()], "at": utc_now().isoformat()}
    repositories.conversation_memory.upsert(conversation_id, str(record.get("summary") or ""), state)


def take_choice(repositories: Repositories, conversation_id: str, text: str) -> tuple[str, str, str | None] | None:
    """(the original message, its act, the chosen goal id or None for "something new") when `text` is a number answering the question asked in the last ten minutes."""
    record = repositories.conversation_memory.get(conversation_id)
    state = dict((record or {}).get("facts") or {})
    pending = state.get("pending_goal_choice")
    if not isinstance(pending, dict) or not re.fullmatch(r"\s*\d{1,2}[.)]?\s*", text or ""):
        return None
    try:
        if utc_now() - datetime.fromisoformat(str(pending["at"])) > timedelta(minutes=10):
            return None
        number = int(re.sub(r"\D", "", text))
        goal_ids = list(pending["goal_ids"])
    except (KeyError, ValueError, TypeError):
        return None
    if not 1 <= number <= len(goal_ids) + 1:
        return None
    state.pop("pending_goal_choice", None)
    repositories.conversation_memory.upsert(conversation_id, str((record or {}).get("summary") or ""), state)
    return str(pending["text"]), str(pending["act"]), goal_ids[number - 1] if number <= len(goal_ids) else None


def sync_from_task(repositories: Repositories, task: TaskRecord) -> None:
    """Update the goal a task belongs to when the task changes state: waiting, working, done, blocked. Safe to call again for the same state."""
    goal_id = task.metadata.get("goal_id")
    if not goal_id:
        return
    status = task.status
    note = ""
    if status == TaskStatus.COMPLETED:
        new_status, outcome = GoalStatus.DONE, "completed"
        note = str(task.metadata.get("synthesized_answer") or "")[:200]
    elif status in (TaskStatus.BLOCKED, TaskStatus.FAILED):
        new_status, outcome = GoalStatus.BLOCKED, status.value
        note = str(task.metadata.get("synthesized_answer") or task.metadata.get("error") or "")[:200]
    elif status == TaskStatus.CANCELLED:
        new_status, outcome = GoalStatus.BLOCKED, "cancelled"  # still wanted: only the person saying to forget it ends a goal
    elif status in (TaskStatus.CLARIFYING, TaskStatus.AWAITING_APPROVAL):
        new_status, outcome = GoalStatus.WAITING, "asked the person" if status == TaskStatus.CLARIFYING else "waited for an approval"
        note = str(task.metadata.get("clarifying_question") or "")[:200]
    elif status in (TaskStatus.RUNNING, TaskStatus.RETRYING, TaskStatus.RECEIVED, TaskStatus.INTERPRETING, TaskStatus.PLANNED):
        new_status, outcome = GoalStatus.OPEN, "started"
    else:
        return
    offer = task.metadata.get("claude_code_offer")

    def change(goal: Goal) -> Goal | None:
        goal.status = new_status
        goal.waiting = {"task_id": task.id, "question": note} if new_status == GoalStatus.WAITING and status == TaskStatus.CLARIFYING else {}
        entry = next((item for item in goal.attempts if item.get("id") == task.id), None)
        if entry is None:
            goal.attempts = [*goal.attempts, attempt_entry("task", task.id, outcome=outcome, note=note)]
        else:
            entry["outcome"], entry["note"] = outcome, note[:200] or entry.get("note", "")
        if isinstance(offer, dict) and offer.get("state") == "declined" and CLAUDE_CODE not in goal.declined:
            goal.declined = [*goal.declined, CLAUDE_CODE]
        return goal

    try:
        repositories.goals.update(str(goal_id), change)
    except Exception:  # noqa: BLE001 - keeping the record is never worth a failed task
        logger.warning("goal_not_synced", exc_info=True)
