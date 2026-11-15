"""The planner: one model call, with its own instructions, that turns a goal into steps with owners, or asks the one question it cannot do without.

The Operator is good at choosing the next tool and poor at writing a plan while it does: asked to build a drone it asked a questionnaire, then
delegated, then ran out of steps without ever writing one. Planning is a different job with a different prompt, so it is a different call.
A request that a skill marks as a project (`route: plan`, plans/stage.py) goes to the planner instead of the Operator loop, and the Operator
can also call it through `plan.manage`. The planner drafts the steps from what the user said, what the assistant can really do (the tools that
are on) and what it cannot (known gaps, such as paying); the result is checked like any plan (tools/plan_manage.py).
"""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SYSTEM_PROMPT = """You plan projects for a personal assistant that lives on the user's computer. You are given a goal, what the user has said
about it, and what the assistant can and cannot do. Write a plan the user can start.

Rules:
- 4 to 10 steps, in order, one thing per step, in plain words ("Compare three flight controllers", not "Research").
- Every step has an owner:
  ybm    = the assistant does it on the computer: research, comparing options, writing code or documents, making lists, drafting messages.
  user   = only the user can: buying, assembling, testing, signing, choosing between options, anything physical or that costs money.
  access = the assistant could do it once the user gives it something (an account, a key, a login). Put what it needs in `needs`.
- Finding things out is the assistant's step (ybm), even when the user decides afterwards: rules and permits, prices, options, who could do the work, what to ask them. The user's steps are what only a person can do: deciding, visiting, meeting, signing, paying, collecting. `needs` is for the user's side only, in plain words (a budget, an account); never the name of a tool.
- The assistant never pays and never places an order. Plan it as two steps: the assistant makes the shopping list with links, then the user buys.
- `summary`: one or two sentences: what done looks like, and the assumptions you made, so the user can correct the plan.
- `depends_on`: positions of earlier steps that must be done first. Leave it empty when a step simply follows the one before it.
- Use only what the assistant can do. If a step needs something it cannot do, make it a user step.
- The steps are about the goal in the real world. Do not invent chores on the computer (making folders, files or spreadsheets) unless the goal is about the computer.
- `due`: the day the step should be done or started, written YYYY-MM-DD, on every step when the goal has a date or a stretch of time (a move on December 20, a party on January 10, a house next year, a skill in three months). Leave it empty when the goal has no time in it. The first steps are this week or next; never a day before today; with a fixed date work backwards from it, with a stretch of time spread the steps across it. Do not put the day in the title. List the steps in the order of their days.
- A goal that takes months (a house, a business, a degree) is a plan of phases, each one a real thing to do ("Find out what the local rules and permits are", "Choose and hire the builder"), with the first steps small and the later ones just specific enough to start. Never state a price, a law or a permit rule as a fact: a step for the assistant finds it out from sources, and the user decides."""

MAY_ASK = (
    "If you cannot tell what the goal is at all (not just a detail), do not write steps: set `question` to ONE short question and `options` to "
    "two to four short answers the user could tap, and leave `steps` empty. If you can tell what it is, assume sensible details and write the plan."
)
MAY_NOT_ASK = "Do not ask questions. Where something is unknown, assume something sensible and say so in `summary`."


def _require_a_day(schema: dict[str, Any]) -> None:
    """A field the model may leave out, it leaves out: with "due" only optional, one live house plan had a day on 1 step of 9 and another, drafted from the same goal, on all of them.
    Required in the schema the model is held to, a day or an empty string is written for every step; a draft without one is still accepted in code."""
    schema["required"] = [*schema.get("required", []), "due"]


class StepDraft(BaseModel):
    model_config = ConfigDict(json_schema_extra=_require_a_day)

    title: str = Field(min_length=1, max_length=160)
    detail: str = Field(default="", max_length=600)
    owner: Literal["ybm", "user", "access"] = "ybm"
    needs: list[str] = Field(default_factory=list)
    depends_on: list[int] = Field(default_factory=list)
    due: str = Field(default="", max_length=20)


class PlanDraft(BaseModel):
    summary: str = Field(default="", max_length=1200)
    steps: list[StepDraft] = Field(default_factory=list, max_length=20)
    question: str = Field(default="", max_length=300)
    options: list[str] = Field(default_factory=list, max_length=4)

    @model_validator(mode="after")
    def steps_or_a_question(self) -> "PlanDraft":
        if not self.steps and not self.question.strip():
            raise ValueError("a draft has steps or a question")
        return self


def can_do_text(catalog: Any) -> str:
    """What the assistant can do now (the tools that are on) and cannot do at all (known gaps), for the planner to read."""
    if catalog is None:
        return ""
    lines = []
    for ability in catalog.index().all():
        if ability.kind.value == "tool" and ability.state.value == "ready":
            lines.append(f"- can: {ability.name}: {ability.summary}")
        elif ability.kind.value == "gap":
            lines.append(f"- cannot: {ability.name}. {ability.guidance}")
    return "\n".join(lines)


def user_prompt(goal: str, context: str, can_do: str, *, may_ask: bool, today: date | None = None) -> str:
    parts = [f"Today is {(today or date.today()).strftime('%A, %B %d, %Y')}.", f"Goal: {goal.strip()}"]
    if context.strip():
        parts.append(f"What the user has said:\n{context.strip()[:2500]}")
    if can_do.strip():
        parts.append(f"What the assistant can and cannot do:\n{can_do.strip()[:3000]}")
    parts.append(MAY_ASK if may_ask else MAY_NOT_ASK)
    return "\n\n".join(parts)


async def draft_plan(provider: Any, goal: str, context: str, can_do: str, *, may_ask: bool = False) -> PlanDraft:
    """Ask the model for the steps (or, when allowed, for the one question). Raises when it cannot be reached or does not answer with a draft."""
    return await provider.generate_structured(
        SYSTEM_PROMPT, user_prompt(goal, context, can_do, may_ask=may_ask), PlanDraft, temperature=0.2
    )
