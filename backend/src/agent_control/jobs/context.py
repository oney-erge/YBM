"""Tell the Operator that a research job is running in this chat, so "how far are you?" and "stop it" mean that job and not a new one.

A message about a job arrives as an ordinary request. Without a line in the prompt the Operator has no reason to look for one, and on 11/14 a status
question was taken for a request for research and started a second job. As with an open plan (plans/context.py), when a job that has not ended
belongs to the chat a few lines say what it is and which tool controls it; a chat with no open job adds nothing.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from agent_control.jobs import research
from agent_control.jobs.model import OPEN_STATUSES, JobStatus
from agent_control.schemas import utc_now
from agent_control.storage.repositories import JobRepository

MAX_JOBS_SHOWN = 3
#: A finished research is still "what you found yesterday" for this long, and its summary is this long in the prompt.
RECENT_DAYS = 7
SUMMARY_CHARS = 500


def open_job_note(jobs: JobRepository, chat_id: str | None, reports_dir: str | Path | None = None) -> str:
    """The research running in this chat, and the research finished in the last week with its summary and where its report is."""
    if not chat_id:
        return ""
    return _running(jobs, chat_id) + _finished(jobs, chat_id, reports_dir)


def _finished(jobs: JobRepository, chat_id: str, reports_dir: str | Path | None) -> str:
    """Research done in the last week, for "what did you find about drones yesterday?". Without it the only record of a finished job was a line
    in a listing that said "Done: the report was sent." (the challenge case c_e3), and the answer had nothing to be true to."""
    cutoff = utc_now() - timedelta(days=RECENT_DAYS)
    done = [job for job in jobs.list(statuses=[JobStatus.DONE], chat_id=chat_id, limit=50) if job.updated_at >= cutoff][-MAX_JOBS_SHOWN:]
    if not done:
        return ""
    lines = ["\n\n## Research finished for this person in the last week"]
    for job in done:
        when = job.updated_at.strftime("%b %d")
        summary = " ".join(str(job.state.get("summary") or "").split())[:SUMMARY_CHARS]
        report = f" Full report: {Path(reports_dir) / job.id / 'report.md'}" if reports_dir else ""
        lines.append(f"- {job.goal[:140]} (done {when}): {summary or 'no summary was kept.'}{report}")
    lines.append(
        "If they ask what you found, answer from these lines and say only what they say; if what they ask about is not here, say you have no record of it and offer to research it. "
        "To send a report again, call artifact.deliver with operation send_file and its path."
    )
    return "\n".join(lines)


def _running(jobs: JobRepository, chat_id: str) -> str:
    found = jobs.list(statuses=list(OPEN_STATUSES), chat_id=chat_id, limit=MAX_JOBS_SHOWN)
    if not found:
        return ""
    lines = ["\n\n## Research is running for this person in this chat"]
    for job in found:
        lines.append(f"- {job.goal[:140]} ({job.status.value}): {research.progress_text(job.state)}")
    lines.append(
        "If the message is about it (how far it is, whether the report is ready, pause it, resume it, stop it, what it has found so far), call job.manage with "
        "operation status, pause, resume or stop: it is about this job, not a new one. Never start another research for a message like that."
    )
    return "\n".join(lines)
