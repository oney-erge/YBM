"""What the model is shown besides the tool list, and what is kept when there is not room for all of it (docs/DESIGN.md, "What the model sees").

The Operator's prompt has a tool catalog that is large and fixed, and a few short notes that are about this person and this moment: what was said just
before, the plan that is open in the chat, the goal this continues, the project, what bears on the request, today's date. The notes used to be glued on
behind the catalog and the whole was cut to one size from the end, so the more tools there were, the less of the notes survived. On 11/19 "revise the plan"
failed twice because the open plan, the one thing that said what "it" was, was the first thing cut; the model searched the disk for a notebook instead.

Here the notes have their own room, in an order of importance. When they do not fit, the least important is shortened first, at a line, and says so; the most
important is whole. The catalog is cut to what is left.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta, timezone

from agent_control.schemas import utc_now

#: How much of the prompt the notes may use. The catalog gets the rest of its own budget.
SITUATION_CHARS = 5_000
#: A part shorter than this is dropped rather than shown as a stub.
_MIN_PART_CHARS = 200
_LEFT_OUT = "\n(more of this was left out to fit; ask for it by name if you need it)"
#: What was said last counts as "just before" for this long.
_JUST_BEFORE = timedelta(hours=6)
_MAX_THEIR_CHARS = 600
_MAX_YOUR_CHARS = 1_500

#: Importance of each part, lower is kept longer. The date is a few words and the last exchange is what "it" and "that" point to.
TODAY, LAST_EXCHANGE, GOAL, PROJECT_INDEX, PROJECT, ABILITIES = 0, 1, 1, 1, 2, 3


def _line_cut(text: str, keep: int) -> str:
    """The first `keep` characters, cut back to the end of a line so no sentence is cut in half, with a note that something was left out."""
    room = keep - len(_LEFT_OUT)
    if room < _MIN_PART_CHARS:
        return ""
    cut = text[:room]
    end = cut.rfind("\n")
    return (cut[:end] if end > _MIN_PART_CHARS // 2 else cut).rstrip() + _LEFT_OUT


def fit(parts: Sequence[tuple[int, str]], budget: int = SITUATION_CHARS) -> str:
    """The parts as they are, in order, when they fit; otherwise the least important shortened first (the later of two equals first)."""
    texts = [text for _, text in parts]
    over = sum(len(text) for text in texts) - budget
    if over <= 0:
        return "".join(texts)
    for index in sorted(range(len(parts)), key=lambda i: (-parts[i][0], -i)):
        if over <= 0:
            break
        text = texts[index]
        if not text:
            continue
        shortened = _line_cut(text, max(0, len(text) - over))
        over -= len(text) - len(shortened)
        texts[index] = shortened
    return "".join(texts)


def _words(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "..."


def last_exchange(episodes, conversation_id: str | None, current_message: str = "", *, now: datetime | None = None) -> str:
    """What the person said before this message and what the assistant answered, word for word, when that was a short while ago.

    A follow-up ("revise it", "that one", "no, the other") means something only next to the last exchange; the rolling summary keeps the gist, not the words. The
    episode of the current message is skipped. Empty when there is nothing recent, so a first message adds nothing.
    """
    if not conversation_id:
        return ""
    now = now or utc_now()
    said = _recent(episodes, conversation_id, "said", now)
    if said and " ".join(said[0]["text"].split()) == " ".join((current_message or "").split()):
        said = said[1:]
    answered = _recent(episodes, conversation_id, "answer", now)
    if not answered:
        return ""
    lines = ["\n\n## What was said just before this message"]
    if said:
        lines.append("They said: " + _words(said[0]["text"], _MAX_THEIR_CHARS))
    lines.append("You answered: " + _words(answered[0]["text"], _MAX_YOUR_CHARS))
    lines.append('If this message says "it", "that" or "the other one", it points at what you answered, not at a file.')
    return "\n".join(lines)


def _recent(episodes, conversation_id: str, kind: str, now: datetime) -> list[dict]:
    try:
        rows = episodes.latest(conversation_id, kind, limit=2)
    except Exception:  # noqa: BLE001 - context is help, never a gate
        return []
    fresh = []
    for row in rows:
        try:
            at = datetime.fromisoformat(str(row["at"]))
        except (TypeError, ValueError):
            continue
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        if now - at <= _JUST_BEFORE:
            fresh.append(row)
    return fresh
