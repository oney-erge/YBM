"""Jobs are saved after every unit, give way to the person, and end with a report that is written and sent (jobs/runner.py, tools/job_manage.py)."""

from __future__ import annotations

import pytest

from agent_control.config import AppSettings
from agent_control.jobs import research
from agent_control.jobs.model import Job, JobStatus
from agent_control.jobs.research import Page, ResearchIO
from agent_control.jobs.runner import MAX_ERRORS_IN_A_ROW, JobRunner
from agent_control.schemas import Capability, TaskStatus, ToolCallRequest, ToolResultStatus
from agent_control.tools.job_manage import JobManageAdapter
from agent_control.tools.registry import build_tool_registry
from helpers import make_repos

PAGE = "Air-source units cost between 4,000 and 8,000 dollars installed in the Chicago area. " * 6
ROUTING = {"source_channel": "telegram", "source_chat_id": "100"}


class _Io:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail

    async def think(self, system, user, schema):
        if self.fail:
            raise RuntimeError("model down")
        if schema is research._Questions:
            return research._Questions(questions=["What does it cost?", "Who installs it?", "What goes wrong?"])
        return research._Notes(facts=[research._Fact(claim="Costs 4,000 to 8,000 dollars.", quote="Air-source units cost between 4,000 and 8,000 dollars installed")])

    async def write(self, system, user):
        return "It costs 4,000 to 8,000 dollars [1]."

    async def search(self, query, count):
        return [{"title": "Costs", "url": f"https://a.example/{abs(hash(query)) % 1000}", "snippet": ""}]

    async def fetch(self, url):
        return Page("Costs", PAGE)

    @property
    def io(self) -> ResearchIO:
        return ResearchIO(think=self.think, write=self.write, search=self.search, fetch=self.fetch)


class _Chat:
    def __init__(self) -> None:
        self.said: list[str] = []
        self.files: list[tuple[str, str]] = []

    async def say(self, routing, text):
        self.said.append(text)

    async def send_file(self, routing, path, caption):
        self.files.append((path, caption))


def _runner(repos, audit, tmp_path, io: _Io | None = None, chat: _Chat | None = None):
    chat = chat or _Chat()
    io = io or _Io()
    return JobRunner(repos, audit, lambda _job: io.io, say=chat.say, send_file=chat.send_file, reports_dir=tmp_path / "jobs"), chat


def _job(repos, **state) -> Job:
    return repos.jobs.create(Job(kind="research", goal="heat pumps in Chicago", state={**research.new_state(10), **state}, routing=ROUTING))


def test_a_job_is_saved_and_found_again_by_status_and_chat(tmp_path) -> None:
    repos, _audit = make_repos(tmp_path)
    first = _job(repos)
    other = repos.jobs.create(Job(kind="research", goal="elsewhere", routing={"source_chat_id": "200"}))
    repos.jobs.save(other.id, status=JobStatus.PAUSED)

    assert [job.id for job in repos.jobs.list(statuses=[JobStatus.RUNNING])] == [first.id]
    assert [job.id for job in repos.jobs.list(chat_id="100")] == [first.id] and repos.jobs.get(other.id).status == JobStatus.PAUSED
    repos.jobs.save(first.id, state={"stage": "read"})
    assert repos.jobs.get(first.id).state == {"stage": "read"} and repos.jobs.get(first.id).is_open


@pytest.mark.asyncio
async def test_each_tick_does_one_unit_saves_it_and_a_restart_picks_up_where_it_stopped(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    job = _job(repos)
    runner, chat = _runner(repos, audit, tmp_path)

    assert await runner.tick() == 1  # the plan: the questions are told to the person
    after_plan = repos.jobs.get(job.id).state
    assert after_plan["stage"] == "search" and len(after_plan["questions"]) == 3 and chat.said[0].startswith("I am researching this")
    # a new runner, as after a restart, reads the saved state and goes on from it
    restarted, _chat2 = _runner(repos, audit, tmp_path)
    assert await restarted.tick() == 1 and repos.jobs.get(job.id).state["queries_done"] == 1


@pytest.mark.asyncio
async def test_the_job_gives_way_to_a_person_who_is_waiting(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    _job(repos)
    runner, chat = _runner(repos, audit, tmp_path)
    repos.tasks.create("What time is it?")  # a task has just arrived: the person is waiting

    assert await runner.tick() == 0 and chat.said == []
    for task in repos.tasks.list_recent(5):
        repos.tasks.update_status(task.id, TaskStatus.COMPLETED)
    assert await runner.tick() == 1


@pytest.mark.asyncio
async def test_a_paused_job_does_nothing_until_resumed(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    job = _job(repos)
    repos.jobs.save(job.id, status=JobStatus.PAUSED)
    runner, _chat = _runner(repos, audit, tmp_path)
    assert await runner.tick() == 0
    repos.jobs.save(job.id, status=JobStatus.RUNNING)
    assert await runner.tick() == 1


@pytest.mark.asyncio
async def test_a_finished_job_writes_its_report_says_the_answer_and_sends_the_file(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    job = _job(repos)
    runner, chat = _runner(repos, audit, tmp_path)
    for _ in range(100):
        await runner.tick()
        if repos.jobs.get(job.id).status == JobStatus.DONE:
            break

    done = repos.jobs.get(job.id)
    assert done.status == JobStatus.DONE and done.state["stage"] == "done"
    report = (tmp_path / "jobs" / job.id / "report.md").read_text(encoding="utf-8")
    assert report.startswith("# heat pumps in Chicago") and "## Sources" in report and "https://a.example/" in report
    assert chat.files and chat.files[0][0].endswith("report.md") and "heat pumps in Chicago" in chat.files[0][1]
    assert any(line.startswith("The research is done.") for line in chat.said) and any("pages to read" in line for line in chat.said)
    assert await runner.tick() == 0  # a job that is done is not touched again


@pytest.mark.asyncio
async def test_a_job_that_keeps_failing_stops_says_so_and_keeps_its_state_and_resume_tries_again(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    job = _job(repos)
    runner, chat = _runner(repos, audit, tmp_path, io=_Io(fail=True))
    for _ in range(MAX_ERRORS_IN_A_ROW):
        await runner.tick()

    failed = repos.jobs.get(job.id)
    assert failed.status == JobStatus.FAILED and failed.state["errors_in_a_row"] == MAX_ERRORS_IN_A_ROW
    assert chat.said and chat.said[-1].startswith("I had to stop the research: model down") and "resume" in chat.said[-1].lower()
    assert await runner.tick() == 0

    from agent_control.jobs.runner import resume_job

    resume_job(repos, failed)
    assert repos.jobs.get(job.id).status == JobStatus.RUNNING and repos.jobs.get(job.id).state["errors_in_a_row"] == 0


# --- the tool ---------------------------------------------------------------------------------------------------------------------------------


def _tool(tmp_path):
    repos, audit = make_repos(tmp_path)
    settings = AppSettings(_env_file=None, jobs={"enabled": True})
    task = repos.tasks.create("Research heat pumps thoroughly", metadata={"source_channel": "telegram", "source_chat_id": "100", "source_sender_id": "7"})

    async def call(**payload):
        return await JobManageAdapter(repos, audit, settings).execute(ToolCallRequest(
            task_id=task.id, tool_name="job.manage", capability=Capability.AGENT_CORE, input=payload,
        ))

    return repos, call


@pytest.mark.asyncio
async def test_the_tool_starts_a_job_for_this_chat_and_says_how_to_follow_it(tmp_path) -> None:
    repos, call = _tool(tmp_path)
    started = await call(operation="start_research", goal="Which heat pump for Chicago?", max_pages=40)

    assert started.status == ToolResultStatus.SUCCEEDED and started.output["job_id"]
    job = repos.jobs.get(started.output["job_id"])
    assert job.kind == "research" and job.routing["source_chat_id"] == "100" and job.state["max_pages"] == 40 and job.status == JobStatus.RUNNING
    assert "how far are you" in started.output["summary"]
    status = await call(operation="status")
    assert job.id in status.output["summary"] and "planning the questions" in status.output["summary"]


@pytest.mark.asyncio
async def test_the_tool_refuses_a_third_open_job_and_controls_the_newest(tmp_path) -> None:
    repos, call = _tool(tmp_path)
    first = (await call(operation="start_research", goal="One")).output["job_id"]
    second = (await call(operation="start_research", goal="Two")).output["job_id"]
    third = await call(operation="start_research", goal="Three")
    assert third.status == ToolResultStatus.FAILED and "already open" in (third.error_message or "")

    paused = await call(operation="pause")
    assert paused.output["job_id"] == second and repos.jobs.get(second).status == JobStatus.PAUSED and repos.jobs.get(first).status == JobStatus.RUNNING
    assert (await call(operation="pause", job_id=second)).status == ToolResultStatus.FAILED  # already paused
    assert (await call(operation="resume")).output["job_id"] == second and repos.jobs.get(second).status == JobStatus.RUNNING
    stopped = await call(operation="stop", job_id=first)
    assert repos.jobs.get(first).status == JobStatus.CANCELLED and "Nothing more will be read" in stopped.output["summary"]
    assert (await call(operation="stop", job_id=first)).status == ToolResultStatus.FAILED  # it already ended


@pytest.mark.asyncio
async def test_a_goal_is_required_and_a_chat_with_no_jobs_is_told_so(tmp_path) -> None:
    _repos, call = _tool(tmp_path)
    assert (await call(operation="status")).output["summary"] == "There are no long jobs in this chat."
    assert (await call(operation="pause")).status == ToolResultStatus.FAILED


def test_starting_needs_the_persons_approval_and_the_tool_is_unnamed_while_off_so_recorded_prompts_do_not_change(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    off = build_tool_registry(AppSettings(_env_file=None), "http://127.0.0.1:8765", repositories=repos, audit_logger=audit)
    definition = off.definition_index["job.manage"]
    assert not definition.enabled and not definition.named_when_disabled and "job.manage" not in off.context()
    assert definition.approval_required_operations == ("start_research",)
    on = build_tool_registry(AppSettings(_env_file=None, jobs={"enabled": True}), "http://127.0.0.1:8765", repositories=repos, audit_logger=audit)
    assert "job.manage" in on.context() and "start_research" in on.context()


@pytest.mark.asyncio
async def test_several_loops_ticking_at_once_do_one_unit_not_the_same_unit_several_times(tmp_path) -> None:
    """Live 11/14: the worker runs several loops that all call tick(); a section was written twice and the finished report was sent twice."""
    import asyncio

    repos, audit = make_repos(tmp_path)
    _job(repos, stage="write", questions=["Q?"], sections=[], sources=[{"id": 1, "url": "https://a.example/x", "title": "T", "question": 0, "status": "read", "facts": [{"claim": "c", "quote": "q" * 30}]}])
    started = []

    class Slow(_Io):
        async def write(self, system, user):
            started.append(1)
            await asyncio.sleep(0.2)
            return "It costs 4,000 to 8,000 dollars [1]."

    runner, _chat = _runner(repos, audit, tmp_path, io=Slow())
    ran = await asyncio.gather(*(runner.tick() for _ in range(4)))
    assert sorted(ran) == [0, 0, 0, 1] and len(started) == 1  # one loop did the unit, the others found it taken
    assert await runner.tick() == 1  # and the next pass is free again
