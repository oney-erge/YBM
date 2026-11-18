"""A message routed to a long job (`route: job`) is turned into the call it asks for (start, status, pause, resume, stop), written in code, not picked by the model.

Found live on 11/14: the skill for in-depth research was in the Operator's prompt, whole, and the 9B still chose `delegate` with web.search,
spent six steps on it and offered to hand the request to Claude Code. A choice among two dozen tools is the one place a small model is least
reliable, and "start the job" is not a choice once the decision model has said the request is that. So, as with plans (plans/stage.py), a
different call does the part that has to be right: one model call, with its own short instructions, writes the goal and the number of pages
the person named; the worker then makes `job.manage start_research` the task's first step. It goes through the same approval, audit and
checks as any call the Operator makes, and the Operator goes on from its result: it writes the answer that says the job is running.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

#: The tool contract permits up to 1000 pages; the configured limit is checked separately.
MIN_PAGES, MAX_PAGES = 5, 1000

SYSTEM = """You prepare a message about research for a program that reads many web pages and writes a report. You are given what the person wrote.

- `action`: what they want. "investigate" when they describe something that does not work on their computer, network or a device (a TV that will not take a cast, a printer that stopped, a Wi-Fi that drops) and want it found out or fixed; their `goal` is then the problem in their own words with what they tried. "start" when they ask for research on a topic to be done. "status" when they ask how far it is, whether the report is ready, or what it has found so far. "pause", "resume" or "stop" when they say so about research that is already under way. "other" when the message is about something else.
- `goal` (for "start" only): what they want found out, as one clear request with everything they said that matters (the topic, the place, the budget, what they care about, what they will use it for). Leave out how to work: how many sources, "take your time", "send me a report", "tell me when it is done". Do not add anything they did not say.
- `pages` (for "start" only): how many sources or pages they asked to read, as a number ("about twelve sources" is 12, "a hundred websites" is 100). Leave it empty when they gave no number."""


class _Start(BaseModel):
    action: Literal["start", "investigate", "status", "pause", "resume", "stop", "other"] = Field(description="what the person wants done with research or with a problem to find out")
    goal: str = Field(default="", description="for start: what to find out, with everything the person said that matters, and nothing about how to work")
    pages: int | None = Field(default=None, description="for start: how many sources or pages the person asked to read; empty when they gave no number")


class JobIntentUnavailable(RuntimeError):
    """The research action could not be determined safely."""


class JobPageLimitExceeded(ValueError):
    """The requested research size is outside the supported range."""


async def start_research_call(provider: Any, request: str) -> dict[str, Any] | None:
    """The input of the `job.manage` call the message asks for (start, status, pause, resume or stop), or None when it is about something else.

    Found live: the skill's yes/no question cannot tell a request for research from a question about research already running ("Pause the research."
    scored 0.09 and "Resume the research." 0.90), and a small model that is asked to pick the control tool calls it with an id it made up, or says it
    cannot. So the call that writes the goal says which of the five it is, and the worker makes that the task's first step. If the action cannot
    be determined, refuse to guess: a pause or stop must not turn into a new job."""
    words = " ".join((request or "").split())[:2000]
    if provider is None or not words:
        raise JobIntentUnavailable("Research action is unavailable")
    try:
        answer = await provider.generate_structured(SYSTEM, f"The person wrote:\n{words}", _Start, temperature=0.0)
        if not isinstance(answer, _Start):
            raise TypeError("Research action was not structured")
        action = answer.action
    except Exception as exc:  # noqa: BLE001 - the action is unknown, so do not choose a side effect
        logger.warning("job_start_extraction_failed: %s", type(exc).__name__)
        raise JobIntentUnavailable("Research action is unavailable") from exc
    if action == "other":
        return None
    if action == "investigate":
        return {"operation": "start_investigation", "goal": " ".join((answer.goal or "").split())[:2000] or words}
    if action != "start":
        return {"operation": action}
    goal = " ".join((answer.goal or "").split())[:2000] or words
    pages = answer.pages
    if pages is not None and not MIN_PAGES <= pages <= MAX_PAGES:
        raise JobPageLimitExceeded(f"Research supports {MIN_PAGES} to {MAX_PAGES} pages per job; requested {pages}.")
    call: dict[str, Any] = {"operation": "start_research", "goal": goal}
    if pages is not None:
        call["max_pages"] = pages
    return call
