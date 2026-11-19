"""Revising a plan: "revise it", "add X", "I do not see Y in it" change the plan that was just shown, through the planner that wrote it (tools/plan_manage.py `revise`)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agent_control.plans.card import card_text
from agent_control.plans.model import Plan, PlanStatus, PlanStep, StepStatus
from agent_control.plans.planner import PlanDraft, StepDraft
from agent_control.plans.runner import render_plan
from agent_control.schemas import ChannelType, ToolCallRequest, ToolResultStatus
from agent_control.tools.contracts import PlanManageInput
from agent_control.tools.plan_manage import PlanManageAdapter
from helpers import make_repos

ROUTING = {"source_channel": "telegram", "source_chat_id": "42", "source_sender_id": "7"}


class _Planner:
    """Stands in for the model: returns the draft it was given and remembers what it was asked."""

    def __init__(self, draft=None, error: Exception | None = None) -> None:
        self.draft, self.error, self.asked = draft, error, []

    async def generate_structured(self, system_prompt, user_prompt, schema, **kwargs):
        self.asked.append((system_prompt, user_prompt, schema.__name__))
        if self.error is not None:
            raise self.error
        return self.draft


def _setup(tmp_path, planner=None):
    repos, audit = make_repos(tmp_path)
    conversation = repos.conversations.get_or_create(ChannelType.TELEGRAM, "42")
    task = repos.tasks.create("Revise the plan", conversation_id=conversation, metadata={**ROUTING, "original_message_text": "Revise the plan"})
    plan = repos.plans.create(Plan(
        conversation_id=conversation, goal="A page that turns miles into kilometres", summary="One page, no build step.", routing=dict(ROUTING),
        steps=[
            PlanStep(position=1, title="Sketch the page", detail="A box, a button and a result line."),
            PlanStep(position=2, title="Write the conversion", detail="Multiply by 1.609."),
            PlanStep(position=3, title="Try it in a browser"),
        ],
    ))
    adapter = PlanManageAdapter(repos, audit, planner)
    return repos, adapter, task, plan


def _request(task_id: str, **payload) -> ToolCallRequest:
    return ToolCallRequest(task_id=task_id, tool_name="plan.manage", capability="agent.core", input=payload, origin="operator")


def _revised_draft() -> PlanDraft:
    return PlanDraft(summary="Same page, now with a short slide-in animation.", steps=[
        StepDraft(title="Sketch the page", detail="A box, a button and a result line."),
        StepDraft(title="Write the conversion", detail="Multiply by 1.609."),
        StepDraft(title="Add the slide-in animation", detail="A CSS transition on the result line; no files to download.", depends_on=[2]),
        StepDraft(title="Try it in a browser", depends_on=[3]),
    ])


@pytest.mark.asyncio
async def test_revising_changes_the_same_plan_and_shows_how_each_new_step_is_done(tmp_path):
    planner = _Planner(_revised_draft())
    repos, adapter, task, plan = _setup(tmp_path, planner)
    result = await adapter.execute(_request(task.id, operation="revise", instruction="Add a short animation when the result appears."))
    assert result.status == ToolResultStatus.SUCCEEDED and result.output["plan_id"] == plan.id
    revised = repos.plans.get(plan.id)
    assert [s.title for s in revised.steps] == ["Sketch the page", "Write the conversion", "Add the slide-in animation", "Try it in a browser"]
    assert revised.steps[2].depends_on == [2] and revised.steps[3].depends_on == [3]
    assert revised.summary == "Same page, now with a short slide-in animation." and revised.status == PlanStatus.PROPOSED
    assert "Revised (version 2): Add a short animation when the result appears." in revised.notes
    assert "A CSS transition on the result line; no files to download." in result.output["terminal_output"][0]["content"]  # the how is in what the model answers with
    assert repos.tasks.get(task.id).metadata["plan_card"] == plan.id  # the card with Start and Cancel goes under the answer
    system, user, schema = planner.asked[0]
    assert schema == "PlanDraft" and "revising a plan that already exists" in system
    assert "Sketch the page" in user and "Multiply by 1.609." in user and "Add a short animation when the result appears." in user  # the plan as it stands and the user's words


@pytest.mark.asyncio
async def test_a_second_revision_is_version_three_and_steps_that_started_stay(tmp_path):
    repos, adapter, task, plan = _setup(tmp_path, _Planner(_revised_draft()))
    await adapter.execute(_request(task.id, operation="revise", instruction="Add an animation."))
    started = repos.plans.get(plan.id)
    repos.plans.claim_step(started.steps[0].id, StepStatus.RUNNING)
    adapter.provider = _Planner(PlanDraft(summary="Again.", steps=[StepDraft(title="Write the conversion"), StepDraft(title="Try it", depends_on=[1])]))
    result = await adapter.execute(_request(task.id, operation="revise", instruction="Drop the animation, keep it plain."))
    assert result.status == ToolResultStatus.SUCCEEDED
    revised = repos.plans.get(plan.id)
    assert [(s.position, s.title) for s in revised.steps] == [(1, "Sketch the page"), (2, "Write the conversion"), (3, "Try it")]
    assert revised.steps[0].status == StepStatus.RUNNING  # started, so not rewritten
    assert revised.steps[2].depends_on == [2]  # the draft's "step 1" is now step 2, after the one that started
    assert "Revised (version 3)" in revised.notes and "first 1 steps have already started" in adapter.provider.asked[0][1]


@pytest.mark.asyncio
async def test_a_revision_that_cannot_be_made_says_why_and_leaves_the_plan_alone(tmp_path):
    for provider, expected in (
        (None, "no planner model"),
        (_Planner(error=RuntimeError("down")), "could not revise"),
        (_Planner(PlanDraft(question="Which page?")), "gave no steps"),
    ):
        repos, adapter, task, plan = _setup(tmp_path / expected.replace(" ", "_"), provider)
        result = await adapter.execute(_request(task.id, operation="revise", instruction="Add an animation."))
        assert result.status == ToolResultStatus.FAILED and expected in result.error_message
        assert [s.title for s in repos.plans.get(plan.id).steps] == ["Sketch the page", "Write the conversion", "Try it in a browser"]


@pytest.mark.asyncio
async def test_a_finished_plan_is_not_revised(tmp_path):
    repos, adapter, task, plan = _setup(tmp_path, _Planner(_revised_draft()))
    repos.plans.transition_status(plan.id, PlanStatus.CANCELLED, (PlanStatus.PROPOSED,))
    result = await adapter.execute(_request(task.id, operation="revise", instruction="Add an animation."))
    assert result.status == ToolResultStatus.FAILED  # a cancelled plan is not found among the open ones


def test_revise_needs_the_users_words_and_nothing_else():
    assert PlanManageInput(operation="revise", instruction="make it shorter").instruction == "make it shorter"
    with pytest.raises(ValidationError, match="requires 'instruction'"):
        PlanManageInput(operation="revise")


def test_the_plan_a_person_is_still_reading_shows_how_each_step_is_done(tmp_path):
    repos, _, _, plan = _setup(tmp_path)
    plan = repos.plans.get(plan.id)
    assert "A box, a button and a result line." not in render_plan(plan)
    assert "A box, a button and a result line." in render_plan(plan, details=True)
    assert "Multiply by 1.609." in card_text(plan)  # proposed: the person is reading it
    repos.plans.transition_status(plan.id, PlanStatus.ACTIVE, (PlanStatus.PROPOSED,))
    assert "Multiply by 1.609." not in card_text(repos.plans.get(plan.id))  # active: the card stays short


def test_the_store_refuses_to_rewrite_a_plan_that_changed_or_whose_started_steps_are_not_at_the_front(tmp_path):
    repos, _, _, plan = _setup(tmp_path)
    steps = [PlanStep(position=1, title="One")]
    with pytest.raises(ValueError, match="changed or is no longer open"):
        repos.plans.replace_unstarted_steps(plan.id, steps, expected_revision=plan.revision + 5)
    third = repos.plans.get(plan.id).steps[2]
    repos.plans.claim_step(third.id, StepStatus.RUNNING)  # step 3 started while 1 and 2 have not
    with pytest.raises(ValueError, match="one at a time"):
        repos.plans.replace_unstarted_steps(plan.id, steps)
