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
    adapter = StaticToolAdapter({"summary": "Research started (job_x): up to 12 pages.", "answer": "Research started."})
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
    assert operator.calls == 1  # only for the answer: the first step was not its to take
    assert finished.status == TaskStatus.COMPLETED and "running in the background" in str(finished.metadata.get("synthesized_answer") or finished.metadata)


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
