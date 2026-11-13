"""Say a bare answer the way a person would.

The Operator sometimes finishes with just the value ("13655") or with what a tool printed ("Wrote text file ...", "Found 3
result(s) ..."), which is right and reads like a log line on a phone. One short model call rewrites such an answer as a sentence
or two. The rewrite is accepted only if it adds nothing: every number in it must already be in the original answer or the request,
so the wording can change and the facts cannot. An answer that is already a sentence is never touched, and any failure keeps the
original.
"""

from __future__ import annotations

import re
from typing import Any

VOICE_SYSTEM_PROMPT = """You write the reply a helpful assistant sends to a person in a phone chat.
You are given what the person asked and the bare answer. Say the answer as one or two plain sentences.

Rules:
- Use only what is in the answer. Keep every number, name and value exactly as it is. Add nothing new.
- No file paths unless the person asked where something is. No markdown headings, no bullet lists unless the answer already is a list.
- Reply with the message only."""

#: How tools phrase a result: correct, and not what a person would say.
_TOOL_VOICE = re.compile(
    r"^\s*(?:Wrote text file|Found \d+ |Moved \d+ file|Read \S|Opened \S|Added \d+ character|Described \d+ file|Created \d+ |"
    r"Resolved \d+ |Disk space:|Replayed \d+/\d+)|Changed paths:|^Workspace:|^Entries:",
    re.IGNORECASE | re.MULTILINE,
)
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


def needs_voice(answer: str | None) -> bool:
    """Whether an answer is a bare value or a tool's own phrasing rather than something a person would say."""
    text = (answer or "").strip()
    if not text or len(text) > 700:
        return False
    if _TOOL_VOICE.search(text):
        return True
    words = text.split()
    return len(text) <= 60 and len(words) < 4 and any(ch.isdigit() for ch in text)


def _numbers(text: str) -> set[str]:
    return {match.group(0).replace(",", "").rstrip(".") for match in _NUMBER.finditer(text)}


def adds_nothing(spoken: str, original: str, request: str) -> bool:
    """The rewrite may say things differently, but every number in it must already be in the answer or the request."""
    return _numbers(spoken) <= (_numbers(original) | _numbers(request))


async def speak(provider: Any, request: str, answer: str) -> str | None:
    """The answer in a person's words, or None to keep the original (no provider, a failure, or anything the rewrite invented)."""
    if provider is None or not needs_voice(answer):
        return None
    try:
        raw = await provider.generate_text(VOICE_SYSTEM_PROMPT, f"They asked: {request.strip()[:600]}\n\nThe answer: {answer.strip()}")
    except Exception:  # noqa: BLE001 - wording is a courtesy; it must never fail a finished task
        return None
    spoken = " ".join((raw or "").split())
    if not spoken or len(spoken) > 700 or spoken.casefold() == answer.strip().casefold():
        return None
    if not adds_nothing(spoken, answer, request):
        return None
    return spoken
