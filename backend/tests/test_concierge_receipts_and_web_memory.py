"""The web console remembers things and accounts for the Concierge's cost.

Two claims the product made that were only true on some channels:

* "Remember that ..." and standing rules are stored, not just acknowledged. Telegram
  and WhatsApp did; web chat, the default front door, said "understood" and kept
  nothing (docs/archive/E2E_FINDINGS.md P1-3, fixed there for the other channels only).
* Receipts show model cost. The Concierge's call - often the largest of a task, and
  the only call of a chat-only reply - was left off because it "runs before a task
  exists", so receipts understated cost.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import agent_control.admin as admin_module
from agent_control.admin import create_admin_router
from agent_control.channels.base import classify_and_spawn_task
from agent_control.config import AppSettings
from agent_control.llm.call_log import cap_messages, merge_token_usage, record_concierge_call
from agent_control.schemas import (
    AuditEventType,
    ChannelType,
    InboundMessage,
    MemorySource,
    MessageClassification,
    MessageKind,
    TaskStatus,
    TaskType,
)
from agent_control.storage import Database, Repositories
from agent_control.tools.vscode_bridge import VSCodeBridgeStore
from helpers import make_repos


def _provider(*, prompt: int = 120, completion: int = 30, key: str | None = None) -> SimpleNamespace:
    """A model client that has just made one call, as providers record it."""
    user_text = "organize my downloads" + (f" api_key={key}" if key else "")
    return SimpleNamespace(
        last_request=[{"role": "system", "content": "You are the concierge."}, {"role": "user", "content": user_text}],
        last_response_text='{"is_task": true}',
        last_usage={"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion, "model": "gpt-4.1"},
        last_model="gpt-4.1",
        last_started_at=datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc),
        last_latency_ms=812.5,
    )


# ---- the token tally (moved out of the worker so every caller shares it) -----------------


def test_a_first_call_starts_the_tally() -> None:
    merged = merge_token_usage(None, "operator", {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "model": "m"})

    assert merged == {
        "calls": 1, "prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15,
        "by_source": {"operator": {"calls": 1, "prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}},
        "last_model": "m",
    }


def test_later_calls_add_to_the_total_and_to_their_own_source() -> None:
    first = merge_token_usage(None, "concierge", {"prompt_tokens": 100, "total_tokens": 100})
    second = merge_token_usage(first, "operator", {"prompt_tokens": 40, "total_tokens": 40})

    assert second["calls"] == 2 and second["total_tokens"] == 140
    assert second["by_source"]["concierge"]["total_tokens"] == 100
    assert second["by_source"]["operator"]["total_tokens"] == 40


def test_no_reported_usage_is_a_no_op_not_a_fabricated_zero() -> None:
    assert merge_token_usage({"calls": 3}, "operator", None) is None
    assert merge_token_usage({"calls": 3}, "operator", {}) is None


def test_a_fallback_flag_is_sticky() -> None:
    flagged = merge_token_usage(None, "operator", {"total_tokens": 1}, fallback_used=True)
    later = merge_token_usage(flagged, "operator", {"total_tokens": 1})

    assert later["fallback_used"] is True


def test_merging_never_mutates_what_it_was_given() -> None:
    original = {"calls": 1, "by_source": {"operator": {"calls": 1}}}

    merge_token_usage(original, "operator", {"total_tokens": 5})

    assert original == {"calls": 1, "by_source": {"operator": {"calls": 1}}}


def test_message_capping_truncates_text_and_replaces_images() -> None:
    capped = cap_messages(
        [
            {"role": "user", "content": "x" * 50},
            {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]},
        ],
        10,
    )

    assert capped[0]["content"] == "x" * 10 + "...[truncated]"
    assert capped[1]["content"][0]["image_url"]["url"] == "[image omitted from trace]"


# ---- recording the Concierge's call --------------------------------------------------------


def test_the_concierge_call_lands_on_the_task_with_its_tokens(tmp_path) -> None:
    repos, _ = make_repos(tmp_path)
    task = repos.tasks.create("organize my downloads")

    record_concierge_call(repos, task.id, _provider())

    calls = repos.llm_calls.list_for_task(task.id)
    assert [c["source"] for c in calls] == ["concierge"]
    assert calls[0]["model"] == "gpt-4.1"
    assert calls[0]["total_tokens"] == 150
    usage = repos.tasks.get(task.id).metadata["token_usage"]
    assert usage["by_source"]["concierge"] == {"calls": 1, "prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150}
    assert usage["total_tokens"] == 150


def test_the_concierge_adds_to_whatever_the_task_already_cost(tmp_path) -> None:
    repos, _ = make_repos(tmp_path)
    task = repos.tasks.create("t", metadata={"token_usage": {"calls": 1, "total_tokens": 500, "by_source": {"operator": {"calls": 1, "total_tokens": 500}}}})

    record_concierge_call(repos, task.id, _provider())

    usage = repos.tasks.get(task.id).metadata["token_usage"]
    assert usage["total_tokens"] == 650 and usage["calls"] == 2
    assert usage["by_source"]["operator"]["total_tokens"] == 500


def test_secrets_in_the_prompt_are_redacted_before_they_are_stored(tmp_path) -> None:
    repos, _ = make_repos(tmp_path)
    task = repos.tasks.create("t")

    record_concierge_call(repos, task.id, _provider(key="sk-live-DO-NOT-STORE-12345"))

    stored = str(repos.llm_calls.list_for_task(task.id))
    assert "sk-live-DO-NOT-STORE-12345" not in stored


def test_turning_call_persistence_off_still_counts_the_tokens(tmp_path) -> None:
    repos, _ = make_repos(tmp_path)
    task = repos.tasks.create("t")

    record_concierge_call(repos, task.id, _provider(), persist=False)

    assert repos.llm_calls.list_for_task(task.id) == []
    assert repos.tasks.get(task.id).metadata["token_usage"]["total_tokens"] == 150


@pytest.mark.parametrize("provider", [None, SimpleNamespace(), SimpleNamespace(last_request=[], last_started_at=None)])
def test_a_classifier_with_nothing_to_report_records_nothing(tmp_path, provider) -> None:
    repos, _ = make_repos(tmp_path)
    task = repos.tasks.create("t")

    record_concierge_call(repos, task.id, provider)

    assert repos.llm_calls.list_for_task(task.id) == []
    assert "token_usage" not in repos.tasks.get(task.id).metadata


def test_a_receipt_that_fails_to_save_never_breaks_the_message(tmp_path) -> None:
    repos, _ = make_repos(tmp_path)

    # An unknown task id: the tally has nothing to update. It must not raise.
    record_concierge_call(repos, "task_that_does_not_exist", _provider())


# ---- the shared channel path (Telegram, WhatsApp) records it too ---------------------------------


class _Classifier:
    def __init__(self, classification: MessageClassification, provider) -> None:
        self.classification = classification
        self.provider = provider

    async def classify(self, message: InboundMessage, context: str | None = None) -> MessageClassification:
        return self.classification


async def _no_progress(chat_id: str, text: str) -> None:
    return None


@pytest.mark.asyncio
async def test_a_task_spawned_from_telegram_or_whatsapp_carries_the_concierge_cost(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    conversation_id = repos.conversations.get_or_create(ChannelType.TELEGRAM, "chat-1")
    classifier = _Classifier(
        MessageClassification(is_task=True, task_type=TaskType.DEVELOPMENT, normalized_objective="build me a script", reason="work"),
        _provider(),
    )
    inbound = InboundMessage(channel=ChannelType.TELEGRAM, kind=MessageKind.TEXT, sender_id="u", chat_id="chat-1", text="build me a script")

    result = await classify_and_spawn_task(
        inbound, conversation_id, repositories=repos, audit=audit, classifier=classifier,
        settings=AppSettings(_env_file=None), send_progress=_no_progress,
    )

    assert result.task is not None
    assert [c["source"] for c in repos.llm_calls.list_for_task(result.task.id)] == ["concierge"]
    assert repos.tasks.get(result.task.id).metadata["token_usage"]["by_source"]["concierge"]["total_tokens"] == 150


@pytest.mark.asyncio
async def test_a_classifier_without_a_model_client_still_spawns_the_task(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    conversation_id = repos.conversations.get_or_create(ChannelType.DISCORD, "chat-1")
    classifier = _Classifier(
        MessageClassification(is_task=True, task_type=TaskType.DEVELOPMENT, normalized_objective="x", reason="work"), None
    )
    inbound = InboundMessage(channel=ChannelType.DISCORD, kind=MessageKind.TEXT, sender_id="u", chat_id="chat-1", text="x")

    result = await classify_and_spawn_task(
        inbound, conversation_id, repositories=repos, audit=audit, classifier=classifier, send_progress=_no_progress,
    )

    assert result.task is not None
    assert repos.llm_calls.list_for_task(result.task.id) == []


# ---- web chat -----------------------------------------------------------------------------------------


def _web(monkeypatch, tmp_path, concierge=None):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AGENT_ADMIN_TOKEN", raising=False)
    database = Database(f"sqlite:///{tmp_path / 'admin.db'}")
    database.initialize()
    repositories = Repositories.for_database(database)
    calls: list[str] = []

    async def fake_concierge(settings, objective):
        calls.append(objective)
        return concierge if concierge is not None else (None, None)

    monkeypatch.setattr(admin_module, "_web_concierge", fake_concierge)
    app = FastAPI()
    app.include_router(create_admin_router(lambda: AppSettings(_env_file=None), lambda: repositories, VSCodeBridgeStore()))
    return TestClient(app), repositories, calls


def _send(client: TestClient, text: str) -> dict:
    response = client.post("/admin/api/chat/messages", json={"text": text, "attachment_ids": []})
    assert response.status_code == 200, response.text
    return response.json()["task"]


def test_remember_that_is_stored_and_acknowledged_without_asking_any_model(monkeypatch, tmp_path) -> None:
    client, repositories, calls = _web(monkeypatch, tmp_path)

    task = _send(client, "Remember that my accountant is Dana Reyes")

    assert task["status"] == TaskStatus.COMPLETED.value
    assert task["metadata"]["synthesized_answer"] == "Got it, I'll remember: my accountant is Dana Reyes"
    facts = repositories.memory_facts.list_all()
    assert [(f.category, f.content, f.source) for f in facts] == [
        ("user_note", "my accountant is Dana Reyes", MemorySource.USER_STATED)
    ]
    assert calls == []  # decided at the runtime level, before any model sees it
    assert any(e.type == AuditEventType.MEMORY_UPDATED for e in repositories.audit.list_recent(20))


def test_a_reminder_shaped_request_is_still_a_task_not_a_memory(monkeypatch, tmp_path) -> None:
    client, repositories, calls = _web(monkeypatch, tmp_path)

    task = _send(client, "remember to call the plumber tomorrow")

    assert repositories.memory_facts.list_all() == []
    assert calls == ["remember to call the plumber tomorrow"]
    assert task["status"] != TaskStatus.COMPLETED.value  # it went to the worker


def test_a_standing_rule_stated_in_chat_is_stored_before_the_reply_says_it_was(monkeypatch, tmp_path) -> None:
    client, repositories, _ = _web(
        monkeypatch, tmp_path, concierge=("Understood, I'll keep answers short.", None)
    )

    task = _send(client, "From now on always answer with exactly three bullet points")

    facts = repositories.memory_facts.list_all()
    assert len(facts) == 1 and facts[0].source == MemorySource.USER_STATED
    assert "three bullet points" in facts[0].content
    assert "(Remembered: " in task["metadata"]["synthesized_answer"]


def test_ordinary_chat_is_not_mistaken_for_a_rule(monkeypatch, tmp_path) -> None:
    client, repositories, _ = _web(monkeypatch, tmp_path, concierge=("Paris.", None))

    task = _send(client, "What is the capital of France?")

    assert repositories.memory_facts.list_all() == []
    assert task["metadata"]["synthesized_answer"] == "Paris."


def test_a_chat_only_reply_shows_what_the_concierge_cost(monkeypatch, tmp_path) -> None:
    client, repositories, _ = _web(monkeypatch, tmp_path, concierge=("Paris.", _provider(prompt=300, completion=20)))

    task = _send(client, "What is the capital of France?")

    usage = repositories.tasks.get(task["id"]).metadata["token_usage"]
    assert usage["by_source"]["concierge"]["total_tokens"] == 320
    assert [c["source"] for c in repositories.llm_calls.list_for_task(task["id"])] == ["concierge"]


def test_a_task_started_from_web_chat_carries_the_concierge_cost_too(monkeypatch, tmp_path) -> None:
    client, repositories, _ = _web(monkeypatch, tmp_path, concierge=(None, _provider()))

    task = _send(client, "Organize my Downloads folder by file type")

    assert repositories.tasks.get(task["id"]).metadata["token_usage"]["by_source"]["concierge"]["total_tokens"] == 150


def test_with_no_model_at_all_web_chat_still_creates_the_task(monkeypatch, tmp_path) -> None:
    client, repositories, _ = _web(monkeypatch, tmp_path, concierge=(None, None))

    task = _send(client, "Organize my Downloads folder by file type")

    assert task["metadata"].get("token_usage") is None
    assert repositories.tasks.get(task["id"]) is not None
