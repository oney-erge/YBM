"""Jobs are saved after every unit, give way to the person, and end with a report that is written and sent (jobs/runner.py, tools/job_manage.py)."""

from __future__ import annotations

import asyncio
import multiprocessing
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

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
        if schema is research._Supported:
            return research._Supported(supported=True)
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


@pytest.mark.asyncio
async def test_pending_report_delivery_recovers_after_restart_without_research(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    report = tmp_path / "finished.md"
    report.write_text("saved report", encoding="utf-8")
    job = repos.jobs.create(Job(kind="research", goal="saved research", status=JobStatus.DONE, routing=ROUTING,
                                state={"delivery": {"status": "pending", "path": str(report)}}))
    runner, chat = _runner(repos, audit, tmp_path)
    assert await runner.tick() == 1
    assert len(chat.files) == 1 and chat.files[0][0] == str(report)
    assert repos.jobs.get(job.id).state["delivery"]["status"] == "sent"
    assert await runner.tick() == 0


@pytest.mark.asyncio
async def test_uncertain_report_delivery_is_not_automatically_duplicated(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    report = tmp_path / "finished.md"
    report.write_text("saved report", encoding="utf-8")
    job = repos.jobs.create(Job(kind="research", goal="saved research", status=JobStatus.DONE, routing=ROUTING,
                                state={"delivery": {"status": "pending", "path": str(report)}}))
    runner, chat = _runner(repos, audit, tmp_path)
    async def uncertain_send(*_args):
        raise OSError("connection dropped after possible send")
    runner.send_file = uncertain_send
    assert await runner.tick() == 1
    assert repos.jobs.get(job.id).state["delivery"]["status"] == "uncertain"
    assert "could not confirm delivery" in chat.said[0]
    assert await runner.tick() == 0
    from agent_control.tools.job_manage import job_line
    assert "could not be confirmed" in job_line(repos.jobs.get(job.id))


@pytest.mark.asyncio
async def test_unavailable_channel_is_not_recorded_as_sent(tmp_path) -> None:
    from agent_control.cli import RoutingNotificationSink

    repos, audit = make_repos(tmp_path)
    report = tmp_path / "finished.md"
    report.write_text("saved report", encoding="utf-8")
    job = repos.jobs.create(Job(kind="research", goal="saved research", status=JobStatus.DONE,
                                routing={"source_channel": "web", "source_chat_id": "42"},
                                state={"delivery": {"status": "pending", "path": str(report)}}))
    runner, _ = _runner(repos, audit, tmp_path)
    runner.send_file = RoutingNotificationSink(AppSettings(_env_file=None), audit).send_file
    assert await runner.tick() == 1
    assert repos.jobs.get(job.id).state["delivery"]["status"] == "unavailable"


def _finish_in_process(database_path, report_path, job_id, ready, release, outcome, block_in_write):
    """Use a separate interpreter and connection for the publication boundary."""
    from agent_control.storage import Database
    from agent_control.storage.repositories import JobRepository

    try:
        jobs = JobRepository(Database(f"sqlite:///{database_path}"))
        job = jobs.get(job_id)

        def write_report():
            if block_in_write:
                ready.set()
                if not release.wait(15):
                    raise TimeoutError("publication release timed out")
            Path(report_path).write_text("synthetic report", encoding="utf-8")

        if not block_in_write:
            ready.set()
            if not release.wait(15):
                raise TimeoutError("completion release timed out")
        outcome.put(("ok", jobs.finish_if_running(job.id, job.revision, job.state, write_report)))
    except Exception as exc:
        outcome.put(("error", type(exc).__name__))


@pytest.mark.asyncio
@pytest.mark.parametrize("operation,expected", [("pause", JobStatus.PAUSED), ("stop", JobStatus.CANCELLED)])
async def test_control_wins_before_a_final_unit_returns(tmp_path, monkeypatch, operation, expected) -> None:
    repos, call = _tool(tmp_path)
    started = await call(operation="start_research", goal="Synthetic research")
    job = repos.jobs.get(started.output["job_id"])
    audit = repos.audit
    from agent_control.storage.audit import AuditLogger

    runner, chat = _runner(repos, AuditLogger(audit), tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()

    async def advance(_goal, state, _io):
        entered.set()
        await release.wait()
        return research.Unit(state={**state, "stage": "done"}, report="synthetic report")

    monkeypatch.setattr(research, "advance", advance)
    pending = asyncio.create_task(runner._unit(job))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        result = await call(operation=operation, job_id=job.id)
        assert result.status == ToolResultStatus.SUCCEEDED
        assert "step already underway may still finish" in result.output["summary"]
    finally:
        release.set()
        await asyncio.wait_for(pending, 5)
    saved = repos.jobs.get(job.id)
    assert saved.status == expected and saved.state == job.state
    assert chat.said == [] and chat.files == []
    assert not (tmp_path / "jobs" / job.id / "report.md").exists()


@pytest.mark.asyncio
async def test_pause_then_resume_does_not_publish_the_previous_unit(tmp_path, monkeypatch) -> None:
    repos, call = _tool(tmp_path)
    started = await call(operation="start_research", goal="Synthetic research")
    job = repos.jobs.get(started.output["job_id"])
    from agent_control.storage.audit import AuditLogger

    runner, chat = _runner(repos, AuditLogger(repos.audit), tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def advance(_goal, state, _io):
        calls.append(1)
        if len(calls) == 1:
            entered.set()
            await release.wait()
        return research.Unit(state={**state, "stage": "done"}, report="synthetic report")

    monkeypatch.setattr(research, "advance", advance)
    pending = asyncio.create_task(runner._unit(job))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert (await call(operation="pause", job_id=job.id)).status == ToolResultStatus.SUCCEEDED
        assert (await call(operation="resume", job_id=job.id)).status == ToolResultStatus.SUCCEEDED
    finally:
        release.set()
        await asyncio.wait_for(pending, 5)
    current = repos.jobs.get(job.id)
    assert current.status == JobStatus.RUNNING and current.state == job.state
    assert chat.said == [] and chat.files == []
    assert not (tmp_path / "jobs" / job.id / "report.md").exists()
    await runner._unit(current)
    assert len(calls) == 2 and repos.jobs.get(job.id).status == JobStatus.DONE
    assert len(chat.files) == 1 and (tmp_path / "jobs" / job.id / "report.md").read_text() == "synthetic report"


@pytest.mark.asyncio
async def test_cancel_during_a_failing_unit_does_not_overwrite_stop_or_send_failure(tmp_path, monkeypatch) -> None:
    repos, audit = make_repos(tmp_path)
    job = _job(repos, errors_in_a_row=MAX_ERRORS_IN_A_ROW - 1)
    runner, chat = _runner(repos, audit, tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()

    async def advance(_goal, _state, _io):
        entered.set()
        await release.wait()
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(research, "advance", advance)
    pending = asyncio.create_task(runner._unit(job))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        from agent_control.jobs.runner import cancel_job
        assert cancel_job(repos, repos.jobs.get(job.id))
    finally:
        release.set()
        await asyncio.wait_for(pending, 5)
    assert repos.jobs.get(job.id).status == JobStatus.CANCELLED
    assert repos.jobs.get(job.id).state == job.state and chat.said == []


@pytest.mark.asyncio
async def test_competing_runners_publish_only_one_completed_report(tmp_path, monkeypatch) -> None:
    repos, audit = make_repos(tmp_path)
    job = _job(repos)
    chat = _Chat()
    first, _ = _runner(repos, audit, tmp_path, chat=chat)
    second, _ = _runner(repos, audit, tmp_path, chat=chat)
    first_entered, both_entered, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    executions = []

    async def advance(_goal, state, _io):
        executions.append(1)
        first_entered.set()
        if len(executions) == 2:
            both_entered.set()
        await release.wait()
        return research.Unit(state={**state, "stage": "done"}, report="synthetic report")

    monkeypatch.setattr(research, "advance", advance)
    pending = [asyncio.create_task(first._unit(job))]
    try:
        await asyncio.wait_for(first_entered.wait(), 5)
        pending.append(asyncio.create_task(second._unit(repos.jobs.get(job.id))))
        await asyncio.wait_for(both_entered.wait(), 5)
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(*pending), 5)
    assert len(executions) == 2  # Work admission is not fenced across runner instances.
    assert repos.jobs.get(job.id).status == JobStatus.DONE
    assert len(chat.files) == 1 and sum(text.startswith("The research is done.") for text in chat.said) == 1


@pytest.mark.asyncio
async def test_failed_report_write_does_not_mark_job_done_or_send(tmp_path, monkeypatch) -> None:
    repos, audit = make_repos(tmp_path)
    job = _job(repos)
    runner, chat = _runner(repos, audit, tmp_path)

    async def advance(_goal, state, _io):
        return research.Unit(state={**state, "stage": "done"}, report="synthetic report")

    def fail_write(_path, *_args, **_kwargs):
        raise OSError("synthetic write failure")

    monkeypatch.setattr(research, "advance", advance)
    monkeypatch.setattr(Path, "write_text", fail_write)
    assert await runner.tick() == 1
    current = repos.jobs.get(job.id)
    assert current.status == JobStatus.FAILED and current.revision == job.revision + 2
    assert current.state == {**job.state, "stop_reason": "report_write_failed"}
    assert len(chat.said) == 1 and "partial file may exist" in chat.said[0] and chat.files == []


@pytest.mark.asyncio
async def test_failed_job_can_be_resumed_without_a_job_id(tmp_path) -> None:
    repos, call = _tool(tmp_path)
    started = await call(operation="start_research", goal="Synthetic research")
    job_id = started.output["job_id"]
    job = repos.jobs.get(job_id)
    repos.jobs.save(job_id, state={**job.state, "stop_reason": "report_write_failed"}, status=JobStatus.FAILED)
    status = await call(operation="status")
    assert "partial file may exist" in status.output["summary"]
    resumed = await call(operation="resume")
    assert resumed.status == ToolResultStatus.SUCCEEDED
    assert repos.jobs.get(job_id).status == JobStatus.RUNNING and "stop_reason" not in repos.jobs.get(job_id).state


@pytest.mark.asyncio
async def test_resume_goes_to_the_job_that_can_be_resumed_not_to_one_that_is_running(tmp_path) -> None:
    repos, call = _tool(tmp_path)
    failed_id = (await call(operation="start_research", goal="The research that failed")).output["job_id"]
    failed = repos.jobs.get(failed_id)
    repos.jobs.save(failed_id, state=failed.state, status=JobStatus.FAILED)
    running_id = (await call(operation="start_watch", target="https://example.com/news")).output["job_id"]  # another job is running in the same chat
    assert repos.jobs.get(running_id).status == JobStatus.RUNNING

    resumed = await call(operation="resume")

    assert resumed.status == ToolResultStatus.SUCCEEDED and resumed.output["job_id"] == failed_id
    assert repos.jobs.get(failed_id).status == JobStatus.RUNNING


@pytest.mark.asyncio
async def test_completed_job_without_file_channel_does_not_claim_the_report_was_sent(tmp_path, monkeypatch) -> None:
    repos, audit = make_repos(tmp_path)
    job = _job(repos)
    chat = _Chat()
    runner = JobRunner(repos, audit, lambda _job: None, say=chat.say, reports_dir=tmp_path / "jobs")

    async def advance(_goal, state, _io):
        return research.Unit(state={**state, "stage": "done"}, report="synthetic report")

    monkeypatch.setattr(research, "advance", advance)
    assert await runner.tick() == 1
    assert repos.jobs.get(job.id).status == JobStatus.DONE
    assert len(chat.said) == 1 and "cannot send it automatically" in chat.said[0]


def test_stale_job_control_cannot_overwrite_newer_state(tmp_path) -> None:
    from agent_control.jobs.runner import cancel_job, pause_job

    repos, _audit = make_repos(tmp_path)
    old = _job(repos)
    repos.jobs.save(old.id, state={**old.state, "stage": "search"})
    assert not cancel_job(repos, old) and not pause_job(repos, old)
    current = repos.jobs.get(old.id)
    assert current.status == JobStatus.RUNNING and current.state["stage"] == "search"


def test_report_commit_that_wins_first_rejects_a_stale_stop(tmp_path) -> None:
    from agent_control.jobs.runner import cancel_job

    repos, _audit = make_repos(tmp_path)
    job = _job(repos)
    writing, release, control_started = threading.Event(), threading.Event(), threading.Event()
    outcomes, errors = [], []

    def write_report():
        writing.set()
        assert release.wait(5)
        (tmp_path / "report.md").write_text("synthetic report", encoding="utf-8")

    def complete():
        try:
            outcomes.append(("complete", repos.jobs.finish_if_running(job.id, job.revision, job.state, write_report)))
        except Exception as exc:
            errors.append(exc)

    def stop():
        try:
            control_started.set()
            outcomes.append(("stop", cancel_job(repos, job)))
        except Exception as exc:
            errors.append(exc)

    publisher, controller = threading.Thread(target=complete), threading.Thread(target=stop)
    publisher.start()
    try:
        assert writing.wait(5)
        controller.start()
        assert control_started.wait(5)
    finally:
        release.set()
        publisher.join(5)
        if controller.ident is not None:
            controller.join(5)
    assert not publisher.is_alive() and not controller.is_alive() and errors == []
    assert dict(outcomes) == {"complete": True, "stop": False}
    assert repos.jobs.get(job.id).status == JobStatus.DONE
    assert (tmp_path / "report.md").read_text(encoding="utf-8") == "synthetic report"


@pytest.mark.parametrize("finish_first", [False, True])
def test_stop_and_report_publication_share_a_database_boundary_across_processes(tmp_path, finish_first) -> None:
    from agent_control.jobs.runner import cancel_job

    repos, _audit = make_repos(tmp_path)
    job = _job(repos)
    report = tmp_path / "report.md"
    context = multiprocessing.get_context("spawn")
    ready, release, outcome = context.Event(), context.Event(), context.Queue()
    process = context.Process(
        target=_finish_in_process,
        args=(str(tmp_path / "agent.db"), str(report), job.id, ready, release, outcome, finish_first),
    )
    controller = None
    control_result, errors = [], []
    started = threading.Event()

    def stop():
        try:
            started.set()
            control_result.append(cancel_job(repos, job))
        except Exception as exc:
            errors.append(type(exc).__name__)

    process.start()
    try:
        assert ready.wait(15), "child did not reach the publication boundary"
        if finish_first:
            controller = threading.Thread(target=stop)
            controller.start()
            assert started.wait(5)
        else:
            stop()
            assert control_result == [True]
        release.set()
        process.join(20)
        if controller is not None:
            controller.join(20)
        assert not process.is_alive() and process.exitcode == 0 and errors == []
        assert controller is None or not controller.is_alive()
        assert outcome.get(timeout=5) == ("ok", finish_first)
        assert control_result == [not finish_first]
        assert repos.jobs.get(job.id).status == (JobStatus.DONE if finish_first else JobStatus.CANCELLED)
        assert report.exists() is finish_first
    finally:
        release.set()
        if process.is_alive():
            process.terminate()
            process.join(5)


@pytest.mark.asyncio
async def test_control_before_unit_admission_prevents_work_from_starting(tmp_path, monkeypatch) -> None:
    from agent_control.jobs.runner import pause_job

    repos, audit = make_repos(tmp_path)
    job = _job(repos)
    runner, chat = _runner(repos, audit, tmp_path)
    calls = []

    async def advance(_goal, state, _io):
        calls.append(1)
        return research.Unit(state=state)

    monkeypatch.setattr(research, "advance", advance)
    assert pause_job(repos, job)
    await runner._unit(job)
    assert calls == [] and chat.said == [] and repos.jobs.get(job.id).status == JobStatus.PAUSED


def test_job_revision_migrates_an_existing_database_without_changing_the_record(tmp_path) -> None:
    from agent_control.storage import Database, Repositories
    from agent_control.schemas import utc_now

    path = tmp_path / "legacy.db"
    now = utc_now().isoformat()
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE background_jobs (id TEXT PRIMARY KEY, kind TEXT NOT NULL, goal TEXT NOT NULL, status TEXT NOT NULL, state_json TEXT NOT NULL, routing_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
        connection.execute("INSERT INTO background_jobs VALUES (?, ?, ?, ?, ?, ?, ?, ?)", ("job_old", "research", "kept", "paused", '{"stage":"plan"}', "{}", now, now))
    database = Database(f"sqlite:///{path}")
    database.initialize()
    database.initialize()
    jobs = Repositories.for_database(database).jobs
    old = jobs.get("job_old")
    assert old.goal == "kept" and old.status == JobStatus.PAUSED and old.state == {"stage": "plan"}
    assert old.revision == 0 and "revision" not in old.model_dump()
    jobs.save(old.id, status=JobStatus.RUNNING)
    assert jobs.get(old.id).revision == 1


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
    assert chat.files and chat.files[0][0].endswith("report.docx") and "heat pumps in Chicago" in chat.files[0][1]
    from zipfile import ZipFile
    with ZipFile(chat.files[0][0]) as document:
        assert document.testzip() is None
        assert b"heat pumps in Chicago" in document.read("word/document.xml")
    assert any(line.startswith("The research is done.") for line in chat.said) and any("pages to read" in line for line in chat.said)
    assert await runner.tick() == 0  # a job that is done is not touched again


@pytest.mark.asyncio
async def test_word_report_failure_delivers_markdown_and_explains_fallback(tmp_path, monkeypatch) -> None:
    from agent_control.jobs import report

    repos, audit = make_repos(tmp_path)
    job = _job(repos)
    runner, chat = _runner(repos, audit, tmp_path)

    async def advance(_goal, state, _io):
        return research.Unit(state={**state, "stage": "done"}, report="# Report\n\nEvidence.")

    def fail_word(_markdown, _path):
        raise ValueError("synthetic Word failure")

    monkeypatch.setattr(research, "advance", advance)
    monkeypatch.setattr(report, "write_docx", fail_word)
    await runner._unit(job)

    assert repos.jobs.get(job.id).status == JobStatus.DONE
    assert chat.files[0][0].endswith("report.md")
    assert any("could not make the Word copy" in line for line in chat.said)


@pytest.mark.asyncio
async def test_quiet_hours_suppress_progress_but_not_urgent_or_completion(tmp_path, monkeypatch) -> None:
    from datetime import datetime, timezone

    repos, audit = make_repos(tmp_path)
    job = _job(repos)
    runner, chat = _runner(repos, audit, tmp_path)
    runner.quiet_hours = (23, 8)
    runner.clock = lambda: datetime(2026, 11, 17, 23, tzinfo=timezone.utc)
    outcomes = iter([
        research.Unit(state=job.state, say="progress"),
        research.Unit(state=job.state, say="a question", urgent=True),
        research.Unit(state={**job.state, "stage": "done"}, report="# Done\n\nResult."),
    ])

    async def advance(_goal, _state, _io):
        return next(outcomes)

    monkeypatch.setattr(research, "advance", advance)
    for _ in range(3):
        await runner._unit(repos.jobs.get(job.id))

    assert "progress" not in chat.said
    assert "a question" in chat.said
    assert any(line.startswith("The research is done.") for line in chat.said)
    assert chat.files


@pytest.mark.asyncio
async def test_hourly_progress_resumes_after_quiet_hours_and_restart(tmp_path, monkeypatch) -> None:
    from datetime import datetime, timezone

    repos, audit = make_repos(tmp_path)
    job = _job(repos)
    runner, chat = _runner(repos, audit, tmp_path)
    runner.quiet_hours = (23, 8)
    times = iter([datetime(2026, 11, 17, 22, tzinfo=timezone.utc),
                  datetime(2026, 11, 17, 23, 30, tzinfo=timezone.utc)])
    now = [next(times)]
    runner.clock = lambda: now[0]
    async def advance(_goal, state, _io):
        return research.Unit(state=dict(state))
    monkeypatch.setattr(research, "advance", advance)
    await runner._unit(job)
    now[0] = next(times)
    await runner._unit(repos.jobs.get(job.id))
    assert not chat.said
    restarted, _ = _runner(repos, audit, tmp_path, chat=chat)
    restarted.quiet_hours = (23, 8)
    restarted.clock = lambda: datetime(2026, 11, 18, 8, tzinfo=timezone.utc)
    await restarted._unit(repos.jobs.get(job.id))
    assert chat.said == [research.progress_text(repos.jobs.get(job.id).state)]


@pytest.mark.asyncio
async def test_a_failed_unit_is_tried_again_after_a_wait_and_not_at_once(tmp_path) -> None:
    """Live 11/19: five failures in five seconds ended a 600-page research when the search engine answered "no results" to a burst of queries."""
    repos, audit = make_repos(tmp_path)
    job = _job(repos)
    runner, _chat = _runner(repos, audit, tmp_path, io=_Io(fail=True))
    now = [datetime(2026, 11, 19, 12, tzinfo=timezone.utc)]
    runner.clock = lambda: now[0]

    assert await runner.tick() == 1 and repos.jobs.get(job.id).state["errors_in_a_row"] == 1
    assert await runner.tick() == 0  # the wait is not over: nothing is tried
    now[0] += timedelta(seconds=31)
    assert await runner.tick() == 1 and repos.jobs.get(job.id).state["errors_in_a_row"] == 2
    assert await runner.tick() == 0  # and the wait after a second failure is longer
    now[0] += timedelta(seconds=45)
    assert await runner.tick() == 0
    now[0] += timedelta(seconds=20)
    assert await runner.tick() == 1 and repos.jobs.get(job.id).state["errors_in_a_row"] == 3


@pytest.mark.asyncio
async def test_a_search_that_stops_answering_leaves_the_pages_already_found_to_be_read() -> None:
    sources = [{"id": 1, "url": "https://a.example/1", "title": "One", "question": 0, "status": "new", "facts": []}]

    class _NoSearch(_Io):
        async def search(self, query, count):
            raise RuntimeError("no search engine gave results")

    io = _NoSearch().io
    state = {**research.new_state(10), "stage": "search", "questions": ["What does it cost?", "Who installs it?"], "sources": list(sources), "queries_done": 0}
    with pytest.raises(RuntimeError):
        await research.advance("heat pumps", {**state, "errors_in_a_row": 1}, io)  # asked once or twice: the job waits and asks again
    unit = await research.advance("heat pumps", {**state, "errors_in_a_row": 2}, io)
    assert unit.state["stage"] == "read" and "stopped answering" in unit.say and "1 pages" in unit.say
    with pytest.raises(RuntimeError):
        await research.advance("heat pumps", {**state, "sources": [], "errors_in_a_row": 4}, io)  # nothing found yet: nothing to read instead


@pytest.mark.asyncio
async def test_a_job_that_keeps_failing_stops_says_so_and_keeps_its_state_and_resume_tries_again(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    job = _job(repos)
    runner, chat = _runner(repos, audit, tmp_path, io=_Io(fail=True))
    now = [datetime(2026, 11, 19, 12, tzinfo=timezone.utc)]
    runner.clock = lambda: now[0]
    for _ in range(MAX_ERRORS_IN_A_ROW):
        await runner.tick()
        now[0] += timedelta(hours=1)  # each try waits out the pause the failure before it asked for

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
    assert "Which heat pump for Chicago?" in status.output["summary"] and "planning the questions" in status.output["summary"] and job.id not in status.output["summary"]


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
    assert repos.jobs.get(first).status == JobStatus.CANCELLED and "step already underway may still finish" in stopped.output["summary"]
    assert (await call(operation="stop", job_id=first)).status == ToolResultStatus.FAILED  # it already ended


@pytest.mark.asyncio
async def test_a_goal_is_required_and_a_chat_with_no_jobs_is_told_so(tmp_path) -> None:
    _repos, call = _tool(tmp_path)
    assert (await call(operation="status")).output["summary"] == "There is no research in this chat."
    assert (await call(operation="pause")).status == ToolResultStatus.FAILED


def test_starting_needs_the_persons_approval_and_the_tool_is_unnamed_while_off_so_recorded_prompts_do_not_change(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    off = build_tool_registry(AppSettings(_env_file=None), "http://127.0.0.1:8765", repositories=repos, audit_logger=audit)
    definition = off.definition_index["job.manage"]
    assert not definition.enabled and not definition.named_when_disabled and "job.manage" not in off.context()
    assert definition.approval_required_operations == ("start_research", "start_investigation", "start_watch", "start_survey")
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


@pytest.mark.asyncio
async def test_a_job_id_the_model_made_up_does_not_stop_a_control_and_status_shows_what_is_running(tmp_path) -> None:
    """Live 11/15: a pause was called with a task id as the job id and refused as "no open research job" while one was running."""
    repos, call = _tool(tmp_path)
    old = (await call(operation="start_research", goal="An old one")).output["job_id"]
    repos.jobs.save(old, status=JobStatus.DONE)
    running = (await call(operation="start_research", goal="The running one")).output["job_id"]
    status = await call(operation="status", job_id="task_69fa6f49b9b5402a930ded7bc3c2ba35")
    assert "The running one" in status.output["summary"] and "An old one" not in status.output["summary"] and "task_" not in status.output["summary"]
    paused = await call(operation="pause", job_id="task_69fa6f49b9b5402a930ded7bc3c2ba35")  # not a job of this chat: the open one is meant
    assert paused.status == ToolResultStatus.SUCCEEDED and repos.jobs.get(running).status == JobStatus.PAUSED
    assert "job_" not in paused.output["summary"]
    resumed = await call(operation="resume", job_id="job_cut_short")
    assert resumed.status == ToolResultStatus.SUCCEEDED and repos.jobs.get(running).status == JobStatus.RUNNING
    refused = await call(operation="resume")
    assert refused.status == ToolResultStatus.FAILED and "the research is running, not paused" in (refused.error_message or "") and "job_" not in (refused.error_message or "")
    assert (await call(operation="status")).output["summary"].count("The running one") == 1


def test_the_computer_is_kept_awake_while_a_long_job_runs_and_let_go_when_none_does(tmp_path) -> None:
    from agent_control.jobs.awake import ES_CONTINUOUS, ES_SYSTEM_REQUIRED, KeepAwake
    from agent_control.jobs.watch import new_state as watch_state

    calls: list[int] = []
    awake = KeepAwake(setter=calls.append)
    awake.want(False)
    assert calls == []  # nothing asked, nothing changed
    awake.want(True)
    awake.want(True)
    assert calls == [ES_CONTINUOUS | ES_SYSTEM_REQUIRED] and awake.holding  # asked once, not on every look
    awake.want(False)
    assert calls[-1] == ES_CONTINUOUS and not awake.holding

    broken = KeepAwake(setter=lambda flags: (_ for _ in ()).throw(OSError("no api")))
    broken.want(True)  # a call that fails is not a failed job
    assert not broken.holding

    repos, audit = make_repos(tmp_path)
    seen: list[bool] = []

    class Spy(KeepAwake):
        def want(self, awake: bool) -> None:
            seen.append(awake)

    runner = JobRunner(repos, audit, lambda _job: _Io().io, say=_Chat().say, reports_dir=tmp_path / "jobs", awake=Spy())
    asyncio.run(runner.tick())
    assert seen == [False]  # no job: no hold
    repos.jobs.create(Job(kind="watch", goal="w", state=watch_state("C:/x", 5), routing=ROUTING))
    asyncio.run(runner.tick())
    assert seen[-1] is False  # a watch is asleep most of the time and does not keep the computer awake
    _job(repos)
    asyncio.run(runner.tick())
    assert seen[-1] is True
