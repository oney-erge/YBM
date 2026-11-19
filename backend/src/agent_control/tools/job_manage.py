"""Start, show and control a job that takes hours: the Operator's way to turn "read a hundred sources and come back to me" into saved work (jobs/).

The tool starts nothing without the person's approval (`start_research` is approval-gated, with a sentence that says what it will do), never runs
the work itself (jobs/runner.py does, one unit at a time, when nobody is waiting), and answers "how far are you?" from the job's own state.
"""

from __future__ import annotations

import asyncio
from typing import Any

from agent_control.config import AppSettings
from agent_control.jobs import engines, investigate, monitors, research, survey, watch
from agent_control.jobs.model import Job, JobStatus
from agent_control.jobs.runner import cancel_job, pause_job, resume_job
from agent_control.plans.runner import routing_of
from agent_control.schemas import AuditEventType, Capability, ToolCallRequest, ToolCallResult, ToolResultStatus
from agent_control.storage.audit import AuditLogger
from agent_control.storage.repositories import Repositories
from agent_control.tools.contracts import JobManageInput, JobManageOutput
from agent_control.tools.spec import Adapters, Definitions, RegistryDeps, ToolDefinition, failed_result, same_output_schema

OPERATIONS = ("start_research", "start_investigation", "start_watch", "start_survey", "status", "pause", "resume", "stop")
#: Jobs open at once in one chat. The one local model serves one request at a time, so a third job would only slow the other two.
MAX_OPEN_JOBS = 2


def job_line(job: Job) -> str:
    """One job in words: what it is for, where it is, and what it is doing."""
    failure = (
        "Could not save the report; a partial file may exist. Check it before resuming."
        if job.state.get("stop_reason") == "report_write_failed"
        else f"Stopped after repeated errors. {engines.progress_text(job)} 'Resume' tries again."
    )
    delivery = job.state.get("delivery") or {}
    delivery_text = {
        "pending": " Delivery is pending.",
        "sending": " Delivery was started; its outcome is not yet confirmed.",
        "uncertain": " Delivery could not be confirmed; the saved report can be sent again.",
        "sent": " Delivery was confirmed by the channel.",
        "unavailable": " Automatic delivery is unavailable on this channel.",
        "missing": " The saved report is missing; check the report location.",
    }.get(delivery.get("status"), "")
    where = {
        JobStatus.RUNNING: engines.progress_text(job) if job.state else "Starting.",
        JobStatus.PAUSED: f"Paused. {engines.progress_text(job)}" if job.state else "Paused.",
        JobStatus.DONE: "Done: the report was saved." + delivery_text,
        JobStatus.FAILED: failure,
        JobStatus.CANCELLED: "Stopped.",
    }[job.status]
    return f"{job.goal[:100]}. {where}"


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
            elif operation == "start_investigation":
                output = self._start_investigation(request, routing, task)
            elif operation == "start_watch":
                output = await self._start_watch(request, routing)
            elif operation == "start_survey":
                output = self._start_survey(request, routing)
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
        open_jobs = [job for job in self._mine(routing, [JobStatus.RUNNING, JobStatus.PAUSED]) if job.kind != "watch"]  # a watch is asleep most of the time: it takes no place from the long jobs
        if len(open_jobs) >= MAX_OPEN_JOBS:
            raise ValueError(f"{len(open_jobs)} researches are already open in this chat; pause or stop one first, or wait for it to finish")
        pages = int(request.input.get("max_pages") or self.config.default_pages)
        if pages > self.config.max_pages:
            raise ValueError(f"requested {pages} pages exceeds the configured research limit of {self.config.max_pages}; change the limit or request fewer pages")
        job = self.repositories.jobs.create(Job(kind="research", goal=goal, state=research.new_state(pages), routing=routing))
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

    def _start_investigation(self, request: ToolCallRequest, routing: dict[str, str], task: Any) -> dict[str, Any]:
        goal = " ".join(str(request.input.get("goal") or "").split())
        if not goal:
            raise ValueError("start_investigation requires a goal")
        open_jobs = [job for job in self._mine(routing, [JobStatus.RUNNING, JobStatus.PAUSED]) if job.kind != "watch"]  # a watch is asleep most of the time: it takes no place from the long jobs
        if len(open_jobs) >= MAX_OPEN_JOBS:
            raise ValueError(f"{len(open_jobs)} jobs are already open in this chat; pause or stop one first, or wait for it to finish")
        # The goal this belongs to travels with the job, so that when it ends what it found goes to the goal (orchestration/goal_routing.py).
        goal_id = str((task.metadata or {}).get("goal_id") or "") if task is not None else ""
        job = self.repositories.jobs.create(Job(kind="investigate", goal=goal, state=investigate.new_state(), routing={**routing, **({"goal_id": goal_id} if goal_id else {})}))
        self.audit.append(
            AuditEventType.TASK_STATE_CHANGED, actor="job", task_id=request.task_id, payload={"reason": "job_started", "job_id": job.id, "kind": "investigate"}
        )
        return {
            "job_id": job.id, "jobs": [{"id": job.id, "status": job.status.value}],
            "summary": (
                f"Started working out: {goal[:200]}. It runs read-only checks of this computer and the home network, one at a time, looks up what is known, and then "
                "messages the person what it found and the ways forward. Tell the person in one or two sentences, without any job number, that it has started and that they "
                "can ask 'how far are you?', pause it or stop it at any time."
            ),
        }

    def _start_survey(self, request: ToolCallRequest, routing: dict[str, str]) -> dict[str, Any]:
        goal = " ".join(str(request.input.get("goal") or "").split())
        if not goal:
            raise ValueError("start_survey requires a goal")
        open_jobs = [job for job in self._mine(routing, [JobStatus.RUNNING, JobStatus.PAUSED]) if job.kind != "watch"]
        if len(open_jobs) >= MAX_OPEN_JOBS:
            raise ValueError(f"{len(open_jobs)} jobs are already open in this chat; pause or stop one first, or wait for it to finish")
        job = self.repositories.jobs.create(Job(kind="survey", goal=goal, state=survey.new_state(), routing=routing))
        self.audit.append(
            AuditEventType.TASK_STATE_CHANGED, actor="job", task_id=request.task_id, payload={"reason": "job_started", "job_id": job.id, "kind": "survey"}
        )
        return {
            "job_id": job.id, "jobs": [{"id": job.id, "status": job.status.value}],
            "summary": (
                f"Started a survey: {goal[:200]}. It finds every one of them it can, checks each against the criteria with a source for every value, and keeps going in the background "
                f"until new searches stop finding anything new, or three days pass. {survey.estimate_text()} Tell the person in two sentences, without any job number, what it is doing, "
                "that it may take hours, and that they can ask 'how far are you?', pause it or stop it at any time."
            ),
        }

    async def _start_watch(self, request: ToolCallRequest, routing: dict[str, str]) -> dict[str, Any]:
        target = " ".join(str(request.input.get("target") or "").split())
        script = str(request.input.get("script") or "")
        tested = ""
        if script.strip():
            # A monitor: the script is saved and run once at once; a script that fails its test is not kept and the reason goes back to the model.
            name = str(request.input.get("name") or "")
            try:
                path, first = await asyncio.to_thread(monitors.save_and_test, self.config.monitors_dir, name, script)
            except (RuntimeError, ValueError) as exc:
                raise ValueError(f"the monitor script did not pass its test run: {exc}. Fix the script and try again.") from exc
            target = f"script:{path.stem}"
            tested = f" It was run once as a test and printed {len([line for line in first.splitlines() if line.strip()])} line(s)."
        if not target:
            raise ValueError("start_watch requires a target: a folder path or a web page address, or a script with a name")
        open_jobs = self._mine(routing, [JobStatus.RUNNING, JobStatus.PAUSED])
        if len([job for job in open_jobs if job.kind != "watch"]) >= MAX_OPEN_JOBS:
            raise ValueError("two long jobs are already open in this chat; pause or stop one first (a watch does not count)")
        if any(job.kind == "watch" and job.state.get("target") == target for job in open_jobs):
            raise ValueError(f"{target} is already being watched in this chat")
        goal = " ".join(str(request.input.get("goal") or "").split()) or f"Tell me when {target} changes"
        every = int(request.input.get("every_minutes") or watch.DEFAULT_EVERY_MINUTES)
        job = self.repositories.jobs.create(Job(kind="watch", goal=goal, state=watch.new_state(target, every), routing=routing))
        self.audit.append(
            AuditEventType.TASK_STATE_CHANGED, actor="job", task_id=request.task_id, payload={"reason": "job_started", "job_id": job.id, "kind": "watch"}
        )
        return {
            "job_id": job.id, "jobs": [{"id": job.id, "status": job.status.value}],
            "summary": (
                f"Started watching {target} every {job.state['every_minutes']} minutes.{tested} The first look only records what is there; after that the person is told once about each change, "
                "and about nothing else. Tell the person in one or two sentences, without any job number, that it is watching, how often it looks, and that they can pause or stop it."
            ),
        }

    def _named(self, request: ToolCallRequest, routing: dict[str, str]) -> Job | None:
        """The job whose id was given, when it is one of this chat's. A model that is shown no job number writes one from somewhere else (a task id, an
        earlier job's id cut short), and found live that a pause with such an id was refused as "no open research job" while one was running."""
        job_id = str(request.input.get("job_id") or "")
        job = self.repositories.jobs.get(job_id) if job_id else None
        return job if job is not None and job.routing.get("source_chat_id") == routing.get("source_chat_id") else None

    def _status(self, request: ToolCallRequest, routing: dict[str, str]) -> dict[str, Any]:
        named = self._named(request, routing)
        if named is not None:
            jobs = [named]
        else:
            mine = self._mine(routing)
            open_jobs = [job for job in mine if job.is_open]
            jobs = open_jobs or mine[-1:]  # what is running, else the last one there was
        if not jobs:
            return {"jobs": [], "summary": "There is no research in this chat."}
        return {"job_id": jobs[-1].id, "jobs": [{"id": job.id, "status": job.status.value} for job in jobs], "summary": "\n".join(job_line(job) for job in jobs)}

    def _control(self, operation: str, request: ToolCallRequest, routing: dict[str, str]) -> dict[str, Any]:
        job = self._named(request, routing)
        if job is None:
            # The newest job the operation applies to. A running job cannot be resumed, so "resume the research" must not land on a watch or another job that is running: it
            # answered "the research is running, not paused" while the research that had failed waited (live 11/19). Only when nothing else is there, a running one is named, so the answer is that it is running.
            applies = {"pause": [[JobStatus.RUNNING]], "resume": [[JobStatus.PAUSED], [JobStatus.FAILED], [JobStatus.RUNNING]], "stop": [[JobStatus.RUNNING, JobStatus.PAUSED]]}.get(operation, [[JobStatus.RUNNING, JobStatus.PAUSED]])
            for statuses in applies:
                job = next(iter(reversed(self._mine(routing, statuses))), None)
                if job is not None:
                    break
        if job is None:
            raise ValueError("there is no research running in this chat to " + operation)
        if operation == "pause":
            if job.status != JobStatus.RUNNING:
                raise ValueError(f"the research is {job.status.value}, not running")
            if not pause_job(self.repositories, job):
                raise ValueError("the research changed while pausing; check its status and retry")
            said = "Paused the research. No new step starts until 'resume'; a step already underway may still finish. What was saved is kept."
        elif operation == "resume":
            if job.status not in (JobStatus.PAUSED, JobStatus.FAILED):
                raise ValueError(f"the research is {job.status.value}, not paused")
            if not resume_job(self.repositories, job):
                raise ValueError("the research changed while resuming; check its status and retry")
            said = f"Resumed the research. {research.progress_text(job.state)}"
        else:
            if not job.is_open and job.status != JobStatus.FAILED:
                raise ValueError(f"the research already ended ({job.status.value})")
            if not cancel_job(self.repositories, job):
                raise ValueError("the research changed while stopping; check its status and retry")
            said = f"Stopped the research. {research.progress_text(job.state)} No new steps will start; a step already underway may still finish."
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
                "start and control a background job: start_research reads many pages into a cited report; start_investigation works out why something on this computer or "
                "network does not work; start_watch tells the person when a folder or page changes; "
                "start_survey finds every one of something in a place and compares them, for hours or days; status, pause, resume, stop control it"
            ),
            operations=OPERATIONS,
            input_schema=JobManageInput,
            output_schema=JobManageOutput,
            operation_output_schemas=same_output_schema(OPERATIONS, JobManageOutput),
            default_operation="status",
            # Hours of work and many web requests start only when the person says so, with a sentence that says what it will do.
            approval_required_operations=("start_research", "start_investigation", "start_watch", "start_survey"),
            approval_reasons={
                "start_research": "Starts a long research job that reads many web pages over several hours.",
                "start_investigation": "Runs read-only checks of this computer and the home network to work out a problem, and looks up what is known.",
                "start_watch": "Looks at a folder or a web page every so often, in the background, and messages you about changes.",
                "start_survey": "Finds every one of something in a place, checks each against criteria with a source for every value, and keeps going in the background until it is done.",
            },
            examples=(
                {"operation": "start_research", "goal": "Which heat pump is best for a 150 square meter house in Chicago? Costs, running costs, reliability.", "max_pages": 60},
                {"operation": "status"},
                {"operation": "pause"},
            ),
        )
    )
    if deps.repositories is not None and deps.audit_logger is not None:
        adapters["job.manage"] = JobManageAdapter(deps.repositories, deps.audit_logger, settings)  # type: ignore[arg-type]
