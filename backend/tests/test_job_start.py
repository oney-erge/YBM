"""A request a skill routes to a long job starts it with a call written in code (orchestration/job_start.py, and the worker's first step)."""

from __future__ import annotations

import pytest

from agent_control.orchestration import StaticToolAdapter, TaskWorker
from agent_control.orchestration.fulfillment import _postcondition_satisfied
from agent_control.orchestration.job_start import MAX_PAGES, MIN_PAGES, _Start, start_research_call
from agent_control.schemas import ChannelType, OperatorAction, OperatorDecision, PostconditionType, TaskStatus
from helpers import make_repos
from test_worker_operator_loop import QueueOperator, _executor_with_adapter, _settings

ROUTING = {"source_channel": "telegram", "source_chat_id": "42", "source_sender_id": "7"}
REQUEST = "Please look into how people heat old stone houses without making them damp. Read about twelve different sources and send me a report."


class _Provider:
    def __init__(self, answer: _Start | Exception) -> None:
        self.answer, self.asked = answer, []

    async def generate_structured(self, system_prompt, user, schema, **kwargs):
        self.asked.append((system_prompt, user, schema.__name__))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


# --- the call ---------------------------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_call_carries_the_goal_without_the_instructions_and_the_number_of_pages_the_person_named():
    provider = _Provider(_Start(goal="How people heat old stone houses without making them damp", pages=12))
    call = await start_research_call(provider, REQUEST)
    assert call == {"operation": "start_research", "goal": "How people heat old stone houses without making them damp", "max_pages": 12}
    system, user, schema = provider.asked[0]
    assert schema == "_Start" and REQUEST in user and "Do not add anything they did not say" in system


@pytest.mark.asyncio
async def test_a_number_outside_what_a_job_accepts_is_brought_inside_and_no_number_is_no_limit():
    for named, expected in ((3, MIN_PAGES), (500, MAX_PAGES), (60, 60)):
        call = await start_research_call(_Provider(_Start(goal="g", pages=named)), REQUEST)
        assert call["max_pages"] == expected
    assert "max_pages" not in await start_research_call(_Provider(_Start(goal="g", pages=None)), REQUEST)
    assert "max_pages" not in await start_research_call(_Provider(_Start(goal="g", pages=0)), REQUEST)


@pytest.mark.asyncio
async def test_a_model_that_fails_or_says_nothing_leaves_the_persons_own_words_as_the_goal_and_the_call_is_still_made():
    words = " ".join(REQUEST.split())
    assert await start_research_call(_Provider(TimeoutError("slow")), REQUEST) == {"operation": "start_research", "goal": words}
    assert (await start_research_call(_Provider(_Start(goal="  ", pages=None)), REQUEST))["goal"] == words
    assert await start_research_call(None, REQUEST) == {"operation": "start_research", "goal": words}
    assert len((await start_research_call(None, "x " * 3000))["goal"]) <= 2000


# --- the worker -------------------------------------------------------------------------------------------------------------------------------


def _task(repos, **metadata):
    conversation = repos.conversations.get_or_create(ChannelType.TELEGRAM, "42")
    return repos.tasks.create(REQUEST, conversation_id=conversation, metadata={**ROUTING, "original_message_text": REQUEST, **metadata})


def _stage(calls: list):
    async def stage(task):
        calls.append(task.id)
        return {"operation": "start_research", "goal": "heating old stone houses", "max_pages": 12}

    return stage


@pytest.mark.asyncio
async def test_a_routed_request_starts_with_the_job_call_once_and_only_on_its_first_step(tmp_path):
    repos, audit = make_repos(tmp_path)
    calls: list = []
    worker = TaskWorker(repos, audit, job_stage=_stage(calls))
    task = _task(repos, task_route="job")
    decision = await worker._started_job(task, [])
    assert decision.action == OperatorAction.CALL_TOOL and decision.tool_name == "job.manage"
    assert decision.tool_input == {"operation": "start_research", "goal": "heating old stone houses", "max_pages": 12}
    assert repos.tasks.get(task.id).metadata["job_start_done"] is True
    assert await worker._started_job(repos.tasks.get(task.id), []) is None  # not twice
    assert await worker._started_job(task, [{"tool_name": "job.manage"}]) is None  # not past the first step
    assert calls == [task.id]


@pytest.mark.asyncio
async def test_only_a_routed_task_with_a_stage_is_started_and_a_stage_that_fails_leaves_it_to_the_operator(tmp_path):
    repos, audit = make_repos(tmp_path)
    calls: list = []
    assert await TaskWorker(repos, audit, job_stage=_stage(calls))._started_job(_task(repos), []) is None  # no route
    assert await TaskWorker(repos, audit, job_stage=_stage(calls))._started_job(_task(repos, task_route="plan"), []) is None  # another route
    assert await TaskWorker(repos, audit)._started_job(_task(repos, task_route="job"), []) is None  # no stage
    assert calls == []

    async def broken(task):
        raise RuntimeError("no model")

    assert await TaskWorker(repos, audit, job_stage=broken)._started_job(_task(repos, task_route="job"), []) is None


@pytest.mark.asyncio
async def test_the_loop_runs_the_job_call_before_the_operator_decides_anything_and_the_operator_then_writes_the_answer(tmp_path):
    """The live failure: the skill was in the prompt and the 9B chose delegate. Now the first call is not its choice, and it goes through the same executor."""
    repos, audit = make_repos(tmp_path)
    adapter = StaticToolAdapter({"summary": "Started the research on: heating old stone houses. It reads up to 12 pages, one at a time, in the background.", "answer": "Research started."})
    operator = QueueOperator([OperatorDecision(action=OperatorAction.DONE, final_answer="It is running in the background; I will message you as it goes.")])
    worker = TaskWorker(
        repos, audit, executor=_executor_with_adapter(_settings(), audit, repos, adapter, tool_name="job.manage"), operator=operator,
        job_stage=_stage([]),
    )
    task = _task(repos, task_route="job")
    for _ in range(4):  # one step per pass: the job call, then the Operator's answer
        await worker.process_next()
        if repos.tasks.get(task.id).status == TaskStatus.COMPLETED:
            break
    finished = repos.tasks.get(task.id)
    assert [r.tool_name for r in adapter.requests] == ["job.manage"] and adapter.requests[0].input["operation"] == "start_research"
    assert operator.calls == 0  # neither the first step nor the answer is the model's: a started job is told in code
    answer = str(finished.metadata.get("synthesized_answer") or "")
    assert finished.status == TaskStatus.COMPLETED and answer.startswith("I have started researching this in the background.")
    assert "up to 12 pages" in answer and "job_" not in answer


def _with_history(repos, *entries):
    task = _task(repos, task_route="job")
    return repos.tasks.update_metadata(task.id, {**task.metadata, "operator_history": list(entries)})


def _entry(operation: str, status: str = "succeeded") -> dict:
    return {"tool_name": "job.manage", "input": {"operation": operation, "goal": "g"}, "status": status, "output_summary": "x"}


def test_a_started_job_does_the_reading_and_the_sending_the_request_asked_for_and_nothing_else_it_could_stand_in_for(tmp_path):
    """Found live 11/14: "read about twelve sources and send me a report" made the check demand source content and a delivered file after the job had started."""
    repos, _ = make_repos(tmp_path)
    started = _with_history(repos, _entry("start_research"))
    assert _postcondition_satisfied(started, PostconditionType.SOURCE_CONTENT) and _postcondition_satisfied(started, PostconditionType.ARTIFACT_DELIVERED)
    assert not _postcondition_satisfied(started, PostconditionType.SCHEDULE_CREATED)  # only what the job does
    for entries in ((), (_entry("status"),), (_entry("start_research", "failed"),)):
        other = _with_history(repos, *entries)
        assert not _postcondition_satisfied(other, PostconditionType.SOURCE_CONTENT) and not _postcondition_satisfied(other, PostconditionType.ARTIFACT_DELIVERED)


@pytest.mark.asyncio
async def test_a_message_about_research_already_running_is_not_a_start_and_the_operator_answers_it():
    """Live 11/14: "How far along is the research?" started a second job whose goal was that sentence."""
    status = _Provider(_Start(goal="", pages=None, new_research=False))
    assert await start_research_call(status, "How far along is the research?") is None
    assert (await start_research_call(_Provider(_Start(goal="g", pages=None, new_research=True)), REQUEST))["operation"] == "start_research"
    assert "already under way" in status.asked[0][0] and "new_research" in status.asked[0][0]


@pytest.mark.asyncio
async def test_the_worker_leaves_a_message_that_is_not_a_start_to_the_operator_and_does_not_ask_again(tmp_path):
    repos, audit = make_repos(tmp_path)

    async def not_a_start(task):
        return None

    worker = TaskWorker(repos, audit, job_stage=not_a_start)
    task = _task(repos, task_route="job")
    assert await worker._started_job(task, []) is None
    assert repos.tasks.get(task.id).metadata["job_start_done"] is True


def test_the_operator_is_told_a_job_is_running_in_the_chat_and_that_a_message_about_it_is_not_a_new_job(tmp_path):
    from agent_control.jobs.context import open_job_note
    from agent_control.jobs.model import Job, JobStatus
    from agent_control.jobs import research

    repos, _ = make_repos(tmp_path)
    conversation = repos.conversations.get_or_create(ChannelType.TELEGRAM, "42")
    assert repos.conversations.external_id(conversation) == "42" and repos.conversations.external_id("conv_nope") is None and repos.conversations.external_id(None) is None
    assert open_job_note(repos.jobs, "42") == "" and open_job_note(repos.jobs, None) == ""
    job = repos.jobs.create(Job(kind="research", goal="how people heat old stone houses without making them damp", state=research.new_state(12), routing=dict(ROUTING)))
    note = open_job_note(repos.jobs, "42")
    assert "## Research is running for this person in this chat" in note and "how people heat old stone houses" in note and "(running)" in note
    assert "Never start another research for a message like that." in note and "job.manage" in note
    assert open_job_note(repos.jobs, "other-chat") == ""  # another chat's job is not shown
    repos.jobs.save(job.id, status=JobStatus.DONE)
    ended = open_job_note(repos.jobs, "42")
    assert "## Research is running" not in ended and "## Research finished for this person in the last week" in ended  # an ended job is not running; it is a finished one


def test_every_bundled_starter_skill_parses_and_the_research_skill_is_routed_to_the_job_stage() -> None:
    """Found live 11/15: the research skill's new `when:` had a colon in it, which YAML reads as a second key. The file was skipped without a word, the route never
    applied, and the 9B went to a web search that is blocked. Every file in skills/starter must load, with its question, and none may be dropped silently."""
    from pathlib import Path

    from agent_control.tools.skills import _load_skills

    root = Path(__file__).resolve().parents[2] / "skills" / "starter"
    files = sorted(root.glob("*.md"))
    loaded = {skill["path"].replace("\\", "/").rsplit("/", 1)[-1] for skill in _load_skills(str(root))}
    assert loaded == {path.name for path in files}, f"skipped without a word: {sorted({p.name for p in files} - loaded)}"
    research = next(skill for skill in _load_skills(str(root)) if skill["name"] == "Research a topic in depth")
    assert research["route"] == "job" and research["when_at"] == 0.55 and research["tools"] == ["job.manage"]
    assert "already under way" in research["when"] and research["when"].startswith("Is the user asking you to start researching")


def test_a_started_job_is_told_to_the_person_in_code_without_a_job_number_and_only_when_it_really_started() -> None:
    """Live 11/15: after the job started, the Operator kept calling the job tool with the id of an earlier job, offered Claude Code, and asked whether to wait or pause."""
    from agent_control.orchestration.planning import _research_started, started_answer

    done = {"tool_name": "job.manage", "status": "succeeded", "input": {"operation": "start_research", "goal": "g"}, "output_summary": "Started the research on: g. It reads up to 10 pages, one at a time"}
    assert _research_started([{"tool_name": "_fulfillment_check"}, done]) is done
    for history in ([], [{**done, "status": "failed"}], [{**done, "input": {"operation": "status"}}], [{"tool_name": "web.search", "status": "succeeded"}]):
        assert _research_started(history) is None
    said = started_answer(done)
    assert "up to 10 pages" in said and "job_" not in said and "pause or stop it" in said
    assert "I will read the pages one at a time" in started_answer({**done, "output_summary": "Started."})


def test_research_finished_in_the_last_week_is_told_to_the_operator_with_its_summary_and_where_its_report_is(tmp_path):
    """Challenge c_e3 "What did you find about drones yesterday?": the only record of a finished job was "Done: the report was sent.", so the answer had nothing true to say."""
    from datetime import timedelta

    from agent_control.jobs import research
    from agent_control.jobs.context import open_job_note
    from agent_control.jobs.model import Job, JobStatus
    from agent_control.schemas import utc_now

    repos, _ = make_repos(tmp_path)
    state = {**research.new_state(10), "summary": "Small indoor drones weigh 250 g and fly about 8 minutes [1]. The sources do not say what a license costs."}
    done = repos.jobs.create(Job(kind="research", goal="what small indoor drones can do", state=state, routing=dict(ROUTING), status=JobStatus.DONE))
    old = repos.jobs.create(Job(kind="research", goal="an old one", state=state, routing=dict(ROUTING), status=JobStatus.DONE))
    with repos.jobs.database.connect() as connection:
        connection.execute("UPDATE background_jobs SET updated_at = ? WHERE id = ?", ((utc_now() - timedelta(days=30)).isoformat(), old.id))
    note = open_job_note(repos.jobs, "42", reports_dir=".agent_control/jobs")
    assert "## Research finished for this person in the last week" in note and "what small indoor drones can do" in note
    assert "weigh 250 g and fly about 8 minutes" in note and f"{done.id}" in note and "report.md" in note and "artifact.deliver" in note
    assert "an old one" not in note  # a month ago is not "the last week"
    assert "## Research is running" not in note  # nothing is running
    assert open_job_note(repos.jobs, "other", reports_dir=".agent_control/jobs") == ""
