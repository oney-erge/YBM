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
import json
import re
from typing import Literal

from agent_control.plans.model import PlanStatus, StepOwner, StepStatus
from agent_control.plans.runner import is_skip
from agent_control.schemas import AuditEventType, TaskRecord, TaskStatus, utc_now
from agent_control.storage.audit import AuditLogger
from agent_control.storage.repositories import Repositories

CANCEL_WORDS = {"cancel", "stop", "drop it", "never mind", "nevermind", "forget it", "no"}

# How long a question stays open for a reply. Without a limit, any task left in
# CLARIFYING captures the next message in its conversation however old it is:
# a question asked weeks ago turned a brand-new request into its "answer" and
# the bot resumed the old task instead.
CLARIFICATION_MAX_AGE = timedelta(hours=24)
_DELEGATE_CHOICE = re.compile(
    r"^(?:please\s+)?(?:(?:(?:can|could|would)\s+)?you\s+(?:choose|decide|pick)(?:\s+(?:it|one|for\s+me))?|"
    r"(?:choose|decide|pick)(?:\s+(?:it|one))?\s+(?:for\s+me|yourself)|"
    r"(?:it(?:'s|\s+is)|that(?:'s|\s+is))\s+up\s+to\s+you|your\s+choice|use\s+your\s+(?:judg(?:e)?ment|discretion))\s*[.!?]*$",
    re.IGNORECASE,
)


def delegates_choice(text: str) -> bool:
    """Recognize an explicit handoff of a pending choice, not an unrelated request."""
    return bool(_DELEGATE_CHOICE.fullmatch(" ".join((text or "").split())))


def defers_plan_step(text: str) -> bool:
    """A short deferral leaves a user-owned step waiting rather than skipping it."""
    value = " ".join((text or "").casefold().split()).rstrip(".!?")
    return value in {"no", "no thanks", "not yet", "not now", "not now, later", "i need more time", "cancel", "stop"}


def explicit_question_answer(task: TaskRecord, text: str) -> bool:
    """Replies with an unambiguous built-in meaning need no model call."""
    from agent_control.channels.quick_answers import clarifying_options

    answer = " ".join((text or "").casefold().split()).strip(".!?")
    options = {" ".join(option.casefold().split()).strip(".!?") for option in clarifying_options(task)}
    if answer in options or answer in CANCEL_WORDS or answer in {"yes", "yes please", "no thanks", "no thank you"}:
        return True
    if delegates_choice(text):
        return True
    if task.metadata.get("plan_id") and defers_plan_step(text):
        return True
    offer = task.metadata.get("claude_code_offer")
    return bool(isinstance(offer, dict) and offer.get("state") == "pending" and re.fullmatch(
        r"no[, ]+\s*(?:(?:i want )?you (?:to )?(?:do (?:it|this)|keep trying|continue)|"
        r"keep trying(?: locally)?|continue(?: locally)?|try again|do it yourself|use (?:another|a different) (?:method|approach|route))",
        answer,
    ))


async def assess_unaddressed_reply(task: TaskRecord, text: str, decision: object | None) -> Literal["reply", "new", "uncertain"]:
    """Separate a free-form answer from a new request; unavailable or weak opinions remain unresolved."""
    if explicit_question_answer(task, text):
        return "reply"
    if decision is None or getattr(decision, "mode", "off") not in {"cascade", "decision"}:
        return "uncertain"
    from agent_control.llm.decision import DecisionModelClient

    state = json.dumps({"pending_task": task.objective[:1000],
                        "pending_question": str(task.metadata.get("clarifying_question") or "")[:1000],
                        "user_message": text[:2000]}, ensure_ascii=False)
    try:
        answers = await DecisionModelClient(decision).yes_no(state, {
            "reply": "Is the user message an answer or instruction about the pending question?",
            "unrelated": "Is the user message a new unrelated request?",
        })
        reply, unrelated = answers.get("reply"), answers.get("unrelated")
        if reply is not None and unrelated is not None:
            if reply >= 0.9 and unrelated <= 0.1:
                return "reply"
            if unrelated >= 0.9 and reply <= 0.1:
                return "new"
    except Exception:  # noqa: BLE001 - an unavailable classifier cannot safely choose either target
        pass
    return "uncertain"


def objective_with_answer(objective: str, question: str | None, answer: str) -> str:
    """The objective with the user's answer folded in, after the question it answers.

    An answer such as "yes, go ahead with what you proposed" means nothing without the question, and
    the Operator never saw its own question once the task resumed: in a live run it asked the same
    question again and the task waited until it was cancelled.
    """
    asked = " ".join(str(question or "").split())
    if asked:
        return f"{objective}\n[Question to the user: {asked}]\n[User clarification: {answer}]"
    return f"{objective}\n[User clarification: {answer}]"


def objective_after_answer(task: TaskRecord, answer: str) -> str:
    """The task's objective once `answer` is in: the question and answer folded in, except for a permission question.

    "May I send this to a hosted model?" and its "yes" are not part of what the user asked for, and the word "send"
    in it would make the fulfillment check expect a delivery. The worker reads that answer from the task's
    metadata (llm/cloud_gate.py) instead.
    """
    pending = task.metadata.get("cloud_fallback_pending")
    if isinstance(pending, dict) and pending.get("question") == task.metadata.get("clarifying_question"):
        return task.objective
    return objective_with_answer(task.objective, task.metadata.get("clarifying_question"), answer)


@dataclass
class ClarificationResumeResult:
    task: TaskRecord
    cancelled: bool
    #: The question was a step of a plan (plans/runner.py), answered or skipped, not a task that goes on working.
    plan_step: bool = False
    accepted: bool = True
    delegated: bool = False
    reason: str | None = None


def _same_conversation(task: TaskRecord, conversation_id: str | None, chat_id: str | None) -> bool:
    """Use the conversation identity when known; chat IDs alone can overlap across channels."""
    if conversation_id is not None:
        if task.conversation_id is not None:
            return task.conversation_id == conversation_id
        channel = task.metadata.get("source_channel")
        return bool(channel and chat_id is not None and conversation_id == f"conv_{channel}_{chat_id}"
                    and str(task.metadata.get("source_chat_id")) == str(chat_id))
    return chat_id is not None and str(task.metadata.get("source_chat_id")) == str(chat_id)


def is_user_plan_step(repositories: Repositories, task: TaskRecord) -> bool:
    """Only a plan step actually waiting for the person is completed by their reply.

    An assistant-owned step may itself ask a question. Its answer belongs to the
    running task and must not be mistaken for a report that the step is done.
    """
    plan_id = task.metadata.get("plan_id")
    step_id = task.metadata.get("plan_step_id")
    plan = repositories.plans.get(str(plan_id)) if plan_id and step_id else None
    return bool(plan and any(
        step.id == step_id and step.task_id == task.id
        and step.owner != StepOwner.YBM and step.status == StepStatus.WAITING
        for step in plan.steps
    ))


def find_clarifying_tasks(
    repositories: Repositories,
    *,
    conversation_id: str | None,
    chat_id: str | None,
    max_age: timedelta = CLARIFICATION_MAX_AGE,
    now: datetime | None = None,
) -> list[TaskRecord]:
    """Recent ordinary questions belonging to this conversation."""
    cutoff = (now or utc_now()) - max_age
    return [
            candidate for candidate in repositories.tasks.list_by_statuses([TaskStatus.CLARIFYING], limit=200)
            # A step of a plan that waits for the user is not an open question for whatever they say next: it waits for days, and is
            # answered by its own buttons or by the words it offered (find_plan_step_answer), so it never takes an unrelated message.
            if not is_user_plan_step(repositories, candidate)
            and candidate.updated_at >= cutoff
            and _same_conversation(candidate, conversation_id, chat_id)
    ]


def find_clarifying_task(
    repositories: Repositories, *, conversation_id: str | None, chat_id: str | None,
    max_age: timedelta = CLARIFICATION_MAX_AGE, now: datetime | None = None,
) -> TaskRecord | None:
    candidates = find_clarifying_tasks(repositories, conversation_id=conversation_id, chat_id=chat_id, max_age=max_age, now=now)
    return candidates[0] if len(candidates) == 1 else None


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
        if candidate.updated_at >= cutoff or is_user_plan_step(repositories, candidate):
            continue
        updated = repositories.tasks.answer_clarification(candidate, objective=candidate.objective, metadata=candidate.metadata,
                                                          status=TaskStatus.CANCELLED, user_plan_step=False)
        if updated is None:
            continue
        expired.append(updated)
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


def find_plan_step_answers(
    repositories: Repositories, *, conversation_id: str | None, chat_id: str | None, text: str
) -> list[TaskRecord]:
    """The open step of a plan in this chat that `text` answers: exactly one of the words its buttons offer, or "skip".

    A step waits for the user for as long as the thing it asks for takes, so it cannot take whatever they say next as its answer the way a
    question about one task does. What it takes is what a tap on its buttons would have sent.
    """
    # Import after module initialization: channels.base also uses this module.
    from agent_control.channels.quick_answers import clarifying_options

    wanted = " ".join((text or "").lower().split()).rstrip(".!?")
    if not wanted:
        return []
    answers = []
    for candidate in repositories.tasks.list_by_statuses([TaskStatus.CLARIFYING], limit=200):
        if not is_user_plan_step(repositories, candidate):
            continue
        same_chat = _same_conversation(candidate, conversation_id, chat_id)
        if same_chat and (is_skip(wanted) or delegates_choice(wanted) or defers_plan_step(wanted)
                          or wanted in {option.lower().rstrip(".!?") for option in clarifying_options(candidate)}):
            answers.append(candidate)
    return answers


def find_plan_step_answer(
    repositories: Repositories, *, conversation_id: str | None, chat_id: str | None, text: str,
) -> TaskRecord | None:
    answers = find_plan_step_answers(repositories, conversation_id=conversation_id, chat_id=chat_id, text=text)
    return answers[0] if len(answers) == 1 else None


def reply_candidates(repositories: Repositories, *, conversation_id: str | None, chat_id: str | None, text: str) -> list[TaskRecord]:
    """All plausible targets; never choose arbitrarily when more than one is open."""
    plans = find_plan_step_answers(repositories, conversation_id=conversation_id, chat_id=chat_id, text=text)
    ordinary = find_clarifying_tasks(repositories, conversation_id=conversation_id, chat_id=chat_id)
    return list({task.id: task for task in [*plans, *ordinary]}.values())


def explicit_reply_task(repositories: Repositories, *, task_id: str, conversation_id: str | None, chat_id: str | None) -> TaskRecord | None:
    task = repositories.tasks.get(task_id)
    return task if task is not None and task.status == TaskStatus.CLARIFYING and _same_conversation(task, conversation_id, chat_id) else None


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
    if is_user_plan_step(repositories, task):
        if delegates_choice(text):
            return _delegate_plan_step(repositories, audit, task, text=text, actor=actor, message_id=message_id,
                                       received_at=received_at, correlation_id=correlation_id)
        return _answer_plan_step(repositories, audit, task, text=text, actor=actor, correlation_id=correlation_id)
    offer = task.metadata.get("claude_code_offer")
    declining_offer = isinstance(offer, dict) and offer.get("state") == "pending" and text.strip().lower() == "no"
    from agent_control.channels.quick_answers import clarifying_options
    no_is_offered_answer = text.strip().casefold() == "no" and any(option.casefold() == "no" for option in clarifying_options(task))
    if text.lower() in CANCEL_WORDS and not declining_offer and not no_is_offered_answer:
        updated = repositories.tasks.answer_clarification(task, objective=task.objective, metadata=task.metadata,
                                                          status=TaskStatus.CANCELLED, user_plan_step=False)
        if updated is None:
            return ClarificationResumeResult(task=repositories.tasks.get(task.id) or task, cancelled=False, accepted=False)
        audit.append(
            AuditEventType.TASK_STATE_CHANGED,
            actor=actor,
            task_id=task.id,
            correlation_id=correlation_id,
            payload={"reason": "clarification_cancelled", "status": TaskStatus.CANCELLED.value},
        )
        return ClarificationResumeResult(task=updated, cancelled=True)

    # Fold the answer into the objective so replanning sees it, and reset
    # the attempt counters - the user's input makes this a fresh attempt.
    objective = objective_after_answer(task, text)
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
    updated = repositories.tasks.answer_clarification(task, objective=objective, metadata=metadata,
                                                      status=TaskStatus.RECEIVED, user_plan_step=False)
    if updated is None:
        return ClarificationResumeResult(task=repositories.tasks.get(task.id) or task, cancelled=False, accepted=False)
    audit.append(
        AuditEventType.TASK_STATE_CHANGED,
        actor=actor,
        task_id=task.id,
        correlation_id=correlation_id,
        payload={"reason": "clarification_answered", "answer": text[:400], "status": TaskStatus.RECEIVED.value},
    )
    return ClarificationResumeResult(task=updated, cancelled=False)


def _delegate_plan_step(
    repositories: Repositories, audit: AuditLogger, task: TaskRecord, *, text: str, actor: str,
    message_id: str, received_at: datetime, correlation_id: str | None,
) -> ClarificationResumeResult:
    plan = repositories.plans.get(str(task.metadata.get("plan_id") or ""))
    step = next((item for item in plan.steps if item.id == task.metadata.get("plan_step_id")), None) if plan else None
    if plan is None or step is None:
        return ClarificationResumeResult(task=repositories.tasks.get(task.id) or task, cancelled=False, accepted=False)
    if plan.status != PlanStatus.ACTIVE:
        return ClarificationResumeResult(task=task, cancelled=False, accepted=False,
                                         reason="The plan is paused. Resume it before delegating this step.")
    objective = (f"Choose an option for this plan step: {step.title}\n"
                 f"Context: {step.detail}\nPlan goal: {plan.goal}\n"
                 f"The user delegated the choice: {text}. Explain the choice and any remaining prerequisites. "
                 "The choice does not itself complete other parts of the step. Follow the existing approval policy.")
    answers = list(task.metadata.get("clarification_answers") or [])
    answers.append({"question": task.metadata.get("clarifying_question"), "answer": text, "message_id": message_id,
                    "created_at": received_at.isoformat()})
    metadata = {**task.metadata, "clarification_answer": text, "clarification_answers": answers,
                "answered_clarifying_question": task.metadata.get("clarifying_question"), "plan_delegated_from_owner": step.owner.value,
                "delegated_choice_contract": {"title": step.title, "detail": step.detail, "needs": step.needs,
                                              "choice_only": step.choice_only},
                "original_message_text": f"Choose an option for: {step.title}"}
    metadata.pop("clarifying_question", None)
    metadata.pop("clarifying_options", None)
    updated = repositories.plans.delegate_waiting_step(plan, step, task, objective=objective, metadata=metadata)
    if updated is None:
        return ClarificationResumeResult(task=repositories.tasks.get(task.id) or task, cancelled=False, accepted=False)
    audit.append(AuditEventType.TASK_STATE_CHANGED, actor=actor, task_id=task.id, correlation_id=correlation_id,
                 payload={"reason": "plan_step_delegated", "previous_owner": step.owner.value})
    return ClarificationResumeResult(task=updated, cancelled=False, plan_step=True, delegated=True)


def _answer_plan_step(
    repositories: Repositories, audit: AuditLogger, task: TaskRecord, *, text: str, actor: str, correlation_id: str | None
) -> ClarificationResumeResult:
    """Accept explicit completion or skip for a step that belongs to the user.

    The task ends here (nothing is left for the Operator to do with it) and the plan runner reads how it ended on its next pass: completed is
    a step done, with the answer kept as its result, and cancelled is a step skipped.
    """
    skipped = is_skip(text)
    answer_text = " ".join((text or "").casefold().split()).rstrip(".!?")
    completion_reports = {"done", "finished", "completed", "ready", "it's ready", "it is ready"}
    if not skipped and answer_text not in completion_reports:
        if answer_text in {"cancel", "stop"}:
            return ClarificationResumeResult(task=task, cancelled=False, plan_step=True, accepted=False,
                                             reason="The step is still waiting. Use Pause or Cancel on the plan to stop it, "
                                                    "or say Skip to leave out only this step.")
        return ClarificationResumeResult(task=task, cancelled=False, plan_step=True, accepted=False,
                                         reason="I have not marked this step done. Reply Done when it is complete, "
                                                "or say Skip to leave it out. Say 'you choose' to delegate a choice.")
    if skipped:
        metadata, status = task.metadata, TaskStatus.CANCELLED
    else:
        prior_choice = str(task.metadata.get("delegated_choice_result") or "")
        answer = f"{text}\nChoice: {prior_choice}" if prior_choice else text
        metadata = {**task.metadata, "plan_step_answer": answer, "synthesized_answer": answer}
        metadata.pop("clarifying_question", None)
        status = TaskStatus.COMPLETED
    updated = repositories.tasks.answer_clarification(task, objective=task.objective, metadata=metadata,
                                                      status=status, user_plan_step=True)
    if updated is None:
        return ClarificationResumeResult(task=repositories.tasks.get(task.id) or task, cancelled=False, plan_step=True, accepted=False)
    audit.append(
        AuditEventType.TASK_STATE_CHANGED, actor=actor, task_id=task.id, correlation_id=correlation_id,
        payload={"reason": "plan_step_skipped" if skipped else "plan_step_answered", "answer": text[:400]},
    )
    return ClarificationResumeResult(task=updated, cancelled=skipped, plan_step=True)
