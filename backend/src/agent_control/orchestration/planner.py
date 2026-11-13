"""Split a big request into parts that can each be done, and checked, on their own.

A request such as "get the headline from these five websites and tell me which one is shortest" is five small jobs and one
comparison. Left to one loop with a twelve-step budget and one growing history, a small local model does the first two
sites, forgets the third, and answers from what it remembers. Run as parts, each part starts from nothing with only its own
instruction, finishes or fails honestly, and the final answer is written from what every part really returned.

Whether a request has parts is the model's call (one short question, answered as JSON), never a word list. Code only decides
whether the question is worth asking: a short single-sentence request has no parts, and asking would add seconds to every
message for nothing.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

PARTS_MAX = 6
#: A request shorter than this, naming fewer than two places and written on one line, is not worth a planning call.
LONG_REQUEST_CHARS = 160

PLAN_SYSTEM_PROMPT = """You split one request into independent parts that a helper with tools can each finish on its own.
The helper can read and write files, read web pages, run Python on the computer, and build small web apps.

Return ONLY JSON: {"parts": [{"title": "...", "objective": "..."}]}

Rules:
- Make parts only when the request really has several separate pieces of work, for example several websites or files to handle one by one, or something to build and then something to produce from it. Otherwise return {"parts": []}.
- title: three to six words.
- objective: one complete instruction with everything the helper needs: the exact web address, file or folder, and what to bring back. The helper cannot see the other parts or the original request.
- At most 6 parts.
- Combining or comparing the parts' results is NOT a part: it is done afterwards, from their results."""

_URL = re.compile(r"https?://\S+|\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:com|org|net|io|dev|gov|edu|co|ai)\b", re.IGNORECASE)
_PATH = re.compile(r"[A-Za-z]:[\\/][^\s\"'<>|?*]+")


@dataclass(frozen=True)
class Part:
    title: str
    objective: str

    def as_dict(self) -> dict[str, str]:
        return {"title": self.title, "objective": self.objective}


def should_plan(request: str) -> bool:
    """Whether asking the model to split this request is worth a model call (shape only, no words)."""
    text = (request or "").strip()
    if not text:
        return False
    places = {match.group(0).lower().rstrip(".,;)") for match in _URL.finditer(text)} | {
        match.group(0).lower().rstrip(".,;)") for match in _PATH.finditer(text)
    }
    return len(text) >= LONG_REQUEST_CHARS or len(places) >= 2 or text.count("\n") >= 2


def parse_parts(raw: str | None) -> list[Part]:
    """The parts in a model's reply; nothing at all when the reply is not a clean list of at least two."""
    text = (raw or "").strip()
    if not text:
        return []
    try:
        payload = json.loads(text[text.index("{") : text.rindex("}") + 1])
    except (ValueError, json.JSONDecodeError):
        return []
    items = payload.get("parts") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return []
    parts: list[Part] = []
    seen: set[str] = set()
    for item in items[:PARTS_MAX]:
        if not isinstance(item, dict):
            return []
        title = " ".join(str(item.get("title") or "").split())[:80]
        objective = " ".join(str(item.get("objective") or "").split())[:900]
        if not title or not objective or objective.casefold() in seen:
            return []
        seen.add(objective.casefold())
        parts.append(Part(title=title, objective=objective))
    return parts if len(parts) >= 2 else []


async def plan_parts(provider: Any, request: str) -> list[Part]:
    """Ask the model to split the request. Any failure means no plan: the request then runs as it always did."""
    if provider is None or not should_plan(request):
        return []
    try:
        raw = await provider.generate_text(PLAN_SYSTEM_PROMPT, f"Request:\n{request.strip()[:3000]}")
    except Exception:  # noqa: BLE001 - planning is an optimisation; it must never fail a task
        return []
    return parse_parts(raw)


def plan_summary(parts: list[dict[str, Any]]) -> str:
    """The plan in words, for the person: what is about to happen, in order."""
    lines = [f"{number}. {part['title']}" for number, part in enumerate(parts, start=1)]
    return f"This has {len(parts)} parts, so I will do them one at a time:\n" + "\n".join(lines)


PARTS_DONE_TEXT = (
    "Every part has run. Work out the comparison or conclusion the request asked for first (count and compare carefully), then "
    "write the final answer once (action done): one short line per part, then the conclusion. Use only what the parts returned, "
    "and say plainly which part failed and why. The answer must not contain second thoughts or corrections."
)
