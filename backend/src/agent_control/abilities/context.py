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

from collections.abc import Awaitable, Callable, Iterable

import structlog

from agent_control.abilities.index import AbilityIndex
from agent_control.abilities.model import Ability, AbilityKind, AbilityState

logger = structlog.get_logger(__name__)

#: A record shares at least this much with the request to be asked about. One common word scores about 2 to 4.
SHORTLIST_SCORE = 5.0
#: A capability that could be set up is asked about on a single shared word: its yes/no question decides, and a request phrased in words the
#: record does not list ("read that out loud" for a voice) should still reach it. Only a few records are in this state, so few questions are added.
INSTALLABLE_SHORTLIST_SCORE = 2.0
#: A record whose words match this strongly is shown without a question: two or three specific words ("order", "pizza").
CLEAR_SCORE = 10.0
MAX_SHOWN = 3
#: Records of a kind the prompt already lists are not repeated, whatever their state: the tool catalog names every tool, on or off, the
#: skills section every skill, and the MCP section the first MCP tools. A coding agent that is ready is the `coding.agent` tool.
_ALREADY_LISTED = frozenset({AbilityKind.TOOL, AbilityKind.SKILL, AbilityKind.HELPER, AbilityKind.MCP_TOOL})
#: A skill is only listed by name in the prompt, and a small model does not always go and read it. One that says when it applies (`when:` in
#: its frontmatter) is put in front of the Operator, whole, when that question is answered yes for the request: never on words alone, because
#: a skill about reports matches every request that mentions one.
_PUSHED_WHEN_CONFIRMED = frozenset({AbilityKind.SKILL, AbilityKind.HELPER})

#: Asks yes/no questions about one request and returns the probability of yes for each name answered. Raises when it cannot be reached.
Confirmer = Callable[[str, dict[str, str]], Awaitable[dict[str, float]]]


async def relevant(
    index: AbilityIndex, request: str, *, confirm: Confirmer | None = None, limit: int = MAX_SHOWN, shortlist: float | None = None
) -> list[Ability]:
    """`shortlist` replaces the score a record needs to be asked about: an index of a few records scores low across the board, so a caller
    that searches only those, and lets the question decide, passes a score just above zero."""
    candidates = [
        (ability, score)
        for ability, score in index.search(request, limit=limit * 6)
        if score >= (
            shortlist if shortlist is not None
            else INSTALLABLE_SHORTLIST_SCORE if ability.kind == AbilityKind.MODEL and ability.confirm else SHORTLIST_SCORE
        )
        and (ability.kind not in _ALREADY_LISTED or (ability.kind in _PUSHED_WHEN_CONFIRMED and ability.confirm))
        and not (ability.kind == AbilityKind.AGENT and ability.state == AbilityState.READY)
    ]
    # A skill that sends a request to the planner (route: plan) is asked about for every request, whatever words it shares. "I'm moving on
    # December 20. Plan it." and "Build me a robot." share one common word with it, which the shortlist above drops, and the first challenge
    # run showed what that cost: with no plan, the Operator asked a questionnaire for one and retried for ten minutes on the other. The yes/no
    # question, not the words, decides, and it is asked in the same single call as every other question.
    # The same for a skill that says `ask: always` ("what can't you do?" shares no content word with the skill that answers it).
    listed = {ability.id for ability, _ in candidates}
    candidates += [
        (ability, 0.0) for ability in index.all() if (ability.route or ability.ask_always) and ability.confirm and ability.id not in listed
    ]
    unclear = [ability for ability, score in candidates if ability.confirm and (score < CLEAR_SCORE or ability.kind in _PUSHED_WHEN_CONFIRMED)]
    answers: dict[str, float] = {}
    if unclear and confirm is not None:
        try:
            answers = await confirm(request, {ability.id: ability.confirm for ability in unclear})
        except Exception:  # noqa: BLE001 - an unreachable decision model leaves only the strong matches
            logger.info("abilities_confirm_unavailable", exc_info=True)
    shown = []
    for ability, score in candidates:
        asked = ability.confirm and answers.get(ability.id, 0.0) >= ability.confirm_at
        if asked or (score >= CLEAR_SCORE and ability.kind not in _PUSHED_WHEN_CONFIRMED):
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
        if ability.kind in _PUSHED_WHEN_CONFIRMED:
            lines.append(f"- Follow this procedure, {ability.name}: {ability.summary}\n{ability.detail}")
            continue
        lines.append(f"- {ability.name} ({state}): {ability.summary}")
        if ability.guidance:
            lines.append(f"  {ability.guidance}")
        elif ability.call:
            lines.append(f"  use: {ability.call}")
    return "\n".join(lines)


async def asks_for_what_a_recipe_covers(abilities: Iterable[Ability], request: str, *, confirm: Confirmer | None) -> bool:
    """Whether a message that was taken for chat asks for something a recipe covers (a voice, speech recognition, a video tool), set up or not.

    "Read that to me out loud" is chat to a classifier, and a chat reply knows nothing of recipes: it said "I cannot speak" and offered nothing,
    though a local voice is a 120 MB download away, and once the voice was installed it said "that needs a task, send it again". A message the
    same search and the same yes/no question find a recipe for is a task instead, so the Operator is told what could be set up, or how to use
    what is. No decision model, no answer.
    """
    recipes = [ability for ability in abilities if ability.state in (AbilityState.INSTALLABLE, AbilityState.READY)]
    if not recipes or confirm is None:
        return False
    return bool(await relevant(AbilityIndex(recipes), request, confirm=confirm, shortlist=0.01))


#: Asked about a message taken for chat. Measured on the running decision model: 9 of 10 ways of asking about the assistant clear 0.8 and the tenth
#: ("what can you do now that you couldn't do last week") scores 0.56; ten ordinary messages that share its words score at most 0.26, and none of
#: the 178 messages in the e2e suites that chat would have taken reaches 0.5.
ABOUT_THE_ASSISTANT = "Is the user asking what this assistant can do, cannot do, is missing, or needs from them to be more useful?"
ABOUT_THE_ASSISTANT_AT = 0.5


async def asks_about_the_assistant(request: str, *, confirm: Confirmer | None) -> bool:
    """Whether a message taken for chat is a question about the assistant itself: what it can do, cannot do, is missing or needs.

    The chat reply is composed with the classification, from a prompt that knows nothing of the ability index: asked "what can't you do?" it
    listed what it can do, and asked what is new it said commands run "without needing explicit approval". Such a question is a task instead,
    so the Operator can read the index (`ability.search` overview) and answer from it. No decision model, no answer.
    """
    if confirm is None:
        return False
    try:
        answers = await confirm(request, {"about_the_assistant": ABOUT_THE_ASSISTANT})
    except Exception:  # noqa: BLE001 - help, not a gate: the message stays chat
        logger.info("about_the_assistant_unavailable", exc_info=True)
        return False
    return answers.get("about_the_assistant", 0.0) >= ABOUT_THE_ASSISTANT_AT
