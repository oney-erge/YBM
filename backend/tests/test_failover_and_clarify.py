from __future__ import annotations

import httpx
import pytest
from pydantic import BaseModel

from agent_control.channels.telegram import TelegramAdapter, TelegramIntakeService
from agent_control.config import AppSettings, LLMConfig, LLMProfileConfig, TelegramConfig
from agent_control.llm.providers import (
    ChainLLMProvider,
    FailoverLLMProvider,
    OpenAICompatibleProvider,
    _ChainEntry,
    build_default_llm_provider,
    build_role_llm_provider,
)
from agent_control.schemas import (
    ChannelType,
    TaskStatus,
)
from helpers import make_repos


# --- FailoverLLMProvider ---


class _Recorder:
    def __init__(self, response: str = "ok", error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.calls = 0

    async def generate_text(self, system_prompt: str, user_prompt: str) -> str:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.response

    async def generate_multimodal_text(self, system_prompt: str, user_prompt: str, image_paths: list[str]) -> str:
        return await self.generate_text(system_prompt, user_prompt)

    async def generate_structured(self, system_prompt, user_prompt, output_model, *, temperature=None):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return output_model()


class _EmptyModel(BaseModel):
    pass


@pytest.mark.asyncio
async def test_failover_uses_fallback_on_connection_error() -> None:
    primary = _Recorder(error=httpx.ConnectError("connection refused"))
    fallback = _Recorder(response="from fallback")
    provider = FailoverLLMProvider(primary, fallback)

    assert await provider.generate_text("s", "u") == "from fallback"
    assert primary.calls == 1 and fallback.calls == 1


@pytest.mark.asyncio
async def test_failover_uses_fallback_on_timeout_and_5xx() -> None:
    for error in (httpx.ReadTimeout("timed out"), ValueError("LLM request failed with HTTP 503 at x: overloaded")):
        primary = _Recorder(error=error)
        fallback = _Recorder(response="fallback")
        provider = FailoverLLMProvider(primary, fallback)
        assert await provider.generate_text("s", "u") == "fallback"


@pytest.mark.asyncio
async def test_failover_does_not_mask_request_bugs() -> None:
    primary = _Recorder(error=ValueError("LLM request failed with HTTP 400 at x: bad request"))
    fallback = _Recorder()
    provider = FailoverLLMProvider(primary, fallback)

    with pytest.raises(ValueError):
        await provider.generate_text("s", "u")
    assert fallback.calls == 0


def test_build_default_provider_wraps_fallback_profile() -> None:
    settings = AppSettings(
        _env_file=None,
        llm=LLMConfig(
            default_profile="local",
            fallback_profile="cloud",
            profiles={
                "local": LLMProfileConfig(model="local-model", base_url="http://127.0.0.1:8000/v1"),
                "cloud": LLMProfileConfig(model="cloud-model", base_url="https://api.example.com/v1"),
            },
        ),
    )
    provider = build_default_llm_provider(settings)
    assert isinstance(provider, FailoverLLMProvider)


def test_build_default_provider_without_fallback_stays_plain() -> None:
    settings = AppSettings(
        _env_file=None,
        llm=LLMConfig(
            default_profile="local",
            profiles={"local": LLMProfileConfig(model="local-model", base_url="http://127.0.0.1:8000/v1")},
        ),
    )
    provider = build_default_llm_provider(settings)
    assert not isinstance(provider, FailoverLLMProvider)


@pytest.mark.asyncio
async def test_failover_now_treats_429_401_403_as_unavailability() -> None:
    """docs/ROADMAP.md's per-role/fallback item: a 429 (this client should
    back off) or 401/403 (this profile's own credential is bad) says nothing
    about whether the *request* is wrong, unlike a 400 - all three are worth
    trying a different profile for.
    """
    for status in (429, 401, 403):
        primary = _Recorder(error=ValueError(f"LLM request failed with HTTP {status} at x: nope"))
        fallback = _Recorder(response="fallback")
        provider = FailoverLLMProvider(primary, fallback)
        assert await provider.generate_text("s", "u") == "fallback"

    # 400 still must not fail over - already covered by
    # test_failover_does_not_mask_request_bugs above, restated here as the
    # negative case for the same three-status change.
    primary = _Recorder(error=ValueError("LLM request failed with HTTP 400 at x: bad request"))
    fallback = _Recorder()
    provider = FailoverLLMProvider(primary, fallback)
    with pytest.raises(ValueError):
        await provider.generate_text("s", "u")
    assert fallback.calls == 0


# --- ChainLLMProvider (docs/ROADMAP.md: ordered fallback chain + cooldown) -

def _entry(name: str, response: str = "ok", error: Exception | None = None) -> _ChainEntry:
    return _ChainEntry(name, _Recorder(response=response, error=error))


@pytest.mark.asyncio
async def test_chain_provider_uses_the_first_healthy_entry() -> None:
    chain = ChainLLMProvider([_entry("primary", response="from primary")])

    assert await chain.generate_text("s", "u") == "from primary"
    assert chain.last_profile_name == "primary"
    assert chain.last_fallback_used is False


@pytest.mark.asyncio
async def test_chain_provider_falls_through_multiple_unavailable_entries() -> None:
    down1 = _entry("down1", error=httpx.ConnectError("refused"))
    down2 = _entry("down2", error=httpx.ReadTimeout("timed out"))
    healthy = _entry("healthy", response="from healthy")
    chain = ChainLLMProvider([down1, down2, healthy])

    result = await chain.generate_text("s", "u")

    assert result == "from healthy"
    assert chain.last_profile_name == "healthy"
    assert chain.last_fallback_used is True


@pytest.mark.asyncio
async def test_chain_provider_does_not_mask_a_request_bug() -> None:
    bad_request = _entry("primary", error=ValueError("LLM request failed with HTTP 400 at x: bad request"))
    fallback = _entry("fallback", response="unreachable")
    chain = ChainLLMProvider([bad_request, fallback])

    with pytest.raises(ValueError):
        await chain.generate_text("s", "u")
    assert fallback.provider.calls == 0


@pytest.mark.asyncio
async def test_chain_provider_raises_the_last_error_when_every_entry_fails() -> None:
    chain = ChainLLMProvider([
        _entry("a", error=httpx.ConnectError("a down")),
        _entry("b", error=httpx.ConnectError("b down")),
    ])

    with pytest.raises(httpx.ConnectError, match="b down"):
        await chain.generate_text("s", "u")


@pytest.mark.asyncio
async def test_chain_provider_skips_a_cooling_down_entry_on_the_next_call() -> None:
    """A profile that just failed should not eat its own timeout again on the
    very next call - it should be skipped straight to whatever comes after
    it until the cooldown window passes."""
    flaky = _entry("flaky", error=httpx.ConnectError("down"))
    healthy = _entry("healthy", response="from healthy")
    chain = ChainLLMProvider([flaky, healthy], cooldown_seconds=100.0)

    await chain.generate_text("s", "u")
    assert flaky.provider.calls == 1

    # Second call: flaky is cooling down, so it should not be attempted again.
    await chain.generate_text("s", "u")
    assert flaky.provider.calls == 1
    assert healthy.provider.calls == 2


@pytest.mark.asyncio
async def test_chain_provider_always_attempts_the_last_entry_even_cooling_down() -> None:
    """Every entry cooling down must still attempt something rather than
    raising with nothing tried - the last entry is the one exception to the
    cooldown skip."""
    only = _entry("only", error=httpx.ConnectError("down"))
    chain = ChainLLMProvider([only], cooldown_seconds=100.0)

    with pytest.raises(httpx.ConnectError):
        await chain.generate_text("s", "u")
    with pytest.raises(httpx.ConnectError):
        await chain.generate_text("s", "u")
    assert only.provider.calls == 2


def test_build_role_llm_provider_returns_none_without_an_override() -> None:
    settings = AppSettings(
        _env_file=None,
        llm=LLMConfig(
            default_profile="local",
            profiles={"local": LLMProfileConfig(model="local-model", base_url="http://127.0.0.1:8000/v1")},
        ),
    )

    assert build_role_llm_provider(settings, "operator") is None
    assert build_role_llm_provider(settings, "auditor") is None
    assert build_role_llm_provider(settings, "concierge") is None


def test_build_role_llm_provider_builds_the_configured_override() -> None:
    settings = AppSettings(
        _env_file=None,
        llm=LLMConfig(
            default_profile="local",
            operator_profile="strong",
            profiles={
                "local": LLMProfileConfig(model="local-model", base_url="http://127.0.0.1:8000/v1"),
                "strong": LLMProfileConfig(model="strong-model", base_url="https://api.example.com/v1"),
            },
        ),
    )

    provider = build_role_llm_provider(settings, "operator")

    assert provider is not None
    assert not isinstance(provider, ChainLLMProvider)  # no fallback configured for this role
    assert isinstance(provider, OpenAICompatibleProvider)
    assert provider.profile.model == "strong-model"


def test_build_role_llm_provider_chains_the_configured_fallback() -> None:
    settings = AppSettings(
        _env_file=None,
        llm=LLMConfig(
            default_profile="local",
            auditor_profile="strong",
            fallback_chain=["cheap", "local"],
            profiles={
                "local": LLMProfileConfig(model="local-model", base_url="http://127.0.0.1:8000/v1"),
                "strong": LLMProfileConfig(model="strong-model", base_url="https://api.example.com/v1"),
                "cheap": LLMProfileConfig(model="cheap-model", base_url="https://cheap.example.com/v1"),
            },
        ),
    )

    provider = build_role_llm_provider(settings, "auditor")

    assert isinstance(provider, ChainLLMProvider)
    assert [entry.name for entry in provider.entries] == ["strong", "cheap", "local"]


def test_build_default_provider_with_fallback_chain_orders_every_entry() -> None:
    settings = AppSettings(
        _env_file=None,
        llm=LLMConfig(
            default_profile="local",
            fallback_chain=["cheap", "cloud"],
            profiles={
                "local": LLMProfileConfig(model="local-model", base_url="http://127.0.0.1:8000/v1"),
                "cheap": LLMProfileConfig(model="cheap-model", base_url="https://cheap.example.com/v1"),
                "cloud": LLMProfileConfig(model="cloud-model", base_url="https://api.example.com/v1"),
            },
        ),
    )

    provider = build_default_llm_provider(settings)

    assert isinstance(provider, ChainLLMProvider)
    assert [entry.name for entry in provider.entries] == ["local", "cheap", "cloud"]


# --- CLARIFYING ask-user loop ---




@pytest.mark.asyncio
async def test_clarifying_reply_resumes_the_same_task(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    config = TelegramConfig(enabled=True, allowed_user_ids=[42], allowed_chat_ids=[100])
    adapter = TelegramAdapter(config, audit)
    service = TelegramIntakeService(adapter, repos, audit)

    task = repos.tasks.create(
        "Summarize the report",
        conversation_id=repos.conversations.get_or_create(ChannelType.TELEGRAM, "100"),
        metadata={
            "source_chat_id": "100",
            "clarifying_question": "Which report?",
            "clarify_count": 1,
            "operator_history": [{"tool_name": "knowledge.search", "status": "succeeded"}],
        },
    )
    repos.tasks.update_status(task.id, TaskStatus.CLARIFYING)

    result = await service.handle_update_async(
        {
            "message": {
                "message_id": 7,
                "from": {"id": 42},
                "chat": {"id": 100},
                "text": "The Q3 sales report on my desktop",
            }
        }
    )

    resumed = repos.tasks.get(task.id)
    assert resumed.status == TaskStatus.RECEIVED
    assert "Q3 sales report" in resumed.objective
    assert resumed.metadata["clarification_answer"] == "The Q3 sales report on my desktop"
    assert resumed.metadata["operator_history_offset_after_clarification"] == 1
    assert resumed.metadata["retry_count"] == 0
    assert result.outbound_message is not None
    assert "resuming" in result.outbound_message.text.lower()
    # No new task was spawned for the reply.
    assert len(repos.tasks.list_recent(10)) == 1


@pytest.mark.asyncio
async def test_clarifying_cancel_reply_cancels_task(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    config = TelegramConfig(enabled=True, allowed_user_ids=[42], allowed_chat_ids=[100])
    service = TelegramIntakeService(TelegramAdapter(config, audit), repos, audit)

    task = repos.tasks.create(
        "Summarize the report",
        conversation_id=repos.conversations.get_or_create(ChannelType.TELEGRAM, "100"),
        metadata={"source_chat_id": "100", "clarifying_question": "Which report?"},
    )
    repos.tasks.update_status(task.id, TaskStatus.CLARIFYING)

    result = await service.handle_update_async(
        {"message": {"message_id": 8, "from": {"id": 42}, "chat": {"id": 100}, "text": "cancel"}}
    )

    assert repos.tasks.get(task.id).status == TaskStatus.CANCELLED
    assert "cancelled" in result.outbound_message.text.lower()


# --- Telegram deterministic fast lane for coding sessions ---


@pytest.mark.asyncio
async def test_codex_status_question_is_answered_from_session_files(tmp_path) -> None:
    import json

    session_root = tmp_path / "sessions"
    session_root.mkdir()
    log_path = session_root / "codex_abc.log"
    log_path.write_text("applying patch to app.py", encoding="utf-8")
    (session_root / "codex_abc.json").write_text(
        json.dumps(
            {
                "session_id": "codex_abc",
                "provider": "codex",
                "status": "running",
                "pid": 1234,
                "workspace_dir": str(tmp_path),
                "log_path": str(log_path),
                "started_at": "2026-07-05T00:00:00+00:00",
                "files_before": {},
            }
        ),
        encoding="utf-8",
    )

    repos, audit = make_repos(tmp_path)
    config = TelegramConfig(enabled=True, allowed_user_ids=[42], allowed_chat_ids=[100])
    settings = AppSettings(
        _env_file=None,
        adapters={"coding_agent": {"enabled": True, "session_root": str(session_root)}},
    )
    service = TelegramIntakeService(TelegramAdapter(config, audit), repos, audit, settings=settings)

    result = await service.handle_update_async(
        {"message": {"message_id": 9, "from": {"id": 42}, "chat": {"id": 100}, "text": "what is codex doing"}}
    )

    # Answered instantly from session files: no classifier, no task, no LLM.
    assert result.task is None
    text = result.outbound_message.text
    assert "codex" in text and "running" in text
    assert "applying patch to app.py" in text


@pytest.mark.asyncio
async def test_provider_mention_without_status_intent_falls_through(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    config = TelegramConfig(enabled=True, allowed_user_ids=[42], allowed_chat_ids=[100])
    settings = AppSettings(
        _env_file=None,
        adapters={"coding_agent": {"enabled": True, "session_root": str(tmp_path / "none")}},
    )
    service = TelegramIntakeService(TelegramAdapter(config, audit), repos, audit, settings=settings)

    result = await service.handle_update_async(
        {"message": {"message_id": 10, "from": {"id": 42}, "chat": {"id": 100}, "text": "use codex to fix my repo tests"}}
    )

    # No fast-lane hijack: the request needs real classification (none is
    # configured here, so intake reports the spawn failure).
    assert result.outbound_message is not None
    assert "could not start" in result.outbound_message.text.lower()
