"""The assistant remembers what the owner says, across every chat, and keeps what stopped being true (channels/memory_learning.py, storage episodes)."""

from __future__ import annotations

import pytest

from agent_control.channels.memory import ConversationMemoryService
from agent_control.channels.memory_learning import FactLearner, _Change, _Changes, worth_asking
from agent_control.config import AppSettings
from agent_control.schemas import ChannelType, InboundMessage, MemoryFact, MessageKind, ToolCallRequest, Capability
from agent_control.tools.memory_manage import MemoryManageAdapter
from helpers import make_repos


class _Provider:
    def __init__(self, changes: list[_Change]) -> None:
        self.changes = changes
        self.asked: list[tuple[str, str]] = []

    async def generate_structured(self, system_prompt, user_prompt, schema, **kwargs):
        self.asked.append((system_prompt, user_prompt))
        return _Changes(changes=self.changes)

    async def generate_text(self, system_prompt, user_prompt):
        return "A short summary."


def test_memory_is_on_by_default_and_the_two_switches_are_settings() -> None:
    settings = AppSettings(_env_file=None)
    assert settings.memory.enabled is True and settings.memory.auto_remember is True


def test_only_a_message_about_the_person_is_worth_a_model_call() -> None:
    assert worth_asking("We just had a daughter in October, her name is Maya.")
    assert worth_asking("I prefer short answers please")
    assert worth_asking("my daughter is called Maya and she was born in October")
    assert not worth_asking("Find my resume.")
    assert not worth_asking("ok")
    assert not worth_asking("What is 12 times 12?")


@pytest.mark.asyncio
async def test_a_new_fact_is_kept_and_what_changed_closes_the_old_one_with_its_history(tmp_path) -> None:
    repos, _audit = make_repos(tmp_path)
    home = repos.memory_facts.create(MemoryFact(category="life", content="Lives at 12 Elm Road, Pearland", about="home address"))
    provider = _Provider([
        _Change(op="add", about="daughter", text="Has a daughter, Maya, born in October 2026"),
        _Change(op="change", about="home address", text="Lives on Oak Street, Pearland", id=home.id),
    ])
    kept = await FactLearner(repos, provider).learn("We had a daughter Maya in October and we moved to Oak Street.", conversation_id="c1")

    assert [line.split(":")[0] for line in kept] == ["added", "changed"]
    now = {fact.content for fact in repos.memory_facts.list_all()}
    assert now == {"Has a daughter, Maya, born in October 2026", "Lives on Oak Street, Pearland"}  # the old address is no longer offered as what is known
    history = repos.memory_facts.list_all(include_closed=True)
    old = next(f for f in history if f.id == home.id)
    new = next(f for f in history if f.content.startswith("Lives on Oak"))
    assert old.valid_to is not None and not old.is_current and new.supersedes_id == home.id and new.is_current  # nothing was deleted
    assert "What the person just said" in provider.asked[0][1] and home.id in provider.asked[0][1]  # the model saw what was already known


@pytest.mark.asyncio
async def test_a_fact_that_is_simply_no_longer_so_is_ended_and_a_secret_or_a_duplicate_is_never_kept(tmp_path) -> None:
    repos, _audit = make_repos(tmp_path)
    dog = repos.memory_facts.create(MemoryFact(category="life", content="Has a dog named Rex", about="dog"))
    known = repos.memory_facts.create(MemoryFact(category="life", content="Prefers short answers", about="answer style"))
    provider = _Provider([
        _Change(op="end", id=dog.id),
        _Change(op="add", about="wifi", text="The wifi password is hunter2"),
        _Change(op="add", about="card", text="Card number is 4111 1111 1111 1111"),
        _Change(op="add", about="answer style", text="prefers short answers"),
    ])
    kept = await FactLearner(repos, provider).learn("We no longer have the dog. My wifi password is hunter2. I prefer short answers.")

    assert kept == [f"ended: {dog.content}"]
    assert [f.content for f in repos.memory_facts.list_all()] == [known.content]
    assert not repos.memory_facts.get(dog.id).is_current


@pytest.mark.asyncio
async def test_a_message_that_is_not_about_the_person_does_not_reach_the_model(tmp_path) -> None:
    repos, _audit = make_repos(tmp_path)
    provider = _Provider([_Change(op="add", text="Should never be written")])
    assert await FactLearner(repos, provider).learn("Find my resume.") == []
    assert provider.asked == [] and repos.memory_facts.list_all() == []


@pytest.mark.asyncio
async def test_the_service_learns_once_per_message_after_the_reply_and_only_when_a_learner_is_set(tmp_path) -> None:
    repos, _audit = make_repos(tmp_path)
    conversation = repos.conversations.get_or_create(ChannelType.TELEGRAM, "42")
    provider = _Provider([_Change(op="add", about="pool", text="Plans to build a pool next spring")])
    service = ConversationMemoryService(repos, provider=provider, learner=FactLearner(repos, provider))
    await service.update_from_user_message(conversation, "I am planning to build a pool in the backyard next spring", summarize=False)

    await service.refresh_summary(conversation)
    await service.refresh_summary(conversation)  # the same message is not asked twice

    assert [f.content for f in repos.memory_facts.list_all()] == ["Plans to build a pool next spring"]
    assert sum(1 for system, _ in provider.asked if "keep a list of facts" in system) == 1
    off = ConversationMemoryService(repos, provider=provider)
    await off.update_from_user_message(conversation, "We moved to Oak Street last week and love it", summarize=False)
    await off.refresh_summary(conversation)
    assert len(repos.memory_facts.list_all()) == 1  # no learner (memory.auto_remember off): nothing new is kept


def test_what_the_owner_says_is_searchable_from_any_chat_and_answers_are_kept_once_per_task(tmp_path) -> None:
    repos, _audit = make_repos(tmp_path)
    one = repos.conversations.get_or_create(ChannelType.TELEGRAM, "42")
    two = repos.conversations.get_or_create(ChannelType.TELEGRAM, "99")
    repos.messages.create(InboundMessage(channel=ChannelType.TELEGRAM, sender_id="7", chat_id="42", kind=MessageKind.TEXT, text="Little Sprouts in Pearland quoted 1200 a month for the daycare"), one)
    repos.messages.create(InboundMessage(channel=ChannelType.TELEGRAM, sender_id="7", chat_id="99", kind=MessageKind.TEXT, text="Order the cheapest USB-C cable"), two)
    assert repos.episodes.add("answer", "I found three daycares in Pearland.", conversation_id=one, task_id="task_1")
    assert not repos.episodes.add("answer", "A second answer for the same task", conversation_id=one, task_id="task_1")

    found = repos.episodes.search("daycare Pearland")
    assert {item["kind"] for item in found} == {"said", "answer"} and "Little Sprouts" in " ".join(item["text"] for item in found)
    assert repos.episodes.search("usb cable")[0]["conversation_id"] == two  # another chat is found as well
    assert repos.episodes.search("zebra") == [] and repos.episodes.search("   ") == []


@pytest.mark.asyncio
async def test_memory_search_shows_facts_with_when_they_stopped_and_what_was_said(tmp_path) -> None:
    repos, _audit = make_repos(tmp_path)
    repos.memory_facts.create(MemoryFact(category="life", content="Lives at 12 Elm Road, Pearland", about="home address"))
    old = repos.memory_facts.create(MemoryFact(category="life", content="Lived at 3 Pine Road, Houston", about="home address"))
    repos.memory_facts.close(old.id)
    repos.episodes.add("said", "We are moving from Pine Road to Elm Road in March")
    result = await MemoryManageAdapter(repos).execute(ToolCallRequest(
        task_id="t", tool_name="memory.manage", capability=Capability.MEMORY_MANAGE, input={"operation": "search", "query": "Pine Road"},
    ))
    text = result.output["summary"]
    assert "Lived at 3 Pine Road" in text and "(until " in text and "We are moving from Pine Road" in text
    assert "12 Elm Road" not in text or "known" in text  # a current fact appears only if it matches the words


def test_a_database_made_before_episodes_gets_what_was_already_said_as_episodes(tmp_path) -> None:
    from agent_control.storage import Database, Repositories
    from agent_control.storage.migrations import backfill_episodes

    database = Database("sqlite:///" + str(tmp_path / "old.db").replace("\\", "/"))
    database.initialize()
    repos = Repositories.for_database(database)
    repos.messages.create(InboundMessage(channel=ChannelType.TELEGRAM, sender_id="7", chat_id="42", kind=MessageKind.TEXT, text="I live in Pearland"), None)
    with database.connect() as connection:
        connection.execute("DELETE FROM episodes")
        backfill_episodes(connection)
    assert repos.episodes.search("Pearland")[0]["kind"] == "said"
