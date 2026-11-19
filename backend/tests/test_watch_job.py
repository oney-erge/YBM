"""A watch looks every so often, remembers what it saw, and speaks only about a change, once (jobs/watch.py)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agent_control.jobs import engines, watch
from agent_control.jobs.model import Job, JobStatus
from agent_control.jobs.research import Page, ResearchIO
from agent_control.jobs.runner import JobRunner
from helpers import make_repos


class _World:
    """A folder and a page that change when the test says so."""

    def __init__(self) -> None:
        self.files = {"a.txt": (10, 1)}
        self.page = "Opening hours: nine to five on weekdays. The shop is closed on Sunday." * 2
        self.fail = False

    async def list_folder(self, path):
        if self.fail:
            raise OSError("the folder is not there")
        return [(name, size, mtime) for name, (size, mtime) in self.files.items()]

    async def fetch(self, url):
        if self.fail:
            return None
        return Page("Shop", self.page)

    @property
    def io(self) -> ResearchIO:
        return ResearchIO(think=None, write=None, search=None, fetch=self.fetch, list_folder=self.list_folder)


async def _look(state, world, goal="tell me when something changes"):
    unit = await watch.advance(goal, state, world.io)
    return unit.state, unit.say


@pytest.mark.asyncio
async def test_the_first_look_announces_nothing_and_a_new_file_is_told_once() -> None:
    world, state = _World(), watch.new_state("C:/data/inbox", 5)
    state, said = await _look(state, world)
    assert said is None and state["baseline"] and state["looks"] == 1  # what was already there is not news
    state, said = await _look(state, world)
    assert said is None  # nothing changed: nothing said
    world.files["contract.txt"] = (200, 5)
    state, said = await _look(state, world)
    assert said == "Something changed in C:/data/inbox. New: contract.txt."
    state, said = await _look(state, world)
    assert said is None and state["changes_told"] == 1  # the same file is not announced again
    world.files["a.txt"] = (11, 9)
    del world.files["contract.txt"]
    state, said = await _look(state, world)
    assert "Changed: a.txt." in said and "Removed: contract.txt." in said and state["changes_told"] == 2


@pytest.mark.asyncio
async def test_a_page_that_changes_is_told_with_what_is_new_and_a_failing_look_is_not_nothing_changed() -> None:
    world, state = _World(), watch.new_state("https://shop.example/hours", 30)
    assert state["target_kind"] == "page"
    state, said = await _look(state, world)
    assert said is None
    world.page += "\nNew: open on Sunday from ten to two."
    state, said = await _look(state, world)
    assert said.startswith("The page https://shop.example/hours changed. What is new:") and "open on Sunday" in said
    world.fail = True
    for _ in range(2):
        state, said = await _look(state, world)
        assert said is None  # one or two failed looks are not worth a message
    state, said = await _look(state, world)
    assert said.startswith("I cannot look at https://shop.example/hours right now") and state["said_failing"]
    state, said = await _look(state, world)
    assert said is None  # said once
    world.fail = False
    state, said = await _look(state, world)
    assert said == "I can look at https://shop.example/hours again." and state["failures"] == 0


def test_a_watch_sleeps_between_looks_and_progress_is_told_in_words() -> None:
    state = watch.new_state("C:/data/inbox", 15)
    assert not watch.asleep(state)
    state["sleep_until"] = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    assert watch.asleep(state) and not watch.asleep(state, datetime.now(timezone.utc) + timedelta(minutes=6))
    assert watch.progress_text(state) == "I am about to look at C:/data/inbox for the first time."
    assert watch.new_state("x", 0)["every_minutes"] == watch.MIN_EVERY_MINUTES and watch.new_state("x", 99999)["every_minutes"] == watch.MAX_EVERY_MINUTES
    assert engines.engine_for("watch") is watch


@pytest.mark.asyncio
async def test_the_runner_leaves_a_sleeping_watch_alone_looks_when_it_is_due_and_keeps_what_it_saw_across_a_restart(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    world, told = _World(), []

    async def say(routing, text):
        told.append(text)

    job = repos.jobs.create(Job(kind="watch", goal="Tell me when a new file shows up", state=watch.new_state("C:/data/inbox", 5), routing={"source_chat_id": "42"}))
    runner = JobRunner(repos, audit, lambda _job: world.io, say=say, reports_dir=tmp_path / "jobs")
    assert await runner.tick() == 1  # the first look: records what is there
    looks = repos.jobs.get(job.id).state["looks"]
    assert await runner.tick() == 0 and repos.jobs.get(job.id).state["looks"] == looks  # asleep until its next look
    world.files["new.txt"] = (1, 1)
    due = repos.jobs.get(job.id).state | {"sleep_until": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()}
    repos.jobs.save(job.id, state=due)
    restarted = JobRunner(repos, audit, lambda _job: world.io, say=say, reports_dir=tmp_path / "jobs")  # a restart: nothing but the saved state
    assert await restarted.tick() == 1 and told == ["Something changed in C:/data/inbox. New: new.txt."]
    assert repos.jobs.get(job.id).status == JobStatus.RUNNING  # a watch goes on until it is stopped


@pytest.mark.asyncio
async def test_a_watch_is_started_through_job_manage_asked_first_once_per_target_and_not_counted_against_the_long_job_limit(tmp_path) -> None:
    from agent_control.config import AppSettings
    from agent_control.human_text import describe_action
    from agent_control.schemas import Capability, ToolCallRequest, ToolResultStatus
    from agent_control.tools.job_manage import JobManageAdapter

    repos, audit = make_repos(tmp_path)
    adapter = JobManageAdapter(repos, audit, AppSettings(_env_file=None, jobs={"enabled": True}))
    task = repos.tasks.create("Tell me when a new file shows up", metadata={"source_channel": "telegram", "source_chat_id": "42"})

    def call(**payload):
        return adapter.execute(ToolCallRequest(task_id=task.id, tool_name="job.manage", capability=Capability.AGENT_CORE, input=payload))

    started = await call(operation="start_watch", target="C:/data/inbox", every_minutes=10)
    assert started.status == ToolResultStatus.SUCCEEDED and "every 10 minutes" in started.output["summary"]
    job = repos.jobs.get(started.output["job_id"])
    assert job.kind == "watch" and job.state["target_kind"] == "folder" and job.state["every_minutes"] == 10 and job.goal == "Tell me when C:/data/inbox changes"
    again = await call(operation="start_watch", target="C:/data/inbox")
    assert again.status == ToolResultStatus.FAILED and "already being watched" in again.error_message
    page = await call(operation="start_watch", target="https://shop.example/hours")
    assert page.status == ToolResultStatus.SUCCEEDED and repos.jobs.get(page.output["job_id"]).state["target_kind"] == "page" and repos.jobs.get(page.output["job_id"]).state["every_minutes"] == 15
    for name in ("research one", "research two"):
        assert (await call(operation="start_research", goal=name)).status == ToolResultStatus.SUCCEEDED  # two watches do not take a place from the long jobs
    assert (await call(operation="start_watch")).status == ToolResultStatus.FAILED
    card = describe_action("job.manage", {"operation": "start_watch", "target": "C:/data/inbox", "every_minutes": 10})
    assert card == "Watch C:/data/inbox every 10 minutes in the background and message you when it changes"


@pytest.mark.asyncio
async def test_the_start_parser_makes_a_watch_call_from_the_target_and_says_none_without_one() -> None:
    from agent_control.orchestration.job_start import _Start, start_research_call

    class _Provider:
        def __init__(self, answer):
            self.answer = answer

        async def generate_structured(self, system, user, schema, temperature=0.0):
            return self.answer

    watch_call = await start_research_call(_Provider(_Start(action="watch", goal="Tell me whenever a new file shows up", target="C:/data/inbox", every_minutes=5)), "Tell me whenever a new file shows up in C:/data/inbox, look every 5 minutes")
    assert watch_call == {"operation": "start_watch", "target": "C:/data/inbox", "goal": "Tell me whenever a new file shows up", "every_minutes": 5}
    assert await start_research_call(_Provider(_Start(action="watch", goal="tell me when it changes")), "tell me when it changes") is None
