"""Plans that last months: steps with days, held until their day is near, in the order of their days; check-ins; shifting the days; notes (plans/)."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from agent_control.plans.card import card_text
from agent_control.plans.context import open_plan_note
from agent_control.plans.model import HOLD_DAYS, Plan, PlanStatus, PlanStep, StepOwner, StepStatus, in_date_order
from agent_control.plans.planner import PlanDraft, StepDraft
from agent_control.plans.runner import PlanRunner, check_in_text, day_text, render_plan, step_objective
from agent_control.schemas import ChannelType, ToolCallRequest, ToolResultStatus, utc_now
from agent_control.tools.plan_manage import PlanManageAdapter
from helpers import make_repos

ROUTING = {"source_channel": "telegram", "source_chat_id": "42", "source_sender_id": "7"}
TODAY = date.today()


def _in(days: int) -> date:
    return TODAY + timedelta(days=days)


def _plan(steps: list[PlanStep], **kwargs) -> Plan:
    return Plan(goal="Build a house on my plot", routing=dict(ROUTING), steps=steps, **kwargs)


class _Outbox:
    def __init__(self) -> None:
        self.said: list[str] = []

    async def say(self, routing, text) -> None:
        self.said.append(text)

    async def announce(self, task) -> None:
        return None


# --- the order of the days ------------------------------------------------------------------------------------------------------------------


def test_steps_are_put_in_the_order_of_their_days_and_what_they_wait_for_follows_them():
    steps = [
        PlanStep(position=1, title="Hire the builder", due=_in(60)),
        PlanStep(position=2, title="Find out the local rules", due=_in(3)),
        PlanStep(position=3, title="Get the permit", due=_in(40), depends_on=[2]),
        PlanStep(position=4, title="Sign the builder's contract", due=_in(70), depends_on=[1, 3]),
    ]
    ordered = in_date_order(steps, TODAY)
    assert [s.title for s in ordered] == ["Find out the local rules", "Get the permit", "Hire the builder", "Sign the builder's contract"]
    assert [s.position for s in ordered] == [1, 2, 3, 4]
    assert [s.depends_on for s in ordered] == [[], [1], [], [2, 3]]


def test_a_step_without_a_day_stays_with_the_one_before_it_and_a_day_in_the_past_is_today():
    ordered = in_date_order([
        PlanStep(position=1, title="a", due=_in(20)),
        PlanStep(position=2, title="b"),  # no day: it stays after a
        PlanStep(position=3, title="c", due=_in(-5)),  # in the past: today, so it comes first
    ], TODAY)
    assert [s.title for s in ordered] == ["c", "a", "b"] and ordered[0].due == TODAY
    # nothing has a day: nothing moves
    plain = [PlanStep(position=i, title=t) for i, t in enumerate("xyz", 1)]
    assert [s.title for s in in_date_order(plain, TODAY)] == ["x", "y", "z"]


def test_a_day_that_is_not_one_is_no_day_and_one_far_away_is_dropped():
    assert PlanStep(position=1, title="a", due="2026-12-06").due == date(2026, 12, 6)
    assert PlanStep(position=1, title="a", due="next spring").due is None and PlanStep(position=1, title="a", due="").due is None
    assert PlanStep(position=1, title="a", due="2026-02-31").due is None
    far = in_date_order([PlanStep(position=1, title="a", due=_in(366 * 9))], TODAY)
    assert far[0].due is None


# --- held until the day is near ---------------------------------------------------------------------------------------------------------


def test_a_step_is_not_ready_until_its_day_is_within_a_week_and_the_earliest_held_one_is_next():
    plan = _plan([
        PlanStep(position=1, title="Rules", due=_in(HOLD_DAYS)),
        PlanStep(position=2, title="Permit", due=_in(HOLD_DAYS + 1), depends_on=[]),
        PlanStep(position=3, title="Builder", due=_in(90), depends_on=[1]),
    ])
    assert [s.position for s in plan.ready_steps(TODAY)] == [1]
    plan.steps[0].status = StepStatus.DONE
    assert plan.ready_steps(TODAY) == [] and plan.next_held(TODAY).title == "Permit"
    assert [s.position for s in plan.ready_steps(_in(2))] == [2]  # the day comes near


def test_steps_without_days_are_never_held():
    plan = _plan([PlanStep(position=1, title="a"), PlanStep(position=2, title="b")])
    assert [s.position for s in plan.ready_steps(TODAY)] == [1] and plan.next_held(TODAY) is None


@pytest.mark.asyncio
async def test_a_plan_for_months_waits_for_each_day_and_says_so_on_its_card(tmp_path):
    repos, audit = make_repos(tmp_path)
    outbox = _Outbox()
    runner = PlanRunner(repos, audit, say=outbox.say, announce=outbox.announce)
    plan = repos.plans.create(_plan([PlanStep(position=1, title="Choose the builder", due=_in(30)), PlanStep(position=2, title="Sign", due=_in(45))]))
    await runner.start(plan.id)
    plan = repos.plans.get(plan.id)
    assert plan.status == PlanStatus.ACTIVE and plan.steps[0].status == StepStatus.PENDING and plan.steps[0].task_id is None
    assert await runner.tick() == 0 and outbox.said == []
    assert f"Nothing to do until {day_text(_in(30))}: Choose the builder." in card_text(plan)

    repos.plans.update_step(plan.steps[0].id, due=_in(3))  # the day comes near: the step starts by itself
    await runner.tick()
    assert repos.plans.get(plan.id).steps[0].status == StepStatus.RUNNING and "Step 1 of 2" in outbox.said[0]


# --- check-ins --------------------------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_active_plan_checks_in_when_the_days_have_passed_and_not_before_or_again(tmp_path):
    repos, audit = make_repos(tmp_path)
    outbox = _Outbox()
    runner = PlanRunner(repos, audit, say=outbox.say, announce=outbox.announce)
    steps = [PlanStep(position=1, title="Rules", due=_in(40)), PlanStep(position=2, title="Permit", due=_in(80))]
    plan = repos.plans.create(_plan(steps, check_in_days=7, last_check_in_at=utc_now() - timedelta(days=3)))
    repos.plans.set_status(plan.id, PlanStatus.ACTIVE)
    await runner.tick()
    assert outbox.said == []  # three days of seven

    repos.plans.update_plan(plan.id, last_check_in_at=utc_now() - timedelta(days=8))
    await runner.tick()
    assert len(outbox.said) == 1
    text = outbox.said[0]
    assert text.startswith("Check-in on your plan: Build a house on my plot") and "0 of 2 steps done." in text
    assert f"Next: Rules, due {day_text(_in(40))}." in text and "Tell me if anything changed" in text
    await runner.tick()
    assert len(outbox.said) == 1  # said once for this period

    repos.plans.set_status(plan.id, PlanStatus.PAUSED)
    repos.plans.update_plan(plan.id, last_check_in_at=utc_now() - timedelta(days=30))
    await runner.tick()
    assert len(outbox.said) == 1  # a paused plan is quiet
    repos.plans.set_status(plan.id, PlanStatus.ACTIVE)
    repos.plans.update_plan(plan.id, check_in_days=0)
    await runner.tick()
    assert len(outbox.said) == 1  # never asked for: never said


def test_a_check_in_says_what_is_done_what_waits_for_the_user_and_what_is_late():
    plan = _plan([
        PlanStep(position=1, title="Rules", status=StepStatus.DONE),
        PlanStep(position=2, title="Choose the plot layout", owner=StepOwner.USER, status=StepStatus.WAITING),
        PlanStep(position=3, title="Get the permit", due=_in(-4)),
    ])
    text = check_in_text(plan, TODAY)
    assert "1 of 3 steps done." in text and "Waiting for you: Choose the plot layout." in text
    assert f"Past its day: Get the permit (was due {day_text(_in(-4))})." in text


def test_a_check_in_does_not_count_skipped_steps_as_done():
    plan = _plan([PlanStep(position=1, title="Skipped", status=StepStatus.SKIPPED),
                  PlanStep(position=2, title="Done", status=StepStatus.DONE)])
    text = check_in_text(plan, TODAY)
    assert "1 of 2 steps done." in text and "1 step skipped." in text


# --- the tool ---------------------------------------------------------------------------------------------------------------------------------


def _request(task_id: str, **payload) -> ToolCallRequest:
    return ToolCallRequest(task_id=task_id, tool_name="plan.manage", capability="agent.core", input=payload, origin="operator")


def _adapter(tmp_path):
    repos, audit = make_repos(tmp_path)
    conversation = repos.conversations.get_or_create(ChannelType.TELEGRAM, "42")
    task = repos.tasks.create("Plan my house", conversation_id=conversation, metadata={**ROUTING, "original_message_text": "Plan my house"})
    return repos, audit, PlanManageAdapter(repos, audit), task, conversation


class _Planner:
    def __init__(self, draft) -> None:
        self.draft = draft

    async def generate_structured(self, system_prompt, user_prompt, schema, **kwargs):
        return self.draft


@pytest.mark.asyncio
async def test_a_drafted_plan_is_saved_in_the_order_of_its_days_whatever_order_the_model_wrote_them(tmp_path):
    repos, _, adapter, task, _ = _adapter(tmp_path)
    adapter.provider = _Planner(PlanDraft(summary="A house in about a year.", steps=[
        StepDraft(title="Hire the builder", due=_in(120).isoformat()),
        StepDraft(title="Find out the local rules and permits", due=_in(5).isoformat()),
        StepDraft(title="Buy the land papers check", owner="user", due="soon"),
    ]))
    result = await adapter.execute(_request(task.id, operation="create", goal="Build a house on my plot next year"))
    assert result.status == ToolResultStatus.SUCCEEDED
    plan = repos.plans.get(result.output["plan_id"])
    assert [s.title for s in plan.steps] == ["Find out the local rules and permits", "Buy the land papers check", "Hire the builder"]
    assert plan.steps[0].due == _in(5) and plan.steps[1].due is None and plan.steps[2].due == _in(120)
    assert f"{day_text(_in(5))} - Find out the local rules" in result.output["terminal_output"][0]["content"]


@pytest.mark.asyncio
async def test_check_ins_dates_and_notes_are_changed_by_what_the_user_says(tmp_path):
    repos, _, adapter, task, conversation = _adapter(tmp_path)
    created = await adapter.execute(_request(task.id, operation="create", goal="Build a house", steps=[
        {"title": "Rules", "due": _in(10).isoformat()}, {"title": "Permit", "due": _in(30).isoformat()}, {"title": "Builder", "due": _in(60).isoformat()},
    ]))
    plan_id = created.output["plan_id"]

    on = await adapter.execute(_request(task.id, operation="set_check_in", days=7))
    assert on.status == ToolResultStatus.SUCCEEDED and repos.plans.get(plan_id).check_in_days == 7 and repos.plans.get(plan_id).last_check_in_at is not None
    assert "every 7 days" in on.output["terminal_output"][0]["content"]
    off = await adapter.execute(_request(task.id, operation="set_check_in", days=0))
    assert repos.plans.get(plan_id).check_in_days == 0 and "No more check-ins" in off.output["terminal_output"][0]["content"]
    assert (await adapter.execute(_request(task.id, operation="set_check_in", days=400))).status == ToolResultStatus.FAILED

    repos.plans.update_step(repos.plans.get(plan_id).steps[0].id, status=StepStatus.DONE)
    moved = await adapter.execute(_request(task.id, operation="shift_dates", days=14))
    assert moved.status == ToolResultStatus.SUCCEEDED and "2 steps moved by 14 days" in moved.output["terminal_output"][0]["content"]
    assert [s.due for s in repos.plans.get(plan_id).steps] == [_in(10), _in(44), _in(74)]  # the finished step stays where it was
    for refused in (_request(task.id, operation="shift_dates", days=0), _request(task.id, operation="shift_dates", days=5000)):
        assert (await adapter.execute(refused)).status == ToolResultStatus.FAILED

    await adapter.execute(_request(task.id, operation="add_note", note="The plot is 600 square meters outside Bursa."))
    await adapter.execute(_request(task.id, operation="add_note", note="The budget is 250,000 euros."))
    plan = repos.plans.get(plan_id)
    assert plan.notes == "- The plot is 600 square meters outside Bursa.\n- The budget is 250,000 euros."
    assert "What the user has told you about this project:\n- The plot is 600" in step_objective(plan, plan.steps[1])
    note = open_plan_note(repos.plans, conversation)
    assert "(due " in note and "- The budget is 250,000 euros." in note and "shift_dates" in note and "set_check_in" in note
    assert (await adapter.execute(_request(task.id, operation="add_note", note="x" * 3))).status == ToolResultStatus.SUCCEEDED
    assert (await adapter.execute(_request(task.id, operation="add_note", note="  "))).status == ToolResultStatus.FAILED


@pytest.mark.asyncio
async def test_a_step_is_given_a_day_and_a_day_that_is_not_one_is_refused(tmp_path):
    repos, _, adapter, task, _ = _adapter(tmp_path)
    created = await adapter.execute(_request(task.id, operation="create", goal="g", steps=[{"title": "a"}, {"title": "b"}]))
    plan_id = created.output["plan_id"]
    done = await adapter.execute(_request(task.id, operation="update_step", position=2, due="2027-03-01"))
    assert done.status == ToolResultStatus.SUCCEEDED and repos.plans.get(plan_id).steps[1].due == date(2027, 3, 1)
    bad = await adapter.execute(_request(task.id, operation="update_step", position=2, due="after the rain"))
    assert bad.status == ToolResultStatus.FAILED and "YYYY-MM-DD" in bad.error_message
    cleared = await adapter.execute(_request(task.id, operation="update_step", position=2, due=""))
    assert cleared.status == ToolResultStatus.SUCCEEDED and repos.plans.get(plan_id).steps[1].due is None


# --- storage ----------------------------------------------------------------------------------------------------------------------------------


def test_days_notes_and_check_ins_are_stored_and_the_days_of_unfinished_steps_shift(tmp_path):
    repos, _ = make_repos(tmp_path)
    saved = repos.plans.create(_plan([PlanStep(position=1, title="a", due=_in(2)), PlanStep(position=2, title="b", due=_in(9))], notes="- a fact", check_in_days=14))
    again = repos.plans.get(saved.id)
    assert [s.due for s in again.steps] == [_in(2), _in(9)] and again.notes == "- a fact" and again.check_in_days == 14 and again.last_check_in_at is None
    repos.plans.update_step(again.steps[0].id, status=StepStatus.DONE)
    assert repos.plans.shift_dates(saved.id, -1) == 1
    assert [s.due for s in repos.plans.get(saved.id).steps] == [_in(2), _in(8)]
    with pytest.raises(ValueError):
        repos.plans.update_plan(saved.id, goal="nope")


def test_date_shift_rechecks_plan_revision_and_rolls_back_every_step_on_error(tmp_path):
    repos, _ = make_repos(tmp_path)
    plan = repos.plans.create(_plan([PlanStep(position=1, title="First", due=date(2027, 1, 1)),
                                     PlanStep(position=2, title="Last", due=date.max)]))
    with pytest.raises(ValueError, match="cannot be shifted"):
        repos.plans.shift_dates(plan.id, 1, expected_revision=plan.revision)
    unchanged = repos.plans.get(plan.id)
    assert [step.due for step in unchanged.steps] == [date(2027, 1, 1), date.max]
    assert unchanged.revision == plan.revision
    repos.plans.set_status(plan.id, PlanStatus.CANCELLED)
    with pytest.raises(ValueError, match="changed or is no longer open"):
        repos.plans.shift_dates(plan.id, 1, expected_revision=plan.revision)


def test_a_database_from_before_days_and_check_ins_gets_the_columns(tmp_path):
    from agent_control.storage import Database, Repositories

    path = tmp_path / "old.db"
    old = Database(f"sqlite:///{path}")
    with old.connect() as connection:
        connection.execute(
            "CREATE TABLE project_plans (id TEXT PRIMARY KEY, conversation_id TEXT, goal TEXT NOT NULL, summary TEXT NOT NULL, status TEXT NOT NULL, "
            "source_task_id TEXT, routing_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE project_plan_steps (id TEXT PRIMARY KEY, plan_id TEXT NOT NULL, position INTEGER NOT NULL, title TEXT NOT NULL, detail TEXT NOT NULL, "
            "owner TEXT NOT NULL, needs_json TEXT NOT NULL, depends_json TEXT NOT NULL, ability TEXT NOT NULL, status TEXT NOT NULL, task_id TEXT, "
            "result TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO project_plans VALUES ('plan_old', NULL, 'An old plan', '', 'active', NULL, '{}', '2026-10-01T00:00:00+00:00', '2026-10-01T00:00:00+00:00')"
        )
        connection.execute(
            "INSERT INTO project_plan_steps VALUES ('pstep_old', 'plan_old', 1, 'Old step', '', 'ybm', '[]', '[]', '', 'pending', NULL, '', '2026-10-01T00:00:00+00:00')"
        )
    database = Database(f"sqlite:///{path}")
    database.initialize()
    repos = Repositories.for_database(database)
    plan = repos.plans.get("plan_old")
    assert plan.notes == "" and plan.check_in_days == 0 and plan.last_check_in_at is None and plan.steps[0].due is None
    assert plan.revision == 0 and plan.steps[0].revision == 0 and not plan.steps[0].choice_only
    repos.plans.update_plan("plan_old", check_in_days=7)
    assert repos.plans.get("plan_old").check_in_days == 7


def test_the_plan_text_puts_the_day_first_and_the_year_only_when_it_is_not_this_one():
    assert day_text(date(2026, 12, 6), date(2026, 11, 14)) == "Dec 6" and day_text(date(2027, 1, 10), date(2026, 11, 14)) == "Jan 10, 2027"
    plan = _plan([PlanStep(position=1, title="Book the movers", owner=StepOwner.USER, due=_in(5))])
    assert f"[ ] 1. {day_text(_in(5))} - Book the movers (you)" in render_plan(plan)


def test_a_plan_that_runs_for_weeks_offers_a_check_in_once_and_a_short_one_does_not():
    from agent_control.plans.card import card_text
    from agent_control.plans.stage import answer_for

    long = _plan([PlanStep(position=1, title="Rules", due=_in(10)), PlanStep(position=2, title="Builder", due=_in(100))])
    assert long.weeks_ahead() == 14
    assert 'say "check in every week"' in card_text(long) and "about 14 weeks" in answer_for(long)
    assert answer_for(long).endswith("Start it when you are ready, or tell me what to change.")
    asked_for = long.model_copy(update={"check_in_days": 7})
    assert "check in every week" not in card_text(asked_for)
    short = _plan([PlanStep(position=1, title="Pack", due=_in(5)), PlanStep(position=2, title="Move", due=_in(14))])
    assert short.weeks_ahead() == 0 and "check in" not in card_text(short) and "check in" not in answer_for(short)
    assert _plan([PlanStep(position=1, title="No day at all")]).weeks_ahead() == 0
