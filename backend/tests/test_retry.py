"""After a failure: notice it in code, say why, change the kind of approach, and start the next attempt from a short list of what failed (orchestration/retry.py)."""

from __future__ import annotations

import pytest

from agent_control.config import AppSettings, CapabilityPolicy
from agent_control.orchestration import retry
from agent_control.orchestration.check_entries import CHECK_ENTRY_APPROACH, CHECK_ENTRY_STRATEGY
from agent_control.orchestration.operator import _format_history
from agent_control.orchestration import StaticToolAdapter, TaskWorker, ToolExecutor
from agent_control.plans.model import Plan, PlanStep
from agent_control.policy import PolicyEngine
from agent_control.schemas import Capability, ChannelType, RiskLevel
from agent_control.tools.spec import ToolDefinition
from helpers import make_repos


def _failed(tool="plan.manage", operation="update_step", error="invalid input for plan.manage: operation=update_step requires 'position'", **extra):
    return {"tool_name": tool, "input": {"operation": operation, **extra}, "status": "failed", "error": error}


def _empty(query="x"):
    return {"tool_name": "filesystem.manage", "input": {"operation": "search", "query": query}, "status": "succeeded", "output_summary": "Found 0 result(s) for 'x'."}


def _worked(tool="filesystem.manage", operation="inspect_folder"):
    return {"tool_name": tool, "input": {"operation": operation}, "status": "succeeded", "output_summary": "Found 3 item(s)."}


# --- noticing ---------------------------------------------------------------------------------------------------------------------------


def test_the_same_call_failing_the_same_way_twice_is_stuck_whatever_the_numbers_say():
    assert "same call failed" in retry.stuck([_failed(error="step 3 not found"), _failed(error="step 5 not found")])
    assert retry.stuck([_failed(), _failed(tool="web.search", error="boom")]) is None  # two different tools failing is not the same call


def test_two_empty_lookups_or_three_failures_or_a_sub_task_out_of_steps_are_stuck():
    assert "found nothing" in retry.stuck([_empty("a"), _empty("b")])
    assert "three steps" in retry.stuck([_failed(tool="a", error="x"), _failed(tool="b", error="y"), _failed(tool="c", error="z")])
    out = {"tool_name": "delegate", "input": {}, "status": "failed", "error": "sub-task step budget (6) exhausted without finishing"}
    assert "sub-task" in retry.stuck([_worked(), out])


def test_one_failure_a_success_in_between_and_the_notes_a_check_leaves_are_not_stuck():
    assert retry.stuck([]) is None and retry.stuck([_failed()]) is None
    assert retry.stuck([_failed(), _worked(), _failed()]) is None
    note = {"tool_name": CHECK_ENTRY_STRATEGY, "input": None, "status": "strategy", "error": "a hint"}
    assert retry.stuck([_failed(), note, _worked()]) is None
    assert "same call" in retry.stuck([_failed(), note, _failed()])  # the note between them is not a step


# --- choosing ---------------------------------------------------------------------------------------------------------------------------


def test_the_kind_follows_the_cause_is_never_repeated_and_must_fit_the_task():
    everything = set(retry.KINDS) - {"fix_the_call"}  # the error did not say what to change
    assert retry.choose("misread_the_request", [], everything) == "answer_from_context"
    assert retry.choose("wrong_arguments", [], everything) == "owning_home"
    assert retry.choose("wrong_arguments", [], everything - {"owning_home"}) == "other_tool"
    assert retry.choose("too_big", [], everything) == "break_down"
    assert retry.choose("does_not_exist", [], everything) == "step_back"


def test_what_the_tools_own_error_names_comes_before_the_models_guess_at_a_cause():
    available = set(retry.KINDS)
    for cause in ("misread_the_request", "missing_context", "wrong_tool", "does_not_exist"):
        assert retry.choose(cause, [], available) == "fix_the_call"  # whatever the diagnosis said
    assert retry.choose("misread_the_request", ["fix_the_call"], available) == "answer_from_context"  # once, and then the cause decides


def test_when_every_kind_that_fits_was_tried_there_is_nothing_left_to_choose():
    assert retry.choose("refused", ["other_tool", "step_back", "look_up"], set(retry.KINDS) - {"fix_the_call"}) is None
    assert retry.choose("misread_the_request", [], set()) is None


def test_the_note_says_what_went_wrong_what_to_do_instead_and_what_already_failed():
    diagnosis = retry.Diagnosis(
        asked="Change the plan they were just shown.", assumed="That the plan is a file on the disk.",
        mistake="The plan is the one in the last reply, not a file.", cause="misread_the_request",
    )
    facts = retry.Facts(home="plan.manage with operation revise", last_error="requires 'position'")
    text = retry.approach_text("owning_home", diagnosis, facts, ["- filesystem.manage search: found nothing"], may_ask=False)
    assert "The plan is the one in the last reply, not a file." in text and "Take a different approach" in text and "plan.manage with operation revise" in text
    assert "filesystem.manage search: found nothing" in text and "Do not ask the person about this" in text
    asked = retry.approach_text("step_back", None, retry.Facts(), [], may_ask=True)
    assert "Do not ask the person about this" not in asked and "Several steps in a row did not work" in asked


# --- the fresh attempt ------------------------------------------------------------------------------------------------------------------


def test_after_a_change_of_approach_the_prompt_shows_a_short_list_of_what_failed_not_the_transcript():
    history = [
        _failed(error="a very long error " * 30), _failed(error="a very long error " * 30),
        {"tool_name": CHECK_ENTRY_APPROACH, "input": None, "status": "approach", "hint_id": "owning_home", "error": "Take a different approach: use the planner."},
        _worked("plan.manage", "revise"),
    ]
    shown = _format_history(history)
    assert "Before the change of approach below, these were tried and did not work" in shown
    assert shown.count("plan.manage update_step: failed") == 2 and "a very long error " * 10 not in shown
    assert "Take a different approach: use the planner." in shown and "plan.manage (succeeded)" in shown
    assert "Before the change" not in _format_history([_failed(), _worked()])  # no change, no list


# --- in the worker: the 11/19 failure ---------------------------------------------------------------------------------------------------


class _Provider:
    def __init__(self, diagnosis=None, error: Exception | None = None):
        self.diagnosis, self.error, self.asked = diagnosis, error, []

    async def generate_structured(self, system, user, schema, **kwargs):
        self.asked.append((system, user, schema.__name__))
        if self.error is not None:
            raise self.error
        return self.diagnosis


class _Operator:
    def __init__(self, provider=None):
        self.provider = provider


def _worker(repos, audit, provider=None, *, code=True, tools=("plan.manage", "web.search", "filesystem.manage")):
    settings = AppSettings(
        _env_file=None,
        capabilities={Capability.AGENT_CORE: CapabilityPolicy(enabled=True, requires_approval=False, max_risk_level=RiskLevel.LOW),
                      Capability.TERMINAL_RUN: CapabilityPolicy(enabled=True, requires_approval=False, max_risk_level=RiskLevel.HIGH)},
    )
    definitions = {
        "plan.manage": ToolDefinition(name="plan.manage", capability=Capability.AGENT_CORE, enabled=True, description="plans"),
        "web.search": ToolDefinition(name="web.search", capability=Capability.AGENT_CORE, enabled=True, description="search"),
        "filesystem.manage": ToolDefinition(name="filesystem.manage", capability=Capability.AGENT_CORE, enabled=True, description="files"),
    }
    definitions = {name: definition for name, definition in definitions.items() if name in tools}
    adapters = {name: StaticToolAdapter({"summary": "ok"}) for name in definitions}
    if code:
        definitions["code.interpreter"] = ToolDefinition(name="code.interpreter", capability=Capability.TERMINAL_RUN, enabled=True, description="python")
        adapters["code.interpreter"] = StaticToolAdapter({"summary": "ok"})
    executor = ToolExecutor(PolicyEngine(settings, audit), repos, audit, adapters=adapters, tool_definitions=definitions)
    return TaskWorker(repos, audit, executor=executor, operator=_Operator(provider))


def _task(repos, history, *, with_plan=True, **metadata):
    conversation = repos.conversations.get_or_create(ChannelType.TELEGRAM, "42")
    if with_plan:
        repos.plans.create(Plan(conversation_id=conversation, goal="A page that turns miles into kilometres", steps=[PlanStep(position=1, title="Sketch the page")]))
    task = repos.tasks.create("Revise it so there is a short animation.", conversation_id=conversation,
                              metadata={"original_message_text": "Revise it so there is a short animation.", "initiative_level": 3, **metadata})
    return repos.tasks.update_metadata(task.id, {**task.metadata, "operator_loop": True, "operator_history": history})


@pytest.mark.asyncio
async def test_a_plan_edit_that_fails_twice_is_diagnosed_and_the_next_attempt_is_told_to_fix_the_call_then_to_use_the_planner(tmp_path):
    repos, audit = make_repos(tmp_path)
    diagnosis = retry.Diagnosis(asked="Add an animation to the plan.", assumed="That the plan steps are edited by a made-up step id.",
                                mistake="update_step takes a position, and revise changes the plan from the person's words.", cause="wrong_arguments")
    provider = _Provider(diagnosis)
    worker = _worker(repos, audit, provider)
    history = [_failed(step_id="visuals"), _failed(step_id="visuals")]
    task = _task(repos, history)

    changed = await worker._retry_step(task, list(history), "step_1")
    assert changed is not None and changed.metadata["retry_changes"][0]["kind"] == "fix_the_call"  # the error names what to fix
    note = changed.metadata["operator_history"][-1]
    assert note["tool_name"] == CHECK_ENTRY_APPROACH and "requires 'position'" in note["error"] and "Do not ask the person about this" in note["error"]
    system, user, schema = provider.asked[0]
    assert schema == "Diagnosis" and "Revise it so there is a short animation." in user and "plan.manage update_step: failed" in user

    # the same wrong call again: the next kind is the planner, which owns the plan
    again = [*changed.metadata["operator_history"], _failed(step_id="visuals"), _failed(step_id="visuals")]
    changed = await worker._retry_step(repos.tasks.update_metadata(task.id, {**changed.metadata, "operator_history": again}), list(again), "step_2")
    assert [c["kind"] for c in changed.metadata["retry_changes"]] == ["fix_the_call", "owning_home"]
    assert "plan.manage with operation revise" in changed.metadata["operator_history"][-1]["error"]


@pytest.mark.asyncio
async def test_the_diagnosis_is_asked_once_for_the_same_steps_and_never_after_three_changes(tmp_path):
    repos, audit = make_repos(tmp_path)
    provider = _Provider(retry.Diagnosis(asked="a", assumed="b", mistake="c", cause="wrong_tool"))
    worker = _worker(repos, audit, provider)
    history = [_failed(tool="web.search", error="blocked"), _failed(tool="web.search", error="blocked")]
    task = _task(repos, history, with_plan=False)
    first = await worker._retry_step(task, list(history), "s1")
    assert first is not None and len(provider.asked) == 1
    assert await worker._retry_step(first, list(first.metadata["operator_history"]), "s2") is None  # nothing new has happened since
    assert len(provider.asked) == 1
    spent = repos.tasks.update_metadata(first.id, {**first.metadata, "retry_changes": [{"kind": "a"}, {"kind": "b"}, {"kind": "c"}], "retry_handled_through": 0})
    assert await worker._retry_step(spent, list(spent.metadata["operator_history"]), "s3") is None and len(provider.asked) == 1


@pytest.mark.asyncio
async def test_without_a_diagnosis_the_kind_is_chosen_from_the_evidence_and_the_loop_still_works(tmp_path):
    repos, audit = make_repos(tmp_path)
    for provider in (None, _Provider(error=RuntimeError("model down"))):
        worker = _worker(repos, audit, provider)
        history = [_empty("diaper"), _empty("calculator")]
        task = _task(repos, history, with_plan=False)
        changed = await worker._retry_step(task, list(history), "s1")
        assert changed is not None and changed.metadata["retry_changes"][0]["kind"] == "answer_from_context"
        assert "What was said just before" in changed.metadata["operator_history"][-1]["error"] or "in this conversation" in changed.metadata["operator_history"][-1]["error"]


@pytest.mark.asyncio
async def test_a_task_that_is_not_stuck_is_left_alone(tmp_path):
    repos, audit = make_repos(tmp_path)
    worker = _worker(repos, audit, _Provider(retry.Diagnosis(asked="a", assumed="b", mistake="c", cause="wrong_tool")))
    history = [_worked(), _failed()]
    assert await worker._retry_step(_task(repos, history), list(history), "s1") is None


@pytest.mark.asyncio
async def test_when_no_kind_fits_it_is_written_down_and_the_task_ends_the_way_it_always_did(tmp_path):
    repos, audit = make_repos(tmp_path)
    worker = _worker(repos, audit, _Provider(retry.Diagnosis(asked="a", assumed="b", mistake="c", cause="refused")), code=False, tools=("web.search",))
    history = [_failed(tool="web.search", error="blocked"), _failed(tool="web.search", error="blocked")]
    task = _task(repos, history, with_plan=False, retry_changes=[{"kind": "step_back"}, {"kind": "look_up"}])
    result = await worker._retry_step(task, list(history), "s1")
    assert result is not None and result.metadata["retry_handled_through"] == 2
    assert result.metadata["operator_history"] == history  # no note was added: there was nothing left to try


@pytest.mark.asyncio
async def test_an_open_plan_is_not_the_home_of_a_request_that_never_went_to_it(tmp_path):
    """Live 11/19 (hands_download): a download failed while a plan was open in the chat, and the note told it to revise the plan."""
    repos, audit = make_repos(tmp_path)
    worker = _worker(repos, audit, _Provider(retry.Diagnosis(asked="a", assumed="b", mistake="c", cause="wrong_tool")))
    history = [_failed(tool="web.search", error="blocked"), _failed(tool="web.search", error="blocked")]
    task = _task(repos, history)  # a plan is open in this chat
    changed = await worker._retry_step(task, list(history), "s1")
    assert changed.metadata["retry_changes"][0]["kind"] != "owning_home"
    assert "operation revise" not in changed.metadata["operator_history"][-1]["error"]
