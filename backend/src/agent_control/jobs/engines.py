"""Which engine carries a job of each kind forward: research (jobs/research.py) or investigation (jobs/investigate.py). Both keep the same `Unit` and the same saved-state contract."""

from __future__ import annotations

from types import ModuleType
from typing import Any

from agent_control.jobs import investigate, research
from agent_control.jobs.model import Job

KINDS = ("research", "investigate")


def engine_for(kind: str) -> ModuleType:
    return investigate if kind == "investigate" else research


def progress_text(job: Job) -> str:
    return engine_for(job.kind).progress_text(job.state)


def new_state_for(kind: str) -> dict[str, Any]:
    return engine_for(kind).new_state()
