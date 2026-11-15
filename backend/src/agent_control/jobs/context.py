"""Tell the Operator that a research job is running in this chat, so "how far are you?" and "stop it" mean that job and not a new one.

A message about a job arrives as an ordinary request. Without a line in the prompt the Operator has no reason to look for one, and on 11/14 a status
question was taken for a request for research and started a second job. As with an open plan (plans/context.py), when a job that has not ended
belongs to the chat a few lines say what it is and which tool controls it; a chat with no open job adds nothing.
"""

from __future__ import annotations

from agent_control.jobs import research
from agent_control.jobs.model import OPEN_STATUSES
from agent_control.storage.repositories import JobRepository

MAX_JOBS_SHOWN = 3


def open_job_note(jobs: JobRepository, chat_id: str | None) -> str:
    if not chat_id:
        return ""
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
