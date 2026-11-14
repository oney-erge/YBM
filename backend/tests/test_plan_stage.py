"""A request a skill routes to the planner is planned before the Operator sees it (plans/stage.py, and the worker's step)."""

from __future__ import annotations

import pytest

from agent_control.orchestration import TaskWorker
from agent_control.plans.model import PlanStatus, StepOwner
from agent_control.plans.planner import PlanDraft, StepDraft, can_do_text, user_prompt
from agent_control.plans.stage import StageOutcome, answer_for, run_plan_stage
from agent_control.schemas import ChannelType, TaskStatus
from helpers import make_repos

ROUTING = {"source_channel": "telegram", "source_chat_id": "42", "source_sender_id": "7"}


class _Provider:
    def __init__(self, draft: PlanDraft | Exception) -> None:
        self.draft, self.asked = draft, []

    async def generate_structured(self, system_prompt, user, schema, **kwargs):
        self.asked.append(user)
        if isinstance(self.draft, Exception):
            raise self.draft
        return self.draft


def _steps_draft() -> PlanDraft:
    return PlanDraft(summary="Assumed Wi-Fi only.", steps=[
        StepDraft(title="Compare flight controllers"), StepDraft(title="Buy the parts", owner="user", needs=["300 dollars"]),
        StepDraft(title="Write the code", depends_on=[1]),
    ])


def _task(repos, **metadata):
    conversation = repos.conversations.get_or_create(ChannelType.TELEGRAM, "42")
    return repos.tasks.create("Build me a drone", conversation_id=conversation, metadata={**ROUTING, "original_message_text": "Build me a drone", **metadata})


@pytest.mark.asyncio
async def test_the_stage_saves_the_plan_the_planner_drafted_as_proposed_with_the_chat_it_came_from(tmp_path):
    repos, audit = make_repos(tmp_path)
    provider = _Provider(_steps_draft())
    task = _task(repos)
    outcome = await run_plan_stage(repos, audit, provider, "- can: files", task)
    assert outcome.kind == "plan" and outcome.plan.status == PlanStatus.PROPOSED and outcome.plan.routing == ROUTING
    assert [(s.title, s.owner) for s in repos.plans.get(outcome.plan.id).steps][1] == ("Buy the parts", StepOwner.USER)
    assert outcome.plan.source_task_id == task.id and outcome.plan.summary == "Assumed Wi-Fi only."
    assert "Goal: Build me a drone" in provider.asked[0] and "ONE short question" in provider.asked[0]  # first time: it may ask
    assert answer_for(outcome.plan).endswith("Start it when you are ready, or tell me what to change.")


@pytest.mark.asyncio
async def test_a_goal_that_cannot_be_told_is_asked_about_once_with_options_and_never_twice(tmp_path):
    repos, audit = make_repos(tmp_path)
    question = PlanDraft(question="What kind of robot?", options=["A small drone", "A desk arm", " ", "Something else"])
    first = await run_plan_stage(repos, audit, _Provider(question), "", _task(repos))
    assert first == StageOutcome("ask", question="What kind of robot?", options=("A small drone", "A desk arm", "Something else"))
    provider = _Provider(question)
    again = await run_plan_stage(repos, audit, provider, "", _task(repos, clarify_count=1))
    assert again.kind == "none" and "Do not ask questions." in provider.asked[0]


@pytest.mark.asyncio
async def test_a_planner_that_fails_or_writes_nonsense_leaves_the_request_to_the_operator(tmp_path):
    repos, audit = make_repos(tmp_path)
    assert (await run_plan_stage(repos, audit, _Provider(TimeoutError("slow")), "", _task(repos))).kind == "none"
    forward = PlanDraft(steps=[StepDraft(title="a", depends_on=[2]), StepDraft(title="b")])
    outcome = await run_plan_stage(repos, audit, _Provider(forward), "", _task(repos))
    assert outcome.kind == "plan" and outcome.plan.steps[0].depends_on == []  # a forward dependency is dropped, not trusted
    with pytest.raises(ValueError):
        PlanDraft()  # neither steps nor a question


def test_the_planner_is_told_what_the_assistant_can_and_cannot_do():
    from agent_control.abilities.model import Ability, AbilityKind, AbilityState

    class Index:
        def all(self):
            return (
                Ability(id="tool:files", kind=AbilityKind.TOOL, name="files", summary="move files", state=AbilityState.READY),
                Ability(id="tool:off", kind=AbilityKind.TOOL, name="off", summary="x", state=AbilityState.DISABLED),
                Ability(id="gap:pay", kind=AbilityKind.GAP, name="pay", summary="x", state=AbilityState.UNAVAILABLE, guidance="never pays"),
            )

    class Catalog:
        def index(self):
            return Index()

    assert can_do_text(Catalog()) == "- can: files: move files\n- cannot: pay. never pays" and can_do_text(None) == ""
    assert "What the assistant can and cannot do:\n- can: files" in user_prompt("g", "said", "- can: files", may_ask=False)


# --- the worker -------------------------------------------------------------------------------------------------------------------------------


def _worker(repos, audit, outcome: StageOutcome):
    async def stage(task):
        return outcome

    return TaskWorker(repos, audit, plan_stage=stage)


@pytest.mark.asyncio
async def test_a_routed_request_ends_as_a_plan_the_user_starts_with_the_card_under_the_answer(tmp_path):
    repos, audit = make_repos(tmp_path)
    plan = (await run_plan_stage(repos, audit, _Provider(_steps_draft()), "", _task(repos))).plan
    task = _task(repos, task_route="plan")
    finished = await _worker(repos, audit, StageOutcome("plan", plan=plan))._planned(task)
    assert finished.status == TaskStatus.COMPLETED and finished.metadata["plan_card"] == plan.id and finished.metadata["plan_stage_done"]
    assert finished.metadata["synthesized_answer"].startswith("Plan: Build me a drone")


@pytest.mark.asyncio
async def test_a_question_becomes_a_clarifying_task_with_tap_able_options_and_the_stage_runs_again_after_the_answer(tmp_path):
    repos, audit = make_repos(tmp_path)
    task = _task(repos, task_route="plan")
    asked = await _worker(repos, audit, StageOutcome("ask", question="What kind?", options=("Drone", "Arm")))._planned(task)
    assert asked.status == TaskStatus.CLARIFYING and asked.metadata["clarifying_question"] == "What kind?"
    assert asked.metadata["clarifying_options"] == ["Drone", "Arm"] and not asked.metadata["plan_stage_done"] and asked.metadata["clarify_count"] == 1


@pytest.mark.asyncio
async def test_a_stage_with_nothing_to_say_hands_the_request_to_the_operator_once(tmp_path):
    repos, audit = make_repos(tmp_path)
    task = _task(repos, task_route="plan")
    worker = _worker(repos, audit, StageOutcome("none"))
    assert await worker._planned(task) is None
    assert repos.tasks.get(task.id).metadata["plan_stage_done"] is True
    assert await worker._planned(repos.tasks.get(task.id)) is None  # not asked again


@pytest.mark.asyncio
async def test_only_a_routed_task_with_a_stage_is_planned(tmp_path):
    repos, audit = make_repos(tmp_path)
    called = []

    async def stage(task):
        called.append(task.id)
        return StageOutcome("none")

    assert await TaskWorker(repos, audit, plan_stage=stage)._planned(_task(repos)) is None  # no route
    assert await TaskWorker(repos, audit)._planned(_task(repos, task_route="plan")) is None  # no stage
    assert called == []
