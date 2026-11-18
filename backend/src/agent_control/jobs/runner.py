"""Carry jobs forward one small unit at a time, whenever the person is not waiting for the assistant.

Like plans/runner.py this keeps no state of its own: every pass reads the job from the database, does one unit of work, and saves the result, so a
restart, or two workers calling it at once, loses at most the unit that was running (a unit is re-done from the saved state). It gives way to the
person: when a task is running or has just arrived, the pass does nothing, because the one local model serves one request at a time and a question
asked in the middle of a six-hour job should be answered at once, not after the next page.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
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


class DeliveryUnavailable(RuntimeError):
    """No send was attempted because the destination is unavailable."""

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
        quiet_hours: tuple[int, int] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.repositories = repositories
        self.audit = audit
        self.io_for = io_for
        self.say = say
        self.send_file = send_file
        self.reports_dir = Path(reports_dir)
        self.unit_seconds = unit_seconds
        self.quiet_hours = quiet_hours
        self.clock = clock or (lambda: datetime.now().astimezone())
        # One unit at a time in this process, whoever asks: the worker runs several loops that all call tick() on this one runner.
        self._unit_lock = asyncio.Lock()

    async def tick(self) -> int:
        """Do one unit of the oldest running job, if nobody is waiting. Returns 1 when a unit ran."""
        if self._unit_lock.locked() or self.repositories.tasks.list_by_statuses(_ACTIVE, limit=1):
            return 0
        # The worker runs max_parallel_tasks loops, each calling tick(). Without this, two of them read the same saved state and did the same unit
        # twice: live on 11/14 a section was written twice and the report and its closing message were sent twice, ten seconds apart.
        async with self._unit_lock:
            # A crash before the first send leaves a safely retryable pending
            # delivery. An interrupted send has an uncertain outcome and is
            # deliberately not retried automatically.
            if self.send_file is not None:
                job = self.repositories.jobs.pending_delivery()
                if job is not None:
                    await self._deliver(job)
                    return 1
            for job in self.repositories.jobs.list(statuses=[JobStatus.RUNNING], limit=1):
                try:
                    await self._unit(job)
                except Exception:  # noqa: BLE001 - one job's trouble must not stop the worker taking tasks
                    logger.warning("job_unit_failed", job_id=job.id, exc_info=True)
                return 1
        return 0

    async def _unit(self, job: Job) -> None:
        state = job.state or research.new_state()
        # Admission and control updates share the same revision. A control that
        # wins first prevents this snapshot from starting another unit.
        if not self.repositories.jobs.save_if_revision(
            job.id, job.revision, allowed=(JobStatus.RUNNING,), state=state,
        ):
            return
        job = job.model_copy(update={"revision": job.revision + 1})
        try:
            unit = await asyncio.wait_for(research.advance(job.goal, state, self.io_for(job)), timeout=self.unit_seconds)
        except Exception as exc:  # noqa: BLE001 - counted, and said once it has failed too often
            await self._failed_unit(job, state, exc)
            return
        unit.state["errors_in_a_row"] = 0
        if unit.report is not None:
            await self._finish(job, unit)
            return
        notice = unit.say if unit.urgent or not self._quiet_now() else None
        now = self.clock()
        previous = state.get("progress_notice_at")
        try:
            previous_at = datetime.fromisoformat(previous) if isinstance(previous, str) else now
            due = now - previous_at >= timedelta(hours=1)
        except (TypeError, ValueError):
            due = False
            previous = None
        if notice is None and due and not self._quiet_now():
            notice = research.progress_text(unit.state)
        if notice or not previous:
            unit.state["progress_notice_at"] = now.isoformat()
        if not self.repositories.jobs.save_if_revision(
            job.id, job.revision, allowed=(JobStatus.RUNNING,), state=unit.state,
        ):
            return
        if notice:
            await self._tell(job, notice)

    def _quiet_now(self) -> bool:
        if self.quiet_hours is None:
            return False
        start, end = self.quiet_hours
        hour = self.clock().hour
        if start == end:
            return False
        return start <= hour < end if start < end else hour >= start or hour < end

    async def _failed_unit(self, job: Job, state: dict, exc: Exception) -> None:
        errors = int(state.get("errors_in_a_row", 0)) + 1
        reason = str(exc) or type(exc).__name__
        logger.info("job_unit_error", job_id=job.id, errors=errors, reason=reason[:200])
        if errors < MAX_ERRORS_IN_A_ROW:
            self.repositories.jobs.save_if_revision(
                job.id, job.revision, allowed=(JobStatus.RUNNING,), state={**state, "errors_in_a_row": errors},
            )
            return
        if not self.repositories.jobs.save_if_revision(
            job.id, job.revision, allowed=(JobStatus.RUNNING,),
            state={**state, "errors_in_a_row": errors}, status=JobStatus.FAILED,
        ):
            return
        self._audit(job, "job_failed", reason=reason[:300])
        await self._tell(
            job,
            f"I had to stop the research: {reason[:200]} ({errors} tries in a row). What I had by then is saved. "
            f"{research.progress_text(state)} Say 'resume the research' to try again.",
        )

    async def _finish(self, job: Job, unit: research.Unit) -> None:
        folder = self.reports_dir / job.id
        path = folder / "report.md"
        word_path = folder / "report.docx"
        delivery_path = path
        word_failed = False
        final_state = dict(unit.state)

        def write_report() -> None:
            nonlocal delivery_path, word_failed
            folder.mkdir(parents=True, exist_ok=True)
            path.write_text(unit.report or "", encoding="utf-8")
            try:
                from agent_control.jobs.report import write_docx

                write_docx(unit.report or "", word_path)
                delivery_path = word_path
            except Exception:  # noqa: BLE001 - the Markdown report is still useful
                word_failed = True
                logger.warning("job_word_report_failed", job_id=job.id, exc_info=True)
            final_state["delivery"] = {"path": str(delivery_path), "status": "pending" if self.send_file else "unavailable"}

        try:
            if not self.repositories.jobs.finish_if_running(job.id, job.revision, final_state, write_report):
                return
        except OSError as exc:
            failed_state = {**job.state, "stop_reason": "report_write_failed"}
            if self.repositories.jobs.save_if_revision(
                job.id, job.revision, allowed=(JobStatus.RUNNING,), state=failed_state, status=JobStatus.FAILED,
            ):
                logger.warning("job_report_write_failed", job_id=job.id, error_type=type(exc).__name__)
                self._audit(job, "report_write_failed", error_type=type(exc).__name__)
                await self._tell(job, "I could not save the research report, so I stopped. A partial file may exist; check before resuming.")
            return
        self._audit(job, "job_done", path=str(delivery_path))
        if word_failed:
            await self._tell(job, "I could not make the Word copy. The report is available as Markdown instead.")
        delivery = "I will try to send it next." if self.send_file is not None else "This channel cannot send it automatically."
        await self._tell(job, f"The research is done.\n\n{unit.state.get('summary', '')}\n\nThe full report, with every source, was saved. {delivery}")
        if self.send_file is not None:
            saved = self.repositories.jobs.get(job.id)
            if saved is not None:
                await self._deliver(saved)

    async def _deliver(self, job: Job) -> None:
        delivery = dict(job.state.get("delivery") or {})
        if self.send_file is None or delivery.get("status") != "pending":
            return
        path = str(delivery.get("path") or "")
        if not path or not Path(path).is_file():
            delivery["status"] = "missing"
            self.repositories.jobs.save_if_revision(job.id, job.revision, allowed=(JobStatus.DONE,), state={**job.state, "delivery": delivery})
            return
        delivery["status"] = "sending"
        state = {**job.state, "delivery": delivery}
        if not self.repositories.jobs.save_if_revision(job.id, job.revision, allowed=(JobStatus.DONE,), state=state):
            return
        try:
            await self.send_file(job.routing, path, f"Research report: {job.goal[:120]}")
            delivery["status"] = "sent"
        except DeliveryUnavailable:
            delivery["status"] = "unavailable"
        except Exception:  # noqa: BLE001 - the channel may have sent before reporting an error
            delivery["status"] = "uncertain"
            logger.warning("job_report_send_failed", job_id=job.id, exc_info=True)
            await self._tell(job, f"I could not confirm delivery. The report is saved at {path}; ask me to send it again if needed.")
        self.repositories.jobs.save_if_revision(job.id, job.revision + 1, allowed=(JobStatus.DONE,), state={**job.state, "delivery": delivery})

    async def _tell(self, job: Job, text: str) -> None:
        if self.say is not None:
            try:
                await self.say(job.routing, text)
            except Exception:  # noqa: BLE001 - telling is best effort; the job goes on
                logger.warning("job_say_failed", job_id=job.id, exc_info=True)

    def _audit(self, job: Job, what: str, **payload: str) -> None:
        self.audit.append(AuditEventType.TASK_STATE_CHANGED, actor="job_runner", payload={"reason": what, "job_id": job.id, **payload})


def pause_job(repositories: Repositories, job: Job) -> bool:
    return repositories.jobs.save_if_revision(job.id, job.revision, allowed=(JobStatus.RUNNING,), status=JobStatus.PAUSED)


def resume_job(repositories: Repositories, job: Job) -> bool:
    state = dict(job.state)
    state["errors_in_a_row"] = 0  # a person's "resume" is a fresh start for a job that stopped on errors
    state.pop("stop_reason", None)
    return repositories.jobs.save_if_revision(
        job.id, job.revision, allowed=(JobStatus.PAUSED, JobStatus.FAILED), state=state, status=JobStatus.RUNNING,
    )


def cancel_job(repositories: Repositories, job: Job) -> bool:
    return repositories.jobs.save_if_revision(
        job.id, job.revision, allowed=(JobStatus.RUNNING, JobStatus.PAUSED, JobStatus.FAILED), status=JobStatus.CANCELLED,
    )
