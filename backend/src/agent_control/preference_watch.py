"""Notice a lasting preference in a chat message ("always keep answers short") and offer to remember it.

Preferences are mostly stated in passing, in a message that is chat, not a task, so a hook that only looks at finished tasks (the worker's
`_maybe_learn_preference`) never sees them. This looks at a message once it has been answered and does the cheap thing first: the decision model
is asked one yes/no question on the CPU (about half a second, and it does not use the GPU the assistant's own model is working on). Only when
that says the message may state a preference is the assistant's model asked to extract it (persona_learning.py, with a schema), and a proposal
goes to the chat as a Remember / No card (learning_cards.py). Nothing is written to persona.md without the tap.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import structlog

from agent_control.config import PersonaAdapterConfig
from agent_control.learning_cards import card
from agent_control.persona_learning import propose_from_message

logger = structlog.get_logger(__name__)

GATE_QUESTION = (
    "Does the message state a lasting preference about how the assistant should behave or answer in the future "
    "(always, never, from now on, I prefer) rather than ask for something now?"
)
#: The decision model's yes below this is a request, a question or small talk. States of a real preference scored 0.5 to 0.9 and requests under 0.1.
GATE_AT = 0.4
MIN_CHARS = 8

YesNo = Callable[[str, dict[str, str]], Awaitable[dict[str, float]]]
Say = Callable[[dict[str, str], str, dict[str, Any] | None], Awaitable[None]]


class PreferenceWatch:
    def __init__(self, config: PersonaAdapterConfig, provider: Any, yes_no: YesNo | None, say: Say) -> None:
        self._config = config
        self._provider = provider
        self._yes_no = yes_no
        self._say = say

    async def consider(self, text: str, routing: dict[str, str], *, task_id: str | None = None) -> bool:
        """Look at one message. True when a proposal was offered. Never raises: this is the least important thing that happens to a message."""
        text = (text or "").strip()
        if not (self._config.enabled and self._config.learning_enabled) or self._yes_no is None or self._provider is None:
            return False
        if len(text) < MIN_CHARS or text.startswith("/") or not routing:
            return False
        try:
            answers = await self._yes_no(text, {"preference": GATE_QUESTION})
            if answers.get("preference", 0.0) < GATE_AT:
                return False
            suggestion = await propose_from_message(self._provider, self._config, text, task_id=task_id)
            if suggestion is None:
                return False
            shown, keyboard = card(suggestion)
            await self._say(routing, shown, keyboard)
            return True
        except Exception:  # noqa: BLE001
            logger.info("preference_watch_failed", exc_info=True)
            return False
