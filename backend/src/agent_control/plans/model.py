"""A plan: a goal too big for one task, as steps that each have an owner.

"Build me a drone with a small language model in it" is not a task. It is a project: some steps the assistant does (research, a parts list,
the code), some only the user can do (buy the parts, assemble them), and some need something from the user first (an account, a decision)
before the assistant can go on. A task runs the Operator loop for twelve steps and ends; a plan outlives tasks, survives a restart, and
waits for people. Each step that is the assistant's own becomes an ordinary task; each step that is the user's becomes a question the user
answers when it is done. Nothing here runs anything: plans/runner.py moves a plan forward, and the tools, approvals and checks of every
task a plan starts are the ones every task has.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
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
#: A step with a date is not started more than this many days before it: a plan that spans months waits, active, for each date to come near.
HOLD_DAYS = 7
#: The longest stretch between check-ins a person can ask for, and the farthest date a step may have.
MAX_CHECK_IN_DAYS = 60
#: A plan that runs at least this many days is offered a check-in, once, when it is shown.
CHECK_IN_OFFER_DAYS = 21
MAX_DUE_YEARS = 5


class PlanStep(StrictBaseModel):
    id: str = Field(default_factory=lambda: new_id("pstep"))
    plan_id: str = ""
    #: 1-based order. A step runs after the steps before it unless `depends_on` names others.
    position: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=160)
    detail: str = Field(default="", max_length=2000)
    owner: StepOwner = StepOwner.YBM
    #: True only when choosing an option is the entire step; older steps default to unknown.
    choice_only: bool = False
    #: What the user has to give or do first, in words ("an Amazon account", "the budget"). For ACCESS and USER steps this is the question.
    needs: list[str] = Field(default_factory=list)
    #: Positions of the steps that must be done first. Empty means "the one before it".
    depends_on: list[int] = Field(default_factory=list)
    #: The ability (abilities/) this step is expected to use, when the planner knew one. A hint to the task, not a constraint.
    ability: str = ""
    #: The day the step should be done or started, when the goal has a date or a stretch of time. Steps are kept in the order of these days.
    due: date | None = None
    status: StepStatus = StepStatus.PENDING
    task_id: str | None = None
    result: str = ""
    revision: int = Field(default=0, ge=0)
    updated_at: datetime = Field(default_factory=utc_now)

    @field_validator("title", "detail", mode="before")
    @classmethod
    def _clean(cls, value: object) -> object:
        return " ".join(value.split()) if isinstance(value, str) else value

    @field_validator("due", mode="before")
    @classmethod
    def _a_day(cls, value: object) -> object:
        """A day written as YYYY-MM-DD. Anything else (empty, a phrase, a date that does not exist) is no date at all, not an error."""
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, str):
            try:
                return date.fromisoformat(value.strip()[:10])
            except ValueError:
                return None
        return value


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
    #: What the user has told the assistant about this project (the plot, the budget, a decision), one line each, kept with the plan so every
    #: step's task is told it and a later one does not ask again.
    notes: str = Field(default="", max_length=2000)
    #: Tell the user how the plan stands every this many days while it is active; 0 is never. The user asks for it, nothing sets it by itself.
    check_in_days: int = Field(default=0, ge=0, le=MAX_CHECK_IN_DAYS)
    last_check_in_at: datetime | None = None
    revision: int = Field(default=0, ge=0)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    def step(self, position: int) -> PlanStep | None:
        return next((step for step in self.steps if step.position == position), None)

    def dependencies_of(self, step: PlanStep) -> list[int]:
        """Positions that must be finished before `step`: those it names, or the step just before it."""
        if step.depends_on:
            return list(step.depends_on)
        return [step.position - 1] if step.position > 1 else []

    def _unblocked(self) -> list[PlanStep]:
        finished = {step.position for step in self.steps if step.status in (StepStatus.DONE, StepStatus.SKIPPED)}
        done = {step.position for step in self.steps if step.status == StepStatus.DONE}
        return [
            step
            for step in sorted(self.steps, key=lambda s: s.position)
            if step.status == StepStatus.PENDING
            and all(dep in (done if step.depends_on else finished) for dep in self.dependencies_of(step))
        ]

    def skipped_prerequisite(self) -> tuple[PlanStep, PlanStep] | None:
        """Find an explicit prerequisite that was skipped rather than completed."""
        for step in sorted(self.steps, key=lambda item: item.position):
            if step.status != StepStatus.PENDING:
                continue
            for position in step.depends_on:
                dependency = self.step(position)
                if dependency is not None and dependency.status == StepStatus.SKIPPED:
                    return step, dependency
        return None

    def ready_steps(self, today: date | None = None) -> list[PlanStep]:
        """Pending steps whose prerequisites are met and whose day is near."""
        horizon = (today or date.today()) + timedelta(days=HOLD_DAYS)
        return [step for step in self._unblocked() if step.due is None or step.due <= horizon]

    def weeks_ahead(self, today: date | None = None) -> int:
        """How many whole weeks until the last day on the plan: 0 for a plan with no days or one that ends within three weeks."""
        today = today or date.today()
        days = max((step.due for step in self.steps if step.due is not None), default=today) - today
        return days.days // 7 if days.days >= CHECK_IN_OFFER_DAYS else 0

    def next_held(self, today: date | None = None) -> PlanStep | None:
        """The step that could go on now but is waiting for its day, the earliest one: what "next" means to a person who asks."""
        ready = {step.id for step in self.ready_steps(today)}
        held = [step for step in self._unblocked() if step.id not in ready and step.due is not None]
        return min(held, key=lambda step: (step.due, step.position), default=None)

    def finished(self) -> bool:
        return bool(self.steps) and all(step.status in (StepStatus.DONE, StepStatus.SKIPPED) for step in self.steps)


def in_date_order(steps: list[PlanStep], today: date | None = None) -> list[PlanStep]:
    """Steps in the order of their days, numbered 1..n, with `depends_on` following the steps it named.

    A day before today becomes today and a day too far away is dropped. A step without a day sorts with the step before it, so the steps a
    drafter wrote without dates stay where they were; steps are only moved by a day they were given. A dependency that, after the move,
    points at the step itself or a later one is dropped: the order of the days is the order of the work.
    """
    today = today or date.today()
    farthest = today + timedelta(days=366 * MAX_DUE_YEARS)
    cleaned: list[PlanStep] = []
    for step in steps:
        due = step.due
        if due is not None and due < today:
            due = today
        if due is not None and due > farthest:
            due = None
        cleaned.append(step.model_copy(update={"due": due}))
    keys: list[date] = []
    for step in cleaned:
        keys.append(step.due or (keys[-1] if keys else today))
    order = sorted(range(len(cleaned)), key=lambda index: (keys[index], index))
    new_position = {cleaned[index].position: place for place, index in enumerate(order, 1)}
    result = []
    for place, index in enumerate(order, 1):
        step = cleaned[index]
        depends = sorted({new_position[d] for d in step.depends_on if d in new_position and new_position[d] < place})
        result.append(step.model_copy(update={"position": place, "depends_on": depends}))
    return result
