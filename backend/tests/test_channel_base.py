"""channels/base.py (docs/UI_UX_AUDIT.md Phase 16) - the channel-agnostic
"classify -> task" core extracted out of TelegramIntakeService. These tests
deliberately use a non-Telegram ChannelType to prove the extraction is
real: a future channel calling classify_and_spawn_task directly gets
correct behavior, not something that only happens to still work because
it's secretly still Telegram-shaped.
"""

from __future__ import annotations

import pytest

from agent_control.channels.base import (
    _standing_instruction,
    classify_and_spawn_task,
    resume_clarifying_reply,
    status_summary,
)
from agent_control.channels.memory import memory_context
from agent_control.schemas import (
    AuditEventType,
    ChannelType,
    InboundMessage,
    MessageClassification,
    MessageKind,
    TaskType,
)
from helpers import make_repos


def _message(text: str = "build me a script", channel: ChannelType = ChannelType.DISCORD) -> InboundMessage:
    return InboundMessage(channel=channel, kind=MessageKind.TEXT, sender_id="user-1", chat_id="chat-1", text=text)


class _StaticClassifier:
    def __init__(self, classification: MessageClassification) -> None:
        self.classification = classification

    async def classify(self, message: InboundMessage, context: str | None = None) -> MessageClassification:
        return self.classification


async def _noop_progress(chat_id: str, text: str) -> None:
    return None


@pytest.mark.asyncio
async def test_classify_and_spawn_task_creates_a_task_for_a_non_telegram_channel(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    conversation_id = repos.conversations.get_or_create(ChannelType.DISCORD, "chat-1")
    classifier = _StaticClassifier(
        MessageClassification(is_task=True, task_type=TaskType.DEVELOPMENT, normalized_objective="build me a script", reason="looks like work")
    )
    inbound = _message()

    result = await classify_and_spawn_task(
        inbound, conversation_id,
        repositories=repos, audit=audit, classifier=classifier, send_progress=_noop_progress,
    )

    assert result.task is not None
    assert result.task.metadata["source_channel"] == "discord"
    assert result.task.metadata["source_chat_id"] == "chat-1"
    # The audit trail must say which channel this came from, not silently
    # assume Telegram (docs/UI_UX_AUDIT.md Phase 16's whole point).
    events = repos.audit.list_for_task(result.task.id)
    assert any(event.actor.startswith("discord:user:") for event in events)


@pytest.mark.asyncio
async def test_classify_and_spawn_task_persists_declared_expected_postconditions(tmp_path) -> None:
    """orchestration/fulfillment.py's expected_postconditions() reads this
    off task.metadata before falling back to keyword inference
    (docs/ROADMAP.md "Finish the Proof") - it has to actually land there
    for a classifier that declares one to matter at all."""
    repos, audit = make_repos(tmp_path)
    conversation_id = repos.conversations.get_or_create(ChannelType.DISCORD, "chat-1")
    classifier = _StaticClassifier(
        MessageClassification(
            is_task=True,
            task_type=TaskType.DEVELOPMENT,
            normalized_objective="make me a slide deck",
            reason="wants a presentation",
            expected_postconditions=["presentation_file"],
        )
    )
    inbound = _message(text="make me a slide deck")

    result = await classify_and_spawn_task(
        inbound, conversation_id,
        repositories=repos, audit=audit, classifier=classifier, send_progress=_noop_progress,
    )

    assert result.task is not None
    assert result.task.metadata["expected_postconditions"] == ["presentation_file"]


@pytest.mark.asyncio
async def test_classify_and_spawn_task_persists_an_empty_list_when_nothing_declared(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    conversation_id = repos.conversations.get_or_create(ChannelType.DISCORD, "chat-1")
    classifier = _StaticClassifier(
        MessageClassification(is_task=True, task_type=TaskType.DEVELOPMENT, normalized_objective="build me a script", reason="looks like work")
    )
    inbound = _message()

    result = await classify_and_spawn_task(
        inbound, conversation_id,
        repositories=repos, audit=audit, classifier=classifier, send_progress=_noop_progress,
    )

    assert result.task is not None
    assert result.task.metadata["expected_postconditions"] == []


@pytest.mark.asyncio
async def test_classify_and_spawn_task_chat_only_reply_uses_the_inbound_channel(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    conversation_id = repos.conversations.get_or_create(ChannelType.SLACK, "chat-1")
    classifier = _StaticClassifier(
        MessageClassification(is_task=False, reason="just chatting", reply="Hello from the Slack path")
    )
    inbound = _message(text="hey", channel=ChannelType.SLACK)

    result = await classify_and_spawn_task(
        inbound, conversation_id,
        repositories=repos, audit=audit, classifier=classifier, send_progress=_noop_progress,
    )

    assert result.task is None
    assert result.outbound_message is not None
    assert result.outbound_message.channel == ChannelType.SLACK
    assert result.outbound_message.text == "Hello from the Slack path"


def test_standing_instruction_detects_a_durable_rule_not_a_one_off_request() -> None:
    for instruction in (
        "Remember this for the future: always answer summaries with three bullet points.",
        "From now on, reply in British English.",
        "Whenever you send me a file, put it in a zip.",
        "I prefer metric units.",
    ):
        assert _standing_instruction(instruction) is not None, instruction

    for ordinary in (
        "What is on my desktop?",
        "Summarize quarterly-ops-report.md for me.",
        # "always" mid-sentence describes the user, it does not instruct.
        "I always get lost in that folder, can you find the invoice?",
        # Reminiscence, not a rule to keep.
        "Remember when we fixed the worker bug?",
    ):
        assert _standing_instruction(ordinary) is None, ordinary


@pytest.mark.asyncio
async def test_chat_route_persists_a_stated_preference_and_says_so(tmp_path) -> None:
    """The chat route composed "Understood, all future summaries will be…" and
    persisted nothing, so the next task had no fact to read and silently ignored
    the rule (docs/E2E_FINDINGS.md P1-3). Claiming to have learned while
    learning nothing is worse than declining - the user gets no signal."""
    repos, audit = make_repos(tmp_path)
    conversation_id = repos.conversations.get_or_create(ChannelType.SLACK, "chat-1")
    classifier = _StaticClassifier(
        MessageClassification(is_task=False, reason="preference", reply="Understood.")
    )
    inbound = _message(
        text="Remember this for the future: always answer summaries with exactly three bullet points.",
        channel=ChannelType.SLACK,
    )

    result = await classify_and_spawn_task(
        inbound, conversation_id,
        repositories=repos, audit=audit, classifier=classifier, send_progress=_noop_progress,
    )

    facts = repos.memory_facts.list_all()
    assert len(facts) == 1
    assert "three bullet points" in facts[0].content
    assert facts[0].category == "preference"
    # The acknowledgment must reflect what was actually stored.
    assert "Remembered:" in result.outbound_message.text
    assert any(event.type == AuditEventType.MEMORY_UPDATED for event in repos.audit.list_recent(limit=20))


@pytest.mark.asyncio
async def test_stored_preference_reaches_a_later_task_context(tmp_path) -> None:
    """The end of the chain that matters: a rule stated in chat has to arrive in
    the operator context of a *separate* task, or persisting it changed nothing."""
    repos, audit = make_repos(tmp_path)
    conversation_id = repos.conversations.get_or_create(ChannelType.SLACK, "chat-1")
    await classify_and_spawn_task(
        _message(text="From now on, always end summaries with a Confidence: line.", channel=ChannelType.SLACK),
        conversation_id,
        repositories=repos, audit=audit,
        classifier=_StaticClassifier(MessageClassification(is_task=False, reason="preference", reply="Understood.")),
        send_progress=_noop_progress,
    )

    later_task = await classify_and_spawn_task(
        _message(text="Summarize the ops report.", channel=ChannelType.SLACK),
        conversation_id,
        repositories=repos, audit=audit,
        classifier=_StaticClassifier(
            MessageClassification(
                is_task=True, task_type=TaskType.OTHER,
                normalized_objective="Summarize the ops report.", reason="work",
            )
        ),
        send_progress=_noop_progress,
    )

    assert later_task.task is not None
    assert "Confidence:" in later_task.task.metadata["memory_context"]
    # And the same context builder the chat path uses sees it too.
    assert "Confidence:" in memory_context(
        repos.conversation_memory.get(conversation_id),
        remembered_facts=repos.memory_facts.list_all(),
        objective="Summarize the ops report.",
    )


@pytest.mark.asyncio
async def test_classify_and_spawn_task_with_no_classifier_fails_clearly(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    conversation_id = repos.conversations.get_or_create(ChannelType.DISCORD, "chat-1")

    result = await classify_and_spawn_task(
        _message(), conversation_id,
        repositories=repos, audit=audit, classifier=None, send_progress=_noop_progress,
    )

    assert result.task is None
    assert "not configured" in (result.outbound_message.text or "")


def test_status_summary_is_pure_repository_text_no_channel_involved(tmp_path) -> None:
    repos, _audit = make_repos(tmp_path)
    repos.tasks.create("do something")

    summary = status_summary(repos)

    assert "1 recent task" in summary


def test_resume_clarifying_reply_returns_none_without_a_pending_clarification(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    conversation_id = repos.conversations.get_or_create(ChannelType.DISCORD, "chat-1")

    assert resume_clarifying_reply(repos, audit, _message(), conversation_id) is None
