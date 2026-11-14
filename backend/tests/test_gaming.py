"""Gaming mode: the person says they are about to play, the GPU is freed, and nothing loads a model until they say they are done."""

from __future__ import annotations

import json

import httpx
import pytest

from agent_control import gaming
from agent_control.channels.gaming_command import gaming_command_reply, while_gaming_reply
from agent_control.llm.warmup import forget_warm_state, warm_up
from agent_control.orchestration import TaskWorker
from agent_control.schemas import ChannelType, InboundMessage, MessageKind, TaskStatus
from helpers import make_repos


def _say(repos, text: str, flag, *, channel: ChannelType = ChannelType.TELEGRAM, url: str | None = "http://127.0.0.1:8000/v1"):
    inbound = InboundMessage(channel=channel, chat_id="42", sender_id="1", text=text, kind=MessageKind.TEXT)
    return gaming_command_reply(repos, inbound, localdeploy_url=url, flag=flag)


@pytest.fixture
def server(monkeypatch):
    """A fake LocalDeploy: what /models/free answers, and whether llama.cpp is still reachable after it."""
    state = {"free": {"success": True, "unloaded": 1, "llamacpp": "qwen"}, "llamacpp_reachable": False, "asked": []}

    def handler(request: httpx.Request) -> httpx.Response:
        state["asked"].append((request.method, request.url.path))
        if request.url.path == "/models/free":
            return httpx.Response(200, json=state["free"])
        return httpx.Response(200, json={"llamacpp": {"reachable": state["llamacpp_reachable"]}})

    real = httpx.Client

    monkeypatch.setattr(gaming.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    return state


# --- the state -------------------------------------------------------------------------------------------------------------------------


def test_it_is_off_until_turned_on_and_what_arrives_meanwhile_is_kept(tmp_path) -> None:
    flag = tmp_path / "state" / "gaming.json"
    assert gaming.is_on(flag) is False and gaming.hold("hello", flag) == 0
    assert gaming.turn_on(flag) is True and gaming.is_on(flag) is True
    assert gaming.turn_on(flag) is False  # already on: not started again
    assert gaming.hold("  find   my  resume ", flag) == 1 and gaming.hold("x" * 500, flag) == 2
    assert gaming.turn_off(flag) == ["find my resume", "x" * gaming.MAX_HELD_CHARS]
    assert gaming.is_on(flag) is False and gaming.turn_off(flag) == []


def test_the_gpu_is_freed_once(tmp_path) -> None:
    flag = tmp_path / "gaming.json"
    assert gaming.gpu_still_to_free(flag) is False
    gaming.turn_on(flag)
    assert gaming.gpu_still_to_free(flag) is True
    gaming.mark_gpu_freed(flag)
    assert gaming.gpu_still_to_free(flag) is False and gaming.is_on(flag)


def test_a_damaged_flag_file_is_off_not_a_crash(tmp_path) -> None:
    flag = tmp_path / "gaming.json"
    flag.write_text("not json", encoding="utf-8")
    assert gaming.is_on(flag) is False
    flag.write_text(json.dumps([1, 2]), encoding="utf-8")
    assert gaming.is_on(flag) is False


def test_the_model_servers_address_comes_from_the_models_base_url() -> None:
    assert gaming.localdeploy_root("http://127.0.0.1:8000/v1") == "http://127.0.0.1:8000"
    assert gaming.localdeploy_root("http://127.0.0.1:8000/v1/") == "http://127.0.0.1:8000"
    assert gaming.localdeploy_root(None) is None


# --- freeing the GPU, said in words ---------------------------------------------------------------------------------------------------


def test_the_model_server_is_asked_and_the_answer_is_said_plainly(server) -> None:
    assert gaming.free_gpu("http://127.0.0.1:8000") == "I unloaded 1 model; the GPU is free."
    assert ("POST", "/models/free") in server["asked"]
    server["free"] = {"success": True, "unloaded": 0}
    assert "already free" in gaming.free_gpu("http://127.0.0.1:8000")
    server["free"] = {"success": False, "error": "Ollama is not reachable."}
    assert "could not free the GPU: Ollama is not reachable." in gaming.free_gpu("http://127.0.0.1:8000")


def test_a_language_model_that_is_still_running_is_not_reported_as_freed(server) -> None:
    server["llamacpp_reachable"] = True
    said = gaming.free_gpu("http://127.0.0.1:8000")
    assert "still running" in said and "older LocalDeploy" in said


def test_an_unreachable_server_or_no_address_is_said_not_raised(monkeypatch) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    real = httpx.Client
    monkeypatch.setattr(gaming.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(refuse), **kw))
    assert "could not reach the model server" in gaming.free_gpu("http://127.0.0.1:8000")
    assert "do not know where the model server is" in gaming.free_gpu(None)


# --- what the person says ------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    ["gaming mode", "Gaming mode on", "turn on gaming mode", "I'm about to play a game, free up the GPU", "free the GPU", "free up my gpu please",
     "I'm going to play some games", "about to play steam", "gaming time"],
)
def test_the_ways_of_saying_they_are_about_to_play(tmp_path, server, text) -> None:
    from helpers import make_repos as _make

    repos, _ = _make(tmp_path)
    flag = tmp_path / "gaming.json"
    reply = _say(repos, text, flag)
    assert reply is not None and reply.text.startswith("Gaming mode is on. I unloaded 1 model; the GPU is free.") and gaming.is_on(flag)


@pytest.mark.parametrize(
    "text",
    ["I'm going to play with the cat", "what is gaming mode?", "I need a game plan for the report", "play some music", "free the afternoon", "I am done with the report"],
)
def test_other_messages_are_left_alone(tmp_path, server, text) -> None:
    repos, _ = make_repos(tmp_path)
    flag = tmp_path / "gaming.json"
    assert _say(repos, text, flag) is None and not gaming.is_on(flag)


def test_it_is_only_for_telegram_like_the_other_chat_commands(tmp_path, server) -> None:
    repos, _ = make_repos(tmp_path)
    assert _say(repos, "gaming mode", tmp_path / "g.json", channel=ChannelType.WHATSAPP) is None


def test_a_task_that_is_running_is_finished_before_the_gpu_is_taken(tmp_path, server) -> None:
    repos, _ = make_repos(tmp_path)
    task = repos.tasks.create("working on it")
    repos.tasks.update_status(task.id, TaskStatus.RUNNING)
    flag = tmp_path / "gaming.json"
    reply = _say(repos, "gaming mode", flag)
    assert "finishing the task that is running" in reply.text and gaming.is_on(flag) and gaming.gpu_still_to_free(flag)
    assert server["asked"] == []  # the model was not pulled out from under it


def test_saying_it_twice_says_so_and_done_gaming_brings_back_what_was_sent_meanwhile(tmp_path, server) -> None:
    repos, _ = make_repos(tmp_path)
    flag = tmp_path / "gaming.json"
    _say(repos, "gaming mode", flag)
    assert _say(repos, "gaming mode on", flag).text.startswith("Gaming mode is already on. ")
    inbound = InboundMessage(channel=ChannelType.TELEGRAM, chat_id="42", sender_id="1", text="find my resume", kind=MessageKind.TEXT)
    held = while_gaming_reply(inbound, flag=flag)
    assert "gaming mode" in held.text and "can't work on this yet" in held.text
    off = _say(repos, "I'm done gaming", flag)
    assert off.text.startswith("Gaming mode is off.") and "You sent 1 message" in off.text and "- find my resume" in off.text
    assert not gaming.is_on(flag) and while_gaming_reply(inbound, flag=flag) is None
    assert _say(repos, "gaming mode off", flag).text == "Gaming mode was not on."


@pytest.mark.parametrize("text", ["gaming mode off", "turn off gaming mode", "I'm done playing", "done gaming", "you can have the GPU back", "the gpu is yours"])
def test_the_ways_of_saying_they_are_done(tmp_path, server, text) -> None:
    repos, _ = make_repos(tmp_path)
    flag = tmp_path / "gaming.json"
    gaming.turn_on(flag)
    assert _say(repos, text, flag).text.startswith("Gaming mode is off.") and not gaming.is_on(flag)


# --- nothing loads a model meanwhile ------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_worker_starts_no_task_while_the_person_is_gaming_and_resumes_after(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("do something")
    holding = {"on": True}
    worker = TaskWorker(repos, audit, gpu_hold=lambda: holding["on"])
    assert await worker.process_next() is None
    assert repos.tasks.get(task.id).status == TaskStatus.RECEIVED  # not claimed, not failed
    holding["on"] = False
    assert (await worker.process_next()) is not None  # claimed once it is off (it has no operator, so it ends blocked: not our concern here)


@pytest.mark.asyncio
async def test_the_wake_up_call_is_not_sent_while_gaming(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    forget_warm_state()
    sent: list[str] = []

    class _Provider:
        async def generate_text(self, system: str, user: str) -> str:
            sent.append(user)
            return "OK"

    from types import SimpleNamespace

    profile = SimpleNamespace(provider="openai_compatible", base_url="http://127.0.0.1:8000/v1", model_copy=lambda update: profile)
    settings = SimpleNamespace(llm=SimpleNamespace(operator_profile=None, major_profile=None, default_profile="local", profiles={"local": profile}))
    gaming.turn_on()
    assert await warm_up(settings, build=lambda p: _Provider()) is False and sent == []
    gaming.turn_off()
    assert await warm_up(settings, build=lambda p: _Provider()) is True and sent == ["OK?"]
    forget_warm_state()
