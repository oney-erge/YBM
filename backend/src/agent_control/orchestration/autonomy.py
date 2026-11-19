"""How much the assistant does without asking (three levels), and the one thing it never does alone: spend the owner's money.

Levels (`operator.autonomy_level`):

1. Ask: asks before meaningful implementation choices.
2. Balanced (the default): chooses routine details, asks when a missing fact blocks the work.
3. Autonomous: does not ask. It works out what is meant, says what it assumed, chooses methods and recovery routes, and the routine
   approvals a tool call would wait for are granted by the assistant itself and recorded as such.

Two things wait for the owner at every level: a payment or purchase (`payment_reason`), and any call whose risk is critical (an
unrecoverable change). Neither the level nor a grant ("Allow for this task") covers them.
"""

from __future__ import annotations

import json
import re
from typing import Any, Mapping

from agent_control.schemas import RiskLevel, ToolCallRequest

ASK, BALANCED, AUTONOMOUS = 1, 2, 3

#: The old five-level `initiative_level` as a new level: 1-2 asked, 3 was balanced, 4-5 chose for themselves.
_FROM_LEGACY = {1: ASK, 2: ASK, 3: BALANCED, 4: AUTONOMOUS, 5: AUTONOMOUS}


def level_from_legacy(value: int) -> int:
    return _FROM_LEGACY.get(int(value), BALANCED)


def autonomy_of(metadata: Mapping[str, Any] | None) -> int:
    """The task's autonomy level (1-3); a task made before the levels were three keeps what it meant."""
    value = (metadata or {}).get("initiative_level")
    if isinstance(value, bool) or not isinstance(value, int):
        return BALANCED
    return value if 1 <= value <= 3 else level_from_legacy(value)


# --- money -------------------------------------------------------------------------------------------------------------------------------

#: Words on a button, a link or in a goal that spend money or commit to paying. A guard, not the whole defence: the purchase skill says the same
#: and the model is told it first. It is deliberately broad; a false match costs one tap, a miss costs an order.
_PAYMENT_WORDS = re.compile(
    r"\b(?:place\s+(?:your\s+|my\s+|the\s+)?order|pay\s+now|pay\s+with|buy\s+now|buy\s+it|complete\s+(?:the\s+|my\s+|your\s+)?(?:purchase|order|checkout|payment)"
    r"|confirm\s+(?:the\s+|my\s+|your\s+)?(?:purchase|order|payment)|submit\s+(?:the\s+|my\s+|your\s+)?(?:order|payment)|checkout|check\s+out"
    r"|purchase|subscribe|start\s+(?:my\s+|your\s+|a\s+)?(?:free\s+)?trial|add\s+(?:a\s+)?payment|add\s+(?:a\s+)?card|order\s+(?:it|now|this|that|me)\b)",
    re.IGNORECASE,
)
#: Field names that only a payment form has.
_CARD_FIELDS = re.compile(r"(?:card[\s_-]?number|cc[\s_-]?num|cvv|cvc|security[\s_-]?code|expir(?:y|ation)|credit[\s_-]?card|debit[\s_-]?card)", re.IGNORECASE)
#: Operations that only look (nothing is clicked, typed or submitted): never a payment.
_LOOKING = frozenset({"open", "search", "research", "inspect_tabs", "screenshot", "summarize_page", "research_pages", "observe", "extract_page_state", "navigate", "read"})
_ACTING_TOOLS = frozenset({"browser.control", "computer.use", "desktop.control"})


def payment_reason(request: ToolCallRequest) -> str | None:
    """Why this call would spend money, or None. Such a call is an approval-only action at every autonomy level."""
    return payment_in(request.tool_name, request.input)


def payment_in(tool_name: str, tool_input: Mapping[str, Any] | None) -> str | None:
    """The same test on a tool name and its input, so the record of a finished step can be read the way the request was."""
    if tool_name not in _ACTING_TOOLS or not isinstance(tool_input, Mapping):
        return None
    operation = str(tool_input.get("operation") or "")
    if operation in _LOOKING:
        return None
    try:
        text = json.dumps(tool_input, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(tool_input)
    found = _CARD_FIELDS.search(text)
    if found:
        return f"it fills in payment details ({found.group(0).strip()})"
    found = _PAYMENT_WORDS.search(text)
    if found:
        return f"it would {found.group(0).strip().lower()}"
    return None


# --- approvals the assistant grants itself ----------------------------------------------------------------------------------------------

def grants_itself(metadata: Mapping[str, Any] | None, request: ToolCallRequest) -> bool:
    """Whether the assistant approves this call itself: only at the autonomous level, never for money, never for a critical call.

    Policy has already decided that the capability is enabled and the call is inside its scope; this only skips the wait for a person.
    """
    if autonomy_of(metadata) < AUTONOMOUS:
        return False
    if payment_reason(request) is not None:
        return False
    return request.risk_level != RiskLevel.CRITICAL
