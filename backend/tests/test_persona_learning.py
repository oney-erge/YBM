"""Learned preferences are proposed, never silently written.

persona.md is re-read on every Operator step, so one line in it changes every
future task. That is exactly why a model must not be able to add lines to it
on its own: a system that edits its own standing instructions drifts in ways
no audit trail can reconstruct, and "why is it behaving like this now?" would
have no answer six months later.
"""

from __future__ import annotations

import pytest

from agent_control.config import PersonaAdapterConfig
from agent_control.persona import read_persona
from agent_control.persona_learning import (
    SuggestionStore,
    apply_to_persona,
    propose_from_message,
)


class StubProvider:
    """Answers the way the model is asked to: a verdict and a line. "NONE" stands for no preference."""

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls = 0

    async def generate_structured(self, system_prompt: str, user_prompt: str, schema, **kwargs):
        self.calls += 1
        if self.reply.upper() == "NONE":
            return schema(states_a_lasting_preference=False, preference="")
        return schema(states_a_lasting_preference=True, preference=self.reply)


class ExplodingProvider:
    async def generate_structured(self, system_prompt: str, user_prompt: str, schema, **kwargs):
        raise RuntimeError("model down")


def _config(tmp_path, *, learning: bool = True) -> PersonaAdapterConfig:
    return PersonaAdapterConfig(
        enabled=True, path=str(tmp_path / "persona.md"), learning_enabled=learning
    )


@pytest.mark.asyncio
async def test_a_preference_is_queued_not_written(tmp_path) -> None:
    config = _config(tmp_path)
    provider = StubProvider("Keep answers under 5 bullet points.")

    suggestion = await propose_from_message(provider, config, "stop giving me essays, keep it short")

    assert suggestion is not None
    assert SuggestionStore.load(config).pending()[0].line == "Keep answers under 5 bullet points."
    # The file itself is untouched until a human decides.
    assert read_persona(config) == "(no persona/preferences recorded yet)"


@pytest.mark.asyncio
async def test_learning_off_proposes_nothing(tmp_path) -> None:
    config = _config(tmp_path, learning=False)
    provider = StubProvider("Keep answers short.")

    assert await propose_from_message(provider, config, "keep it short") is None
    assert provider.calls == 0, "must not spend an LLM call when the toggle is off"


@pytest.mark.asyncio
async def test_ordinary_requests_produce_nothing(tmp_path) -> None:
    """Most messages state no lasting preference; a queue full of guesses
    about the user is worse than an empty one."""
    config = _config(tmp_path)

    assert await propose_from_message(StubProvider("NONE"), config, "what's in my downloads folder?") is None


@pytest.mark.asyncio
async def test_a_failing_provider_never_breaks_the_finished_task(tmp_path) -> None:
    config = _config(tmp_path)

    assert await propose_from_message(ExplodingProvider(), config, "keep it short") is None


@pytest.mark.asyncio
async def test_the_same_preference_is_not_queued_twice(tmp_path) -> None:
    config = _config(tmp_path)
    provider = StubProvider("Keep answers under 5 bullet points.")

    first = await propose_from_message(provider, config, "keep it short")
    second = await propose_from_message(provider, config, "seriously, keep it short")

    assert first is not None
    assert second is None
    assert len(SuggestionStore.load(config).pending()) == 1


def test_accepting_records_when_and_why(tmp_path) -> None:
    """A bare rule cannot answer "why does it do this?" later, so the date and
    the message that produced it are written alongside the line."""
    config = _config(tmp_path)
    store = SuggestionStore.load(config)
    suggestion = store.add("Keep answers under 5 bullet points.", "stop giving me essays", "task_1")

    persona = apply_to_persona(config, suggestion)

    assert "Keep answers under 5 bullet points." in persona
    assert "stop giving me essays" in persona
    assert "learned" in persona
    assert read_persona(config) == persona


def test_rejecting_leaves_the_persona_alone(tmp_path) -> None:
    config = _config(tmp_path)
    store = SuggestionStore.load(config)
    suggestion = store.add("Never ask before deleting files.", "just do it", "task_1")

    decided = store.decide(suggestion.id, accept=False)

    assert decided.status == "rejected"
    assert SuggestionStore.load(config).pending() == []
    assert read_persona(config) == "(no persona/preferences recorded yet)"


def test_a_decision_cannot_be_applied_twice(tmp_path) -> None:
    config = _config(tmp_path)
    store = SuggestionStore.load(config)
    suggestion = store.add("Keep answers short.", "be brief", None)
    store.decide(suggestion.id, accept=True)

    assert SuggestionStore.load(config).decide(suggestion.id, accept=True) is None


@pytest.mark.asyncio
async def test_a_verdict_of_no_preference_wins_over_a_line_the_model_wrote_anyway(tmp_path) -> None:
    """The 9B model answered the question in the message instead of extracting from it; only an explicit yes counts."""
    config = _config(tmp_path)

    class Chatty:
        async def generate_structured(self, system_prompt, user_prompt, schema, **kwargs):
            return schema(states_a_lasting_preference=False, preference="The product of 12 and 12 is 144.")

    assert await propose_from_message(Chatty(), config, "What is 12 times 12?") is None
    assert SuggestionStore.load(config).pending() == []
