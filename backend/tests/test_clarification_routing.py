"""A waiting question must not silently capture an unrelated message."""
import pytest

from agent_control.channels.base import route_clarifying_reply
from agent_control.clarification import assess_unaddressed_reply, resume_clarifying_task
from agent_control.channels.quick_answers import clarification_token
from agent_control.config import AppSettings, DecisionConfig
from agent_control.llm.decision import DecisionModelClient
from agent_control.schemas import ChannelType, InboundMessage, MessageKind, TaskStatus, utc_now
from helpers import make_repos


def _waiting(repos):
    conversation = repos.conversations.get_or_create(ChannelType.TELEGRAM, "42")
    task = repos.tasks.create("Connect the TV", conversation_id=conversation,
                              metadata={"clarifying_question": "Which casting library?"})
    return conversation, repos.tasks.update_status(task.id, TaskStatus.CLARIFYING)


def _message(text):
    return InboundMessage(channel=ChannelType.TELEGRAM, kind=MessageKind.TEXT, sender_id="7", chat_id="42", text=text)


@pytest.mark.asyncio
async def test_when_the_model_cannot_tell_an_answer_from_a_new_request_nobody_is_asked_to_type_syntax(tmp_path):
    """Live 11/18: a plain "Ethernet" to "which network interface?" was answered with "use 'reply task_...: your answer'". A question that belongs to a goal is left to the one call
    that reads the message with the goal in view; one with no goal is answered by the reply, as it was before there was any doubt. The typed forms still work for anyone who uses them."""
    repos, audit = make_repos(tmp_path)
    conversation, task = _waiting(repos)
    # no goal behind the question: the reply is its answer
    answer = await route_clarifying_reply(repos, audit, _message("Chromecast"), conversation, None)
    assert answer is not None
    assert repos.tasks.get(task.id).metadata["clarification_answer"] == "Chromecast"
    assert "reply task_" not in answer.text
    # a question with a goal: handed on (None) to the goal-aware classification, and the question stays open
    other = repos.tasks.create("Connect the printer", conversation_id=conversation, metadata={"clarifying_question": "Which port?", "goal_id": "goal_x"})
    repos.tasks.update_status(other.id, TaskStatus.CLARIFYING)
    assert await route_clarifying_reply(repos, audit, _message("Ethernet"), conversation, None) is None
    assert repos.tasks.get(other.id).status == TaskStatus.CLARIFYING
    # the typed forms still work
    assert await route_clarifying_reply(repos, audit, _message("new: What is the weather?"), conversation, None) is None
    typed = await route_clarifying_reply(repos, audit, _message(f"reply {other.id}: COM3"), conversation, None)
    assert typed is not None and repos.tasks.get(other.id).metadata["clarification_answer"] == "COM3"


@pytest.mark.asyncio
@pytest.mark.parametrize(("scores", "expected"), [
    ({"reply": .97, "unrelated": .03}, "reply"),
    ({"reply": .02, "unrelated": .98}, "new"),
    ({"reply": .75, "unrelated": .2}, "uncertain"),
    ({"reply": .99}, "uncertain"),
])
async def test_question_relation_requires_both_confident_signals(tmp_path, monkeypatch, scores, expected):
    repos, _ = make_repos(tmp_path)
    _, task = _waiting(repos)
    async def answer(self, text, questions):
        assert "Which casting library?" in text and "weather" in text
        return scores
    monkeypatch.setattr(DecisionModelClient, "yes_no", answer)
    assert await assess_unaddressed_reply(task, "weather", DecisionConfig(mode="decision")) == expected


@pytest.mark.asyncio
async def test_changed_question_cannot_receive_a_classified_answer(tmp_path, monkeypatch):
    repos, audit = make_repos(tmp_path)
    conversation, task = _waiting(repos)
    async def answer(self, text, questions):
        repos.tasks.update_metadata(task.id, {**task.metadata, "clarifying_question": "Which room?"}, TaskStatus.CLARIFYING)
        return {"reply": .99, "unrelated": .01}
    monkeypatch.setattr(DecisionModelClient, "yes_no", answer)
    settings = AppSettings(_env_file=None, decision=DecisionConfig(mode="decision"))
    response = await route_clarifying_reply(repos, audit, _message("Use Chromecast"), conversation, settings)
    assert "question changed" in response.text
    assert repos.tasks.get(task.id).status == TaskStatus.CLARIFYING
    assert "clarification_answer" not in repos.tasks.get(task.id).metadata


@pytest.mark.asyncio
async def test_new_question_during_classification_prevents_a_guessed_target(tmp_path, monkeypatch):
    repos, audit = make_repos(tmp_path)
    conversation, task = _waiting(repos)
    async def answer(self, text, questions):
        other = repos.tasks.create("Second task", conversation_id=conversation, metadata={"clarifying_question": "Which room?"})
        repos.tasks.update_status(other.id, TaskStatus.CLARIFYING)
        return {"reply": .01, "unrelated": .99}
    monkeypatch.setattr(DecisionModelClient, "yes_no", answer)
    settings = AppSettings(_env_file=None, decision=DecisionConfig(mode="decision"))
    response = await route_clarifying_reply(repos, audit, _message("Find my notes"), conversation, settings)
    assert "open questions changed" in response.text
    assert repos.tasks.get(task.id).status == TaskStatus.CLARIFYING


def test_stale_question_token_is_rejected_after_aba_with_a_frozen_clock(tmp_path, monkeypatch):
    import agent_control.storage.repositories as storage

    repos, audit = make_repos(tmp_path)
    _, first = _waiting(repos)
    monkeypatch.setattr(storage, "utc_now", lambda: first.updated_at)
    repos.tasks.update_status(first.id, TaskStatus.RECEIVED)
    current = repos.tasks.update_status(first.id, TaskStatus.CLARIFYING)
    assert current.updated_at == first.updated_at and current.revision > first.revision
    assert clarification_token(current) != clarification_token(first)
    result = resume_clarifying_task(repos, audit, first, text="Yes", actor="test", message_id="old", received_at=utc_now())
    assert not result.accepted and repos.tasks.get(first.id).status == TaskStatus.CLARIFYING


@pytest.mark.asyncio
async def test_question_button_survives_notification_bookkeeping(tmp_path):
    from agent_control.orchestration import TaskWorker

    repos, audit = make_repos(tmp_path)
    _, question = _waiting(repos)
    class Sink:
        sent = []
        async def notify(self, task):
            self.sent.append(task)
    sink = Sink()
    worker = TaskWorker(repos, audit, notification_sink=sink)
    await worker._notify_if_needed(question)
    current = repos.tasks.get(question.id)
    assert current.metadata["notified_statuses"] == ["clarifying:0"]
    assert current.revision == question.revision and clarification_token(sink.sent[0]) == clarification_token(current)
    result = resume_clarifying_task(repos, audit, question, text="Yes", actor="test", message_id="m", received_at=utc_now())
    assert result.accepted and repos.tasks.get(question.id).status == TaskStatus.RECEIVED
    assert repos.tasks.get(question.id).metadata["notified_statuses"] == ["clarifying:0"]


@pytest.mark.asyncio
async def test_missing_notification_sink_does_not_record_delivery(tmp_path):
    from agent_control.orchestration import TaskWorker

    repos, audit = make_repos(tmp_path)
    _, question = _waiting(repos)
    await TaskWorker(repos, audit)._notify_if_needed(question)
    assert "notified_statuses" not in repos.tasks.get(question.id).metadata


@pytest.mark.asyncio
async def test_shadow_mode_never_uses_model_opinion_to_route(tmp_path, monkeypatch):
    repos, _ = make_repos(tmp_path)
    _, task = _waiting(repos)
    called = []
    async def forbidden(self, text, questions):
        called.append(True)
        return {"reply": 1.0, "unrelated": 0.0}
    monkeypatch.setattr(DecisionModelClient, "yes_no", forbidden)
    assert await assess_unaddressed_reply(task, "Use Chromecast", DecisionConfig(mode="shadow")) == "uncertain"
    assert not called


@pytest.mark.asyncio
async def test_declining_handoff_and_an_offered_no_are_answers_not_cancellation(tmp_path):
    repos, audit = make_repos(tmp_path)
    conversation, task = _waiting(repos)
    task = repos.tasks.update_metadata(task.id, {**task.metadata, "claude_code_offer": {"state": "pending",
                                                                                         "question": "Which casting library?"}},
                                       TaskStatus.CLARIFYING)
    answer = await route_clarifying_reply(repos, audit, _message("No, you do it"), conversation, None)
    assert "resuming" in answer.text and repos.tasks.get(task.id).metadata["clarification_answer"] == "No, you do it"
    second = repos.tasks.create("Choose a setting", conversation_id=conversation,
                                metadata={"clarifying_question": "Enable it?", "clarifying_options": ["Yes", "No"]})
    repos.tasks.update_status(second.id, TaskStatus.CLARIFYING)
    answer = await route_clarifying_reply(repos, audit, _message("No"), conversation, None)
    assert "resuming" in answer.text and repos.tasks.get(second.id).status == TaskStatus.RECEIVED
