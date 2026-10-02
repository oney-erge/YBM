"""docs/archive/UI_UX_AUDIT.md Phase 14: format_audit_event's CATEGORY_BY_TYPE had
no dedicated test coverage at all before this - the timeline's category
labels are read straight from it now, so a wrong mapping here would
silently mislabel every event of that type across the console.
"""

from __future__ import annotations

from agent_control.schemas import AuditEvent, AuditEventType, ChannelType
from agent_control.storage.audit_view import format_audit_event


def _event(event_type: AuditEventType, actor: str = "worker", payload: dict | None = None) -> AuditEvent:
    return AuditEvent(type=event_type, actor=actor, payload=payload or {})


def test_task_state_changed_categorizes_as_task_state() -> None:
    """The single most common event type in any task's timeline (fires on
    every status transition) - previously fell through to the generic
    "system" fallback, leaving almost every timeline row looking like
    miscellaneous noise instead of the actual state machine."""
    assert format_audit_event(_event(AuditEventType.TASK_STATE_CHANGED)).category == "task_state"


def test_task_cancelled_categorizes_as_task_state() -> None:
    assert format_audit_event(_event(AuditEventType.TASK_CANCELLED)).category == "task_state"


def test_artifact_created_categorizes_as_artifact() -> None:
    assert format_audit_event(_event(AuditEventType.ARTIFACT_CREATED)).category == "artifact"


def test_egress_contacted_categorizes_as_egress() -> None:
    assert format_audit_event(_event(AuditEventType.EGRESS_CONTACTED)).category == "egress"


def test_an_uncategorized_type_falls_back_to_system() -> None:
    """PLAN_CREATED is deliberately left uncategorized - PlanModel is dead
    (docs/archive/HISTORY.md §1.1), nothing creates one anymore, so it isn't worth
    its own category."""
    assert format_audit_event(_event(AuditEventType.PLAN_CREATED)).category == "system"


def test_every_known_category_is_a_non_empty_string() -> None:
    """A cheap exhaustiveness guard: every AuditEventType produces some
    category, never a crash or an empty string, even ones not explicitly
    listed above."""
    for event_type in AuditEventType:
        category = format_audit_event(_event(event_type)).category
        assert isinstance(category, str) and category


def test_source_reads_the_actor_prefix_for_every_real_channel() -> None:
    """_source() used to recognize only "telegram"/"whatsapp" despite its
    own comment claiming to generalize - a web-chat-originated event (a
    channel that predates WhatsApp) silently lost its source attribution.
    Every ChannelType value must now round-trip through the actor prefix
    channels/base.py's classify_and_spawn_task already stamps
    ("<channel>:user:<id>")."""
    for channel in ChannelType:
        event = _event(AuditEventType.TASK_CREATED, actor=f"{channel.value}:user:42")
        assert format_audit_event(event).source == channel.value


def test_source_does_not_misattribute_a_non_channel_actor() -> None:
    """A ":"-delimited actor that happens to start with a word which is
    NOT a real channel name must not be reported as a source channel."""
    event = _event(AuditEventType.ERROR, actor="policy_engine:evaluate")
    assert format_audit_event(event).source is None


def test_channel_access_decision_summary_names_the_actual_channel() -> None:
    """Regression test: CHANNEL_ACCESS_DECISION (the generic counterpart to
    TELEGRAM_ACCESS_DECISION) had no dedicated _summary() branch and fell
    through to a bare payload["reason"] string like "allowlist_empty"
    instead of a readable "Denied WhatsApp message"."""
    event = _event(
        AuditEventType.CHANNEL_ACCESS_DECISION,
        actor="whatsapp",
        payload={"channel": ChannelType.WHATSAPP.value, "allowed": False, "reason": "allowlist_empty"},
    )
    assert format_audit_event(event).summary == "Denied Whatsapp message"
