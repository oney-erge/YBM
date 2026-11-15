"""A request a skill routes to a long job (`route: job`) starts it with a first call written in code, not with whichever tool the model picks.

Found live on 11/14: the skill for in-depth research was in the Operator's prompt, whole, and the 9B still chose `delegate` with web.search,
spent six steps on it and offered to hand the request to Claude Code. A choice among two dozen tools is the one place a small model is least
reliable, and "start the job" is not a choice once the decision model has said the request is that. So, as with plans (plans/stage.py), a
different call does the part that has to be right: one model call, with its own short instructions, writes the goal and the number of pages
the person named; the worker then makes `job.manage start_research` the task's first step. It goes through the same approval, audit and
checks as any call the Operator makes, and the Operator goes on from its result: it writes the answer that says the job is running.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

#: `job.manage` accepts 5 to 150 pages (tools/contracts.py JobManageInput); a number outside it is brought inside rather than refused.
MIN_PAGES, MAX_PAGES = 5, 150

SYSTEM = """You prepare a research request for a program that will read many web pages and write a report. You are given what the person wrote.

- `goal`: what they want found out, as one clear request with everything they said that matters (the topic, the place, the budget, what they care about, what they will use it for). Leave out how to work: how many sources, "take your time", "send me a report", "tell me when it is done". Do not add anything they did not say.
- `pages`: how many sources or pages they asked to read, as a number ("about twelve sources" is 12, "a hundred websites" is 100). Leave it empty when they gave no number.
- `new_research`: true when they ask for research on a topic to be done. False when the message is about research that is already under way: how far it is ("how far along is the research?", "is the report ready?"), pausing it, resuming it, stopping it, or what it has found so far."""


class _Start(BaseModel):
    goal: str = Field(default="", description="what to find out, with everything the person said that matters, and nothing about how to work")
    pages: int | None = Field(default=None, description="how many sources or pages the person asked to read; empty when they gave no number")
    new_research: bool = Field(default=True, description="true when they ask for new research; false when they ask about, pause, resume or stop research already running")


async def start_research_call(provider: Any, request: str) -> dict[str, Any] | None:
    """The input of the `job.manage` call that starts the research the person asked for, or None when the message is about research already running.

    Found live on 11/14: "How far along is the research?" was taken for a request and started a second job whose goal was that sentence. The skill's
    yes/no question cannot tell the two apart (0.61 for the status question, 0.90 for "Resume the research."), so the call that writes the goal says
    which it is. Never raises: their own words are the goal when the model fails, and a failure is taken for a request, as before."""
    words = " ".join((request or "").split())[:2000]
    goal, pages = words, None
    if provider is not None and words:
        try:
            answer = await provider.generate_structured(SYSTEM, f"The person wrote:\n{words}", _Start, temperature=0.0)
        except Exception:  # noqa: BLE001 - their own words are a fine goal; the call must still be made
            logger.warning("job_start_extraction_failed", exc_info=True)
        else:
            if not answer.new_research:
                return None
            goal = " ".join((answer.goal or "").split())[:2000] or words
            if answer.pages:
                pages = min(MAX_PAGES, max(MIN_PAGES, int(answer.pages)))
    call: dict[str, Any] = {"operation": "start_research", "goal": goal}
    if pages is not None:
        call["max_pages"] = pages
    return call
