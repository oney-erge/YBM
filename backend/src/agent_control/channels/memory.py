from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from agent_control.llm.providers import LLMProvider
from agent_control.prompts import render_prompt
from agent_control.schemas import MemoryFact, utc_now
from agent_control.storage.repositories import Repositories


logger = logging.getLogger(__name__)

DEFAULT_SUMMARY = "No durable conversation memory yet."

# "Remember that ..." (docs/archive/UI_UX_AUDIT.md Phase 15) - shared by every
# channel (Telegram, web chat) so provenance is decided identically
# everywhere: at the runtime level, before any LLM sees the message, never
# selectable by the model. Matched against the ORIGINAL-case text so the
# stored fact preserves the user's actual words, not a lowercased copy.
#
# Requires "that" (or a ":"/"," separator) right after remember/don't
# forget, not just the bare verb - "remember to call the plumber" is a
# reminder-shaped request that should still reach the classifier and
# become a task, not get silently swallowed into a memory fact just
# because it starts with "remember".
_REMEMBER_PATTERN = re.compile(
    r"^(?:please\s+)?(?:remember|don'?t\s+forget)(?:\s+that|[:,])\s+(.+)$",
    re.IGNORECASE | re.DOTALL,
)


def detect_remember_request(text: str) -> str | None:
    """Extracts the content of a "remember that ..." message, or None if
    ``text`` doesn't match. Capped to MemoryFact.content's 2000-char limit
    so a caller can construct one directly without a validation error.
    """
    match = _REMEMBER_PATTERN.match(text.strip())
    if match is None:
        return None
    content = match.group(1).strip()
    return content[:2000] if content else None

_STOPWORDS = frozenset({
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "to", "of", "in", "on", "at",
    "for", "and", "or", "with", "this", "that", "it", "its", "my", "me", "please", "can", "you",
    "i", "do", "does", "did", "will", "would", "should", "could", "have", "has", "had", "not",
})


def _keywords(text: str) -> set[str]:
    return {word for word in re.findall(r"[a-z0-9]+", text.lower()) if len(word) > 2 and word not in _STOPWORDS}


def score_facts(facts: list[MemoryFact], objective: str, *, limit: int = 15) -> list[MemoryFact]:
    """Deterministic relevance selection (docs/archive/UI_UX_AUDIT.md Phase 15) -
    replaces "inject every fact into every task," which is fine at five
    facts and actively harmful at a thousand. No vector database: a scorer
    built from word overlap, category match, and recency is fully
    inspectable in a way an embedding search is not.

    Facts with no ``task_id`` are treated as durable, global preferences -
    set by a human via the Memory page or a "remember that ..." message,
    not incidentally learned mid-task - and always included, unscored.
    There is no explicit "pinned" flag in the schema; this is the closest
    grounded proxy available: a fact tied to one specific task's context is
    exactly the kind that stops being relevant once that task is old news,
    while a fact with no task behind it doesn't decay that way.

    Scoring combines four of the roadmap's five signals into one ranking:
    - entity match: a capitalized word/phrase in the fact (a proper-noun
      proxy - "Python", "Chrome") that also appears in the objective,
      weighted heavily.
    - keyword overlap: shared lowercase words with the objective, minus
      stopwords.
    - category relevance: the fact's own category name (split into words,
      so "coding_style" matches "coding" or "style") overlapping with the
      objective's keywords.
    - recency: an exponential-ish decay on ``updated_at`` age in days.
    The fifth - "current folder/service context" - is NOT implemented: no
    caller of this function has a folder/service signal available today
    (a disclosed gap, not a silent one), a natural follow-up once one does.
    """
    pinned = [fact for fact in facts if fact.task_id is None]
    scoped = [fact for fact in facts if fact.task_id is not None]
    if not scoped:
        return pinned

    objective_keywords = _keywords(objective)
    now = utc_now()

    def score(fact: MemoryFact) -> float:
        entities = re.findall(r"\b[A-Z][A-Za-z0-9]{2,}\b", fact.content)
        entity_score = sum(4.0 for entity in entities if entity.lower() in objective_keywords)
        keyword_score = float(len(_keywords(fact.content) & objective_keywords))
        category_score = 2.0 if _keywords(fact.category) & objective_keywords else 0.0
        age_days = max((now - fact.updated_at).total_seconds() / 86400, 0.0)
        recency_score = 1.0 / (1.0 + age_days / 30)
        return entity_score + keyword_score + category_score + recency_score

    ranked = sorted(scoped, key=score, reverse=True)
    return pinned + ranked[:limit]


class ConversationMemoryService:
    """Rolling conversation memory with a concise summary plus a small recent-turn window."""

    def __init__(
        self,
        repositories: Repositories,
        provider: LLMProvider | None = None,
        *,
        max_summary_chars: int = 2400,
        max_recent_turns: int = 10,
        summarization_timeout_seconds: int = 20,
    ) -> None:
        self.repositories = repositories
        self.provider = provider
        self.max_summary_chars = max_summary_chars
        self.max_recent_turns = max_recent_turns
        self.summarization_timeout_seconds = summarization_timeout_seconds

    async def update_from_user_message(self, conversation_id: str, text: str) -> dict[str, Any]:
        return await self.update_from_turn(conversation_id, "user", text)

    async def update_from_assistant_message(self, conversation_id: str, text: str) -> dict[str, Any]:
        return await self.update_from_turn(conversation_id, "assistant", text)

    async def update_from_task_summary(self, conversation_id: str, text: str) -> dict[str, Any]:
        return await self.update_from_turn(conversation_id, "task", text)

    async def update_from_turn(self, conversation_id: str, role: str, text: str) -> dict[str, Any]:
        existing = self.repositories.conversation_memory.get(conversation_id)
        state = dict((existing or {}).get("facts") or {})
        turns = _recent_turns(state)
        turns.append({"role": _clean_role(role), "text": _trim_turn(text)})
        turns = turns[-self.max_recent_turns :]
        state["recent_turns"] = turns
        state["message_count"] = int(state.get("message_count") or 0) + 1
        state["strategy"] = "rolling_summary_with_recent_turns"

        existing_summary = str((existing or {}).get("summary") or DEFAULT_SUMMARY)
        summary = await self._summarize(existing_summary, turns)
        state["summary"] = summary
        return self.repositories.conversation_memory.upsert(conversation_id, summary, state)

    async def _summarize(self, existing_summary: str, recent_turns: list[dict[str, str]]) -> str:
        if self.provider is None:
            return _fallback_summary(existing_summary, recent_turns, self.max_summary_chars)
        try:
            output = await asyncio.wait_for(
                self.provider.generate_text(
                    _summary_system_prompt(self.max_summary_chars),
                    _summary_user_prompt(existing_summary, recent_turns),
                ),
                timeout=self.summarization_timeout_seconds,
            )
        except Exception:
            logger.warning("conversation summarization failed; falling back to heuristic summary", exc_info=True)
            return _fallback_summary(existing_summary, recent_turns, self.max_summary_chars)
        return _clean_summary(output, self.max_summary_chars)


def memory_context(
    memory_record: dict[str, Any] | None,
    *,
    recent_turns: int = 4,
    max_chars: int = 1800,
    remembered_facts: list[Any] | None = None,
    objective: str = "",
) -> str:
    """``remembered_facts`` (docs/archive/UI_UX_AUDIT.md Phase 4): structured
    MemoryFact rows, distinct from the rolling summary below - a fact the
    user or a task explicitly recorded, not a model's compressed guess at
    what mattered in recent turns. Rendered first and not subject to
    max_chars trimming, since a person deliberately added these and
    silently dropping one because the summary ran long would defeat the
    point of "remember".

    ``objective`` (docs/archive/UI_UX_AUDIT.md Phase 15) scores and caps
    ``remembered_facts`` via score_facts before rendering - callers still
    pass the full, unfiltered fact list; the selection happens here, once,
    rather than duplicated at every call site.
    """
    scored_facts = score_facts(list(remembered_facts), objective) if remembered_facts else []
    facts_block = ""
    if scored_facts:
        lines = "\n".join(f"- [{f.category}] {f.content}" for f in scored_facts)
        facts_block = f"Remembered facts:\n{lines}\n"

    if not memory_record:
        return facts_block + DEFAULT_SUMMARY if facts_block else DEFAULT_SUMMARY
    summary = str(memory_record.get("summary") or DEFAULT_SUMMARY)
    state = dict(memory_record.get("facts") or {})
    turns = _recent_turns(state)[-recent_turns:]
    if not turns:
        return facts_block + _clean_summary(summary, max_chars)
    recent = "\n".join(f"- {turn['role']}: {turn['text']}" for turn in turns)
    return facts_block + _clean_summary(f"{summary}\nRecent turns:\n{recent}", max_chars)


def _summary_system_prompt(max_summary_chars: int) -> str:
    return render_prompt("base/conversation_memory_system.md", max_summary_chars=max_summary_chars)


def _summary_user_prompt(existing_summary: str, recent_turns: list[dict[str, str]]) -> str:
    turns = "\n".join(f"{turn['role']}: {turn['text']}" for turn in recent_turns)
    return render_prompt(
        "tasks/conversation_memory_user.md",
        existing_summary=existing_summary,
        recent_turns=turns,
    )


def _fallback_summary(existing_summary: str, recent_turns: list[dict[str, str]], limit: int) -> str:
    useful_existing = "" if existing_summary == DEFAULT_SUMMARY else _stable_existing_summary(existing_summary)
    latest = "; ".join(f"{turn['role']}: {turn['text']}" for turn in recent_turns[-3:] if turn.get("text"))
    if useful_existing and latest:
        summary = f"{useful_existing}\nRecent conversation context: {latest}"
    elif latest:
        summary = f"Recent conversation context: {latest}"
    else:
        summary = DEFAULT_SUMMARY
    return _clean_summary(summary, limit)


def _stable_existing_summary(existing_summary: str) -> str:
    text = existing_summary.strip()
    for marker in ("Recent conversation context:", "Recent user context:", "Recent turns:"):
        if marker in text:
            text = text.split(marker, 1)[0].strip()
    return text


def _recent_turns(state: dict[str, Any]) -> list[dict[str, str]]:
    turns = state.get("recent_turns") or []
    clean: list[dict[str, str]] = []
    for turn in turns:
        if not isinstance(turn, dict):
            continue
        role = _clean_role(str(turn.get("role") or "user"))
        text = str(turn.get("text") or "").strip()
        if text:
            clean.append({"role": role, "text": _trim_turn(text)})
    return clean


def _clean_role(value: str) -> str:
    role = value.strip().lower().replace(" ", "_")
    if role in {"user", "assistant", "task", "system", "tool"}:
        return role
    return "user"


_PLACEHOLDER_PATH_PATTERN = re.compile(
    r"[A-Za-z]:[\\/]Users[\\/]"
    r"(?:me|user|username|youruser|your_user)"
    r"(?:[\\/][^\s\"'<>]*)?",
    re.IGNORECASE,
)


def _trim_turn(text: str, limit: int = 600) -> str:
    clean = " ".join(text.split())
    return clean if len(clean) <= limit else f"{clean[: limit - 3]}..."


def _strip_placeholder_paths(text: str) -> str:
    """Remove hallucinated paths like ``C:\\Users\\me\\Desktop\\foo.pdf`` from memory.

    If the LLM stored a placeholder username path in memory, the planner will
    happily re-use it on the next turn. Strip these before they propagate.
    Keep the basename if present so 'send me that <file>' still has the filename.
    """
    def replace(match: "re.Match[str]") -> str:
        path = match.group(0)
        # Try to keep the basename (e.g. "resume.pdf") since that's the useful part.
        tail = path.replace("/", "\\").rsplit("\\", 1)[-1]
        if "." in tail and len(tail) > 2:
            return tail
        return ""

    return _PLACEHOLDER_PATH_PATTERN.sub(replace, text)


def _clean_summary(text: str, limit: int) -> str:
    cleaned_lines = (
        _strip_placeholder_paths(line.strip())
        for line in text.strip().splitlines()
        if line.strip()
    )
    clean = " ".join(line for line in cleaned_lines if line)
    if not clean:
        return DEFAULT_SUMMARY
    return clean if len(clean) <= limit else f"{clean[: limit - 3]}..."
