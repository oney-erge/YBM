"""The abilities that bear on a request and that the Operator cannot already see, as a few lines for its prompt.

The Operator's prompt lists the tools (on or off), the skills, and the first MCP tools. It does not say that the thing asked for cannot be
done here, that a saved workflow does exactly this, that a local model could be loaded to do it, or that a tool needs an account first.
Those are the records that decide whether a task should start at all, so they are found from the request before the first step and put in
front of the model once. A request that matches none of them adds nothing, so most prompts are unchanged.

Two stages, because words alone cannot tell "pay my electricity bill" from "remind me to pay the rent":

1. Words shortlist. Records that share words with the request (abilities/index.py) and that the prompt does not already list.
2. A question confirms. A record that carries a yes/no question (`Ability.confirm`) is asked of the decision model, in one call for all the
   candidates, and shown when the answer is yes. When the decision model cannot be reached, only a very strong word match is shown: a note
   that is missing is better than one that is wrong, and the tools' own approvals still stand between a request and any real action.

What is shown says what exists and what it would take. The model still chooses.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import structlog

from agent_control.abilities.index import AbilityIndex
from agent_control.abilities.model import Ability, AbilityKind, AbilityState

logger = structlog.get_logger(__name__)

#: A record shares at least this much with the request to be asked about. One common word scores about 2 to 4.
SHORTLIST_SCORE = 5.0
#: A record whose words match this strongly is shown without a question: two or three specific words ("order", "pizza").
CLEAR_SCORE = 10.0
MAX_SHOWN = 3
#: Records of a kind the prompt already lists are not repeated, whatever their state: the tool catalog names every tool, on or off, the
#: skills section every skill, and the MCP section the first MCP tools. A coding agent that is ready is the `coding.agent` tool.
_ALREADY_LISTED = frozenset({AbilityKind.TOOL, AbilityKind.SKILL, AbilityKind.HELPER, AbilityKind.MCP_TOOL})

#: Asks yes/no questions about one request and returns the probability of yes for each name answered. Raises when it cannot be reached.
Confirmer = Callable[[str, dict[str, str]], Awaitable[dict[str, float]]]


async def relevant(
    index: AbilityIndex, request: str, *, confirm: Confirmer | None = None, limit: int = MAX_SHOWN
) -> list[Ability]:
    candidates = [
        (ability, score)
        for ability, score in index.search(request, limit=limit * 4)
        if score >= SHORTLIST_SCORE
        and ability.kind not in _ALREADY_LISTED
        and not (ability.kind == AbilityKind.AGENT and ability.state == AbilityState.READY)
    ]
    unclear = [ability for ability, score in candidates if score < CLEAR_SCORE and ability.confirm]
    answers: dict[str, float] = {}
    if unclear and confirm is not None:
        try:
            answers = await confirm(request, {ability.id: ability.confirm for ability in unclear})
        except Exception:  # noqa: BLE001 - an unreachable decision model leaves only the strong matches
            logger.info("abilities_confirm_unavailable", exc_info=True)
    shown = []
    for ability, score in candidates:
        if score >= CLEAR_SCORE or (ability.confirm and answers.get(ability.id, 0.0) >= ability.confirm_at):
            shown.append(ability)
        if len(shown) == limit:
            break
    return shown


def render(abilities: list[Ability]) -> str:
    """The prompt block: empty when there is nothing to say, so that a prompt for an ordinary request does not change."""
    if not abilities:
        return ""
    lines = ["\n\n## What bears on this request that is not in your tool list as ready"]
    for ability in abilities:
        state = {
            AbilityState.UNAVAILABLE: "cannot be done by this assistant",
            AbilityState.INSTALLABLE: "can be loaded or installed with the user's approval",
            AbilityState.NEEDS_ACCESS: "needs an account, key or login from the user",
            AbilityState.DISABLED: "switched off; the user can turn it on in Access",
            AbilityState.READY: "ready",
        }[ability.state]
        lines.append(f"- {ability.name} ({state}): {ability.summary}")
        if ability.guidance:
            lines.append(f"  {ability.guidance}")
        elif ability.call:
            lines.append(f"  use: {ability.call}")
    return "\n".join(lines)
