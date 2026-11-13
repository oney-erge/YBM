"""The rolling summary is written after the reply, not before it (channels/memory.py, summarize_in_background)."""

from __future__ import annotations

import asyncio

from agent_control.channels.memory import ConversationMemoryService
from agent_control.schemas import ChannelType, TaskStatus
from helpers import make_repos


class _Provider:
    def __init__(self, release: asyncio.Event | None = None) -> None:
        self.calls = 0
        self.release = release

    async def generate_text(self, system_prompt: str, user_prompt: str) -> str:
        self.calls += 1
        if self.release is not None:
            await self.release.wait()
        return f"Summary number {self.calls}."


def _setup(tmp_path, **kwargs):
    repos, _ = make_repos(tmp_path)
    provider = kwargs.pop("provider", None) or _Provider()
    service = ConversationMemoryService(repos, provider, summarize_in_background=True, summarize_delay_seconds=0, **kwargs)
    return repos, provider, service, repos.conversations.get_or_create(ChannelType.TELEGRAM, "42")


def test_the_message_is_stored_at_once_and_the_model_is_asked_afterwards(tmp_path):
    async def scenario():
        repos, provider, service, conv = _setup(tmp_path)
        record = await service.update_from_user_message(conv, "my gym locker is 4172")
        # nothing waited for the model: the turn is stored and the summary is the heuristic one
        assert provider.calls == 0
        assert "4172" in str(record["facts"]["recent_turns"])
        await service.wait_for_background()
        assert provider.calls == 1
        stored = repos.conversation_memory.get(conv)
        assert stored["summary"] == "Summary number 1."
        assert "4172" in str(stored["facts"]["recent_turns"])

    asyncio.run(scenario())


def test_messages_that_arrive_while_it_summarizes_cost_one_more_run_not_one_each(tmp_path):
    async def scenario():
        release = asyncio.Event()
        repos, provider, service, conv = _setup(tmp_path, provider=_Provider(release))
        await service.update_from_user_message(conv, "first")
        await asyncio.sleep(0)  # let the background run start and wait on the model
        await asyncio.sleep(0)
        for text in ("second", "third", "fourth"):
            await service.update_from_user_message(conv, text)
        release.set()
        await service.wait_for_background()
        assert provider.calls == 2, "the run in progress goes once more with the newer turns"
        turns = str(repos.conversation_memory.get(conv)["facts"]["recent_turns"])
        assert all(word in turns for word in ("first", "second", "third", "fourth"))

    asyncio.run(scenario())


def test_a_turn_that_arrives_during_the_model_call_is_not_lost(tmp_path):
    async def scenario():
        release = asyncio.Event()
        repos, provider, service, conv = _setup(tmp_path, provider=_Provider(release))
        await service.update_from_user_message(conv, "before")
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await service.update_from_user_message(conv, "during")
        release.set()
        await service.wait_for_background()
        assert "during" in str(repos.conversation_memory.get(conv)["facts"]["recent_turns"])

    asyncio.run(scenario())


def test_a_task_working_on_the_model_means_no_summary_call(tmp_path):
    async def scenario():
        repos, provider, service, conv = _setup(tmp_path)
        task = repos.tasks.create("build an app", conversation_id=conv)
        repos.tasks.update_metadata(task.id, task.metadata, TaskStatus.RUNNING)
        await service.update_from_user_message(conv, "how is it going?")
        await service.wait_for_background()
        assert provider.calls == 0

    asyncio.run(scenario())


def test_a_failing_model_leaves_the_heuristic_summary_and_no_error(tmp_path):
    class Broken:
        async def generate_text(self, system_prompt, user_prompt):
            raise RuntimeError("model is down")

    async def scenario():
        repos, _provider, service, conv = _setup(tmp_path, provider=Broken())
        await service.update_from_user_message(conv, "hello there")
        await service.wait_for_background()
        assert repos.conversation_memory.get(conv)["summary"]

    asyncio.run(scenario())


def test_without_the_option_the_summary_is_still_written_before_returning(tmp_path):
    async def scenario():
        repos, _ = make_repos(tmp_path)
        provider = _Provider()
        service = ConversationMemoryService(repos, provider)
        conv = repos.conversations.get_or_create(ChannelType.TELEGRAM, "7")
        record = await service.update_from_user_message(conv, "hello")
        assert provider.calls == 1 and record["summary"] == "Summary number 1."

    asyncio.run(scenario())
