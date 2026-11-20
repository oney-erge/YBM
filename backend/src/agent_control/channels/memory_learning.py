"""Remembering what the owner says, without being asked to, and keeping what stopped being true.

"Remember that ..." and "my X is Y" were the only ways a fact got kept (channels/profile_facts.py, channels/base.py). Everything else the owner said about their life ("we
just had a daughter", "we moved to Oak Street", "I'm thinking of a pool next spring") was gone once the conversation summary rolled over. This is the other way in. After a
reply, one small model call reads what the owner just said next to what is already known and proposes changes to the list of facts. The model never edits the list: it
proposes, and code applies the proposal with the rules below.

    add     a new fact          ("Has a daughter, Maya, born in October 2026")
    change  a fact that is no longer so, and what is so now. The old one is closed, not deleted: it keeps what it said and the day it stopped being true
    end     a fact that is simply no longer so ("we no longer have the dog")

Rules that do not depend on the model: only what the person said about themselves is kept (what the assistant said, what a web page said and what a command asked for are not facts
about them); a secret (a password, a card number, a code) is never kept; a fact is short; the same fact is not added twice; at most a few changes per message. Switching it off is
`memory.auto_remember: false`; the explicit "remember that" path stays.
"""

from __future__ import annotations

import logging
import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

from agent_control.schemas import MemoryFact, MemorySource, utc_now
from agent_control.storage.repositories import Repositories

logger = logging.getLogger(__name__)

MAX_CHANGES = 4
MAX_FACT_CHARS = 240
MIN_MESSAGE_CHARS = 12
RELATED_FACTS = 20
#: A message with none of these words is almost never a statement about the person; the model is not asked about it. A saving of calls, not a decision.
_FIRST_PERSON = re.compile(
    r"\b(?:i|i'm|i've|i'll|i'd|we|we're|we've|we'll|we'd|our|ours|us)\b|\bmy\s+\w+(?:\s+\w+)?\s+(?:is|are|was|were|has|have|had|will|just|got|needs)\b", re.IGNORECASE,
)
_SECRET = re.compile(r"(?:password|passcode|pin\b|cvv|cvc|security code|api[ _-]?key|secret|token|one[- ]time code|verification code|\b\d{4}[ -]?\d{4}[ -]?\d{4}[ -]?\d{3,4}\b|\b\d{3}-\d{2}-\d{4}\b)", re.IGNORECASE)

SYSTEM = """You keep a list of facts about one person for their assistant. You are given what the person just said and the facts already on the list (each with an id). Propose changes to the list.

- add: something lasting the person said about themselves, their family, home, health, work, belongings, plans, preferences or projects (a daughter's name, where they live, a pool they plan to build, a budget, a rule such as "prefers short answers"). One short sentence in the third person ("Has a daughter, Maya, born in October 2026"). `about` is a short label for what it is about ("home address", "daughter", "pool project").
- change: a fact on the list that is no longer true, and what is true now (they moved, the budget changed). Give the id of the old fact and the new sentence.
- end: a fact on the list that is simply no longer true, with nothing replacing it. Give its id.
- Write nothing for a question, a request, a command or thanks. Write nothing the person did not say about themselves. Never write a password, a card number, a code or any secret. Never write what the assistant said.
- notes: when the person speaks of something they are working on over time (building, buying, searching for, planning, learning), write one notebook entry for it: the project's short name, a few hooks (words and names that point to it), the kind (decision, fact, question, purchase, step, result) and one sentence. Reuse the name of a project already listed. A one-off request is not a project.
- If nothing lasting was said, return no changes and no notes. Most messages have none."""


# The bounds below are for the grammar the model writes under; what is read back after the call is not held to them. A field with no bound lets the 9B write on and on when it has
# nothing true to put there: with no facts on the list, "for change and end: the id of the fact" made it write 1,366 tokens of invented letters and digits for "I live in Dallas.",
# and the model server, which has one slot, kept every other call waiting for over three minutes (live 11/19, the sealed exam's v4_fact_moves; the same call with no grammar took 1.8 s).
_Word = Annotated[str, Field(json_schema_extra={"maxLength": 40})]


class _Change(BaseModel):
    op: Literal["add", "change", "end"]
    about: str = Field(default="", description="a short label for what it is about", json_schema_extra={"maxLength": 80})
    text: str = Field(default="", description="the fact, one short sentence; empty for end", json_schema_extra={"maxLength": MAX_FACT_CHARS})
    id: str = Field(default="", description="for change and end: the id of the fact on the list", json_schema_extra={"maxLength": 48})


class _Note(BaseModel):
    project: str = Field(description="a short name for the long-lived thing the person is working on (a pool, a robot, a daycare search)", json_schema_extra={"maxLength": 80})
    hooks: list[_Word] = Field(default_factory=list, description="3 to 6 words or names that point to it", json_schema_extra={"maxItems": 6})
    kind: Literal["decision", "fact", "question", "purchase", "step", "result"]
    text: str = Field(description="one sentence: what was decided, found, asked, bought, done or achieved", json_schema_extra={"maxLength": MAX_FACT_CHARS})


class _Changes(BaseModel):
    changes: list[_Change] = Field(default_factory=list, json_schema_extra={"maxItems": MAX_CHANGES})
    notes: list[_Note] = Field(default_factory=list, json_schema_extra={"maxItems": 3})


def worth_asking(text: str) -> bool:
    """Whether the model is asked at all: a message of some length that speaks of the person or their people."""
    text = " ".join((text or "").split())
    return len(text) >= MIN_MESSAGE_CHARS and bool(_FIRST_PERSON.search(text))


def _related(facts: list[MemoryFact], text: str) -> list[MemoryFact]:
    from agent_control.channels.memory import score_facts

    return score_facts(facts, text, limit=RELATED_FACTS)


class FactLearner:
    """Applies the model's proposed changes to the facts. `provider` is the same small model that writes the conversation summary."""

    def __init__(self, repositories: Repositories, provider: Any) -> None:
        self.repositories = repositories
        self.provider = provider

    async def learn(self, said: str, *, conversation_id: str | None = None) -> list[str]:
        """What was kept or changed, as short lines for the log. Never raises."""
        try:
            if self.provider is None or not worth_asking(said):
                return []
            current = self.repositories.memory_facts.list_all()
            related = _related(current, said)
            listing = "\n".join(f"{fact.id}: [{fact.about or fact.category}] {fact.content}" for fact in related) or "(nothing yet)"
            projects = "\n".join(f"- {project.name} ({', '.join(project.hooks[:4])})" for project in self.repositories.projects.list_all(limit=30) if project.status != "done") or "(none yet)"
            answer = await self.provider.generate_structured(
                SYSTEM, f"The facts already on the list:\n{listing}\n\nThe projects already known:\n{projects}\n\nWhat the person just said:\n{said}", _Changes, temperature=0.0,
            )
            if not isinstance(answer, _Changes):
                return []
            kept = self.apply(answer.changes, said, current, conversation_id)
            return kept + self.apply_notes(answer.notes, conversation_id)
        except Exception:  # noqa: BLE001 - remembering is never worth a failed message
            logger.warning("fact learning failed", exc_info=True)
            return []

    def apply_notes(self, notes: list[_Note], conversation_id: str | None = None) -> list[str]:
        from agent_control.projects_memory import note_to_project

        kept = []
        for note in notes[:3]:
            if _SECRET.search(note.text) or _SECRET.search(note.project):
                continue
            project = note_to_project(self.repositories, note.project, note.hooks, note.kind, note.text, conversation_id=conversation_id)
            if project is not None:
                kept.append(f"noted in {project.name}: {note.kind}: {note.text}")
        return kept

    def apply(self, changes: list[_Change], said: str, current: list[MemoryFact], conversation_id: str | None = None) -> list[str]:
        by_id = {fact.id: fact for fact in current}
        kept: list[str] = []
        for change in changes[:MAX_CHANGES]:
            text = " ".join(change.text.split())[:MAX_FACT_CHARS]
            if change.op != "end" and (not text or _SECRET.search(text) or _SECRET.search(change.about)):
                continue
            about = " ".join(change.about.split())[:80] or None
            old = by_id.get(change.id.strip()) if change.id else None
            if change.op == "end":
                if old is not None and self.repositories.memory_facts.close(old.id):
                    kept.append(f"ended: {old.content}")
                continue
            if any(fact.content.casefold() == text.casefold() for fact in current if fact.is_current):
                continue  # already known, word for word
            if change.op == "change" and old is None:
                # The model named a fact that is not on the list: keep the new one as an addition rather than lose it.
                old = next((f for f in current if about and f.about and f.about.casefold() == about.casefold()), None)
            fact = self.repositories.memory_facts.create(
                MemoryFact(category="life", content=text, source=MemorySource.USER_STATED, about=about, supersedes_id=old.id if old is not None else None, valid_from=utc_now())
            )
            if old is not None:
                self.repositories.memory_facts.close(old.id)
                kept.append(f"changed: {old.content} -> {fact.content}")
            else:
                kept.append(f"added: {fact.content}")
            self.repositories.episodes.add("note", f"Remembered: {fact.content}", conversation_id=conversation_id)
        return kept
