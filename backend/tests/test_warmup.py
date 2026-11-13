"""A message wakes the local model while it is still being routed, and never a hosted one."""

from __future__ import annotations

import pytest

from agent_control.config import AppSettings
from agent_control.llm import warmup
from agent_control.llm.warmup import MIN_INTERVAL_SECONDS, warm_profile_name, warm_up


def _settings(base_url="http://127.0.0.1:8000/v1", provider="openai_compatible", **llm):
    return AppSettings(
        _env_file=None,
        llm={"default_profile": "local", "profiles": {"local": {"provider": provider, "model": "qwen", "base_url": base_url}}, **llm},
    )


class _Provider:
    def __init__(self, error=None):
        self.error, self.calls = error, []

    async def generate_text(self, system_prompt, user_prompt):
        self.calls.append((system_prompt, user_prompt))
        if self.error:
            raise self.error
        return "OK"


class _Builder:
    def __init__(self, provider):
        self.provider, self.profiles = provider, []

    def __call__(self, profile, **kwargs):
        self.profiles.append(profile)
        return self.provider


@pytest.fixture(autouse=True)
def _fresh():
    warmup.forget_warm_state()
    yield
    warmup.forget_warm_state()


@pytest.mark.asyncio
async def test_a_local_model_gets_a_one_token_request_with_a_long_timeout():
    provider = _Provider()
    builder = _Builder(provider)
    assert await warm_up(_settings(), build=builder, clock=lambda: 1000.0) is True
    assert len(provider.calls) == 1
    assert builder.profiles[0].max_tokens == 1 and builder.profiles[0].timeout_seconds == 170


@pytest.mark.asyncio
async def test_it_is_sent_at_most_once_in_a_few_minutes():
    provider = _Provider()
    builder = _Builder(provider)
    now = {"t": 1000.0}
    assert await warm_up(_settings(), build=builder, clock=lambda: now["t"]) is True
    now["t"] += MIN_INTERVAL_SECONDS - 1
    assert await warm_up(_settings(), build=builder, clock=lambda: now["t"]) is False
    now["t"] += 2
    assert await warm_up(_settings(), build=builder, clock=lambda: now["t"]) is True
    assert len(provider.calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs", [
    {"base_url": "https://api.openai.com/v1"}, {"provider": "anthropic", "base_url": None}, {"base_url": None},
])
async def test_a_hosted_or_unknown_model_is_never_woken_by_a_message(kwargs):
    provider = _Provider()
    assert await warm_up(_settings(**kwargs), build=_Builder(provider), clock=lambda: 1.0) is False
    assert provider.calls == []


@pytest.mark.asyncio
async def test_a_failure_is_swallowed_and_a_missing_profile_does_nothing():
    assert await warm_up(_settings(), build=_Builder(_Provider(error=RuntimeError("down"))), clock=lambda: 1.0) is False
    missing = AppSettings(_env_file=None, llm={"default_profile": "nope", "profiles": {}})
    assert await warm_up(missing, build=_Builder(_Provider()), clock=lambda: 1.0) is False


def test_the_operator_profile_is_the_one_woken():
    settings = _settings(operator_profile="local")
    assert warm_profile_name(settings) == "local"
