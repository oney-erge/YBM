"""Start, show and control a job that takes hours: the Operator's way to turn "read a hundred sources and come back to me" into saved work (jobs/).

The tool starts nothing without the person's approval (`start_research` is approval-gated, with a sentence that says what it will do), never runs
the work itself (jobs/runner.py does, one unit at a time, when nobody is waiting), and answers "how far are you?" from the job's own state.
"""

from __future__ import annotations

from typing import Any

from agent_control.config import AppSettings
from agent_control.jobs import research
from agent_control.jobs.model import Job, JobStatus
from agent_control.jobs.runner import cancel_job, pause_job, resume_job
from agent_control.plans.runner import routing_of
from agent_control.schemas import AuditEventType, Capability, ToolCallRequest, ToolCallResult, ToolResultStatus
from agent_control.storage.audit import AuditLogger
from agent_control.storage.repositories import Repositories
from agent_control.tools.contracts import JobManageInput, JobManageOutput
from agent_control.tools.spec import Adapters, Definitions, RegistryDeps, ToolDefinition, failed_result, same_output_schema

OPERATIONS = ("start_research", "status", "pause", "resume", "stop")
#: Jobs open at once in one chat. The one local model serves one request at a time, so a third job would only slow the other two.
MAX_OPEN_JOBS = 2


def job_line(job: Job) -> str:
    """One job in words: what it is for, where it is, and what it is doing."""
    where = {
        JobStatus.RUNNING: research.progress_text(job.state) if job.state else "Starting.",
        JobStatus.PAUSED: f"Paused. {research.progress_text(job.state)}" if job.state else "Paused.",
        JobStatus.DONE: "Done: the report was sent.",
        JobStatus.FAILED: f"Stopped after repeated errors. {research.progress_text(job.state)} 'Resume' tries again.",
        JobStatus.CANCELLED: "Stopped.",
    }[job.status]
    return f"{job.id}: {job.goal[:100]}. {where}"


class JobManageAdapter:
    def __init__(self, repositories: Repositories, audit: AuditLogger, settings: AppSettings) -> None:
        self.repositories = repositories
        self.audit = audit
        self.config = settings.jobs

    async def execute(self, request: ToolCallRequest) -> ToolCallResult:
        operation = str(request.input.get("operation") or "status")
        task = self.repositories.tasks.get(request.task_id)
        routing = routing_of(task) if task is not None else {}
        try:
            if operation == "start_research":
                output = self._start(request, routing)
            elif operation == "status":
                output = self._status(request, routing)
            elif operation in ("pause", "resume", "stop"):
                output = self._control(operation, request, routing)
            else:
                return failed_result(request, f"unsupported job operation: {operation}")
        except ValueError as exc:
            return failed_result(request, str(exc))
        output["operation"] = operation
        output["terminal_output"] = [{"content": output["summary"], "is_final": True, "exit_code": 0}]
        return ToolCallResult(request_id=request.id, status=ToolResultStatus.SUCCEEDED, output=output)

    def _mine(self, routing: dict[str, str], statuses: list[JobStatus] | None = None) -> list[Job]:
        return self.repositories.jobs.list(statuses=statuses, chat_id=routing.get("source_chat_id"), limit=20)

    def _start(self, request: ToolCallRequest, routing: dict[str, str]) -> dict[str, Any]:
        goal = " ".join(str(request.input.get("goal") or "").split())
        if not goal:
            raise ValueError("start_research requires a goal")
        open_jobs = self._mine(routing, [JobStatus.RUNNING, JobStatus.PAUSED])
        if len(open_jobs) >= MAX_OPEN_JOBS:
            raise ValueError(f"{len(open_jobs)} research jobs are already open in this chat ({open_jobs[0].id}); pause or stop one first, or wait for it to finish")
        pages = int(request.input.get("max_pages") or self.config.default_pages)
        job = self.repositories.jobs.create(Job(kind="research", goal=goal, state=research.new_state(min(pages, self.config.max_pages)), routing=routing))
        self.audit.append(
            AuditEventType.TASK_STATE_CHANGED, actor="job", task_id=request.task_id, payload={"reason": "job_started", "job_id": job.id, "kind": "research"}
        )
        return {
            "job_id": job.id, "jobs": [{"id": job.id, "status": job.status.value}],
            "summary": (
                f"Started the research on: {goal[:200]}. It reads up to {job.state['max_pages']} pages, one at a time, in the background; "
                "the person's messages are answered first. Progress messages come to this chat, and the report comes at the end. Tell the person "
                "in one or two sentences, without any job number, that it has started and that they can ask 'how far are you?', pause it or stop it at any time."
            ),
        }

    def _status(self, request: ToolCallRequest, routing: dict[str, str]) -> dict[str, Any]:
        job_id = str(request.input.get("job_id") or "")
        jobs = [job for job in [self.repositories.jobs.get(job_id)] if job] if job_id else self._mine(routing)[-5:]
        if not jobs:
            return {"jobs": [], "summary": "There are no long jobs in this chat."}
        return {"job_id": jobs[-1].id, "jobs": [{"id": job.id, "status": job.status.value} for job in jobs], "summary": "\n".join(job_line(job) for job in jobs)}

    def _control(self, operation: str, request: ToolCallRequest, routing: dict[str, str]) -> dict[str, Any]:
        job_id = str(request.input.get("job_id") or "")
        job = self.repositories.jobs.get(job_id) if job_id else next(iter(reversed(self._mine(routing, [JobStatus.RUNNING, JobStatus.PAUSED]))), None)
        if job is None:
            raise ValueError("there is no open research job to " + operation)
        if operation == "pause":
            if job.status != JobStatus.RUNNING:
                raise ValueError(f"job {job.id} is {job.status.value}, not running")
            pause_job(self.repositories, job)
            said = f"Paused the research. Nothing more is read until 'resume'. What it has so far is kept."
        elif operation == "resume":
            if job.status not in (JobStatus.PAUSED, JobStatus.FAILED):
                raise ValueError(f"job {job.id} is {job.status.value}, not paused")
            resume_job(self.repositories, job)
            said = f"Resumed the research. {research.progress_text(job.state)}"
        else:
            if not job.is_open and job.status != JobStatus.FAILED:
                raise ValueError(f"job {job.id} already ended ({job.status.value})")
            cancel_job(self.repositories, job)
            said = f"Stopped the research. {research.progress_text(job.state)} Nothing more will be read."
        return {"job_id": job.id, "jobs": [{"id": job.id, "status": self.repositories.jobs.get(job.id).status.value}], "summary": said}


def register(deps: RegistryDeps, definitions: Definitions, adapters: Adapters) -> None:
    settings = deps.settings
    enabled = deps.repositories is not None and deps.audit_logger is not None and settings.jobs.enabled
    definitions.append(
        ToolDefinition(
            name="job.manage",
            group="planning",
            provides=("research", "investigate", "sources", "report", "background", "hours", "compare", "survey", "long"),
            capability=Capability.AGENT_CORE,
            enabled=enabled,
            named_when_disabled=False,
            description=(
                "start and control a long job that runs in the background for hours: start_research reads many web pages one at a time, keeps the "
                "words that support every claim, messages the person as it goes and ends with a cited report; status says how far it is; pause, "
                "resume and stop control it. Use it when the person asks for thorough research, many sources, or to be told when it is done"
            ),
            operations=OPERATIONS,
            input_schema=JobManageInput,
            output_schema=JobManageOutput,
            operation_output_schemas=same_output_schema(OPERATIONS, JobManageOutput),
            default_operation="status",
            # Hours of work and many web requests start only when the person says so, with a sentence that says what it will do.
            approval_required_operations=("start_research",),
            approval_reasons={"start_research": "Starts a long research job that reads many web pages over several hours."},
            examples=(
                {"operation": "start_research", "goal": "Which heat pump is best for a 150 square meter house in Chicago? Costs, running costs, reliability.", "max_pages": 60},
                {"operation": "status"},
                {"operation": "pause"},
            ),
        )
    )
    if deps.repositories is not None and deps.audit_logger is not None:
        adapters["job.manage"] = JobManageAdapter(deps.repositories, deps.audit_logger, settings)  # type: ignore[arg-type]
