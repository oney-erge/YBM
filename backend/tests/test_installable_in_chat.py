"""A message taken for chat that asks for something that could be set up becomes a task (abilities/context.py, channels/base.py)."""

from __future__ import annotations

import pytest

from agent_control.abilities.context import asks_for_what_could_be_set_up
from agent_control.abilities.recipes import recipe_abilities
from agent_control.channels.base import classify_and_spawn_task
from agent_control.config import AppSettings
from agent_control.schemas import ChannelType, InboundMessage, MessageClassification, MessageKind
from helpers import make_repos


class _Confirmer:
    def __init__(self, answers: dict[str, float] | Exception) -> None:
        self.answers, self.asked = answers, []

    async def __call__(self, request: str, questions: dict[str, str]) -> dict[str, float]:
        self.asked.append((request, dict(questions)))
        if isinstance(self.answers, Exception):
            raise self.answers
        return {name: p for name, p in self.answers.items() if name in questions}


def _settings(**abilities) -> AppSettings:
    return AppSettings(_env_file=None, abilities={"enabled": True, "acquire": True, **abilities}, decision={"mode": "decision"})


@pytest.mark.asyncio
async def test_a_request_for_what_could_be_set_up_is_found_and_confirmed() -> None:
    installable = recipe_abilities(_settings())
    yes = _Confirmer({"model:voice.kokoro": 0.9})
    assert await asks_for_what_could_be_set_up(installable, "Now read that to me out loud.", confirm=yes) is True
    assert "model:voice.kokoro" in yes.asked[0][1]  # one shared word was enough to ask: the question decides


@pytest.mark.asyncio
async def test_a_no_nothing_shared_no_decision_model_or_a_broken_one_leave_it_as_chat() -> None:
    installable = recipe_abilities(_settings())
    assert await asks_for_what_could_be_set_up(installable, "Now read that to me out loud.", confirm=_Confirmer({"model:voice.kokoro": 0.1})) is False
    never_asked = _Confirmer({})
    assert await asks_for_what_could_be_set_up(installable, "How are you today?", confirm=never_asked) is False and never_asked.asked == []
    assert await asks_for_what_could_be_set_up(installable, "Read that out loud.", confirm=None) is False
    assert await asks_for_what_could_be_set_up([], "Read that out loud.", confirm=_Confirmer({"x": 1.0})) is False


def _message(text: str) -> InboundMessage:
    return InboundMessage(channel=ChannelType.SLACK, chat_id="chat-1", sender_id="u1", text=text, kind=MessageKind.TEXT)


class _Classifier:
    def __init__(self, classification: MessageClassification) -> None:
        self.classification = classification

    async def classify(self, inbound, context=None):
        return self.classification


async def _noop(chat_id: str, text: str):
    return None


@pytest.mark.asyncio
async def test_the_intake_turns_that_chat_into_a_task_and_leaves_ordinary_chat_alone(tmp_path, monkeypatch) -> None:
    async def fake_yes_no(self, text: str, questions: dict[str, str]) -> dict[str, float]:
        return {name: (0.9 if "out loud" in text.lower() else 0.0) for name in questions}

    monkeypatch.setattr("agent_control.llm.decision.DecisionModelClient.yes_no", fake_yes_no)
    repos, audit = make_repos(tmp_path)
    conversation_id = repos.conversations.get_or_create(ChannelType.SLACK, "chat-1")
    chat = MessageClassification(is_task=False, reason="just chatting", reply="I cannot speak.")

    async def intake(text: str, settings: AppSettings):
        return await classify_and_spawn_task(
            _message(text), conversation_id, repositories=repos, audit=audit, classifier=_Classifier(chat), settings=settings, send_progress=_noop,
        )

    aloud = await intake("Now read that to me out loud.", _settings())
    assert aloud.task is not None and aloud.outbound_message is None  # a task: the Operator is told what could be set up
    plain = await intake("Read me the news, thanks.", _settings())
    assert plain.task is None and plain.outbound_message.text == "I cannot speak."  # the answer to the question is no
    off = await intake("Now read that to me out loud.", _settings(acquire=False))
    assert off.task is None and off.outbound_message is not None  # acquire off: chat, as before
