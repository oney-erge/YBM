"""A big request is split into parts, each part runs in its own sub-loop, and the answer is written from what they returned."""

from __future__ import annotations

import json

import pytest

from agent_control.channels.task_notify import progress_text
from agent_control.orchestration import StaticToolAdapter, TaskWorker, ToolExecutor
from agent_control.orchestration.planner import PARTS_DONE_TEXT, Part, parse_parts, plan_parts, plan_summary, should_plan
from agent_control.orchestration.worker import CHECK_ENTRY_STRATEGY
from agent_control.config import AppSettings, CapabilityPolicy
from agent_control.policy import PolicyEngine
from agent_control.schemas import Capability, OperatorAction, OperatorDecision, RiskLevel, TaskRecord, TaskStatus
from agent_control.tools.spec import ToolDefinition
from helpers import make_repos
from test_worker_operator_loop import QueueOperator

FIVE_SITES = (
    "Get the main headline from each of these sites and tell me which one is the shortest: "
    "https://a.example.org, https://b.example.org, https://c.example.org, https://d.example.org and https://e.example.org"
)


# --- whether a request is worth splitting (shape only) ---------------------------------------------------------------------


@pytest.mark.parametrize("request_text", [
    "How many Steam games do I have installed?", "Where is my resume?", "Read C:\\notes\\a.txt", "", "   ",
])
def test_a_short_single_line_request_is_not_sent_to_the_planner(request_text):
    assert should_plan(request_text) is False


@pytest.mark.parametrize("request_text", [
    FIVE_SITES,
    "Compare C:\\a\\one.txt with C:\\a\\two.txt",
    "First do this\nthen do that\nthen the third thing",
    "x " * 90,
])
def test_a_long_request_or_one_naming_several_places_is_worth_asking_about(request_text):
    assert should_plan(request_text) is True


# --- reading the model's reply ----------------------------------------------------------------------------------------------------


def _reply(*parts):
    return json.dumps({"parts": [{"title": t, "objective": o} for t, o in parts]})


def test_a_clean_list_of_parts_is_read():
    parts = parse_parts(_reply(("Read site A", "Fetch https://a.example.org and return its headline."), ("Read site B", "Fetch https://b.example.org and return its headline.")))
    assert parts == [
        Part("Read site A", "Fetch https://a.example.org and return its headline."),
        Part("Read site B", "Fetch https://b.example.org and return its headline."),
    ]


@pytest.mark.parametrize("raw", [
    None, "", "no json here", '{"parts": []}', '{"parts": "x"}', _reply(("Only one", "do it")),
    _reply(("A", "same"), ("B", "same")), _reply(("", "no title"), ("B", "ok")),
    '{"parts": [{"title": "A", "objective": "o1"}, "not a dict"]}',
])
def test_anything_that_is_not_a_clean_list_of_at_least_two_parts_means_no_plan(raw):
    assert parse_parts(raw) == []


def test_text_around_the_json_is_ignored_and_the_list_is_capped():
    many = _reply(*[(f"Part {n}", f"Do thing {n}") for n in range(10)])
    parts = parse_parts("Sure, here you go:\n" + many + "\nHope that helps")
    assert len(parts) == 6 and parts[0].title == "Part 0"


class _Provider:
    def __init__(self, reply=None, error=None):
        self.reply, self.error, self.calls = reply, error, []

    async def generate_text(self, system_prompt, user_prompt):
        self.calls.append((system_prompt, user_prompt))
        if self.error:
            raise self.error
        return self.reply


@pytest.mark.asyncio
async def test_the_planner_asks_the_model_only_for_a_request_worth_asking_about_and_never_fails_the_task():
    provider = _Provider(_reply(("A", "do a thing here"), ("B", "do another thing")))
    assert await plan_parts(provider, "Where is my resume?") == [] and provider.calls == []
    assert len(await plan_parts(provider, FIVE_SITES)) == 2 and len(provider.calls) == 1
    assert await plan_parts(_Provider(error=RuntimeError("model down")), FIVE_SITES) == []
    assert await plan_parts(None, FIVE_SITES) == []


def test_the_plan_is_put_in_words_for_the_person():
    text = plan_summary([{"title": "Read site A", "objective": "x"}, {"title": "Read site B", "objective": "y"}])
    assert text == "This has 2 parts, so I will do them one at a time:\n1. Read site A\n2. Read site B"


# --- how the worker runs the parts ----------------------------------------------------------------------------------------------------


class _Seen(QueueOperator):
    def __init__(self, decisions, provider):
        super().__init__(decisions)
        self.provider = provider
        self.histories: list[list[dict]] = []
        self.objectives: list[str] = []

    async def decide(self, objective, config_context, history, **kwargs):
        self.histories.append([dict(entry) for entry in history])
        self.objectives.append(objective)
        return await super().decide(objective, config_context, history, **kwargs)


def _worker(repos, audit, operator, *, plan=True):
    settings = AppSettings(
        _env_file=None,
        capabilities={Capability.LLM_GENERATE: CapabilityPolicy(enabled=True, requires_approval=False, max_risk_level=RiskLevel.LOW)},
    )
    definitions = {"llm": ToolDefinition(name="llm", capability=Capability.LLM_GENERATE, enabled=True, description="t")}
    executor = ToolExecutor(PolicyEngine(settings, audit), repos, audit, adapters={"llm": StaticToolAdapter({"answer": "headline"})},
                            tool_definitions=definitions)
    return TaskWorker(repos, audit, executor=executor, operator=operator, plan_parts=plan)


@pytest.mark.asyncio
async def test_a_planned_request_runs_each_part_as_a_delegate_then_the_model_writes_the_answer(tmp_path):
    repos, audit = make_repos(tmp_path)
    provider = _Provider(_reply(
        ("Headline of site A", "Fetch https://a.example.org and return its headline."),
        ("Headline of site B", "Fetch https://b.example.org and return its headline."),
    ))
    # Each part is a sub-loop that answers at once; then the parent writes the final answer.
    operator = _Seen([
        OperatorDecision(action=OperatorAction.DONE, final_answer="A: Alpha"),
        OperatorDecision(action=OperatorAction.DONE, final_answer="B: Beta News"),
        OperatorDecision(action=OperatorAction.DONE, final_answer="A is shorter: Alpha (5 letters) against Beta News (9)."),
    ], provider)
    task = repos.tasks.create(FIVE_SITES, metadata={"original_message_text": FIVE_SITES})
    worker = _worker(repos, audit, operator)
    current = await worker.process_task(task.id)
    assert current.metadata["plan_parts"][0]["title"] == "Headline of site A" and current.metadata["plan_index"] == 1
    for _ in range(4):
        if current.status in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.BLOCKED}:
            break
        current = await worker.process_task(current.id)
    assert current.status == TaskStatus.COMPLETED
    assert current.metadata["synthesized_answer"].startswith("A is shorter")
    delegated = [e for e in current.metadata["operator_history"] if e.get("tool_name") == "delegate"]
    assert [e["input"]["objective"] for e in delegated] == [
        "Fetch https://a.example.org and return its headline.", "Fetch https://b.example.org and return its headline.",
    ]
    # Before the parent wrote its answer it was told every part had run.
    assert any(e.get("hint_id") == "parts_done" and e["error"] == PARTS_DONE_TEXT for e in operator.histories[-1] if e["tool_name"] == CHECK_ENTRY_STRATEGY)
    # The task was given the time its parts need.
    assert current.metadata["task_budget_seconds"] >= 480


@pytest.mark.asyncio
async def test_a_request_the_model_does_not_split_runs_as_it_always_did(tmp_path):
    repos, audit = make_repos(tmp_path)
    provider = _Provider('{"parts": []}')
    operator = _Seen([OperatorDecision(action=OperatorAction.DONE, final_answer="Done in one go.")], provider)
    task = repos.tasks.create(FIVE_SITES, metadata={"original_message_text": FIVE_SITES})
    done = await _worker(repos, audit, operator).process_task(task.id)
    assert done.status == TaskStatus.COMPLETED and done.metadata["plan_parts"] == []
    assert len(provider.calls) == 1


@pytest.mark.asyncio
async def test_planning_is_off_unless_it_is_switched_on_and_never_happens_mid_task(tmp_path):
    repos, audit = make_repos(tmp_path)
    provider = _Provider(_reply(("A", "do a thing here"), ("B", "do another thing")))
    operator = _Seen([OperatorDecision(action=OperatorAction.DONE, final_answer="ok")], provider)
    task = repos.tasks.create(FIVE_SITES, metadata={"original_message_text": FIVE_SITES})
    done = await _worker(repos, audit, operator, plan=False).process_task(task.id)
    assert provider.calls == [] and "plan_parts" not in done.metadata

    operator2 = _Seen([OperatorDecision(action=OperatorAction.DONE, final_answer="ok")], provider)
    midway = repos.tasks.create(FIVE_SITES, metadata={"original_message_text": FIVE_SITES, "operator_loop": True,
                                                       "operator_history": [{"tool_name": "llm", "input": {}, "status": "succeeded"}]})
    repos.tasks.update_metadata(midway.id, midway.metadata, TaskStatus.RUNNING)
    await _worker(repos, audit, operator2).process_task(midway.id)
    assert provider.calls == []


# --- the person sees which part, and that it is still going ---------------------------------------------------------------------------


def test_the_progress_line_names_the_part_being_worked_on():
    plan = [{"title": "Headline of site A", "objective": "x"}, {"title": "Headline of site B", "objective": "y"}]
    task = TaskRecord(objective="x", status=TaskStatus.RUNNING, metadata={"plan_parts": plan, "plan_index": 2})
    assert progress_text(task) == "Working on part 2 of 2: Headline of site B…"
    assert progress_text(TaskRecord(objective="x", status=TaskStatus.RUNNING, metadata={"plan_parts": plan, "plan_index": 0})).startswith("Working on part 1 of 2")


def test_a_long_step_says_how_long_it_has_been_going():
    plan = [{"title": "Read it", "objective": "x"}, {"title": "Then that", "objective": "y"}]
    task = TaskRecord(objective="x", status=TaskStatus.RUNNING, metadata={"plan_parts": plan, "plan_index": 1, "progress_waiting_seconds": 45})
    assert progress_text(task) == "Working on part 1 of 2: Read it… (still going, 45 s on this step)"


@pytest.mark.asyncio
async def test_while_a_tick_runs_long_the_progress_line_is_sent_again_with_the_time(tmp_path):
    import asyncio

    repos, audit = make_repos(tmp_path)

    class _Slow(QueueOperator):
        provider = None

        async def decide(self, *args, **kwargs):
            await asyncio.sleep(0.35)
            return await super().decide(*args, **kwargs)

    sent = []

    class _Sink:
        async def notify(self, task):
            sent.append(dict(task.metadata))

    operator = _Slow([OperatorDecision(action=OperatorAction.DONE, final_answer="ok")])
    worker = _worker(repos, audit, operator, plan=False)
    worker.heartbeat_seconds = 0.1
    worker.notification_sink = _Sink()
    task = repos.tasks.create("Find a thing")
    await worker.process_task(task.id)
    waits = [m["progress_waiting_seconds"] for m in sent if "progress_waiting_seconds" in m]
    assert waits and all(isinstance(w, int) for w in waits)
