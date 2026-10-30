"""Shared "resume a clarifying task" logic, used by every inbound channel.

Extracted from channels/telegram.py's _resume_clarifying_task (the only
channel that had it) so admin.py's web chat endpoint can share the exact
same behavior instead of admin_send_chat_message's prior behavior of always
creating a brand-new, unrelated task while the real one sat stuck in
CLARIFYING forever.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from agent_control.schemas import AuditEventType, TaskRecord, TaskStatus, utc_now
from agent_control.storage.audit import AuditLogger
from agent_control.storage.repositories import Repositories

CANCEL_WORDS = {"cancel", "stop", "drop it", "never mind", "nevermind", "forget it", "no"}

# How long a question stays open for a reply. Without a limit, any task left in
# CLARIFYING captures the next message in its conversation however old it is:
# a question asked weeks ago turned a brand-new request into its "answer" and
# the bot resumed the old task instead.
CLARIFICATION_MAX_AGE = timedelta(hours=24)


@dataclass
class ClarificationResumeResult:
    task: TaskRecord
    cancelled: bool


def find_clarifying_task(
    repositories: Repositories,
    *,
    conversation_id: str | None,
    chat_id: str | None,
    max_age: timedelta = CLARIFICATION_MAX_AGE,
    now: datetime | None = None,
) -> TaskRecord | None:
    """The task in this conversation waiting on an answer, if its question is recent."""
    cutoff = (now or utc_now()) - max_age
    return next(
        (
            candidate
            for candidate in repositories.tasks.list_by_statuses([TaskStatus.CLARIFYING], limit=20)
            if candidate.updated_at >= cutoff
            and (
                (conversation_id is not None and candidate.conversation_id == conversation_id)
                or (chat_id is not None and str(candidate.metadata.get("source_chat_id")) == str(chat_id))
            )
        ),
        None,
    )


def expire_stale_clarifications(
    repositories: Repositories,
    audit: AuditLogger,
    *,
    max_age: timedelta = CLARIFICATION_MAX_AGE,
    now: datetime | None = None,
) -> list[TaskRecord]:
    """Cancel tasks whose question went unanswered past `max_age`.

    find_clarifying_task already ignores them; cancelling keeps the task list
    honest (nothing sits "waiting for your answer" forever) and leaves an audit
    trail for why.
    """
    cutoff = (now or utc_now()) - max_age
    expired: list[TaskRecord] = []
    for candidate in repositories.tasks.list_by_statuses([TaskStatus.CLARIFYING], limit=200):
        if candidate.updated_at >= cutoff:
            continue
        expired.append(repositories.tasks.update_status(candidate.id, TaskStatus.CANCELLED))
        audit.append(
            AuditEventType.TASK_STATE_CHANGED,
            actor="system",
            task_id=candidate.id,
            payload={
                "reason": "clarification_expired",
                "status": TaskStatus.CANCELLED.value,
                "waiting_since": candidate.updated_at.isoformat(),
            },
        )
    return expired


def resume_clarifying_task(
    repositories: Repositories,
    audit: AuditLogger,
    task: TaskRecord,
    *,
    text: str,
    actor: str,
    message_id: str,
    received_at: datetime,
    correlation_id: str | None = None,
) -> ClarificationResumeResult:
    """Route a reply to the task waiting on a question instead of spawning a new task."""
    if text.lower() in CANCEL_WORDS:
        repositories.tasks.update_status(task.id, TaskStatus.CANCELLED)
        audit.append(
            AuditEventType.TASK_STATE_CHANGED,
            actor=actor,
            task_id=task.id,
            correlation_id=correlation_id,
            payload={"reason": "clarification_cancelled", "status": TaskStatus.CANCELLED.value},
        )
        updated = repositories.tasks.get(task.id)
        return ClarificationResumeResult(task=updated or task, cancelled=True)

    # Fold the answer into the objective so replanning sees it, and reset
    # the attempt counters - the user's input makes this a fresh attempt.
    objective = f"{task.objective}\n[User clarification: {text}]"
    repositories.tasks.update_objective(task.id, objective)
    answers = list(task.metadata.get("clarification_answers") or [])
    answers.append(
        {
            "question": task.metadata.get("clarifying_question"),
            "answer": text,
            "message_id": message_id,
            "created_at": received_at.isoformat(),
        }
    )
    metadata = {
        **task.metadata,
        "clarification_answer": text,
        "clarification_answers": answers,
        "answered_clarifying_question": task.metadata.get("clarifying_question"),
        # Evidence collected before the user answered is still useful context
        # for the Operator, but it must not be treated as fresh output by the
        # post-DONE Auditor. Without this checkpoint, a read performed before
        # a confirmation question could be synthesized again after the user
        # declined the change, replacing the Operator's correct "I stopped"
        # response with stale content.
        "operator_history_offset_after_clarification": len(task.metadata.get("operator_history") or []),
        "retry_count": 0,
        "replan_count": 0,
        "evaluator_repair_count": 0,
        "fulfillment_retry_count": 0,
    }
    metadata.pop("clarifying_question", None)
    updated = repositories.tasks.update_metadata(task.id, metadata, TaskStatus.RECEIVED)
    audit.append(
        AuditEventType.TASK_STATE_CHANGED,
        actor=actor,
        task_id=task.id,
        correlation_id=correlation_id,
        payload={"reason": "clarification_answered", "answer": text[:400], "status": TaskStatus.RECEIVED.value},
    )
    return ClarificationResumeResult(task=updated, cancelled=False)
