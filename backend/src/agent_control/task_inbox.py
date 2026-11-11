"""Let the operator talk to work that is already running.

Until now a task in flight was deaf. The only things that reached it were
`pause`, `resume` and `cancel`; anything else you typed was either answered as
chat or spawned a *second* task. The one channel inward - `resume_clarifying_reply` -
opens only while the task sits in CLARIFYING, which is to say only when the
agent asked you a question first. You could answer. You could never begin a
sentence.

So "actually make it five, not three" meant cancelling and starting over,
throwing away everything the run had already done.

A note lands in `operator_history` like any other entry, and the Operator sees
it on its next step alongside its own tool results. It is not a command and
not a new objective: the loop weighs it in context, which is the same way it
handles every other piece of evidence. Prefixed with `_` so the existing
"skip bookkeeping entries" filters in task_notify and elsewhere already ignore
it when summarizing tool attempts.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from agent_control.human_text import humanize, progress_phrase
from agent_control.schemas import AuditEventType, TaskRecord, TaskStatus, utc_now
from agent_control.storage.audit import AuditLogger
from agent_control.storage.repositories import Repositories


NOTE_ENTRY_NAME = "_user_note"

# A task in any of these is still doing something a note could usefully change.
# CLARIFYING is deliberately absent: that has its own resume path which feeds
# the answer back to the specific question that was asked.
STEERABLE_STATUSES = [
    TaskStatus.RECEIVED,
    TaskStatus.INTERPRETING,
    TaskStatus.PLANNED,
    TaskStatus.RUNNING,
    TaskStatus.RETRYING,
    TaskStatus.AWAITING_APPROVAL,
    TaskStatus.AWAITING_EXTERNAL,
]


def find_steerable_task(
    repositories: Repositories, *, conversation_id: str | None, chat_id: str | None
) -> TaskRecord | None:
    """The most recent in-flight task for this conversation, if any.

    Newest wins: if two are somehow live, the operator almost certainly means
    the one they just watched start.
    """
    candidates = [
        task
        for task in repositories.tasks.list_by_statuses(STEERABLE_STATUSES, limit=20)
        if (conversation_id and task.conversation_id == conversation_id)
        or (chat_id and str(task.metadata.get("source_chat_id") or "") == str(chat_id))
    ]
    return max(candidates, key=lambda task: task.created_at) if candidates else None


def attach_note(
    repositories: Repositories,
    audit: AuditLogger,
    task: TaskRecord,
    *,
    text: str,
    actor: str,
) -> TaskRecord:
    """Append the operator's words to the running task's history."""
    note = (text or "").strip()
    if not note:
        return task
    history = list(task.metadata.get("operator_history") or [])
    history.append({
        "tool_name": NOTE_ENTRY_NAME,
        "input": None,
        "status": "user_note",
        "output_summary": note[:2000],
        "error": None,
    })
    metadata = {**task.metadata, "operator_history": history}
    # Counted so a reader of the trace can see the task was steered, without
    # having to scan history for note entries.
    metadata["user_note_count"] = int(task.metadata.get("user_note_count", 0)) + 1
    updated = repositories.tasks.update_metadata(task.id, metadata)
    audit.append(
        AuditEventType.TASK_STATE_CHANGED,
        actor=actor,
        task_id=task.id,
        payload={"action": "user_note", "note": note[:500], "status": task.status.value},
    )
    return updated


def acknowledgement(task: TaskRecord) -> str:
    """What to say back. Names the task being steered so a note that lands on
    the wrong one is obvious immediately, rather than at the end."""
    return (
        f"Noted - I'll factor that into the task I'm working on now "
        f"(\"{task.objective[:80]}\"). Say \"cancel\" if you'd rather I stop it."
    )


# --- "How is it going?" -----------------------------------------------------------------------
#
# A message sent while a task runs used to go through the whole intake again: the concierge model
# classified it, and "what are you doing right now?" came back as a brand-new task (a second copy
# of the same web app was built) while "how's it going?" got a cheerful chat answer that said
# nothing about the job. Asking about progress is the most natural thing to do mid-task, and the
# answer is already in the task's own record, so it is read from there: instant, no model call, no
# new task.

_STATUS_QUESTION = re.compile(
    r"^\s*(?:(?:hey|hi|ok|okay|so|and|well)[,!.]?\s+)*(?:"
    r"how(?:'s|s| is| are)(?: it| things| that| the (?:task|job|work|build))? "
    r"(?:going|coming along|progressing|looking|getting on)"
    r"|any (?:update|updates|news|progress)(?: yet| on (?:it|that|this))?"
    r"|(?:an? )?(?:status|progress)(?: update| report| check)?"
    r"|what(?:'s|s| is| are) (?:you|it|that|this) (?:doing|up to|working on)(?: right now| now| at the moment)?"
    r"|what(?:'s|s| is) (?:happening|going on)(?: right now| now| there)?"
    r"|are you (?:done|finished|still (?:working|there|on it|busy)|ready|stuck)(?: yet)?"
    r"|is (?:it|that|this) (?:done|ready|finished|working)(?: yet)?"
    r"|(?:are we|are you|you) (?:there|done)(?: yet)?"
    r"|how long(?: (?:will|is|until|before)(?: it| this| that)?(?: take| going| done| left| ready)*)?(?: to go)?"
    r"|where (?:are you|are we|is it) at"
    r")(?: right now| now| so far)?\s*[?.!]*\s*$",
    re.IGNORECASE,
)

#: A task that finished this recently is still "what we were just talking about"; older than this a
#: bare "how's it going?" is small talk and goes to the normal chat reply.
RECENT_TASK_WINDOW = timedelta(minutes=30)


def is_status_question(text: str) -> bool:
    return bool(text) and len(text) <= 80 and _STATUS_QUESTION.match(text) is not None


def _minutes_since(moment: datetime | None) -> int:
    if moment is None:
        return 0
    try:
        return max(0, int((utc_now() - moment).total_seconds() // 60))
    except TypeError:
        return 0


def _quoted(objective: str) -> str:
    text = humanize(" ".join((objective or "").split()))
    return text if len(text) <= 90 else text[:87].rstrip() + "..."


def _happening(task: TaskRecord) -> str:
    history = [
        item for item in (task.metadata.get("operator_history") or [])
        if isinstance(item, dict) and not str(item.get("tool_name") or "").startswith("_")
    ]
    minutes = _minutes_since(task.created_at)
    took = f"{minutes} min in" if minutes >= 1 else "just started"
    if task.status in {TaskStatus.AWAITING_APPROVAL}:
        return f"I'm waiting for your OK to go on with \"{_quoted(task.objective)}\". Tap Approve below the last message, or say \"cancel\"."
    if task.status == TaskStatus.CLARIFYING:
        question = str(task.metadata.get("clarifying_question") or "").strip()
        return f"I'm waiting for your answer on \"{_quoted(task.objective)}\": {humanize(question)}" if question else (
            f"I'm waiting for your answer on \"{_quoted(task.objective)}\"."
        )
    if task.status == TaskStatus.AWAITING_EXTERNAL:
        session = task.metadata.get("awaiting_external")
        provider = str(session.get("provider") or "A coding assistant") if isinstance(session, dict) else "A coding assistant"
        return f"{provider} is working on \"{_quoted(task.objective)}\" in the background ({took}). I'll report back when it finishes."
    if task.status == TaskStatus.PAUSED:
        return f"\"{_quoted(task.objective)}\" is paused. Say \"resume\" to carry on or \"cancel\" to drop it."
    if not history:
        return f"Just getting started on \"{_quoted(task.objective)}\" ({took})."
    latest = history[-1]
    tool_input = latest.get("input") if isinstance(latest.get("input"), dict) else {}
    phrase = progress_phrase(str(latest.get("tool_name") or ""), str(tool_input.get("operation") or ""), tool_input)
    steps = "1 step" if len(history) == 1 else f"{len(history)} steps"
    return f"Still on \"{_quoted(task.objective)}\" ({took}, {steps} so far). The latest was {phrase}."


def status_reply(repositories: Repositories, *, conversation_id: str | None, chat_id: str | None) -> str | None:
    """A plain answer to "how is it going?", or None when there is nothing to report on."""
    def mine(task: TaskRecord) -> bool:
        return bool(
            (conversation_id and task.conversation_id == conversation_id)
            or (chat_id and str(task.metadata.get("source_chat_id") or "") == str(chat_id))
        )

    live_statuses = [*STEERABLE_STATUSES, TaskStatus.CLARIFYING, TaskStatus.PAUSED]
    live = [task for task in repositories.tasks.list_by_statuses(live_statuses, limit=30) if mine(task)]
    if live:
        lines = [_happening(task) for task in sorted(live, key=lambda t: t.created_at, reverse=True)[:3]]
        return "\n".join(lines)
    recent = next((task for task in repositories.tasks.list_recent(30) if mine(task)), None)
    if recent is None or _minutes_since(recent.updated_at) > RECENT_TASK_WINDOW.total_seconds() // 60:
        return None
    outcome = {
        TaskStatus.COMPLETED: "finished that",
        TaskStatus.BLOCKED: "got stuck on that",
        TaskStatus.FAILED: "could not finish that",
        TaskStatus.CANCELLED: "stopped that",
    }.get(recent.status)
    if outcome is None:
        return None
    return f"Nothing is running right now. The last thing was \"{_quoted(recent.objective)}\": I {outcome}."
