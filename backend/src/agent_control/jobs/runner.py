"""Carry jobs forward one small unit at a time, whenever the person is not waiting for the assistant.

Like plans/runner.py this keeps no state of its own: every pass reads the job from the database, does one unit of work, and saves the result, so a
restart, or two workers calling it at once, loses at most the unit that was running (a unit is re-done from the saved state). It gives way to the
person: when a task is running or has just arrived, the pass does nothing, because the one local model serves one request at a time and a question
asked in the middle of a six-hour job should be answered at once, not after the next page.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path

import structlog

from agent_control.jobs import research
from agent_control.jobs.model import Job, JobStatus
from agent_control.schemas import AuditEventType, TaskStatus
from agent_control.storage.audit import AuditLogger
from agent_control.storage.repositories import Repositories

logger = structlog.get_logger(__name__)

#: Sends a line to the chat a job belongs to: (routing, text).
Say = Callable[[dict[str, str], str], Awaitable[None]]
#: Sends a file to that chat: (routing, path, caption).
SendFile = Callable[[dict[str, str], str, str], Awaitable[None]]

#: A task in one of these states is the person waiting: jobs give way.
_ACTIVE = [TaskStatus.RECEIVED, TaskStatus.INTERPRETING, TaskStatus.RUNNING, TaskStatus.RETRYING, TaskStatus.AWAITING_APPROVAL]
UNIT_SECONDS = 240.0  # a unit that takes longer than this is counted as failed: a model that hangs must not stop the worker taking tasks
MAX_ERRORS_IN_A_ROW = 5


class JobRunner:
    def __init__(
        self,
        repositories: Repositories,
        audit: AuditLogger,
        io_for: Callable[[Job], research.ResearchIO],
        *,
        say: Say | None = None,
        send_file: SendFile | None = None,
        reports_dir: str | Path = ".agent_control/jobs",
        unit_seconds: float = UNIT_SECONDS,
    ) -> None:
        self.repositories = repositories
        self.audit = audit
        self.io_for = io_for
        self.say = say
        self.send_file = send_file
        self.reports_dir = Path(reports_dir)
        self.unit_seconds = unit_seconds

    async def tick(self) -> int:
        """Do one unit of the oldest running job, if nobody is waiting. Returns 1 when a unit ran."""
        if self.repositories.tasks.list_by_statuses(_ACTIVE, limit=1):
            return 0
        for job in self.repositories.jobs.list(statuses=[JobStatus.RUNNING], limit=1):
            try:
                await self._unit(job)
            except Exception:  # noqa: BLE001 - one job's trouble must not stop the worker taking tasks
                logger.warning("job_unit_failed", job_id=job.id, exc_info=True)
            return 1
        return 0

    async def _unit(self, job: Job) -> None:
        state = job.state or research.new_state()
        try:
            unit = await asyncio.wait_for(research.advance(job.goal, state, self.io_for(job)), timeout=self.unit_seconds)
        except Exception as exc:  # noqa: BLE001 - counted, and said once it has failed too often
            await self._failed_unit(job, state, exc)
            return
        unit.state["errors_in_a_row"] = 0
        self.repositories.jobs.save(job.id, state=unit.state)
        if unit.report is not None:
            await self._finish(job, unit)
        elif unit.say:
            await self._tell(job, unit.say)

    async def _failed_unit(self, job: Job, state: dict, exc: Exception) -> None:
        errors = int(state.get("errors_in_a_row", 0)) + 1
        reason = str(exc) or type(exc).__name__
        logger.info("job_unit_error", job_id=job.id, errors=errors, reason=reason[:200])
        if errors < MAX_ERRORS_IN_A_ROW:
            self.repositories.jobs.save(job.id, state={**state, "errors_in_a_row": errors})
            return
        self.repositories.jobs.save(job.id, state={**state, "errors_in_a_row": errors}, status=JobStatus.FAILED)
        self._audit(job, "job_failed", reason=reason[:300])
        await self._tell(
            job,
            f"I had to stop the research: {reason[:200]} ({errors} tries in a row). What I had by then is saved. "
            f"{research.progress_text(state)} Say 'resume the research' to try again.",
        )

    async def _finish(self, job: Job, unit: research.Unit) -> None:
        folder = self.reports_dir / job.id
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "report.md"
        path.write_text(unit.report or "", encoding="utf-8")
        self.repositories.jobs.save(job.id, status=JobStatus.DONE)
        self._audit(job, "job_done", path=str(path))
        await self._tell(job, f"The research is done.\n\n{unit.state.get('summary', '')}\n\nThe full report, with every source, is the file I am sending next.")
        if self.send_file is not None:
            try:
                await self.send_file(job.routing, str(path), f"Research report: {job.goal[:120]}")
            except Exception:  # noqa: BLE001 - the report is on disk even when it could not be sent
                logger.warning("job_report_send_failed", job_id=job.id, exc_info=True)
                await self._tell(job, f"I could not send the file. It is saved at {path}.")

    async def _tell(self, job: Job, text: str) -> None:
        if self.say is not None:
            try:
                await self.say(job.routing, text)
            except Exception:  # noqa: BLE001 - telling is best effort; the job goes on
                logger.warning("job_say_failed", job_id=job.id, exc_info=True)

    def _audit(self, job: Job, what: str, **payload: str) -> None:
        self.audit.append(AuditEventType.TASK_STATE_CHANGED, actor="job_runner", payload={"reason": what, "job_id": job.id, **payload})


def pause_job(repositories: Repositories, job: Job) -> None:
    repositories.jobs.save(job.id, status=JobStatus.PAUSED)


def resume_job(repositories: Repositories, job: Job) -> None:
    state = dict(job.state)
    state["errors_in_a_row"] = 0  # a person's "resume" is a fresh start for a job that stopped on errors
    repositories.jobs.save(job.id, state=state, status=JobStatus.RUNNING)


def cancel_job(repositories: Repositories, job: Job) -> None:
    repositories.jobs.save(job.id, status=JobStatus.CANCELLED)
