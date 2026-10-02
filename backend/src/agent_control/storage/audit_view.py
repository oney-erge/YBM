from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from agent_control.schemas import AuditEvent, AuditEventType, ChannelType, FormattedAuditEvent


CATEGORY_BY_TYPE = {
    # "raw_message", not "raw_telegram" (docs/archive/UI_UX_AUDIT.md Phase 16) -
    # MESSAGE_RECEIVED/MESSAGE_SENT were always channel-generic in the enum
    # itself, but displayed as if only Telegram could emit them; WhatsApp
    # emits the exact same event types now. Which channel it was is the
    # `source` field (see _source() below), not the category.
    AuditEventType.MESSAGE_RECEIVED: "raw_message",
    AuditEventType.MESSAGE_SENT: "raw_message",
    AuditEventType.TELEGRAM_ACCESS_DECISION: "telegram_access",
    AuditEventType.CHANNEL_ACCESS_DECISION: "channel_access",
    AuditEventType.MESSAGE_CLASSIFIED: "classification",
    AuditEventType.TASK_SPAWN_FAILED: "failed_classification",
    AuditEventType.TASK_CREATED: "spawned_task",
    AuditEventType.POLICY_DECISION: "policy",
    AuditEventType.CONFIG_UPDATED: "config",
    AuditEventType.TOOL_REQUESTED: "tool",
    AuditEventType.TOOL_COMPLETED: "tool",
    AuditEventType.APPROVAL_REQUESTED: "approval",
    AuditEventType.APPROVAL_DECIDED: "approval",
    AuditEventType.ERROR: "error",
    # Added docs/archive/UI_UX_AUDIT.md Phase 14 - these four previously fell back
    # to the generic "system" category, which was a real gap for
    # TASK_STATE_CHANGED specifically: it's the single most common event
    # type in any task's timeline (fires on every status transition), so
    # lumping it under "system" left almost every timeline row looking
    # like miscellaneous noise instead of the actual state machine.
    AuditEventType.TASK_STATE_CHANGED: "task_state",
    AuditEventType.TASK_CANCELLED: "task_state",
    AuditEventType.ARTIFACT_CREATED: "artifact",
    AuditEventType.MEMORY_UPDATED: "memory",
    AuditEventType.EGRESS_CONTACTED: "egress",
}


def format_audit_event(event: AuditEvent) -> FormattedAuditEvent:
    payload = event.payload
    category = CATEGORY_BY_TYPE.get(event.type, "system")
    title = _title(event.type)
    summary = _summary(event.type, payload)
    decision = _decision(event.type, payload)
    reason = _reason(payload)
    task_type = _task_type(payload)
    source = _source(event, payload)
    return FormattedAuditEvent(
        id=event.id,
        type=event.type,
        category=category,
        formatted_time=_format_time(event.created_at),
        actor=event.actor,
        task_id=event.task_id,
        title=title,
        summary=summary,
        decision=decision,
        reason=reason,
        task_type=task_type,
        source=source,
        details=payload,
    )


def _title(event_type: AuditEventType) -> str:
    return {
        AuditEventType.MESSAGE_RECEIVED: "Message received",
        AuditEventType.MESSAGE_SENT: "Message sent",
        AuditEventType.TELEGRAM_ACCESS_DECISION: "Telegram access decision",
        AuditEventType.CHANNEL_ACCESS_DECISION: "Channel access decision",
        AuditEventType.MESSAGE_CLASSIFIED: "Message classified",
        AuditEventType.TASK_SPAWN_FAILED: "Task not spawned",
        AuditEventType.TASK_CREATED: "Task spawned",
        AuditEventType.POLICY_DECISION: "Policy decision",
        AuditEventType.CONFIG_UPDATED: "Configuration updated",
        AuditEventType.TOOL_REQUESTED: "Tool requested",
        AuditEventType.TOOL_COMPLETED: "Tool completed",
        AuditEventType.APPROVAL_REQUESTED: "Approval requested",
        AuditEventType.APPROVAL_DECIDED: "Approval decided",
        AuditEventType.MEMORY_UPDATED: "Memory updated",
        AuditEventType.ERROR: "Error",
    }.get(event_type, event_type.value.replace("_", " ").title())


def _summary(event_type: AuditEventType, payload: dict[str, Any]) -> str:
    if event_type == AuditEventType.MESSAGE_RECEIVED:
        return _preview(payload.get("text") or payload.get("text_preview") or "Message received")
    if event_type == AuditEventType.MESSAGE_SENT:
        return _preview(payload.get("text") or payload.get("caption") or payload.get("kind") or "Message sent")
    if event_type == AuditEventType.TELEGRAM_ACCESS_DECISION:
        return f"{'Allowed' if payload.get('allowed') else 'Denied'} Telegram message"
    if event_type == AuditEventType.CHANNEL_ACCESS_DECISION:
        # Generic counterpart to TELEGRAM_ACCESS_DECISION above (docs/archive/UI_UX_AUDIT.md
        # Phase 16) - the channel name comes from the payload each adapter's
        # own _audit_access() already stamps, not hardcoded to WhatsApp,
        # so a future channel using this same event type gets a correct
        # summary for free.
        channel_name = str(payload.get("channel") or "channel").capitalize()
        return f"{'Allowed' if payload.get('allowed') else 'Denied'} {channel_name} message"
    if event_type == AuditEventType.MESSAGE_CLASSIFIED:
        return _preview(payload.get("normalized_objective") or payload.get("text") or payload.get("reason") or "Classified message")
    if event_type == AuditEventType.TASK_SPAWN_FAILED:
        return _preview(payload.get("reason") or payload.get("error") or "Task was not created")
    if event_type == AuditEventType.TASK_CREATED:
        return _preview(payload.get("objective") or "Task created")
    if event_type == AuditEventType.MEMORY_UPDATED:
        # The stored text is the whole point of the row - a bare "Memory
        # updated" would make the operator open the payload to learn what the
        # agent now believes.
        return _preview(payload.get("content") or "Remembered a fact")
    if event_type == AuditEventType.POLICY_DECISION:
        return f"{'Allowed' if payload.get('allowed') else 'Denied'} {payload.get('capability', 'capability')}"
    if event_type == AuditEventType.CONFIG_UPDATED:
        return f"Updated {payload.get('section', 'configuration')}"
    if event_type == AuditEventType.TOOL_COMPLETED:
        return str(payload.get("status") or "Tool completed")
    return _preview(payload.get("summary") or payload.get("reason") or payload.get("error") or event_type.value)


def _decision(event_type: AuditEventType, payload: dict[str, Any]) -> str | None:
    if "allowed" in payload:
        return "allowed" if payload.get("allowed") else "denied"
    if event_type == AuditEventType.MESSAGE_CLASSIFIED:
        return "task" if payload.get("is_task") else "not_task"
    if event_type == AuditEventType.TASK_CREATED:
        return "spawned"
    if event_type == AuditEventType.TASK_SPAWN_FAILED:
        return "not_spawned"
    if "decision" in payload:
        return str(payload["decision"])
    if "status" in payload:
        return str(payload["status"])
    return None


def _reason(payload: dict[str, Any]) -> str | None:
    value = payload.get("reason") or payload.get("error") or payload.get("error_message")
    return str(value) if value is not None else None


def _task_type(payload: dict[str, Any]) -> str | None:
    value = payload.get("task_type")
    return str(value) if value is not None else None


_CHANNEL_ACTOR_PREFIXES = frozenset(channel.value for channel in ChannelType)


def _source(event: AuditEvent, payload: dict[str, Any]) -> str | None:
    # Every channel's own code stamps its actor strings "<channel>:..."
    # (docs/archive/UI_UX_AUDIT.md Phase 16 - see channels/base.py's
    # classify_and_spawn_task and each adapter's _audit_access/
    # _normalize_message) - reading the prefix generalizes this for free
    # instead of hardcoding channel names. Checked against ChannelType's
    # own values (not a hand-maintained set that drifts as channels are
    # added) so this isn't just an actor prefix but *some other* actor
    # ("worker", "policy_engine", "scheduler", ...) that happens to also
    # use a ":"-delimited actor string.
    prefix = event.actor.split(":", 1)[0]
    if prefix in _CHANNEL_ACTOR_PREFIXES:
        return prefix
    value = payload.get("source") or payload.get("channel")
    return str(value) if value is not None else None


def _format_time(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    value = value.astimezone(timezone.utc)
    return value.strftime("%Y-%m-%d %H:%M:%S UTC")


def _preview(value: Any, limit: int = 180) -> str:
    text = str(value)
    if len(text) <= limit:
        return text
    return f"{text[: limit - 3]}..."
