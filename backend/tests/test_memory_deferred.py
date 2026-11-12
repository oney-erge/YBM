"""A message sent while a task runs does not queue a model call behind it."""

from __future__ import annotations

import asyncio

from agent_control.channels.memory import ConversationMemoryService
from agent_control.schemas import ChannelType, TaskStatus
from helpers import make_repos


class _Provider:
    def __init__(self) -> None:
        self.calls = 0

    async def generate_text(self, system_prompt: str, user_prompt: str) -> str:
        self.calls += 1
        return "A model-written summary."


def _service(tmp_path):
    repos, _ = make_repos(tmp_path)
    provider = _Provider()
    return repos, provider, ConversationMemoryService(repos, provider)


def _conv(repos):
    return repos.conversations.get_or_create(ChannelType.TELEGRAM, "42")


def test_with_nothing_running_the_summary_comes_from_the_model(tmp_path):
    repos, provider, service = _service(tmp_path)
    record = asyncio.run(service.update_from_user_message(_conv(repos), "hello there"))
    assert provider.calls == 1
    assert record["summary"] == "A model-written summary."


def test_with_a_task_running_the_model_is_left_alone(tmp_path):
    repos, provider, service = _service(tmp_path)
    conv = _conv(repos)
    task = repos.tasks.create("build an app", conversation_id=conv)
    repos.tasks.update_metadata(task.id, task.metadata, TaskStatus.RUNNING)
    record = asyncio.run(service.update_from_user_message(conv, "how's it going?"))
    assert provider.calls == 0
    assert "how's it going?" in str(record["facts"]["recent_turns"])


def test_another_conversations_task_does_not_hold_this_one_back(tmp_path):
    repos, provider, service = _service(tmp_path)
    other = repos.conversations.get_or_create(ChannelType.TELEGRAM, "99")
    task = repos.tasks.create("somebody else's job", conversation_id=other)
    repos.tasks.update_metadata(task.id, task.metadata, TaskStatus.RUNNING)
    asyncio.run(service.update_from_user_message(_conv(repos), "hello"))
    assert provider.calls == 1


def test_a_finished_task_no_longer_defers_it(tmp_path):
    repos, provider, service = _service(tmp_path)
    conv = _conv(repos)
    task = repos.tasks.create("done already", conversation_id=conv)
    repos.tasks.update_metadata(task.id, task.metadata, TaskStatus.COMPLETED)
    asyncio.run(service.update_from_user_message(conv, "thanks"))
    assert provider.calls == 1
