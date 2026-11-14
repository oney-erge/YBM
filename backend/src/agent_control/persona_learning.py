"""Turn the operator's corrections into durable preferences - with review.

`persona.md` already exists and is re-read on every Operator step, so a line
added here changes behavior immediately and permanently. What was missing is
the loop that gets lines into it: nothing watched for "stop asking me that",
"too long", "always use X", so the file only grew when the model happened to
decide to write to it.

Two deliberate constraints:

**Off by default, behind one switch.** `adapters.persona.learning_enabled`.
Learning from an operator's phrasing is the kind of feature that should be
chosen, not discovered after the fact.

**Proposed, never written.** A suggestion lands in a review queue and only
reaches `persona.md` when a human accepts it. A system that silently edits its
own standing instructions drifts in ways no audit trail can reconstruct - six
months on, "why does it behave like this now?" would have no answer. Every
accepted line therefore carries the date and the message that produced it, so
the file reads as a history rather than as an unexplained ruleset.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from agent_control.config import PersonaAdapterConfig
from agent_control.persona import read_persona, write_persona, DEFAULT_PERSONA
from agent_control.schemas import new_id, utc_now


LEARNING_SYSTEM_PROMPT = """You read one message a user sent to their personal assistant. Decide whether it states a LASTING preference about how
the assistant should behave or answer in the future: tone, format, defaults, things to always or never do ("always", "from now on", "never",
"I prefer").

It is NOT a preference when it asks for one thing now (a question, a file to move), or asks for a reminder or a recurring job: those are requests.
Most messages are just requests, so when unsure say it is not a preference.

If it is one, write it as a single imperative line under 120 characters, e.g. "Keep answers under 5 bullet points." or
"Never ask before read-only file searches." Leave the line empty when it is not."""


class _Preference(BaseModel):
    """Free text from a 9B model answered the question in the message instead of extracting from it; a schema keeps it to the job."""

    states_a_lasting_preference: bool = Field(
        description="true only if the user says how the assistant should behave from now on, not just what they want done this time"
    )
    preference: str = Field(default="", description="the preference as one short imperative line; empty when there is none")


@dataclass
class PersonaSuggestion:
    id: str
    line: str
    source_message: str
    task_id: str | None
    created_at: str
    status: str = "pending"  # pending | accepted | rejected
    decided_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SuggestionStore:
    """Flat JSON next to persona.md - the queue is small, human-scale, and
    should be as easy to inspect and delete as the persona file itself."""

    path: Path
    suggestions: list[PersonaSuggestion] = field(default_factory=list)

    @classmethod
    def load(cls, config: PersonaAdapterConfig) -> "SuggestionStore":
        path = Path(config.path).expanduser().with_name("persona_suggestions.json")
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls(path=path, suggestions=[])
        items = [PersonaSuggestion(**item) for item in raw if isinstance(item, dict)]
        return cls(path=path, suggestions=items)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps([s.to_dict() for s in self.suggestions], indent=2), encoding="utf-8"
        )

    def pending(self) -> list[PersonaSuggestion]:
        return [s for s in self.suggestions if s.status == "pending"]

    def add(self, line: str, source_message: str, task_id: str | None) -> PersonaSuggestion | None:
        """None when this preference is already known or already queued -
        re-proposing the same line every time the user repeats themselves
        would make the queue useless."""
        normalized = line.strip().rstrip(".").lower()
        for existing in self.suggestions:
            if existing.status in ("pending", "accepted") and existing.line.strip().rstrip(".").lower() == normalized:
                return None
        suggestion = PersonaSuggestion(
            id=new_id("persona_sugg"),
            line=line.strip(),
            source_message=source_message.strip()[:500],
            task_id=task_id,
            created_at=utc_now().isoformat(),
        )
        self.suggestions.append(suggestion)
        self.save()
        return suggestion

    def decide(self, suggestion_id: str, accept: bool) -> PersonaSuggestion | None:
        for suggestion in self.suggestions:
            if suggestion.id == suggestion_id and suggestion.status == "pending":
                suggestion.status = "accepted" if accept else "rejected"
                suggestion.decided_at = utc_now().isoformat()
                self.save()
                return suggestion
        return None


def apply_to_persona(config: PersonaAdapterConfig, suggestion: PersonaSuggestion) -> str:
    """Append an accepted line, with the date and what prompted it.

    Provenance is the whole point: a bare list of rules cannot answer "why is
    it doing this?" a year later, and this file is the one place a preference
    can change every future task.
    """
    current = read_persona(config)
    body = "" if current == DEFAULT_PERSONA else current
    stamp = datetime.fromisoformat(suggestion.created_at).strftime("%Y-%m-%d")
    entry = f"- {suggestion.line}  _(learned {stamp} from: \"{suggestion.source_message[:120]}\")_"
    if "## Learned preferences" in body:
        updated = body.rstrip() + "\n" + entry
    else:
        updated = (body.rstrip() + "\n\n" if body.strip() else "") + "## Learned preferences\n" + entry
    return write_persona(config, updated)


async def propose_from_message(
    provider: Any,
    config: PersonaAdapterConfig,
    message: str,
    *,
    task_id: str | None = None,
) -> PersonaSuggestion | None:
    """Ask the model whether this message states a lasting preference.

    Returns None on anything uncertain - no provider, learning off, empty
    message, a NONE verdict, or any provider failure. A feature that quietly
    accumulates guesses about the user is worse than one that stays silent.
    """
    if not config.enabled or not config.learning_enabled or provider is None:
        return None
    text = (message or "").strip()
    if not text:
        return None
    try:
        found = await provider.generate_structured(LEARNING_SYSTEM_PROMPT, text[:2000], _Preference, temperature=0.0)
    except Exception:  # noqa: BLE001 - never fail a finished task over this
        return None
    line = " ".join(str(found.preference or "").split())
    if not found.states_a_lasting_preference or not line or len(line) > 200:
        return None
    store = SuggestionStore.load(config)
    return store.add(line, text, task_id)
