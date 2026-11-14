"""The console does not wait for the model server on every page (admin.py, _llm_configured)."""

from __future__ import annotations

import threading
import time

from agent_control import admin
from agent_control.config import AppSettings, LLMProfileConfig


def _settings() -> AppSettings:
    profile = LLMProfileConfig(provider="openai_compatible", model="local-model", base_url="http://127.0.0.1:8000/v1")
    return AppSettings(_env_file=None, llm={"default_profile": "local", "profiles": {"local": profile}})


def test_a_hosted_profile_is_just_a_look_at_a_key_and_is_not_cached(monkeypatch):
    calls = []
    monkeypatch.setattr(admin, "check_llm_configured", lambda settings: calls.append(1) or True)
    hosted = LLMProfileConfig(provider="openai", model="m", base_url="https://api.openai.com/v1")
    settings = AppSettings(_env_file=None, llm={"default_profile": "cloud", "profiles": {"cloud": hosted}})
    admin._llm_configured(settings)
    admin._llm_configured(settings)
    assert len(calls) == 2


def test_the_first_answer_is_waited_for_and_the_next_ones_are_instant(monkeypatch):
    calls = []

    def slow_check(settings):
        calls.append(time.monotonic())
        time.sleep(0.2)
        return True

    monkeypatch.setattr(admin, "check_llm_configured", slow_check)
    settings = _settings()
    started = time.monotonic()
    assert admin._llm_configured(settings) is True
    assert time.monotonic() - started >= 0.15  # sleep(0.2) can return a clock tick early on Windows
    started = time.monotonic()
    for _ in range(20):
        assert admin._llm_configured(settings) is True
    assert time.monotonic() - started < 0.1 and len(calls) == 1


def test_an_old_answer_is_used_at_once_and_refreshed_in_the_background_once(monkeypatch):
    answers = iter([True, False])
    calls = []
    released = threading.Event()

    def check(settings):
        calls.append(1)
        value = next(answers)
        if len(calls) > 1:
            released.wait(2)
        return value

    monkeypatch.setattr(admin, "check_llm_configured", check)
    monkeypatch.setattr(admin, "_LLM_CHECK_TTL_SECONDS", 0.05)
    settings = _settings()
    assert admin._llm_configured(settings) is True
    time.sleep(0.1)
    started = time.monotonic()
    assert admin._llm_configured(settings) is True  # the old answer, while the new one is being asked
    assert admin._llm_configured(settings) is True  # and a second page does not start a second check
    assert time.monotonic() - started < 0.1 and len(calls) == 2
    released.set()
    for _ in range(50):
        if admin._llm_configured(settings) is False:
            break
        time.sleep(0.02)
    assert admin._llm_configured(settings) is False


def test_a_check_that_blows_up_in_the_background_keeps_the_last_answer(monkeypatch):
    state = {"calls": 0}

    def check(settings):
        state["calls"] += 1
        if state["calls"] > 1:
            raise RuntimeError("server went away")
        return True

    monkeypatch.setattr(admin, "check_llm_configured", check)
    monkeypatch.setattr(admin, "_LLM_CHECK_TTL_SECONDS", 0.01)
    settings = _settings()
    assert admin._llm_configured(settings) is True
    time.sleep(0.05)
    assert admin._llm_configured(settings) is True
    time.sleep(0.1)
    assert admin._llm_configured(settings) is True and state["calls"] >= 2
