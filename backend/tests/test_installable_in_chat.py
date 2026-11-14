"""A message taken for chat that asks for something that could be set up becomes a task (abilities/context.py, channels/base.py)."""

from __future__ import annotations

import pytest

from agent_control.abilities.context import asks_for_what_a_recipe_covers
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
    assert await asks_for_what_a_recipe_covers(installable, "Now read that to me out loud.", confirm=yes) is True
    assert "model:voice.kokoro" in yes.asked[0][1]  # one shared word was enough to ask: the question decides


@pytest.mark.asyncio
async def test_a_no_nothing_shared_no_decision_model_or_a_broken_one_leave_it_as_chat() -> None:
    installable = recipe_abilities(_settings())
    assert await asks_for_what_a_recipe_covers(installable, "Now read that to me out loud.", confirm=_Confirmer({"model:voice.kokoro": 0.1})) is False
    never_asked = _Confirmer({})
    assert await asks_for_what_a_recipe_covers(installable, "How are you today?", confirm=never_asked) is False and never_asked.asked == []
    assert await asks_for_what_a_recipe_covers(installable, "Read that out loud.", confirm=None) is False
    assert await asks_for_what_a_recipe_covers([], "Read that out loud.", confirm=_Confirmer({"x": 1.0})) is False


@pytest.mark.asyncio
async def test_a_recipe_that_is_already_set_up_is_still_recognised_so_the_request_is_not_chat_again(tmp_path) -> None:
    voice, model = tmp_path / "kokoro.onnx", tmp_path / "voices.bin"
    voice.write_bytes(b"x"), model.write_bytes(b"x")
    settings = AppSettings(
        _env_file=None, abilities={"enabled": True, "acquire": True}, decision={"mode": "decision"},
        adapters={"tts": {"enabled": True, "model_path": str(voice), "voices_path": str(model)}},
    )
    records = {ability.id: ability for ability in recipe_abilities(settings)}
    assert records["model:voice.kokoro"].state.value == "ready" and "tts.synthesize" in records["model:voice.kokoro"].guidance
    yes = _Confirmer({"model:voice.kokoro": 0.9})
    assert await asks_for_what_a_recipe_covers(records.values(), "Now read that to me out loud.", confirm=yes) is True
    assert await asks_for_what_a_recipe_covers(records.values(), "Now read that to me out loud.", confirm=_Confirmer({"model:voice.kokoro": 0.2})) is False


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
        return {name: (0.9 if "out loud" in text.lower() and name.startswith("model:") else 0.0) for name in questions}

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


@pytest.mark.asyncio
async def test_a_question_about_the_assistant_itself_is_found_and_confirmed() -> None:
    from agent_control.abilities.context import ABOUT_THE_ASSISTANT, asks_about_the_assistant

    yes, no = _Confirmer({"about_the_assistant": 0.9}), _Confirmer({"about_the_assistant": 0.2})
    assert await asks_about_the_assistant("What can't you do?", confirm=yes) is True
    assert yes.asked[0][1] == {"about_the_assistant": ABOUT_THE_ASSISTANT}
    assert await asks_about_the_assistant("What can I do with my evenings?", confirm=no) is False
    assert await asks_about_the_assistant("What can't you do?", confirm=None) is False
    assert await asks_about_the_assistant("What can't you do?", confirm=_Confirmer(RuntimeError("model down"))) is False


@pytest.mark.asyncio
async def test_the_intake_sends_a_question_about_the_assistant_to_the_operator_even_with_installs_off(tmp_path, monkeypatch) -> None:
    """Live (challenge a7, g1, g2): chat answered "what can't you do?" with what it can do, and invented "without needing explicit approval"."""
    async def fake_yes_no(self, text: str, questions: dict[str, str]) -> dict[str, float]:
        return {name: (0.9 if name == "about_the_assistant" and "you" in text.lower() else 0.0) for name in questions}

    monkeypatch.setattr("agent_control.llm.decision.DecisionModelClient.yes_no", fake_yes_no)
    repos, audit = make_repos(tmp_path)
    conversation_id = repos.conversations.get_or_create(ChannelType.SLACK, "chat-1")
    chat = MessageClassification(is_task=False, reason="just chatting", reply="I can inspect your desktop.")

    async def intake(text: str, settings: AppSettings):
        return await classify_and_spawn_task(
            _message(text), conversation_id, repositories=repos, audit=audit, classifier=_Classifier(chat), settings=settings, send_progress=_noop,
        )

    asked = await intake("What can't you do?", _settings(acquire=False))
    assert asked.task is not None and asked.outbound_message is None
    other = await intake("Thanks, that is all.", _settings(acquire=False))
    assert other.task is None and other.outbound_message.text == "I can inspect your desktop."
    abilities_off = AppSettings(_env_file=None, abilities={"enabled": False}, decision={"mode": "decision"})
    off = await intake("What can't you do?", abilities_off)
    assert off.task is None and off.outbound_message is not None  # no ability index to read: chat, as before
