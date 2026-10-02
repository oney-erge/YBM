from __future__ import annotations

import base64

import httpx
import pytest
from pydantic import BaseModel

from agent_control.config import LLMProfileConfig
from agent_control.llm.providers import OpenAICompatibleProvider


@pytest.mark.asyncio
async def test_openai_compatible_provider_sends_multimodal_image_payload(monkeypatch, tmp_path) -> None:
    image = tmp_path / "screen.png"
    image.write_bytes(b"fake-image")
    profile = LLMProfileConfig(
        model="local-vision",
        base_url="http://127.0.0.1:8000/v1",
        api_key=None,
        max_tokens=128,
    )
    provider = OpenAICompatibleProvider(profile)
    captured: dict = {}

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"choices": [{"message": {"content": "observed"}}]}

    class FakeAsyncClient:
        def __init__(self, timeout: int) -> None:
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def post(self, url: str, *, headers: dict, json: dict):
            captured["url"] = url
            captured["headers"] = headers
            captured["payload"] = json
            return FakeResponse()

    monkeypatch.setattr("agent_control.llm.providers.httpx.AsyncClient", FakeAsyncClient)

    result = await provider.generate_multimodal_text("system", "describe", [str(image)])

    payload = captured["payload"]
    user_content = payload["messages"][1]["content"]

    assert result == "observed"
    assert captured["url"] == "http://127.0.0.1:8000/v1/chat/completions"
    assert user_content[0] == {"type": "text", "text": "describe"}
    assert user_content[1]["type"] == "image_url"
    assert user_content[1]["image_url"]["url"] == "data:image/png;base64," + base64.b64encode(b"fake-image").decode("ascii")


@pytest.mark.asyncio
async def test_openai_compatible_provider_allows_localdeploy_clamping(monkeypatch) -> None:
    profile = LLMProfileConfig(
        model="gemma3_4b_ollama_safe",
        base_url="http://127.0.0.1:8000/v1",
        max_tokens=4096,
    )
    provider = OpenAICompatibleProvider(profile)
    captured: dict = {}

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"choices": [{"message": {"content": "ok"}}]}

    class FakeAsyncClient:
        def __init__(self, timeout: int) -> None:
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def post(self, url: str, *, headers: dict, json: dict):
            captured["payload"] = json
            return FakeResponse()

    monkeypatch.setattr("agent_control.llm.providers.httpx.AsyncClient", FakeAsyncClient)

    result = await provider.generate_text("system", "user")

    assert result == "ok"
    assert captured["payload"]["max_tokens"] == 4096
    assert captured["payload"]["allow_clamp"] is True


class TinyStructuredOutput(BaseModel):
    route: str


@pytest.mark.asyncio
async def test_openai_compatible_provider_retries_structured_without_response_format_on_400(monkeypatch) -> None:
    profile = LLMProfileConfig(
        model="gemma3_12b_ollama_safe",
        base_url="http://127.0.0.1:8000/v1",
        max_tokens=128,
    )
    provider = OpenAICompatibleProvider(profile)
    payloads: list[dict] = []

    class FakeResponse:
        def __init__(self, status_code: int, content: str) -> None:
            self.status_code = status_code
            self.content = content
            self.url = "http://127.0.0.1:8000/v1/chat/completions"
            self.request = httpx.Request("POST", self.url)

        def raise_for_status(self) -> None:
            if self.status_code >= 400:
                response = httpx.Response(
                    self.status_code,
                    json={"error": {"message": "unsupported response_format json_schema"}},
                    request=self.request,
                )
                raise httpx.HTTPStatusError("bad request", request=self.request, response=response)

        def json(self) -> dict:
            return {"choices": [{"message": {"content": self.content}}]}

    class FakeAsyncClient:
        def __init__(self, timeout: int) -> None:
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def post(self, url: str, *, headers: dict, json: dict):
            payloads.append(json)
            if len(payloads) == 1:
                return FakeResponse(400, "")
            return FakeResponse(200, '{"route":"desktop.observe"}')

    monkeypatch.setattr("agent_control.llm.providers.httpx.AsyncClient", FakeAsyncClient)

    result = await provider.generate_structured("system", "user", TinyStructuredOutput)

    assert result.route == "desktop.observe"
    assert "response_format" in payloads[0]
    assert "response_format" not in payloads[1]


def _fake_client_returning(payload: dict):
    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return payload

    class FakeAsyncClient:
        def __init__(self, timeout: int) -> None:
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def post(self, url: str, *, headers: dict, json: dict):
            return FakeResponse()

    return FakeAsyncClient


@pytest.mark.asyncio
async def test_openai_compatible_provider_captures_usage_from_response(monkeypatch) -> None:
    """docs/archive/HISTORY.md Part 4 T1.4: every OpenAI-compatible response reports
    a `usage` object and it used to be silently discarded - there was no way
    to see what a task actually cost. `last_usage` must reflect it after a
    successful call."""
    profile = LLMProfileConfig(model="gpt-4.1", base_url="https://api.openai.com/v1", max_tokens=128)
    provider = OpenAICompatibleProvider(profile)
    monkeypatch.setattr(
        "agent_control.llm.providers.httpx.AsyncClient",
        _fake_client_returning(
            {
                "choices": [{"message": {"content": "hi"}}],
                "usage": {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150},
            }
        ),
    )

    assert provider.last_usage is None
    await provider.generate_text("system", "user")

    assert provider.last_usage == {
        "prompt_tokens": 120,
        "completion_tokens": 30,
        "total_tokens": 150,
        "model": "gpt-4.1",
    }


@pytest.mark.asyncio
async def test_openai_compatible_provider_usage_falls_back_to_ollama_style_keys(monkeypatch) -> None:
    """Some Ollama-backed proxies (including LocalDeploy) report
    prompt_eval_count/eval_count instead of the OpenAI-standard field names."""
    profile = LLMProfileConfig(model="qwen3vl_8b_ollama", base_url="http://127.0.0.1:8000/v1", max_tokens=128)
    provider = OpenAICompatibleProvider(profile)
    monkeypatch.setattr(
        "agent_control.llm.providers.httpx.AsyncClient",
        _fake_client_returning(
            {
                "choices": [{"message": {"content": "hi"}}],
                "usage": {"prompt_eval_count": 80, "eval_count": 20},
            }
        ),
    )

    await provider.generate_text("system", "user")

    assert provider.last_usage == {
        "prompt_tokens": 80,
        "completion_tokens": 20,
        "total_tokens": 100,
        "model": "qwen3vl_8b_ollama",
    }


@pytest.mark.asyncio
async def test_openai_compatible_provider_usage_is_none_when_server_omits_it(monkeypatch) -> None:
    """Missing usage must stay None, never a fabricated zero - a caller that
    treats None as "unknown" and 0 as "confirmed free" would otherwise be
    silently lied to."""
    profile = LLMProfileConfig(model="local", base_url="http://127.0.0.1:8000/v1", max_tokens=128)
    provider = OpenAICompatibleProvider(profile)
    monkeypatch.setattr(
        "agent_control.llm.providers.httpx.AsyncClient",
        _fake_client_returning({"choices": [{"message": {"content": "hi"}}]}),
    )

    await provider.generate_text("system", "user")

    assert provider.last_usage is None


@pytest.mark.asyncio
async def test_openai_compatible_provider_captures_request_response_and_latency(monkeypatch) -> None:
    """docs/archive/UI_UX_AUDIT.md Phase 14d: LLM-call persistence needs the actual
    messages sent, the response text, the model, and real timing - last_usage
    alone (token counts only) isn't enough. These are siblings to last_usage,
    same "set on success" contract."""
    profile = LLMProfileConfig(model="gpt-4.1", base_url="https://api.openai.com/v1", max_tokens=128)
    provider = OpenAICompatibleProvider(profile)
    monkeypatch.setattr(
        "agent_control.llm.providers.httpx.AsyncClient",
        _fake_client_returning({"choices": [{"message": {"content": "hi there"}}]}),
    )

    assert provider.last_request is None
    assert provider.last_started_at is None
    result = await provider.generate_text("system prompt", "user prompt")

    assert result == "hi there"
    assert provider.last_request == [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "user prompt"},
    ]
    assert provider.last_response_text == "hi there"
    assert provider.last_model == "gpt-4.1"
    assert provider.last_started_at is not None
    assert provider.last_latency_ms is not None
    assert provider.last_latency_ms >= 0


@pytest.mark.asyncio
async def test_failover_provider_proxies_usage_from_whichever_provider_served_the_call() -> None:
    class FakeProvider:
        def __init__(self, text: str, usage: dict, *, fails: bool = False) -> None:
            self.text = text
            self.last_usage = usage
            self.fails = fails

        async def generate_text(self, system_prompt: str, user_prompt: str) -> str:
            if self.fails:
                raise httpx.TimeoutException("primary down")
            return self.text

        async def generate_multimodal_text(self, *a, **k):  # pragma: no cover - unused here
            raise NotImplementedError

        async def generate_structured(self, *a, **k):  # pragma: no cover - unused here
            raise NotImplementedError

    from agent_control.llm.providers import FailoverLLMProvider

    primary = FakeProvider("from primary", {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "model": "big"})
    fallback = FakeProvider("from fallback", {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2, "model": "small"})
    provider = FailoverLLMProvider(primary, fallback)

    result = await provider.generate_text("s", "u")
    assert result == "from primary"
    assert provider.last_usage == primary.last_usage

    primary.fails = True
    result = await provider.generate_text("s", "u")
    assert result == "from fallback"
    assert provider.last_usage == fallback.last_usage


@pytest.mark.asyncio
async def test_failover_provider_proxies_llm_call_persistence_fields_too() -> None:
    """docs/archive/UI_UX_AUDIT.md Phase 14d: the failover wrapper must proxy the new
    last_request/last_response_text/last_model/last_started_at/last_latency_ms
    fields the same way it already proxies last_usage, or a call served by
    the fallback profile would silently have nothing to persist."""

    class FakeProvider:
        def __init__(self, tag: str) -> None:
            self.last_usage = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2, "model": tag}
            self.last_request = [{"role": "user", "content": tag}]
            self.last_response_text = f"response from {tag}"
            self.last_model = tag
            self.last_started_at = "2026-01-01T00:00:00+00:00"
            self.last_latency_ms = 42.0

        async def generate_text(self, system_prompt: str, user_prompt: str) -> str:
            return self.last_response_text

        async def generate_multimodal_text(self, *a, **k):  # pragma: no cover - unused here
            raise NotImplementedError

        async def generate_structured(self, *a, **k):  # pragma: no cover - unused here
            raise NotImplementedError

    from agent_control.llm.providers import FailoverLLMProvider

    fallback = FakeProvider("fallback")
    provider = FailoverLLMProvider(FakeProvider("primary"), fallback)

    await provider.generate_text("s", "u")

    assert provider.last_request == [{"role": "user", "content": "primary"}]
    assert provider.last_response_text == "response from primary"
    assert provider.last_model == "primary"
    assert provider.last_started_at == "2026-01-01T00:00:00+00:00"
    assert provider.last_latency_ms == 42.0
