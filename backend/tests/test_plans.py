"""Plans: a goal too big for one task, as steps that each have an owner, carried out one at a time (plans/, tools/plan_manage.py)."""

from __future__ import annotations

import pytest

from agent_control.plans.model import MAX_STEPS, Plan, PlanStatus, PlanStep, StepOwner, StepStatus
from agent_control.plans.runner import PlanRunner, cancel_plan, is_skip, render_plan, step_objective, waiting_question
from agent_control.schemas import ChannelType, TaskStatus, ToolCallRequest, ToolResultStatus
from agent_control.tools.plan_manage import PlanManageAdapter
from helpers import make_repos

ROUTING = {"source_channel": "telegram", "source_chat_id": "42", "source_sender_id": "7"}


def _plan(steps: list[PlanStep], **kwargs) -> Plan:
    return Plan(goal="Build a small drone with a language model in it", routing=dict(ROUTING), steps=steps, **kwargs)


def _steps() -> list[PlanStep]:
    return [
        PlanStep(position=1, title="Research flight controllers and small boards"),
        PlanStep(position=2, title="Buy the parts", owner=StepOwner.USER, needs=["a budget"]),
        PlanStep(position=3, title="Write the control code"),
    ]


def _finish(repos, task_id: str, status: TaskStatus = TaskStatus.COMPLETED, **metadata) -> None:
    task = repos.tasks.get(task_id)
    repos.tasks.update_metadata(task_id, {**task.metadata, **metadata}, status)


class _Outbox:
    def __init__(self) -> None:
        self.said, self.announced = [], []

    async def say(self, routing: dict[str, str], text: str) -> None:
        self.said.append((routing, text))

    async def announce(self, task) -> None:
        self.announced.append(task)


def _runner(repos, audit) -> tuple[PlanRunner, _Outbox]:
    outbox = _Outbox()
    return PlanRunner(repos, audit, say=outbox.say, announce=outbox.announce), outbox


# --- the model --------------------------------------------------------------------------------------------------------------------------------


def test_a_step_waits_for_the_one_before_it_unless_it_names_others():
    plan = _plan([
        PlanStep(position=1, title="a"), PlanStep(position=2, title="b"), PlanStep(position=3, title="c", depends_on=[1]),
    ])
    assert [s.position for s in plan.ready_steps()] == [1]
    plan.steps[0].status = StepStatus.DONE
    assert [s.position for s in plan.ready_steps()] == [2, 3]  # 3 names step 1 only, so it does not wait for 2


def test_a_skipped_step_does_not_hold_up_what_follows_and_a_plan_is_finished_when_every_step_is_done_or_skipped():
    plan = _plan(_steps())
    plan.steps[0].status, plan.steps[1].status = StepStatus.DONE, StepStatus.SKIPPED
    assert [s.position for s in plan.ready_steps()] == [3] and not plan.finished()
    plan.steps[2].status = StepStatus.DONE
    assert plan.finished() and not _plan([]).finished()


def test_the_plan_reads_as_a_short_list_with_who_does_what():
    plan = _plan(_steps())
    plan.steps[0].status = StepStatus.DONE
    text = render_plan(plan)
    assert text.splitlines()[0] == "Plan: Build a small drone with a language model in it"
    assert "[x] 1. Research flight controllers" in text and "[ ] 2. Buy the parts (you)" in text and "needs: a budget" in text


# --- storage ----------------------------------------------------------------------------------------------------------------------------------


def test_a_plan_is_stored_with_its_steps_and_routing_and_found_by_status_chat_and_task(tmp_path):
    repos, _ = make_repos(tmp_path)
    saved = repos.plans.create(_plan(_steps(), conversation_id=None))
    again = repos.plans.get(saved.id)
    assert [s.title for s in again.steps] == [s.title for s in _steps()] and again.routing == ROUTING
    assert again.steps[1].owner == StepOwner.USER and again.steps[1].needs == ["a budget"]
    assert [p.id for p in repos.plans.list(statuses=[PlanStatus.PROPOSED])] == [saved.id]
    assert repos.plans.list(statuses=[PlanStatus.ACTIVE]) == []
    repos.plans.update_step(again.steps[0].id, task_id="task_x", status=StepStatus.RUNNING)
    plan, step = repos.plans.for_task("task_x")
    assert plan.id == saved.id and step.position == 1 and repos.plans.for_task("task_nope") is None


def test_a_step_is_claimed_by_exactly_one_of_several_workers_and_added_steps_move_later_ones_down(tmp_path):
    repos, _ = make_repos(tmp_path)
    plan = repos.plans.create(_plan(_steps()))
    first = plan.steps[0].id
    assert repos.plans.claim_step(first, StepStatus.RUNNING) is True
    assert repos.plans.claim_step(first, StepStatus.RUNNING) is False
    repos.plans.add_step(plan.id, PlanStep(position=2, title="Choose the size"))
    assert [(s.position, s.title) for s in repos.plans.get(plan.id).steps] == [
        (1, "Research flight controllers and small boards"), (2, "Choose the size"), (3, "Buy the parts"), (4, "Write the control code"),
    ]
    with pytest.raises(ValueError):
        repos.plans.update_step(first, nonsense=1)


# --- the runner -------------------------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_plan_runs_one_step_at_a_time_a_user_step_is_a_question_and_the_end_is_said(tmp_path):
    repos, audit = make_repos(tmp_path)
    runner, outbox = _runner(repos, audit)
    plan = repos.plans.create(_plan(_steps(), conversation_id=None))

    assert await runner.tick() == 0  # a proposed plan does nothing until it is started
    await runner.start(plan.id)
    plan = repos.plans.get(plan.id)
    first = repos.tasks.get(plan.steps[0].task_id)
    assert plan.status == PlanStatus.ACTIVE and plan.steps[0].status == StepStatus.RUNNING and first.status == TaskStatus.RECEIVED
    assert "Step 1 of 3 of a plan." in first.objective and "Goal of the plan: Build a small drone" in first.objective
    assert first.metadata["source_chat_id"] == "42" and first.metadata["plan_id"] == plan.id and first.metadata["plan_step_id"] == plan.steps[0].id
    assert "Step 1 of 3" in outbox.said[0][1] and outbox.said[0][0] == ROUTING

    assert await runner.tick() == 0  # still running: nothing to do
    _finish(repos, first.id, synthesized_answer="The Pixhawk 6C and a Jetson Orin Nano are the usual pair.")
    await runner.tick()
    plan = repos.plans.get(plan.id)
    assert plan.steps[0].status == StepStatus.DONE and plan.steps[0].result.startswith("The Pixhawk 6C")
    second = repos.tasks.get(plan.steps[1].task_id)
    assert plan.steps[1].status == StepStatus.WAITING and second.status == TaskStatus.CLARIFYING
    assert second.metadata["clarifying_options"] == ["Done", "Skip this step"] and "Your turn (step 2 of 3)" in second.metadata["clarifying_question"]
    assert "I need: a budget" in second.metadata["clarifying_question"] and outbox.announced[-1].id == second.id

    _finish(repos, second.id, plan_step_answer="Done, ordered the Pixhawk")
    await runner.tick()
    plan = repos.plans.get(plan.id)
    third = repos.tasks.get(plan.steps[2].task_id)
    assert plan.steps[1].status == StepStatus.DONE and "What the earlier steps found:" in third.objective and "Pixhawk 6C" in third.objective

    _finish(repos, third.id, synthesized_answer="Wrote control.py")
    await runner.tick()
    assert repos.plans.get(plan.id).status == PlanStatus.DONE
    assert outbox.said[-1][1].startswith("The plan is finished: Build a small drone") and "3 of 3 steps done" in outbox.said[-1][1]
    assert await runner.tick() == 0  # a finished plan is left alone


@pytest.mark.asyncio
async def test_a_failed_step_pauses_the_plan_and_starting_it_again_carries_on(tmp_path):
    repos, audit = make_repos(tmp_path)
    runner, _ = _runner(repos, audit)
    plan = repos.plans.create(_plan(_steps()))
    await runner.start(plan.id)
    task_id = repos.plans.get(plan.id).steps[0].task_id
    _finish(repos, task_id, TaskStatus.BLOCKED, last_worker_error="no web search is switched on")
    await runner.tick()
    plan = repos.plans.get(plan.id)
    assert plan.status == PlanStatus.PAUSED and plan.steps[0].status == StepStatus.FAILED and "web search" in plan.steps[0].result
    assert await runner.tick() == 0  # paused: nothing starts
    repos.plans.update_step(plan.steps[0].id, status=StepStatus.PENDING)
    await runner.start(plan.id)
    assert repos.plans.get(plan.id).steps[0].status == StepStatus.RUNNING


@pytest.mark.asyncio
async def test_stopping_the_work_pauses_the_plan_and_saying_skip_leaves_a_step_out(tmp_path):
    repos, audit = make_repos(tmp_path)
    runner, _ = _runner(repos, audit)
    plan = repos.plans.create(_plan(_steps()))
    await runner.start(plan.id)
    _finish(repos, repos.plans.get(plan.id).steps[0].task_id, TaskStatus.CANCELLED)  # the Stop button
    await runner.tick()
    plan = repos.plans.get(plan.id)
    assert plan.status == PlanStatus.PAUSED and plan.steps[0].status == StepStatus.PENDING and plan.steps[0].task_id is None

    await runner.start(plan.id)
    _finish(repos, repos.plans.get(plan.id).steps[0].task_id, synthesized_answer="found")
    await runner.tick()
    waiting_task = repos.plans.get(plan.id).steps[1].task_id
    _finish(repos, waiting_task, TaskStatus.CANCELLED)  # "Skip this step"
    await runner.tick()
    plan = repos.plans.get(plan.id)
    assert plan.steps[1].status == StepStatus.SKIPPED and plan.steps[2].status == StepStatus.RUNNING  # step 3 goes on without step 2


@pytest.mark.asyncio
async def test_cancelling_stops_the_open_task_and_leaves_nothing_to_run(tmp_path):
    repos, audit = make_repos(tmp_path)
    runner, _ = _runner(repos, audit)
    plan = repos.plans.create(_plan(_steps()))
    await runner.start(plan.id)
    open_task = repos.plans.get(plan.id).steps[0].task_id
    cancelled = cancel_plan(repos, audit, plan.id)
    assert cancelled.status == PlanStatus.CANCELLED and {s.status for s in cancelled.steps} == {StepStatus.SKIPPED}
    assert repos.tasks.get(open_task).status == TaskStatus.CANCELLED
    assert await runner.tick() == 0


@pytest.mark.asyncio
async def test_a_step_claimed_by_a_process_that_stopped_before_its_task_existed_is_given_back(tmp_path):
    repos, audit = make_repos(tmp_path)
    runner, _ = _runner(repos, audit)
    plan = repos.plans.create(_plan(_steps()))
    repos.plans.set_status(plan.id, PlanStatus.ACTIVE)
    step = plan.steps[0]
    assert repos.plans.claim_step(step.id, StepStatus.RUNNING)
    await runner.tick()
    assert repos.plans.get(plan.id).steps[0].status == StepStatus.RUNNING  # just claimed: another worker may be creating the task
    with repos.plans.database.connect() as connection:
        connection.execute("UPDATE project_plan_steps SET updated_at = '2000-01-01T00:00:00+00:00' WHERE id = ?", (step.id,))
    await runner.tick()
    plan = repos.plans.get(plan.id)
    assert plan.steps[0].status == StepStatus.RUNNING and plan.steps[0].task_id  # given back, then started again by the same pass


def test_the_words_that_mean_skip_and_what_each_step_is_asked_to_do():
    assert is_skip("Skip this step") and is_skip("skip") and is_skip("not now, later") and not is_skip("Done") and not is_skip("")
    plan = _plan(_steps())
    plan.steps[0].status, plan.steps[0].result = StepStatus.DONE, "Q" * 900
    objective = step_objective(plan, plan.steps[2])
    assert objective.count("Q") == 300 and "Do only this step." in objective
    ask = waiting_question(plan, PlanStep(position=2, title="Give me the Amazon login", owner=StepOwner.ACCESS, needs=["an Amazon login"]))
    assert ask.startswith("I need something from you to go on (step 2 of 3)") and "skip" in ask


# --- the tool ---------------------------------------------------------------------------------------------------------------------------------


def _request(task_id: str, **payload) -> ToolCallRequest:
    return ToolCallRequest(task_id=task_id, tool_name="plan.manage", capability="agent.core", input=payload, origin="operator")


def _adapter(tmp_path):
    repos, audit = make_repos(tmp_path)
    conversation = repos.conversations.get_or_create(ChannelType.TELEGRAM, "42")
    task = repos.tasks.create("Build me a drone", conversation_id=conversation, metadata={**ROUTING, "original_message_text": "Build me a drone"})
    return repos, audit, PlanManageAdapter(repos, audit), task


@pytest.mark.asyncio
async def test_the_operator_drafts_a_plan_that_waits_for_the_user_and_its_card_goes_under_the_answer(tmp_path):
    repos, _, adapter, task = _adapter(tmp_path)
    result = await adapter.execute(_request(
        task.id, operation="create", goal="Build a small drone", summary="Research, buy, build, test.",
        steps=[{"title": "Ask what size"}, {"title": "Buy parts", "owner": "user", "needs": ["a budget"]}, {"title": "Write code", "depends_on": [1]}],
    ))
    assert result.status == ResultOK
    plan = repos.plans.get(result.output["plan_id"])
    assert plan.status == PlanStatus.PROPOSED and plan.conversation_id == task.conversation_id and plan.routing == ROUTING
    assert [s.position for s in plan.steps] == [1, 2, 3] and plan.steps[2].depends_on == [1]
    assert repos.tasks.get(task.id).metadata["plan_card"] == plan.id
    assert "Do not start any step yourself" in result.output["terminal_output"][0]["content"]


ResultOK = ToolResultStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_a_plan_that_is_not_a_plan_is_refused_with_the_reason(tmp_path):
    repos, _, adapter, task = _adapter(tmp_path)
    for bad, words in (
        ([], "between 1 and"),
        ([{"title": "a", "depends_on": [2]}, {"title": "b"}], "only wait for earlier"),
        ([{"title": ""}], "not valid"),
        ([{"title": "a", "owner": "robot"}], "not valid"),
        (["a string"], "must be an object"),
        ([{"title": f"s{i}"} for i in range(MAX_STEPS + 1)], "between 1 and"),
    ):
        result = await adapter.execute(_request(task.id, operation="create", goal="g", steps=bad))
        assert result.status == ToolResultStatus.FAILED and words in result.error_message, (bad, result.error_message)
    assert repos.plans.list() == []


@pytest.mark.asyncio
async def test_show_finds_the_newest_open_plan_of_the_chat_and_steps_can_be_changed_by_what_the_user_says(tmp_path):
    repos, audit, adapter, task = _adapter(tmp_path)
    created = await adapter.execute(_request(task.id, operation="create", goal="g", steps=[{"title": "a"}, {"title": "b"}]))
    plan_id = created.output["plan_id"]
    shown = await adapter.execute(_request(task.id, operation="show"))
    assert shown.output["plan_id"] == plan_id and "[ ] 1. a" in shown.output["terminal_output"][0]["content"]
    done = await adapter.execute(_request(task.id, operation="update_step", position=1, status="done"))
    assert done.status == ResultOK and repos.plans.get(plan_id).steps[0].status == StepStatus.DONE
    renamed = await adapter.execute(_request(task.id, operation="update_step", position=2, title="b, but in the garage", owner="user"))
    assert renamed.status == ResultOK
    assert repos.plans.get(plan_id).steps[1].title == "b, but in the garage" and repos.plans.get(plan_id).steps[1].owner == StepOwner.USER
    added = await adapter.execute(_request(task.id, operation="add_step", position=2, title="Measure the garage"))
    assert [s.title for s in repos.plans.get(plan_id).steps] == ["a", "Measure the garage", "b, but in the garage"] and added.status == ResultOK
    for refused in (
        _request(task.id, operation="update_step", position=9, status="done"),
        _request(task.id, operation="update_step", position=1, status="running"),
        _request(task.id, operation="update_step", position=1),
    ):
        assert (await adapter.execute(refused)).status == ToolResultStatus.FAILED
    started = await adapter.execute(_request(task.id, operation="set_status", status="active"))
    assert repos.plans.get(plan_id).status == PlanStatus.ACTIVE and started.status == ResultOK
    again = await adapter.execute(_request(task.id, operation="set_status", status="active"))
    assert again.status == ToolResultStatus.FAILED and "cannot be started" in again.error_message
    await adapter.execute(_request(task.id, operation="set_status", status="cancelled"))
    assert repos.plans.get(plan_id).status == PlanStatus.CANCELLED
    gone = await adapter.execute(_request(task.id, operation="show", plan_id="plan_nope"))
    assert gone.status == ToolResultStatus.FAILED


def test_the_tool_is_off_and_unnamed_unless_plans_are_enabled():
    from agent_control.config import AppSettings
    from agent_control.tools.registry import build_tool_registry

    off = build_tool_registry(AppSettings(_env_file=None), "http://127.0.0.1:8765")
    assert "plan.manage" not in off.context()


def test_retention_keeps_a_plan_that_has_not_ended_however_old_and_removes_one_that_has_with_its_steps(tmp_path):
    from agent_control.db_tools import _clean_tables

    repos, _ = make_repos(tmp_path)
    ended, open_plan = repos.plans.create(_plan(_steps())), repos.plans.create(_plan(_steps()))
    repos.plans.set_status(ended.id, PlanStatus.DONE)
    repos.plans.set_status(open_plan.id, PlanStatus.PAUSED)
    with repos.plans.database.connect() as connection:
        connection.execute("UPDATE project_plans SET updated_at = '2000-01-01T00:00:00+00:00'")
        tables = {row["name"] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        _clean_tables(connection, tables, "2020-01-01T00:00:00+00:00")
    assert repos.plans.get(ended.id) is None and repos.plans.get(open_plan.id) is not None
    with repos.plans.database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) AS n FROM project_plan_steps WHERE plan_id = ?", (ended.id,)).fetchone()["n"] == 0
        assert connection.execute("SELECT COUNT(*) AS n FROM project_plan_steps WHERE plan_id = ?", (open_plan.id,)).fetchone()["n"] == 3


def test_a_database_that_still_has_the_old_unused_plans_table_gets_plans_beside_it(tmp_path):
    """An install from before plans/ has a `plans` table of another shape (the old plan-based path). Plans live in their own tables."""
    from agent_control.storage import Database, Repositories

    path = tmp_path / "old.db"
    old = Database(f"sqlite:///{path}")
    with old.connect() as connection:
        connection.execute("CREATE TABLE plans (id TEXT PRIMARY KEY, task_id TEXT NOT NULL, steps_json TEXT NOT NULL)")
        connection.execute("INSERT INTO plans VALUES ('plan_old', 'task_old', '[]')")
    database = Database(f"sqlite:///{path}")
    database.initialize()
    repos = Repositories.for_database(database)
    saved = repos.plans.create(_plan(_steps()))
    assert repos.plans.get(saved.id).goal.startswith("Build a small drone") and repos.plans.list(statuses=[PlanStatus.PROPOSED])
    with database.connect() as connection:
        assert connection.execute("SELECT id FROM plans").fetchone()["id"] == "plan_old"  # left as it was
