"""A free check of the coding agents: installed, which version, and the model Codex is set to use."""

from __future__ import annotations

import pytest

from agent_control.channels.agent_check import agent_check_reply, check_coding_agents, codex_configured_model
from agent_control.channels.chat_commands import chat_command_reply
from agent_control.schemas import ChannelType, InboundMessage, MessageKind
from helpers import make_repos


def _lines(**overrides):
    defaults = dict(which=lambda name: f"C:/bin/{name}.exe", version=lambda exe: "9.9.9", codex_model=lambda: None)
    return check_coding_agents(**{**defaults, **overrides})


def test_both_installed_with_versions():
    codex, claude = _lines(version=lambda exe: "codex-cli 0.149.0" if "codex" in exe else "2.1.240 (Claude Code)")
    assert codex == "- Codex: installed (codex-cli 0.149.0). No model is set in its config."
    assert claude == "- Claude Code: installed (2.1.240 (Claude Code))."


def test_the_model_codex_is_set_to_use_is_named():
    codex, _ = _lines(codex_model=lambda: "gpt-6.1-sol")
    assert "Its config uses the model gpt-6.1-sol" in codex and "a run stops straight away" in codex


def test_a_missing_tool_is_said_plainly():
    codex, claude = _lines(which=lambda name: None if name == "codex" else "C:/bin/claude.exe")
    assert codex.startswith("- Codex: not installed") and "`codex`" in codex
    assert claude.startswith("- Claude Code: installed")


def test_a_tool_that_does_not_answer_is_not_called_ready():
    codex, _ = _lines(version=lambda exe: None)
    assert "did not answer when asked for its version" in codex and "would not rely on it" in codex


def test_the_model_is_read_from_the_codex_config(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text('model = "gpt-6.1-sol"\nmodel_reasoning_effort = "xhigh"\n', encoding="utf-8")
    assert codex_configured_model(config) == "gpt-6.1-sol"
    assert codex_configured_model(tmp_path / "missing.toml") is None
    config.write_text("this is = = not toml", encoding="utf-8")
    assert codex_configured_model(config) is None
    config.write_text('model = ""\n', encoding="utf-8")
    assert codex_configured_model(config) is None


def _say(text, channel=ChannelType.TELEGRAM):
    return InboundMessage(channel=channel, chat_id="42", sender_id="1", text=text, kind=MessageKind.TEXT)


@pytest.mark.parametrize("text", [
    "are my coding agents working?", "is codex working", "are codex and claude code ready?", "check my coding agents", "can you use codex?",
    "test claude code", "Is Claude Code installed?",
])
def test_the_ways_to_ask(text, monkeypatch):
    monkeypatch.setattr("agent_control.channels.agent_check.check_coding_agents", lambda **kw: ["- Codex: installed (x)."])
    reply = agent_check_reply(_say(text))
    assert reply is not None and reply.text.startswith("Coding agents on this computer:\n- Codex: installed (x).")
    assert "does not run them" in reply.text


@pytest.mark.parametrize("text", ["use codex to write a script", "what is codex?", "find my resume", "", "is it working?"])
def test_other_messages_are_left_alone(text):
    assert agent_check_reply(_say(text)) is None


def test_other_channels_are_left_alone():
    assert agent_check_reply(_say("are my coding agents working?", channel=ChannelType.WHATSAPP)) is None


def test_it_is_reachable_from_the_chat_commands(tmp_path, monkeypatch):
    repos, _ = make_repos(tmp_path)
    monkeypatch.setattr("agent_control.channels.agent_check.check_coding_agents", lambda **kw: ["- Codex: installed (x)."])
    reply = chat_command_reply(repos, _say("are my coding agents working?"), None)
    assert reply is not None and "Coding agents on this computer" in reply.text
