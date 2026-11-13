"""Apps YBM started, what it knows about the person, and forgetting."""

from __future__ import annotations

import pytest

from agent_control.channels import chat_commands
from agent_control.channels.chat_commands import chat_command_reply
from agent_control.schemas import ChannelType, InboundMessage, MemoryFact, MemorySource, MessageKind, TaskStatus
from helpers import make_repos


@pytest.fixture
def chat(tmp_path):
    repos, _ = make_repos(tmp_path)
    return repos, repos.conversations.get_or_create(ChannelType.TELEGRAM, "42")


def _say(repos, conv, text, channel=ChannelType.TELEGRAM):
    inbound = InboundMessage(channel=channel, chat_id="42", sender_id="1", text=text, kind=MessageKind.TEXT)
    return chat_command_reply(repos, inbound, conv)


def _app(repos, conv, objective, url, pid=4242, title=""):
    task = repos.tasks.create(objective, conversation_id=conv, metadata={"source_chat_id": "42"})
    metadata = {**task.metadata, "preview_server": {"pid": pid, "url": url, "workspace_dir": "x"}}
    if title:
        metadata["page_controls"] = {"title": title, "controls": [], "buttons": [], "headings": []}
    return repos.tasks.update_metadata(task.id, metadata, TaskStatus.COMPLETED)


@pytest.fixture
def up(monkeypatch):
    """Pages that answer; everything else is down."""
    state = {"up": set()}
    monkeypatch.setattr(chat_commands, "fetch_page", lambda url, timeout=1.0: "<html>" if url in state["up"] else None)
    return state


# ---- apps ---------------------------------------------------------------------------------

def test_listing_shows_only_apps_that_still_answer(chat, up):
    repos, conv = chat
    _app(repos, conv, "Build a recipe scaler", "http://127.0.0.1:8891/", title="Recipe Scaler")
    _app(repos, conv, "Build a unit converter", "http://127.0.0.1:8892/", title="Unit Converter")
    up["up"] = {"http://127.0.0.1:8891/"}
    reply = _say(repos, conv, "what apps are running?")
    assert "- Recipe Scaler: http://127.0.0.1:8891/" in reply.text
    assert "Unit Converter" not in reply.text


def test_listing_with_nothing_running(chat, up):
    repos, conv = chat
    assert _say(repos, conv, "what apps are running?").text == "I have no apps running right now."


def test_stopping_an_app_checks_the_process_before_killing_it(chat, up, monkeypatch):
    repos, conv = chat
    _app(repos, conv, "Build a recipe scaler", "http://127.0.0.1:8891/", pid=4242, title="Recipe Scaler")
    up["up"] = {"http://127.0.0.1:8891/"}
    killed = []
    monkeypatch.setattr(chat_commands, "_command_line", lambda pid: "python -m http.server 8891 --bind 127.0.0.1")
    monkeypatch.setattr(chat_commands.subprocess, "run", lambda cmd, **kw: killed.append(cmd) or None)
    monkeypatch.setattr(chat_commands.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    original_fetch = chat_commands.fetch_page

    def gone_after_kill(url, timeout=1.0):
        return None if killed else original_fetch(url, timeout)

    monkeypatch.setattr(chat_commands, "fetch_page", gone_after_kill)
    reply = _say(repos, conv, "stop the recipe app")
    assert reply.text == "Stopped Recipe Scaler (http://127.0.0.1:8891/)."
    assert killed


def test_a_reused_process_id_is_never_killed(chat, up, monkeypatch):
    repos, conv = chat
    _app(repos, conv, "Build a recipe scaler", "http://127.0.0.1:8891/", pid=4242, title="Recipe Scaler")
    up["up"] = {"http://127.0.0.1:8891/"}
    killed = []
    monkeypatch.setattr(chat_commands, "_command_line", lambda pid: "C:\\Windows\\notepad.exe")
    monkeypatch.setattr(chat_commands.subprocess, "run", lambda cmd, **kw: killed.append(cmd))
    monkeypatch.setattr(chat_commands.os, "kill", lambda pid, sig: killed.append(pid))
    reply = _say(repos, conv, "stop that app")
    assert "I could not stop Recipe Scaler" in reply.text and "left it alone" in reply.text
    assert killed == []


@pytest.mark.parametrize("text", ["stop the music", "stop the recipe app", "close the door"])
def test_stop_with_nothing_of_mine_running_is_left_alone(chat, up, text):
    repos, conv = chat
    assert _say(repos, conv, text) is None


def test_stop_naming_something_that_is_not_an_app_is_left_alone(chat, up):
    repos, conv = chat
    _app(repos, conv, "Build a recipe scaler", "http://127.0.0.1:8891/", title="Recipe Scaler")
    up["up"] = {"http://127.0.0.1:8891/"}
    assert _say(repos, conv, "stop the music") is None


# ---- memory -------------------------------------------------------------------------------

def _fact(repos, category, content):
    return repos.memory_facts.create(MemoryFact(category=category, content=content, source=MemorySource.USER_STATED))


def test_what_do_you_know_about_me_lists_the_profile_and_the_rules(chat):
    repos, conv = chat
    _fact(repos, "profile", "Name: Oney Erge")
    _fact(repos, "profile", "T-shirt size: L")
    _fact(repos, "preference", "Always answer in metric units")
    text = _say(repos, conv, "what do you know about me?").text
    assert "- Name: Oney Erge" in text and "- T-shirt size: L" in text
    assert "Things you asked me to remember:" in text and "- Always answer in metric units" in text
    assert 'Say "forget my <thing>"' in text


def test_what_do_you_know_with_nothing_remembered(chat):
    repos, conv = chat
    assert "don't know anything about you yet" in _say(repos, conv, "what do you remember about me").text


def test_forget_removes_the_one_fact_that_matches(chat):
    repos, conv = chat
    _fact(repos, "profile", "T-shirt size: L")
    _fact(repos, "profile", "GitHub username: oney-erge")
    assert _say(repos, conv, "forget my t-shirt size").text == "Forgot: T-shirt size: L"
    assert [f.content for f in repos.memory_facts.list_all()] == ["GitHub username: oney-erge"]


def test_forget_with_several_matches_asks_which(chat):
    repos, conv = chat
    _fact(repos, "profile", "Favorite color: green")
    _fact(repos, "profile", "Favorite language: Python")
    text = _say(repos, conv, "forget my favorite").text
    assert text.startswith("Which one?") and len(repos.memory_facts.list_all()) == 2


def test_forget_something_unknown_about_me_says_so_but_other_forgetting_is_left_alone(chat):
    repos, conv = chat
    assert _say(repos, conv, "forget my shoe size").text == 'I don\'t have anything about "my shoe size" to forget.'
    assert _say(repos, conv, "forget the meeting") is None


def test_forget_everything_is_not_one_message(chat):
    repos, conv = chat
    _fact(repos, "profile", "Name: Oney Erge")
    assert "one at a time" in _say(repos, conv, "forget everything about me").text
    assert len(repos.memory_facts.list_all()) == 1


def test_other_channels_and_ordinary_requests_are_left_alone(chat):
    repos, conv = chat
    assert _say(repos, conv, "what do you know about me?", channel=ChannelType.WHATSAPP) is None
    for text in ["find my resume", "what is the capital of France?", "show me the weather", ""]:
        assert _say(repos, conv, text) is None
