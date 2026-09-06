"""The channel-agnostic half of "intake -> classify -> task -> notify"
(docs/UI_UX_AUDIT.md Phase 16).

Telegram (`channels/telegram.py`) was the only channel for a while, and its
intake service used to inline all four stages as private methods on one
Telegram-specific class. The classify/task stages never actually depended on
anything Telegram-specific - they operate on `InboundMessage`, `Repositories`,
and the already-channel-agnostic `MessageClassifier`/`ChatResponder`
protocols - so they're extracted here as plain functions a second channel's
intake service can call directly instead of forking. WhatsApp
(`channels/whatsapp.py`) is that second channel, and its own plain-text
"approve"/"status" handling reuses `approve_latest_pending`/`status_summary`
below unchanged - the validation this module's docstring used to say was
still pending.

What is deliberately NOT extracted, and why: the intake stage itself
(`ChannelAdapter.normalize_update`, e.g. `TelegramAdapter`'s Telegram-JSON
parsing vs. `WhatsAppAdapter`'s already-clean bridge JSON) and Telegram's own
`/command` slash syntax and inline-keyboard callback queries have no shared
shape between the two real channels - WhatsApp has neither concept, so
generalizing them would be guessing at a boundary with only one real data
point. `TaskNotificationSink` (orchestration/worker.py) is the working seam
on the notify side; `channels/task_notify.py` holds the shared notification
text both channels' notifiers render into their own transport.
"""

from __future__ import annotations

import time

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import re
from typing import Any, Protocol

from agent_control.channels.memory import memory_context
from agent_control.channels.responder import ChatResponder, gateway_context
from agent_control.clarification import find_clarifying_task, resume_clarifying_task
from agent_control.config import AppSettings
from agent_control.llm.classifier import MessageClassifier, classification_trace
from agent_control.orchestration.signals import requeue_after_approval_decision
from agent_control.schemas import (
    ApprovalStatus,
    AuditEventType,
    ChannelType,
    CommandEnvelope,
    InboundMessage,
    IntentRoute,
    MemoryFact,
    MemorySource,
    MessageClassification,
    OutboundMessage,
    TaskRecord,
    TaskSignal,
    TaskStatus,
    TaskType,
    VoiceAttachment,
)
from agent_control.storage.audit import AuditLogger
from agent_control.storage.repositories import Repositories
from agent_control.task_inbox import acknowledgement, attach_note, find_steerable_task


@dataclass(frozen=True)
class ChannelUpdateResult:
    """What handling one inbound update produced - the same shape regardless
    of which channel it came from. Every field is already channel-agnostic
    (no channel carries its own variant of this)."""

    authorized: bool
    inbound_message: InboundMessage | None = None
    command: CommandEnvelope | None = None
    classification: MessageClassification | None = None
    signal: TaskSignal | None = None
    task: TaskRecord | None = None
    outbound_message: OutboundMessage | None = None
    denial_reason: str | None = None


class ChannelAdapter(Protocol):
    """The intake contract: turn a channel's own raw update payload (a
    Telegram webhook JSON dict, a WhatsApp/Discord equivalent, ...) into a
    `ChannelUpdateResult` - authorization and wire-format parsing are the
    one part of intake that's inherently channel-specific, so this Protocol
    only fixes the shape of the output, not how it's produced."""

    channel: ChannelType

    def normalize_update(self, update: dict[str, Any]) -> ChannelUpdateResult: ...


def _reply(inbound: InboundMessage, text: str) -> OutboundMessage:
    return OutboundMessage(channel=inbound.channel, chat_id=inbound.chat_id, text=text)


# Named (not inlined) so a channel's own `send_progress` implementation can
# recognize and selectively skip the pre-classification acknowledgment -
# WhatsAppIntakeService._send_progress does exactly this: it's pure filler
# with no lasting information (the real reply, or ACKNOWLEDGMENT_TEXT's own
# task-started counterpart below, follows regardless), and every unofficial-
# client message is extra exposure to the account-flagging risk Baileys
# already carries. Telegram's own _send_progress sends both unchanged.
ACKNOWLEDGMENT_TEXT = "Got your message, figuring out what to do…"
TASK_STARTED_TEXT = "On it - I'll send the result here when it's done."


def status_summary(repositories: Repositories) -> str:
    """Shared "what's going on" text - the same summary every channel's
    plain-text /status-equivalent command can show."""
    recent = repositories.tasks.list_recent(20)
    active_statuses = {
        TaskStatus.RECEIVED, TaskStatus.INTERPRETING, TaskStatus.PLANNED,
        TaskStatus.RUNNING, TaskStatus.AWAITING_APPROVAL, TaskStatus.RETRYING,
    }
    active = [task for task in recent if task.status in active_statuses]
    lines = [f"{len(recent)} recent task(s), {len(active)} active."]
    for task in recent[:5]:
        lines.append(f"- {task.status.value}: {task.objective[:120]}")
    return "\n".join(lines)


def approve_latest_pending(repositories: Repositories, audit: AuditLogger, inbound: InboundMessage) -> OutboundMessage:
    """Plain-text "approve" - approves every pending approval on the most
    recent task belonging to this chat, on whichever channel sent it (the
    conversation-id prefix is `conv_<channel>_<chat_id>`, see
    ConversationRepository.get_or_create)."""
    chat_id = str(inbound.chat_id)
    conversation_prefix = f"conv_{inbound.channel.value}_{chat_id}"
    for task in repositories.tasks.list_recent(50):
        if task.conversation_id != conversation_prefix and str(task.metadata.get("source_chat_id")) != chat_id:
            continue
        pending = [a for a in repositories.approvals.list_for_task(task.id) if a.status == ApprovalStatus.PENDING]
        if not pending:
            continue
        approved_count = 0
        for approval in pending:
            if not repositories.approvals.decide_pending(approval.id, ApprovalStatus.APPROVED):
                continue
            approved_count += 1
            audit.append(
                AuditEventType.APPROVAL_DECIDED,
                actor=f"{inbound.channel.value}:user:{inbound.sender_id}",
                task_id=task.id,
                correlation_id=inbound.correlation_id,
                payload={"approval_id": approval.id, "decision": "approve", "source": "plain_text"},
            )
        if approved_count:
            requeue_after_approval_decision(repositories, task.id)
            return _reply(inbound, f"Approved {approved_count} pending approval(s) for {task.id}.")
    return _reply(inbound, "No live pending approval found.")


def resume_clarifying_reply(
    repositories: Repositories, audit: AuditLogger, inbound: InboundMessage, conversation_id: str
) -> OutboundMessage | None:
    """Route a reply to the task waiting on a question instead of spawning a
    new task, or None if no clarification is pending. Core logic lives in
    clarification.py so the web chat channel (admin.py's
    admin_send_chat_message) shares the exact same behavior."""
    text = (inbound.text or "").strip()
    if not text:
        return None
    task = find_clarifying_task(repositories, conversation_id=conversation_id, chat_id=inbound.chat_id)
    if task is None:
        return None
    result = resume_clarifying_task(
        repositories, audit, task,
        text=text, actor=f"{inbound.channel.value}:user:{inbound.sender_id}",
        message_id=inbound.id, received_at=inbound.received_at, correlation_id=inbound.correlation_id,
    )
    if result.cancelled:
        return _reply(inbound, f"Cancelled: {result.task.objective[:200]}")
    return _reply(inbound, "Got it - resuming the task with your answer.")


def _spawn_failed(
    audit: AuditLogger,
    inbound: InboundMessage,
    reason: str,
    actor: str,
    classification: MessageClassification | None = None,
) -> ChannelUpdateResult:
    audit.append(
        AuditEventType.TASK_SPAWN_FAILED,
        actor=actor,
        correlation_id=inbound.correlation_id,
        payload={
            "message_id": inbound.id,
            "chat_id": inbound.chat_id,
            "sender_id": inbound.sender_id,
            "text": inbound.text,
            "reason": reason,
            "classification": classification.model_dump(mode="json") if classification else None,
        },
    )
    return ChannelUpdateResult(
        authorized=True,
        inbound_message=inbound,
        classification=classification,
        outbound_message=_reply(inbound, f"I could not start this request: {reason}"),
    )


async def _non_task_response(
    audit: AuditLogger,
    repositories: Repositories,
    inbound: InboundMessage,
    classification: MessageClassification,
    conversation_id: str,
    responder: ChatResponder | None,
) -> OutboundMessage | None:
    if classification.task_type == TaskType.STATUS_REQUEST:
        return _reply(inbound, status_summary(repositories))
    # The Concierge composes the chat reply in the same call it classifies
    # (see prompts/base/concierge_system.md) - no second LLM round trip
    # needed. Falls back to the separate `responder` (if configured) for a
    # classifier that doesn't populate `.reply`.
    reply = (classification.reply or "").strip()
    if reply:
        return _reply(inbound, reply[:3900])
    if responder is None:
        return None
    try:
        answer = await responder.answer(inbound, conversation_id)
    except Exception as exc:
        actor = f"{inbound.channel.value}:user:{inbound.sender_id}"
        return _spawn_failed(audit, inbound, f"response generation failed: {exc}", actor, classification).outbound_message
    return _reply(inbound, answer[:3900])


# A durable instruction is phrased as a standing rule, not a one-off request.
# These are the openings people actually use for one; a message that merely
# mentions "always" mid-sentence does not match because the cue has to lead the
# clause it governs.
_STANDING_INSTRUCTION = re.compile(
    r"(?i)(?:^|[.!?;\n]\s*|,\s*)(?:please\s+)?(?:"
    # "remember when ..." is reminiscence, not an instruction to keep.
    r"remember(?:\s+(?:this|that|it))?\b(?!\s+when\b)"
    r"|from now on\b"
    r"|going forward\b"
    r"|in future\b|in the future\b"
    r"|whenever\s+(?:you|i)\b"
    r"|every\s+time\s+(?:you|i)\b"
    r"|always\s+(?:answer|reply|use|give|format|include|send|write|show|prefer)\b"
    r"|never\s+(?:answer|reply|use|give|format|include|send|write|show)\b"
    r"|i\s+(?:always\s+)?(?:prefer|want you to|would like you to)\b"
    r"|my\s+preference\s+is\b"
    r")"
)


def _standing_instruction(text: str) -> str | None:
    """The durable rule a chat message states, or None if it states none.

    The chat route used to compose an agreeable "Understood, all future
    summaries will be…" and persist nothing, so the next task had no fact to
    read and silently ignored the rule (docs/E2E_FINDINGS.md P1-3). Saying it
    learned while learning nothing is worse than declining: the user gets no
    signal the preference evaporated.
    """
    cleaned = (text or "").strip()
    if not cleaned or len(cleaned) > 600:
        return None
    return cleaned if _STANDING_INSTRUCTION.search(cleaned) else None


def _remember_standing_instruction(
    repositories: Repositories,
    audit: AuditLogger,
    inbound: InboundMessage,
    actor: str,
) -> str | None:
    """Persist a stated rule as a durable fact. Returns the stored content.

    Stored as USER_STATED so it outranks task-derived guesses, and read back by
    `memory_context(remembered_facts=...)`, which both the chat and task paths
    already build - so a rule stated here reaches the next task's operator
    context without any further wiring.
    """
    content = _standing_instruction(inbound.text or "")
    if content is None:
        return None
    try:
        fact = repositories.memory_facts.create(
            MemoryFact(category="preference", content=content, source=MemorySource.USER_STATED)
        )
    except Exception as exc:
        audit.append(
            AuditEventType.ERROR,
            actor=actor,
            correlation_id=inbound.correlation_id,
            payload={"error": "remember_standing_instruction_failed", "reason": str(exc)},
        )
        return None
    audit.append(
        AuditEventType.MEMORY_UPDATED,
        actor=actor,
        correlation_id=inbound.correlation_id,
        payload={"fact_id": fact.id, "category": fact.category, "content": fact.content},
    )
    return fact.content


async def classify_and_spawn_task(
    inbound: InboundMessage,
    conversation_id: str,
    *,
    repositories: Repositories,
    audit: AuditLogger,
    classifier: MessageClassifier | None,
    responder: ChatResponder | None = None,
    settings: AppSettings | None = None,
    send_progress: Callable[[str, str], Awaitable[None]],
) -> ChannelUpdateResult:
    """Classify one inbound message and either spawn a task or produce a
    chat-only reply - the "classify -> task" half of intake -> classify ->
    task -> notify, identical for every channel. `send_progress` is the one
    channel-specific seam (how to say "on it" before classification/task
    creation finish), injected rather than hardcoded to a transport.
    """
    actor = f"{inbound.channel.value}:user:{inbound.sender_id}"
    if not inbound.text:
        return _spawn_failed(audit, inbound, "message has no text content", actor)
    if classifier is None:
        return _spawn_failed(audit, inbound, "message classifier is not configured", actor)

    await send_progress(inbound.chat_id, ACKNOWLEDGMENT_TEXT)
    try:
        # The Concierge prompt does double duty (classify + compose a chat
        # reply in one call, see prompts/base/concierge_system.md) - a chat
        # reply needs the same runtime context the old separate responder
        # call used (capabilities, recent tasks), not just conversation
        # memory. Falls back to the narrower memory-only context when no
        # settings are available (e.g. a caller that never wired them).
        classification_context = (
            gateway_context(settings, repositories, conversation_id, query_text=inbound.text or "")
            if settings is not None
            else memory_context(
                repositories.conversation_memory.get(conversation_id),
                recent_turns=3,
                max_chars=900,
                remembered_facts=repositories.memory_facts.list_all(),
                objective=inbound.text or "",
            )
        )
        classify_started = time.monotonic()
        try:
            classification = await classifier.classify(inbound, context=classification_context)
        except TypeError:
            classification = await classifier.classify(inbound)
        # The Concierge was the one agent with no timing anywhere: llm_calls
        # records "operator" and "auditor" only, because a classification
        # happens before a task exists to attach it to. It is also the call the
        # operator waits on *first* - before "on it" is even sent - so its cost
        # was both the most felt and the only unmeasured one.
        classify_ms = (time.monotonic() - classify_started) * 1000
    except Exception as exc:
        return _spawn_failed(audit, inbound, f"classification failed: {exc}", actor)

    audit.append(
        AuditEventType.MESSAGE_CLASSIFIED,
        actor=actor,
        correlation_id=inbound.correlation_id,
        payload={
            "message_id": inbound.id,
            "chat_id": inbound.chat_id,
            "sender_id": inbound.sender_id,
            "text": inbound.text,
            "is_task": classification.is_task,
            "task_type": classification.task_type.value,
            "normalized_objective": classification.normalized_objective,
            "confidence": classification.confidence,
            "reason": classification.reason,
            "intent": classification.intent.model_dump(mode="json") if classification.intent else None,
            "llm": classification_trace(inbound, context=classification_context),
            "latency_ms": round(classify_ms, 1),
            "context_chars": len(classification_context or ""),
        },
    )

    # Decide chat-only vs spawn-task. Prefer the intent.route enum because
    # the LLM picks it more consistently than the is_task bool. The bool was
    # flipping wrong for observation/check requests in production (e.g.
    # "tell me what is on my desktop" -> is_task=False AND
    # intent.route=desktop.observe; the bool was wrong, the route was right).
    #
    # Rules:
    #  - intent.route is CONVERSATION  -> chat-only (model explicitly said chat)
    #  - intent.route is any other     -> spawn task (override is_task=False
    #                                     so observation tasks aren't dropped)
    #  - intent missing entirely       -> fall back to is_task (legacy behavior)
    # STATUS_REQUEST task_type still bypasses the gate as before.
    # Steering comes first: a correction to work already running must reach
    # that work, not become a second task. Before this, the only things an
    # in-flight task could hear were pause/resume/cancel - "make it 5 not 3"
    # spawned a rival task while the original carried on with 3.
    if classification.steers_active_task:
        steerable = find_steerable_task(
            repositories, conversation_id=conversation_id, chat_id=inbound.chat_id
        )
        if steerable is not None:
            steered = attach_note(repositories, audit, steerable, text=inbound.text, actor=actor)
            return ChannelUpdateResult(
                authorized=True,
                inbound_message=inbound,
                classification=classification,
                outbound_message=OutboundMessage(
                    channel=inbound.channel,
                    chat_id=inbound.chat_id,
                    text=acknowledgement(steered),
                ),
            )
        # Nothing in flight any more - it finished between the message arriving
        # and being classified. Fall through and treat it as a normal request
        # rather than silently dropping what the user asked for.

    intent_route = classification.intent.route if classification.intent is not None else None
    if intent_route is None:
        is_chat_only = not classification.is_task and classification.task_type != TaskType.STATUS_REQUEST
    else:
        is_chat_only = intent_route == IntentRoute.CONVERSATION and classification.task_type != TaskType.STATUS_REQUEST

    if is_chat_only:
        # Persist before replying, so the acknowledgment only claims what was
        # actually stored.
        remembered = _remember_standing_instruction(repositories, audit, inbound, actor)
        outbound = await _non_task_response(audit, repositories, inbound, classification, conversation_id, responder)
        if outbound is not None:
            if remembered:
                outbound = outbound.model_copy(
                    update={"text": f"{outbound.text}\n\n(Remembered: {remembered[:300]})"}
                )
            return ChannelUpdateResult(
                authorized=True, inbound_message=inbound, classification=classification, outbound_message=outbound,
            )
        return _spawn_failed(audit, inbound, classification.reason, actor, classification)

    objective = (classification.normalized_objective or inbound.text).strip()
    voice_attachment = next((a for a in inbound.attachments if isinstance(a, VoiceAttachment)), None)
    voice_metadata = (
        {"voice_file_id": voice_attachment.file_id, "voice_transcript": voice_attachment.transcript}
        if voice_attachment is not None else {}
    )
    # Snapshot conversation memory so the planner has prior context available.
    mem_ctx = memory_context(
        repositories.conversation_memory.get(conversation_id),
        recent_turns=5, max_chars=1600,
        remembered_facts=repositories.memory_facts.list_all(),
        objective=objective,
    )
    task = repositories.tasks.create(
        objective,
        conversation_id=conversation_id,
        metadata={
            "source_message_id": inbound.id,
            "source_channel": inbound.channel.value,
            "source_chat_id": inbound.chat_id,
            "source_sender_id": inbound.sender_id,
            "task_type": classification.task_type.value,
            "classification_confidence": classification.confidence,
            "classification_reason": classification.reason,
            "orchestration_intent": classification.intent.model_dump(mode="json") if classification.intent else None,
            # orchestration/fulfillment.py's expected_postconditions() reads
            # this before falling back to keyword inference. Empty for
            # every classifier today (docs/ROADMAP.md "Finish the Proof") -
            # stored anyway so the fallback stays automatic the moment a
            # classifier starts declaring, with no worker.py change needed.
            "expected_postconditions": [item.value for item in classification.expected_postconditions],
            "original_message_text": inbound.text,
            "memory_context": mem_ctx,
            **voice_metadata,
        },
    )
    audit.append(
        AuditEventType.TASK_CREATED,
        actor=actor,
        task_id=task.id,
        correlation_id=inbound.correlation_id,
        payload={
            "objective": task.objective,
            "conversation_id": conversation_id,
            "task_type": classification.task_type.value,
            "source_message_id": inbound.id,
            "classification_confidence": classification.confidence,
            "classification_reason": classification.reason,
            "orchestration_intent": classification.intent.model_dump(mode="json") if classification.intent else None,
        },
    )
    await send_progress(inbound.chat_id, TASK_STARTED_TEXT)
    return ChannelUpdateResult(
        authorized=True, inbound_message=inbound, classification=classification, task=task, outbound_message=None,
    )
