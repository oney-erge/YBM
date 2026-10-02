"""Receipts for model calls: token totals and the per-call trace.

The worker records every Operator and Auditor call against its task. The
Concierge (the model that decides whether a message is chat or a task, and
often writes the chat reply in the same call) was left out on the grounds that it
runs "before a task exists". But it is usually the first and sometimes the
largest call of a task, so receipts that claim to show model cost understated it,
and a chat-only reply showed no cost at all.

The task does exist by the time the call is finished with: the web console creates
one even for a chat reply, and Telegram and WhatsApp create one for anything that
is work. This records the Concierge's call against it, the same way the worker
records its own.
"""

from __future__ import annotations

import logging
from typing import Any

from agent_control.schemas import LLMCallRecord
from agent_control.storage.redaction import redact_payload
from agent_control.storage.repositories import Repositories

logger = logging.getLogger(__name__)

_TOKEN_KEYS = ("prompt_tokens", "completion_tokens", "total_tokens")


def merge_token_usage(
    current: dict[str, Any] | None,
    source: str,
    usage: dict[str, Any] | None,
    *,
    fallback_used: bool = False,
) -> dict[str, Any] | None:
    """Fold one call's token usage into a task's ``token_usage`` metadata.

    Returns the new value, or None when the provider reported no usage (a replayed
    test, or a server that omits the field): a no-op, never a fabricated zero.
    ``fallback_used`` is sticky once set - a task where any call fell back to a
    secondary profile stays flagged for the rest of the task.
    """
    if not usage:
        return None
    merged = dict(current or {})
    merged["calls"] = int(merged.get("calls", 0)) + 1
    for key in _TOKEN_KEYS:
        value = usage.get(key)
        if isinstance(value, int | float):
            merged[key] = int(merged.get(key, 0)) + int(value)
    by_source = dict(merged.get("by_source") or {})
    entry = dict(by_source.get(source) or {})
    entry["calls"] = int(entry.get("calls", 0)) + 1
    for key in _TOKEN_KEYS:
        value = usage.get(key)
        if isinstance(value, int | float):
            entry[key] = int(entry.get(key, 0)) + int(value)
    by_source[source] = entry
    merged["by_source"] = by_source
    if usage.get("model"):
        merged["last_model"] = usage["model"]
    if fallback_used:
        merged["fallback_used"] = True
    return merged


def cap_message_content(content: Any, max_chars: int) -> Any:
    if isinstance(content, str):
        return content if len(content) <= max_chars else f"{content[:max_chars]}...[truncated]"
    if isinstance(content, list):
        capped = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "image_url":
                capped.append({"type": "image_url", "image_url": {"url": "[image omitted from trace]"}})
            elif isinstance(part, dict) and isinstance(part.get("text"), str):
                capped.append({**part, "text": cap_message_content(part["text"], max_chars)})
            else:
                capped.append(part)
        return capped
    return content


def cap_messages(messages: list[dict[str, Any]], max_chars: int) -> list[dict[str, Any]]:
    """Bound one call's persisted messages (applied after redaction, per message,
    so the cap cannot truncate mid-redaction-pattern). A multimodal message's
    image parts are a base64 data URI, not prompt text, so they become a
    placeholder rather than being truncated into garbage."""
    return [
        {**message, "content": cap_message_content(message.get("content"), max_chars)}
        for message in messages
        if isinstance(message, dict)
    ]


def record_concierge_call(
    repositories: Repositories,
    task_id: str,
    provider: Any,
    *,
    persist: bool = True,
    max_chars: int = 8000,
    redact_patterns: list[str] | tuple[str, ...] | None = None,
) -> None:
    """Record the Concierge's model call against the task it led to.

    Reads the same ``last_*`` fields the worker reads off the Operator and Auditor
    services (providers set them after every call). Does nothing when the provider
    exposes none (a fake or scripted classifier), and never raises: a receipt
    failing to save must not break the message that is being handled.
    """
    messages = getattr(provider, "last_request", None)
    started_at = getattr(provider, "last_started_at", None)
    if not isinstance(messages, list) or not messages or started_at is None:
        return
    usage = getattr(provider, "last_usage", None)
    try:
        task = repositories.tasks.get(task_id)
        if task is not None:
            merged = merge_token_usage(task.metadata.get("token_usage"), "concierge", usage)
            if merged is not None:
                repositories.tasks.update_metadata(task_id, {**task.metadata, "token_usage": merged})
        if persist:
            response_text = getattr(provider, "last_response_text", None)
            if isinstance(response_text, str) and len(response_text) > max_chars:
                response_text = f"{response_text[:max_chars]}...[truncated]"
            usage = usage or {}
            repositories.llm_calls.create(
                LLMCallRecord(
                    task_id=task_id,
                    source="concierge",
                    model=getattr(provider, "last_model", None),
                    step_index=0,
                    messages=cap_messages(redact_payload(messages, redact_patterns), max_chars),
                    response_text=response_text,
                    prompt_tokens=usage.get("prompt_tokens"),
                    completion_tokens=usage.get("completion_tokens"),
                    total_tokens=usage.get("total_tokens"),
                    latency_ms=getattr(provider, "last_latency_ms", None),
                    created_at=started_at,
                )
            )
    except Exception:  # noqa: BLE001 - a receipt must never break the message it describes
        logger.warning("could not record the concierge call for task %s", task_id, exc_info=True)
