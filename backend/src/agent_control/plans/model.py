"""A plan: a goal too big for one task, as steps that each have an owner.

"Build me a drone with a small language model in it" is not a task. It is a project: some steps the assistant does (research, a parts list,
the code), some only the user can do (buy the parts, assemble them), and some need something from the user first (an account, a decision)
before the assistant can go on. A task runs the Operator loop for twelve steps and ends; a plan outlives tasks, survives a restart, and
waits for people. Each step that is the assistant's own becomes an ordinary task; each step that is the user's becomes a question the user
answers when it is done. Nothing here runs anything: plans/runner.py moves a plan forward, and the tools, approvals and checks of every
task a plan starts are the ones every task has.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import Field, field_validator

from agent_control.schemas import StrictBaseModel, new_id, utc_now


class PlanStatus(StrEnum):
    PROPOSED = "proposed"  # drafted, shown to the user, not started: nothing runs until they say so
    ACTIVE = "active"
    PAUSED = "paused"  # a step failed or the user paused it; resumes on their word
    DONE = "done"
    CANCELLED = "cancelled"


class StepOwner(StrEnum):
    YBM = "ybm"  # the assistant does it, as a task
    USER = "user"  # only the user can: buy, assemble, decide, sign, test
    ACCESS = "access"  # the assistant could, once the user gives it something (an account, a key, a login)


class StepStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"  # a task for it is in flight
    WAITING = "waiting"  # a question to the user is open
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


#: A plan that has no more than this many steps can be read on a phone. A longer one is a project of projects: split it.
MAX_STEPS = 20


class PlanStep(StrictBaseModel):
    id: str = Field(default_factory=lambda: new_id("pstep"))
    plan_id: str = ""
    #: 1-based order. A step runs after the steps before it unless `depends_on` names others.
    position: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=160)
    detail: str = Field(default="", max_length=2000)
    owner: StepOwner = StepOwner.YBM
    #: What the user has to give or do first, in words ("an Amazon account", "the budget"). For ACCESS and USER steps this is the question.
    needs: list[str] = Field(default_factory=list)
    #: Positions of the steps that must be done first. Empty means "the one before it".
    depends_on: list[int] = Field(default_factory=list)
    #: The ability (abilities/) this step is expected to use, when the planner knew one. A hint to the task, not a constraint.
    ability: str = ""
    status: StepStatus = StepStatus.PENDING
    task_id: str | None = None
    result: str = ""
    updated_at: datetime = Field(default_factory=utc_now)

    @field_validator("title", "detail", mode="before")
    @classmethod
    def _clean(cls, value: object) -> object:
        return " ".join(value.split()) if isinstance(value, str) else value


class Plan(StrictBaseModel):
    id: str = Field(default_factory=lambda: new_id("plan"))
    conversation_id: str | None = None
    goal: str = Field(min_length=1, max_length=500)
    #: One or two sentences: what done looks like and the shape of the route there.
    summary: str = Field(default="", max_length=1200)
    status: PlanStatus = PlanStatus.PROPOSED
    #: The task that drafted it, so the card can be attached to that task's answer.
    source_task_id: str | None = None
    #: Where the plan's tasks and messages go: source_channel, source_chat_id, source_sender_id, copied from the task that drafted it so a
    #: plan still knows its chat after that task is cleared from the history.
    routing: dict[str, str] = Field(default_factory=dict)
    steps: list[PlanStep] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    def step(self, position: int) -> PlanStep | None:
        return next((step for step in self.steps if step.position == position), None)

    def dependencies_of(self, step: PlanStep) -> list[int]:
        """Positions that must be finished before `step`: those it names, or the step just before it."""
        if step.depends_on:
            return list(step.depends_on)
        return [step.position - 1] if step.position > 1 else []

    def ready_steps(self) -> list[PlanStep]:
        """Pending steps whose dependencies are done or skipped, in order. A skipped step does not hold up what follows it."""
        finished = {step.position for step in self.steps if step.status in (StepStatus.DONE, StepStatus.SKIPPED)}
        return [
            step
            for step in sorted(self.steps, key=lambda s: s.position)
            if step.status == StepStatus.PENDING and all(dep in finished for dep in self.dependencies_of(step))
        ]

    def finished(self) -> bool:
        return bool(self.steps) and all(step.status in (StepStatus.DONE, StepStatus.SKIPPED) for step in self.steps)
