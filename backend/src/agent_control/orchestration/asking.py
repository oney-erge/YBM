"""When the Operator may ask the user something, and when a question is not needed: a satisfied task, a request that said to ask
first, a question that was already answered, a target that is simply not there."""
from __future__ import annotations

import re
from typing import Any

from agent_control.orchestration import ladder
from agent_control.orchestration.call_rewrites import known_written_file_path
from agent_control.orchestration.check_entries import CHECK_ENTRY_NAMES
from agent_control.orchestration.fulfillment import (
    validate_fulfillment,
)
from agent_control.orchestration.request_facts import RequestFacts, own_words
from agent_control.schemas import (
    PostconditionType,
    TaskRecord,
    ToolResultStatus,
)


def satisfied_task_does_not_need_clarification(
    task: TaskRecord,
    history: list[dict[str, Any]],
) -> bool:
    """Reject optional follow-up questions after typed work is complete.

    An empty expectation set cannot prove that the user's input is complete,
    so the guard deliberately does nothing for open-ended requests. At least
    one successful tool call and one satisfied inferred postcondition are
    required before asking the operator to finish instead of seek polish or
    confirmation that the user never made a prerequisite.
    """
    if user_asked_to_be_asked_first(task):
        return False
    validation = validate_fulfillment(task)
    if not validation.expected or not validation.ok:
        return False
    return any(
        isinstance(entry, dict)
        and entry.get("status") == ToolResultStatus.SUCCEEDED.value
        for entry in history
    )


def user_asked_to_be_asked_first(task: TaskRecord) -> bool:
    """Whether the request itself says to ask the user before acting.

    The guard that turns away an optional question after the work is done judged "Rewrite this file. Ask me before
    changing anything on disk" satisfied once the file was read, rejected the permission question the model rightly
    asked, and ended the task with a summary of the read: the change was never offered to the user.
    """
    return RequestFacts.from_task(task).asks_to_be_asked_first


def still_asking_after_the_work_is_done(task: TaskRecord, history: list[dict[str, Any]]) -> bool:
    """Whether a question comes after two rejections and the request's work is verifiably done."""
    if int(task.metadata.get("operator_optional_question_recovery_count", 0)) < 2:
        return False
    candidate = task.model_copy(update={"metadata": {**task.metadata, "operator_history": history}})
    return satisfied_task_does_not_need_clarification(candidate, history)


def users_own_words(task: TaskRecord) -> str:
    """What the user wrote, without the question and answer a clarification round appends to the task's objective.

    After "Question to the user: ... offer to move them to a folder ..." the objective holds the model's words as well, and an
    intent check that acts on the user's behalf (a delete refused, a file overwritten) must not read the model's words as theirs.
    """
    return own_words(task)


_NOT_FOUND_ERROR = ladder.NOT_FOUND_ERROR


def named_target_not_found(task: TaskRecord, history: list[dict[str, Any]]) -> str | None:
    """A reason to turn down a question when the last real step failed because the thing asked about is not there.

    Asked to read, open or send a file that does not exist, the local model asked the user which of four other files it meant,
    or to paste the contents, and the question waited. The honest answer is that the file is not there (and what similar items
    were found), which the user can reply to just as well. A request that says to ask first keeps its question.
    """
    if user_asked_to_be_asked_first(task):
        return None
    real = [e for e in history if isinstance(e, dict) and e.get("tool_name") not in CHECK_ENTRY_NAMES]
    if not real:
        return None
    last = real[-1]
    if last.get("status") != ToolResultStatus.FAILED.value or not _NOT_FOUND_ERROR.search(str(last.get("error") or "")):
        return None
    return (
        "the last step failed because what the request named does not exist, so the answer is that it is not there; tell the "
        "user so plainly in a final answer (action done), naming any similar items you found, instead of asking them to choose "
        "or to paste its contents"
    )


def clarification_recovery_reason(
    task: TaskRecord,
    history: list[dict[str, Any]],
    question: str | None = None,
) -> str | None:
    answer = str(task.metadata.get("clarification_answer") or "").strip()
    if answer and _same_question(question, task.metadata.get("answered_clarifying_question")):
        return f"this question was already asked and the user answered: {answer!r}. Act on that answer."
    if satisfied_task_does_not_need_clarification(task, history):
        return (
            "the requested work's inferred postconditions are already satisfied; "
            "report optional review or verification gaps instead of asking the user"
        )
    not_found = named_target_not_found(task, history)
    if not_found:
        return not_found

    validation = validate_fulfillment(task)
    missing = set(validation.missing)
    if PostconditionType.ARTIFACT_DELIVERED in missing:
        output_path = known_written_file_path(task, history)
        if output_path:
            return (
                f"artifact delivery is still required and the successful write already provides "
                f"the exact path {output_path!r}; use the delivery tool"
            )
    if PostconditionType.ADAPTER_PROPOSAL in missing and _request_has_named_adapter(task):
        return (
            "the user supplied a concrete adapter name, generic contract, and safety boundary; "
            "scaffold the cache-only proposal with conservative defaults and report review gaps"
        )
    return None


def _same_question(question: object, answered: object) -> bool:
    """Whether a new question asks what an answered one already asked.

    Compared on words, ignoring case and punctuation, with a high bar: a live run re-asked its
    answered question with only the closing sentence reworded.
    """
    words = [set(re.findall(r"[a-z0-9]+", str(text or "").casefold())) for text in (question, answered)]
    if not words[0] or not words[1]:
        return False
    return len(words[0] & words[1]) / len(words[0] | words[1]) >= 0.6


def _request_has_named_adapter(task: TaskRecord) -> bool:
    request_text = " ".join(
        str(value or "")
        for value in (task.metadata.get("original_message_text"), task.objective)
    ).casefold()
    return bool(
        re.search(r"\b(?:adapter|proposal)\s+(?:named|called)\s+[a-z][a-z0-9_-]{2,}\b", request_text)
        or re.search(r"\bcreate\s+(?:a\s+)?[a-z][a-z0-9_-]{2,}\s+adapter\b", request_text)
    )


def post_clarification_history_offset(task: TaskRecord, history: list[dict[str, Any]]) -> int:
    raw_offset = task.metadata.get("operator_history_offset_after_clarification")
    try:
        offset = int(raw_offset)
    except (TypeError, ValueError):
        # Conservative compatibility for a task resumed by an older process:
        # do not let any pre-answer content replace the current answer.
        return len(history) if task.metadata.get("clarification_answer") else 0
    return max(0, min(offset, len(history)))
