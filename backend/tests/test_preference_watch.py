"""A lasting preference stated in chat is noticed after the reply, cheaply, and offered as a Remember / No card (preference_watch.py)."""

from __future__ import annotations

import asyncio

import pytest

from agent_control.config import AppSettings, PersonaAdapterConfig, TelegramConfig
from agent_control.persona_learning import SuggestionStore
from agent_control.preference_watch import GATE_AT, GATE_QUESTION, PreferenceWatch
from test_telegram import _FakeBotApiForCallbacks, _service

ROUTING = {"source_channel": "telegram", "source_chat_id": "100"}


class _Provider:
    def __init__(self, preference: str = "Keep answers to at most two sentences.", yes: bool = True) -> None:
        self.preference, self.yes, self.calls = preference, yes, 0

    async def generate_structured(self, system_prompt, user_prompt, schema, **kwargs):
        self.calls += 1
        return schema(states_a_lasting_preference=self.yes, preference=self.preference if self.yes else "")


class _Say:
    def __init__(self) -> None:
        self.sent = []

    async def __call__(self, routing, text, keyboard):
        self.sent.append((routing, text, keyboard))


def _config(tmp_path, *, learning: bool = True) -> PersonaAdapterConfig:
    return PersonaAdapterConfig(enabled=True, path=str(tmp_path / "persona.md"), learning_enabled=learning)


def _yes_no(score: float | Exception, asked: list):
    async def ask(text: str, questions: dict[str, str]) -> dict[str, float]:
        asked.append((text, questions))
        if isinstance(score, Exception):
            raise score
        return {"preference": score}

    return ask


@pytest.mark.asyncio
async def test_a_message_the_gate_says_may_state_a_preference_is_extracted_and_offered_in_the_chat(tmp_path):
    asked, say, provider = [], _Say(), _Provider()
    watch = PreferenceWatch(_config(tmp_path), provider, _yes_no(0.8, asked), say)
    assert await watch.consider("Always keep your answers to at most two sentences.", ROUTING) is True
    assert asked[0][1] == {"preference": GATE_QUESTION} and provider.calls == 1
    routing, text, keyboard = say.sent[0]
    assert routing == ROUTING and "Keep answers to at most two sentences." in text
    assert [b["text"] for b in keyboard["inline_keyboard"][0]] == ["Remember", "No"]
    assert [s.line for s in SuggestionStore.load(_config(tmp_path)).pending()] == ["Keep answers to at most two sentences."]


@pytest.mark.asyncio
async def test_a_request_is_stopped_at_the_gate_and_never_costs_the_assistants_model_a_call(tmp_path):
    say, provider = _Say(), _Provider()
    assert GATE_AT == 0.4
    watch = PreferenceWatch(_config(tmp_path), provider, _yes_no(0.05, []), say)
    assert await watch.consider("What is 12 times 12?", ROUTING) is False
    assert provider.calls == 0 and say.sent == []


@pytest.mark.asyncio
async def test_a_gate_that_says_yes_to_a_request_is_overruled_by_the_extraction(tmp_path):
    say, provider = _Say(), _Provider(yes=False)
    assert await PreferenceWatch(_config(tmp_path), provider, _yes_no(0.9, []), say).consider("Remind me every Friday to send it", ROUTING) is False
    assert provider.calls == 1 and say.sent == []


@pytest.mark.asyncio
async def test_nothing_happens_when_learning_is_off_there_is_no_gate_no_chat_or_the_message_is_a_command(tmp_path):
    say, provider = _Say(), _Provider()
    asked: list = []
    gate = _yes_no(0.9, asked)
    assert await PreferenceWatch(_config(tmp_path, learning=False), provider, gate, say).consider("Always be brief please", ROUTING) is False
    assert await PreferenceWatch(_config(tmp_path), provider, None, say).consider("Always be brief please", ROUTING) is False
    assert await PreferenceWatch(_config(tmp_path), None, gate, say).consider("Always be brief please", ROUTING) is False
    assert await PreferenceWatch(_config(tmp_path), provider, gate, say).consider("Always be brief please", {}) is False
    assert await PreferenceWatch(_config(tmp_path), provider, gate, say).consider("/plan", ROUTING) is False
    assert await PreferenceWatch(_config(tmp_path), provider, gate, say).consider("ok", ROUTING) is False
    assert asked == [] and provider.calls == 0 and say.sent == []


@pytest.mark.asyncio
async def test_a_decision_model_that_is_down_is_not_an_error_and_a_known_preference_is_not_offered_twice(tmp_path):
    say = _Say()
    assert await PreferenceWatch(_config(tmp_path), _Provider(), _yes_no(RuntimeError("down"), []), say).consider("Always be brief please", ROUTING) is False
    watch = PreferenceWatch(_config(tmp_path), _Provider(), _yes_no(0.9, []), say)
    assert await watch.consider("Always be brief please", ROUTING) is True
    assert await watch.consider("Always be brief please, really", ROUTING) is False  # the same line is already waiting
    assert len(say.sent) == 1


# --- the intake -------------------------------------------------------------------------------------------------------------------------------


class _Bot(_FakeBotApiForCallbacks):
    def __init__(self) -> None:
        super().__init__()
        self.sent = []

    async def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append((chat_id, text, reply_markup))
        return {"ok": True}


@pytest.mark.asyncio
async def test_after_a_chat_message_is_answered_the_intake_looks_at_it_in_the_background_and_a_task_is_left_to_the_worker(tmp_path, monkeypatch):
    seen = []

    async def consider(self, text, routing, **kwargs):
        seen.append((text, routing))
        return False

    monkeypatch.setattr(PreferenceWatch, "consider", consider)
    bot = _Bot()
    service, repos = _service(tmp_path, TelegramConfig(enabled=True, allowed_user_ids=[42], allowed_chat_ids=[100]), bot_api=bot)
    service.settings = AppSettings(
        _env_file=None, adapters={"persona": {"enabled": True, "path": str(tmp_path / "persona.md"), "learning_enabled": True}}
    )
    from agent_control.channels.base import ChannelUpdateResult
    from agent_control.schemas import ChannelType, InboundMessage, MessageKind, TaskRecord

    inbound = InboundMessage(channel=ChannelType.TELEGRAM, kind=MessageKind.TEXT, sender_id="42", chat_id="100", text="Always be brief please")
    service._watch_for_a_preference(inbound, ChannelUpdateResult(authorized=True, inbound_message=inbound))
    await asyncio.gather(*service._background)
    assert seen == [("Always be brief please", {"source_channel": "telegram", "source_chat_id": "100"})]
    service._watch_for_a_preference(inbound, ChannelUpdateResult(authorized=True, inbound_message=inbound, task=TaskRecord(objective="x")))
    assert len(service._background) == 0 and len(seen) == 1  # a message that became a task is the worker's to look at
