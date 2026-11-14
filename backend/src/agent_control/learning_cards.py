"""What the assistant learned, offered in the chat where the person can say yes or no.

A learned skill is instructions the Operator will follow on every similar request, and a learned preference is a line it will read on every
task: a thing that changes tomorrow's behaviour has to be agreed to by the person it works for (skill_learning.py and persona_learning.py say
why). Until now the review was a queue in the web console, which is the last place someone who talks to their assistant on a phone looks.
So the moment a proposal is made it goes to the chat as a short card with two buttons, and a tap is the review: Keep installs it exactly as the
console's accept does, and Drop rejects it. Nothing is installed without the tap, and a proposal nobody answers stays in the queue.

The callback data is `learn:<s|p>:<id>:<k|d>`: short on purpose, because Telegram allows 64 bytes and the ids are 40 or more.
"""

from __future__ import annotations

from typing import Any

from agent_control.config import AppSettings
from agent_control.persona_learning import PersonaSuggestion, SuggestionStore, apply_to_persona
from agent_control.skill_learning import SkillSuggestion, SkillSuggestionStore, install

SKILL = "s"
PERSONA = "p"


def card(suggestion: SkillSuggestion | PersonaSuggestion) -> tuple[str, dict[str, Any]]:
    """The message and the two buttons for one proposal."""
    if isinstance(suggestion, SkillSuggestion):
        kind = SKILL
        text = (
            f"I noticed a procedure worth keeping from that job: {suggestion.name}.\n{suggestion.description}\n\n"
            "If I keep it, I will follow it on similar requests. Keep it as a skill?"
        )
        labels = ("Keep it", "Not now")
    else:
        kind = PERSONA
        text = f"Should I remember this about how you like things done?\n\"{suggestion.line}\""
        labels = ("Remember", "No")
    keyboard = {"inline_keyboard": [[
        {"text": labels[0], "callback_data": f"learn:{kind}:{suggestion.id}:k"},
        {"text": labels[1], "callback_data": f"learn:{kind}:{suggestion.id}:d"},
    ]]}
    return text, keyboard


def decide(settings: AppSettings, kind: str, suggestion_id: str, keep: bool) -> str | None:
    """Apply the person's answer. The line to show for it, or None when that proposal is not waiting any more."""
    if kind == SKILL:
        store = SkillSuggestionStore.load(settings.adapters.skills)
        decided = store.decide(suggestion_id, keep)
        if decided is None:
            return None
        if keep:
            install(settings.adapters.skills, decided)
            return f"Kept: {decided.name}. I will use it on similar requests."
        return "Okay, I will not keep that."
    if kind == PERSONA:
        persona_store = SuggestionStore.load(settings.adapters.persona)
        persona = persona_store.decide(suggestion_id, keep)
        if persona is None:
            return None
        if keep:
            apply_to_persona(settings.adapters.persona, persona)
            return f"Remembered: {persona.line}"
        return "Okay, I will not remember that."
    return None
