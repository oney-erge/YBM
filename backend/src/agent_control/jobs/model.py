"""A job: work that takes hours, kept as saved state so a restart, a pause or a busy minute loses nothing.

A task runs the Operator loop for a dozen steps and ends. "Read a hundred sources about heat pumps and come back to me" is not a task: it is
hours of small steps (search, read one page, take notes, write a section) between which the person may ask something else, pause it, or restart
the computer. A job keeps where it is in its own state, saved after every unit of work, so nothing is held in a model's head or a process's
memory. jobs/runner.py does one unit at a time when nothing else is running; jobs/research.py is what a unit of a research job is.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import Field

from agent_control.schemas import StrictBaseModel, new_id, utc_now


class JobStatus(StrEnum):
    RUNNING = "running"
    PAUSED = "paused"  # on the person's word; resumes on it
    DONE = "done"
    FAILED = "failed"  # too many units failed in a row; the state is kept
    CANCELLED = "cancelled"


OPEN_STATUSES = (JobStatus.RUNNING, JobStatus.PAUSED)


class Job(StrictBaseModel):
    id: str = Field(default_factory=lambda: new_id("job"))
    kind: str  # "research"
    goal: str
    status: JobStatus = JobStatus.RUNNING
    #: Where the job is, as plain data: the engine of its kind reads and writes it (jobs/research.py). Saved after every unit.
    state: dict[str, Any] = Field(default_factory=dict)
    # Internal optimistic-concurrency token; it is not part of the tool output.
    revision: int = Field(default=0, ge=0, exclude=True)
    #: Where its messages go: source_channel and source_chat_id, as a plan keeps them.
    routing: dict[str, str] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_STATUSES
