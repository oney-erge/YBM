"""A request a skill routes to a long job starts it with a call written in code (orchestration/job_start.py, and the worker's first step)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agent_control.orchestration import StaticToolAdapter, TaskWorker
from agent_control.orchestration.fulfillment import _postcondition_satisfied
from agent_control.orchestration.job_start import MAX_PAGES, MIN_PAGES, JobIntentUnavailable, JobPageLimitExceeded, _Start, start_research_call
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
    provider = _Provider(_Start(action="start", goal="How people heat old stone houses without making them damp", pages=12))
    call = await start_research_call(provider, REQUEST)
    assert call == {"operation": "start_research", "goal": "How people heat old stone houses without making them damp", "max_pages": 12}
    system, user, schema = provider.asked[0]
    assert schema == "_Start" and REQUEST in user and "Do not add anything they did not say" in system


@pytest.mark.asyncio
async def test_a_number_outside_what_a_job_accepts_is_rejected_without_silent_reduction():
    for named in (MIN_PAGES, MAX_PAGES, 500, 60):
        call = await start_research_call(_Provider(_Start(action="start", goal="g", pages=named)), REQUEST)
        assert call["max_pages"] == named
    for named in (0, 3, 5000):
        with pytest.raises(JobPageLimitExceeded, match=str(named)):
            await start_research_call(_Provider(_Start(action="start", goal="g", pages=named)), REQUEST)
    assert "max_pages" not in await start_research_call(_Provider(_Start(action="start", goal="g", pages=None)), REQUEST)


@pytest.mark.asyncio
async def test_an_unsupported_page_request_explains_the_limit_without_starting(tmp_path):
    repos, audit = make_repos(tmp_path)
    async def stage(task):
        return await start_research_call(_Provider(_Start(action="start", goal="g", pages=5000)), task.objective)
    task = _task(repos, task_route="job")
    decision = await TaskWorker(repos, audit, job_stage=stage)._started_job(task, [])
    assert decision.action == OperatorAction.BLOCKED
    assert "5000" in decision.reason and "1000" in decision.reason and "no research job was started" in decision.reason
    assert not repos.tasks.get(task.id).metadata.get("job_forced_call")


@pytest.mark.asyncio
async def test_unknown_research_action_does_not_default_to_start(caplog):
    words = " ".join(REQUEST.split())
    for request in ("Pause the research.", "Resume the research.", "Stop the research.", "Research status?", REQUEST):
        with pytest.raises(JobIntentUnavailable):
            await start_research_call(_Provider(TimeoutError("synthetic-secret")), request)
    with pytest.raises(JobIntentUnavailable):
        await start_research_call(None, REQUEST)
    with pytest.raises(JobIntentUnavailable):
        await start_research_call(_Provider(None), REQUEST)
    with pytest.raises(ValidationError):
        _Start.model_validate({"goal": "missing action"})
    assert "synthetic-secret" not in caplog.text
    assert (await start_research_call(_Provider(_Start(action="start", goal="  ")), REQUEST))["goal"] == words
    assert len((await start_research_call(_Provider(_Start(action="start")), "x " * 3000))["goal"]) <= 2000


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
async def test_a_message_that_only_arrives_while_a_job_is_open_may_control_it_but_not_start_another(tmp_path):
    """Frozen exam 11/18: a count of ERROR lines in log files, asked while jobs were open, was read as "investigate" and an investigation was asked for."""
    repos, audit = make_repos(tmp_path)

    def stage_of(call):
        async def stage(task):
            return call
        return stage

    for call in ({"operation": "start_investigation", "goal": "count the ERROR lines"}, {"operation": "start_research", "goal": "g"}, {"operation": "start_watch", "target": "C:/x"}):
        task = _task(repos, task_route="job_open")
        assert await TaskWorker(repos, audit, job_stage=stage_of(call))._started_job(task, []) is None  # the Operator takes the message
        assert repos.tasks.get(task.id).metadata["job_start_done"] is True
    for operation in ("status", "pause", "resume", "stop"):
        decision = await TaskWorker(repos, audit, job_stage=stage_of({"operation": operation}))._started_job(_task(repos, task_route="job_open"), [])
        assert decision.action == OperatorAction.CALL_TOOL and decision.tool_input == {"operation": operation}
    task = _task(repos, task_route="job")  # a skill that took it for a request for a job still starts one
    started = await TaskWorker(repos, audit, job_stage=stage_of({"operation": "start_investigation", "goal": "g"}))._started_job(task, [])
    assert started.tool_input["operation"] == "start_investigation"


@pytest.mark.asyncio
async def test_only_a_routed_task_with_a_stage_is_started_and_a_failed_stage_blocks(tmp_path):
    repos, audit = make_repos(tmp_path)
    calls: list = []
    assert await TaskWorker(repos, audit, job_stage=_stage(calls))._started_job(_task(repos), []) is None  # no route
    assert await TaskWorker(repos, audit, job_stage=_stage(calls))._started_job(_task(repos, task_route="plan"), []) is None  # another route
    assert await TaskWorker(repos, audit)._started_job(_task(repos, task_route="job"), []) is None  # no stage
    assert calls == []

    async def broken(task):
        raise RuntimeError("no model")

    decision = await TaskWorker(repos, audit, job_stage=broken)._started_job(_task(repos, task_route="job"), [])
    assert decision.action == OperatorAction.BLOCKED
    assert "Check its status and try again" in decision.reason


@pytest.mark.asyncio
async def test_failed_intent_does_not_dispatch_or_let_operator_guess(tmp_path, caplog):
    from agent_control.channels.task_notify import format_task_message

    repos, audit = make_repos(tmp_path)
    adapter = StaticToolAdapter({"summary": "unexpected"})
    operator = QueueOperator([OperatorDecision(action=OperatorAction.CALL_TOOL, tool_name="job.manage", tool_input={"operation": "start_research", "goal": "unsafe"})])

    async def stage(task):
        return await start_research_call(_Provider(TimeoutError("synthetic-secret")), task.objective)

    worker = TaskWorker(repos, audit, executor=_executor_with_adapter(_settings(), audit, repos, adapter, tool_name="job.manage"), operator=operator, job_stage=stage)
    conversation = repos.conversations.get_or_create(ChannelType.TELEGRAM, "42")
    task = repos.tasks.create("Pause the research.", conversation_id=conversation, metadata={**ROUTING, "original_message_text": "Pause the research.", "task_route": "job"})
    await worker.process_next()
    finished = repos.tasks.get(task.id)
    assert finished.status == TaskStatus.BLOCKED
    assert operator.calls == 0 and adapter.requests == []
    assert "Check its status and try again" in format_task_message(finished)
    assert "synthetic-secret" not in caplog.text


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
async def test_the_message_is_turned_into_the_call_it_asks_for_and_something_else_into_none():
    """Live 11/14: "How far along is the research?" started a second job whose goal was that sentence; live 11/15: a pause was never made."""
    status = _Provider(_Start(action="status"))
    assert await start_research_call(status, "How far along is the research?") == {"operation": "status"}
    for action in ("pause", "resume", "stop"):
        assert await start_research_call(_Provider(_Start(action=action)), "whatever") == {"operation": action}
    assert await start_research_call(_Provider(_Start(action="other")), "What is the weather?") is None
    assert (await start_research_call(_Provider(_Start(action="start", goal="g")), REQUEST))["operation"] == "start_research"
    assert "already under way" in status.asked[0][0] and '"pause", "resume" or "stop"' in status.asked[0][0]


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
    assert "## A long job is running for this person in this chat" in note and "how people heat old stone houses" in note and "(running)" in note
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


def test_what_the_call_did_is_told_to_the_person_in_code_without_a_job_number_and_a_failure_is_said_as_one() -> None:
    """Live 11/15: after the job started the Operator went on calling the job tool with the id of an earlier job, offered Claude Code, and asked whether to wait or pause;
    a pause was never made and "has been stopped" was said about a job that had not been stopped."""
    from agent_control.orchestration.planning import _job_call_entry, job_answer, started_answer

    done = {"tool_name": "job.manage", "status": "succeeded", "input": {"operation": "start_research", "goal": "g"}, "output_summary": "Started the research on: g. It reads up to 10 pages, one at a time"}
    assert _job_call_entry([{"tool_name": "_fulfillment_check"}, done], "start_research") is done
    for history in ([], [{"tool_name": "web.search", "status": "succeeded"}]):
        assert _job_call_entry(history, "start_research") is None
    assert _job_call_entry([done], "pause") is None
    said = started_answer(done)
    assert "up to 10 pages" in said and "job_" not in said and "pause or stop it" in said
    assert "I will read the pages one at a time" in started_answer({**done, "output_summary": "Started."})
    assert job_answer(done) == said
    paused = {"tool_name": "job.manage", "status": "succeeded", "input": {"operation": "pause"}, "output_summary": "Paused the research. Nothing more is read until 'resume'."}
    assert job_answer(paused) == "Paused the research. Nothing more is read until 'resume'."
    refused = {"tool_name": "job.manage", "status": "failed", "input": {"operation": "pause"}, "error": "there is no research running in this chat to pause."}
    assert job_answer(refused) == "I could not do that: there is no research running in this chat to pause."
    assert job_answer({**refused, "error": None}) == "I could not do that."


@pytest.mark.asyncio
async def test_a_pause_is_made_and_answered_in_code_and_a_refused_one_is_said_so_never_claimed(tmp_path):
    """Live 11/15, "Pause the research.": the Operator called pause with a task id, said the job was "already paused", and later said "has been stopped" about a job that was still running."""
    repos, audit = make_repos(tmp_path)

    async def stage(task):
        return {"operation": "pause"}

    adapter = StaticToolAdapter({"summary": "Paused the research. Nothing more is read until 'resume'.", "answer": "ok"})
    operator = QueueOperator([])
    worker = TaskWorker(repos, audit, executor=_executor_with_adapter(_settings(), audit, repos, adapter, tool_name="job.manage"), operator=operator, job_stage=stage)
    conversation = repos.conversations.get_or_create(ChannelType.TELEGRAM, "42")
    task = repos.tasks.create("Pause the research.", conversation_id=conversation, metadata={**ROUTING, "original_message_text": "Pause the research.", "task_route": "job"})
    for _ in range(4):
        await worker.process_next()
        if repos.tasks.get(task.id).status == TaskStatus.COMPLETED:
            break
    finished = repos.tasks.get(task.id)
    assert [r.input["operation"] for r in adapter.requests] == ["pause"] and operator.calls == 0
    assert finished.status == TaskStatus.COMPLETED and finished.metadata["job_forced_call"] == "pause"
    assert str(finished.metadata.get("synthesized_answer")) == "Paused the research. Nothing more is read until 'resume'."


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


@pytest.mark.asyncio
async def test_a_list_to_find_is_a_survey_and_something_to_keep_watching_is_a_monitor_with_its_script():
    survey = await start_research_call(_Provider(_Start(action="survey", goal="Find all the daycares around Pearland and compare them")), "Go search all the daycares around me and do a large comparative analysis.")
    assert survey == {"operation": "start_survey", "goal": "Find all the daycares around Pearland and compare them"}
    script = "import subprocess\nout = subprocess.run(['arp', '-a'], capture_output=True, text=True).stdout\nprint(out)\n"
    monitor = await start_research_call(
        _Provider(_Start(action="monitor", name="network-devices", script=script, every_minutes=10, goal="Tell me when a new device shows up")),
        "Connect to every device on my network and tell me when a new one shows up.",
    )
    assert monitor == {"operation": "start_watch", "script": script, "name": "network-devices", "goal": "Tell me when a new device shows up", "every_minutes": 10}
    # no script written: the Operator takes it with the skill instead of a call that cannot work
    assert await start_research_call(_Provider(_Start(action="monitor", name="x")), "Watch my network.") is None
    system = _Provider(_Start(action="other"))
    await start_research_call(system, "hello")
    assert "all the daycares around me and compare them" in system.asked[0][0] and "ONLY when they ask for research on a topic" in system.asked[0][0]


@pytest.mark.asyncio
async def test_a_monitor_script_that_failed_its_test_goes_back_to_the_operator_to_be_fixed_and_one_that_passed_is_answered_in_code(tmp_path):
    from agent_control.orchestration.job_start import SYSTEM

    assert 'subprocess.run(["arp", "-a"]' in SYSTEM and "01-00-5e" in SYSTEM  # the stage is given a script that was tried on a real PC
    repos, audit = make_repos(tmp_path)
    worker = TaskWorker(repos, audit, job_stage=_stage([]))
    failed = [{"tool_name": "job.manage", "input": {"operation": "start_watch", "script": "print()", "name": "d"}, "status": "failed", "error": "the script printed nothing"}]
    task = _task(repos, task_route="job", job_forced_call="start_watch", job_start_done=True)
    assert await worker._started_job(task, failed) is None  # the Operator fixes it with the reason in front of it
    passed = [{"tool_name": "job.manage", "input": {"operation": "start_watch"}, "status": "succeeded", "output_summary": "Started watching script:d every 10 minutes."}]
    decision = await worker._started_job(task, passed)
    assert decision is not None and decision.action == OperatorAction.DONE
    refused = [{"tool_name": "job.manage", "input": {"operation": "start_research"}, "status": "failed", "error": "2 researches are already open"}]
    research = _task(repos, task_route="job", job_forced_call="start_research", job_start_done=True)
    assert (await worker._started_job(research, refused)).action == OperatorAction.DONE  # a refused research is still said in code
