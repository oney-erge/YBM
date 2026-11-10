"""Native Anthropic provider.

Anthropic's API is not OpenAI-compatible, and Anthropic's own guidance is to
use the official SDK rather than a compatibility shim, so Claude does not go
through OpenAICompatibleProvider. Three differences would otherwise bite
immediately:

- `temperature` is rejected with a 400 on current Claude models (Opus 5,
  Sonnet 5, Opus 4.7/4.8, Fable 5). Every YBM profile carries a temperature and
  the OpenAI path forwards it unconditionally, so a base_url swap would fail
  every single request - and look like an auth problem while doing it.
- Thinking is configured as `{"type": "adaptive"}`; the older
  `budget_tokens` form is rejected on the same models.
- The system prompt is a top-level argument, not a message with role "system".

Structured output uses a forced single-tool call, which is Anthropic's
equivalent of `response_format=json_schema`: the tool's input schema is the
model we want back, and `tool_choice` makes emitting it the only option.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from agent_control.config import LLMProfileConfig
from agent_control.config_sync import read_env_value

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

DEFAULT_BASE_URL = "https://api.anthropic.com"

#: Models that reject `temperature`/`top_p`/`top_k` outright. Matched as
#: prefixes because Anthropic pins undated aliases (`claude-opus-5`) alongside
#: dated snapshots. Anything not listed keeps the profile's temperature, so an
#: older model still behaves as configured.
_NO_SAMPLING_PARAMS = (
    "claude-opus-5",
    "claude-opus-4-7",
    "claude-opus-4-8",
    "claude-sonnet-5",
    "claude-fable-5",
    "claude-mythos-5",
)


def rejects_temperature(model: str) -> bool:
    """Whether sending `temperature` to this model is a 400."""
    return any(model.startswith(prefix) for prefix in _NO_SAMPLING_PARAMS)


class AnthropicProvider:
    def __init__(self, profile: LLMProfileConfig) -> None:
        self.profile = profile
        # Same "None until a call completes, never a fabricated zero" contract
        # as OpenAICompatibleProvider - these feed LLM-call persistence.
        self.last_usage: dict | None = None
        self.last_request: list[dict] | None = None
        self.last_response_text: str | None = None
        self.last_model: str | None = None
        self.last_started_at: datetime | None = None
        self.last_latency_ms: float | None = None

    # -- public API ------------------------------------------------------

    async def generate_text(self, system_prompt: str, user_prompt: str) -> str:
        message = await self._create(system_prompt, [{"type": "text", "text": user_prompt}])
        return self._first_text(message)

    async def generate_multimodal_text(
        self, system_prompt: str, user_prompt: str, image_paths: list[str]
    ) -> str:
        content: list[dict[str, Any]] = []
        for image_path in image_paths:
            content.append(_image_block(image_path))
        content.append({"type": "text", "text": user_prompt})
        message = await self._create(system_prompt, content)
        return self._first_text(message)

    async def generate_structured(
        self,
        system_prompt: str,
        user_prompt: str,
        output_model: type[T],
        *,
        temperature: float | None = None,
        response_schema: dict | None = None,
    ) -> T:
        # response_schema is for local servers that enforce a schema while writing; this API uses output_model's own.
        schema = output_model.model_json_schema()
        tool_name = _tool_name(output_model.__name__)
        message = await self._create(
            system_prompt,
            [{"type": "text", "text": user_prompt}],
            temperature=temperature,
            tools=[
                {
                    "name": tool_name,
                    "description": f"Return the result as {output_model.__name__}.",
                    "input_schema": schema,
                }
            ],
            # Forcing the tool is Anthropic's equivalent of a json_schema
            # response format: emitting this shape becomes the only option.
            tool_choice={"type": "tool", "name": tool_name},
        )
        for block in message.content:
            if getattr(block, "type", None) == "tool_use":
                try:
                    return output_model.model_validate(block.input)
                except ValidationError as exc:
                    raise ValueError(f"LLM structured output failed validation: {exc}") from exc
        raise ValueError("Anthropic returned no tool_use block for a forced structured call")

    # -- internals -------------------------------------------------------

    async def _create(
        self,
        system_prompt: str,
        content: list[dict[str, Any]],
        *,
        temperature: float | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: dict[str, Any] | None = None,
    ):
        import anthropic

        api_key = self._api_key()
        if not api_key:
            raise ValueError("Anthropic API key is not configured")

        client = anthropic.AsyncAnthropic(
            api_key=api_key,
            base_url=self.profile.base_url or DEFAULT_BASE_URL,
            timeout=float(self.profile.timeout_seconds),
        )
        kwargs: dict[str, Any] = {
            "model": self.profile.model,
            "max_tokens": self.profile.max_tokens,
            # System prompts are a top-level argument on Anthropic, not a
            # message with role "system".
            "system": system_prompt,
            "messages": [{"role": "user", "content": content}],
        }
        # The whole reason this provider exists: sending temperature to a
        # current Claude model is a 400.
        if not rejects_temperature(self.profile.model):
            effective = temperature if temperature is not None else self.profile.temperature
            if effective is not None:
                kwargs["temperature"] = effective
        if tools:
            kwargs["tools"] = tools
        if tool_choice:
            kwargs["tool_choice"] = tool_choice

        started_at = datetime.now(timezone.utc)
        start = time.monotonic()
        try:
            message = await client.messages.create(**kwargs)
        except Exception as exc:  # noqa: BLE001 - normalized below
            raise ValueError(_safe_error(exc)) from None
        finally:
            await client.close()

        # A refusal is a successful HTTP 200 with an empty or partial body -
        # reading content[0] blind would raise something unrecognizable.
        if getattr(message, "stop_reason", None) == "refusal":
            raise ValueError("Anthropic declined this request (stop_reason: refusal)")

        self.last_usage = _usage(message, model=self.profile.model)
        self.last_request = [{"role": "system", "content": system_prompt}, *kwargs["messages"]]
        self.last_model = self.profile.model
        self.last_started_at = started_at
        self.last_latency_ms = (time.monotonic() - start) * 1000
        try:
            self.last_response_text = self._first_text(message)
        except ValueError:
            self.last_response_text = None
        return message

    @staticmethod
    def _first_text(message) -> str:
        for block in message.content:
            if getattr(block, "type", None) == "text":
                return str(block.text)
        raise ValueError("Anthropic response contained no text block")

    def _api_key(self) -> str | None:
        if self.profile.api_key:
            return self.profile.api_key.get_secret_value()
        if self.profile.api_key_env:
            return read_env_value(self.profile.api_key_env)
        return None


def _tool_name(model_name: str) -> str:
    """Anthropic tool names allow [a-zA-Z0-9_-] only."""
    cleaned = "".join(ch if ch.isalnum() or ch in "_-" else "_" for ch in model_name)
    return cleaned or "structured_output"


def _usage(message, *, model: str) -> dict | None:
    usage = getattr(message, "usage", None)
    if usage is None:
        return None
    prompt = getattr(usage, "input_tokens", None)
    completion = getattr(usage, "output_tokens", None)
    if prompt is None and completion is None:
        return None
    return {
        "prompt_tokens": int(prompt or 0),
        "completion_tokens": int(completion or 0),
        "total_tokens": int(prompt or 0) + int(completion or 0),
        "model": model,
    }


def _safe_error(exc: Exception) -> str:
    """Describe a failure without echoing the key or the request body."""
    status = getattr(exc, "status_code", None)
    if status == 401:
        return "Anthropic rejected the API key"
    if status == 429:
        return "Anthropic rate limit reached"
    if isinstance(status, int) and status >= 500:
        # Mirrors the wording OpenAICompatibleProvider uses, because
        # FailoverLLMProvider matches on "failed with HTTP 5" to decide
        # whether an endpoint is down and the fallback should take over.
        return f"Anthropic request failed with HTTP {status}"
    if isinstance(status, int):
        return f"Anthropic request failed with HTTP {status}"
    return f"Anthropic request failed ({type(exc).__name__})"


def _image_block(image_path: str) -> dict[str, Any]:
    import base64
    import mimetypes
    from pathlib import Path

    path = Path(image_path)
    media_type = mimetypes.guess_type(path.name)[0] or "image/png"
    data = base64.standard_b64encode(path.read_bytes()).decode("ascii")
    return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}}
