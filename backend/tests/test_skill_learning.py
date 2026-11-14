"""Skills learned from successful runs are proposed, never self-installed.

An installed skill is injected into every future Operator prompt and followed
as instructions, so a system that writes its own would be changing tomorrow's
behavior with nobody having agreed to it. The queue is where a person decides.
"""

from __future__ import annotations

import json

import pytest

from agent_control.config import SkillsAdapterConfig
from agent_control.schemas import TaskRecord, TaskStatus
from agent_control.skill_learning import (
    SkillSuggestionStore,
    install,
    propose_from_task,
    worth_proposing,
)
from agent_control.tools.skills import _load_skills


class StubProvider:
    """Answers the way the model is asked to: a verdict and a procedure. "NONE" stands for a one-off; anything that is not JSON for a
    reply the schema could not hold (the provider raises, as a structured call that cannot be validated does)."""

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls = 0

    async def generate_structured(self, system_prompt: str, user_prompt: str, schema, **kwargs):
        self.calls += 1
        if self.reply.upper() == "NONE":
            return schema(worth_keeping=False)
        try:
            return schema(worth_keeping=True, **json.loads(self.reply))
        except ValueError as exc:
            raise RuntimeError("the reply did not fit the schema") from exc


def _config(tmp_path, *, learning: bool = True) -> SkillsAdapterConfig:
    return SkillsAdapterConfig(enabled=True, root_dir=str(tmp_path / "skills"), learning_enabled=learning)


def _task(steps: int = 4, status: TaskStatus = TaskStatus.COMPLETED) -> TaskRecord:
    return TaskRecord(
        id="task_1", objective="pull the total out of an invoice pdf", status=status,
        metadata={
            "operator_history": [
                {"tool_name": "filesystem.manage", "input": {"operation": "search"},
                 "reasoning": "find it", "status": "succeeded"}
                for _ in range(steps)
            ]
        },
    )


GOOD_REPLY = json.dumps({
    "name": "Invoice Total Extraction",
    "description": "Pull the total from an invoice PDF.",
    "body": "1. Search for the PDF.\n2. Read it.\n3. Report the total.",
})


@pytest.mark.asyncio
async def test_a_procedure_is_queued_not_installed(tmp_path) -> None:
    config = _config(tmp_path)

    suggestion = await propose_from_task(StubProvider(GOOD_REPLY), config, _task())

    assert suggestion is not None
    assert SkillSuggestionStore.load(config).pending()[0].name == "Invoice Total Extraction"
    # Nothing reaches the catalog until a human accepts.
    assert _load_skills(config.root_dir) == []


@pytest.mark.asyncio
async def test_learning_off_spends_no_llm_call(tmp_path) -> None:
    provider = StubProvider(GOOD_REPLY)

    assert await propose_from_task(provider, _config(tmp_path, learning=False), _task()) is None
    assert provider.calls == 0


def test_trivial_tasks_are_not_procedures(tmp_path) -> None:
    """Two steps is not a runbook, and every skill costs prompt space in
    every future task."""
    assert worth_proposing(_task(steps=2), _config(tmp_path)) is False
    assert worth_proposing(_task(steps=4), _config(tmp_path)) is True


@pytest.mark.asyncio
async def test_a_one_off_produces_nothing(tmp_path) -> None:
    assert await propose_from_task(StubProvider("NONE"), _config(tmp_path), _task()) is None


@pytest.mark.asyncio
async def test_unparseable_model_output_is_dropped(tmp_path) -> None:
    assert await propose_from_task(StubProvider("sure! here you go"), _config(tmp_path), _task()) is None


@pytest.mark.asyncio
async def test_the_same_skill_is_not_queued_twice(tmp_path) -> None:
    config = _config(tmp_path)
    provider = StubProvider(GOOD_REPLY)

    assert await propose_from_task(provider, config, _task()) is not None
    assert await propose_from_task(provider, config, _task()) is None


def test_accepting_installs_a_readable_skill_with_provenance(tmp_path) -> None:
    config = _config(tmp_path)
    store = SkillSuggestionStore.load(config)
    suggestion = store.add("Invoice Total Extraction", "Pull the total.", "1. Search.\n2. Read.", _task())

    installed = install(config, suggestion)

    assert installed["name"] == "Invoice Total Extraction"
    skills = _load_skills(config.root_dir)
    assert len(skills) == 1
    assert "Learned from a completed task" in skills[0]["body"]


def test_rejecting_installs_nothing(tmp_path) -> None:
    config = _config(tmp_path)
    store = SkillSuggestionStore.load(config)
    suggestion = store.add("Bad Idea", "no", "1. do it", _task())

    assert store.decide(suggestion.id, accept=False).status == "rejected"
    assert _load_skills(config.root_dir) == []
    assert SkillSuggestionStore.load(config).pending() == []


# --- helpers: the script that worked is kept, without a model call or a review ------------------------------------------------

from agent_control.skill_learning import MAX_HELPERS, helper_name, helper_script, save_helper  # noqa: E402

SCRIPT = "from pathlib import Path\nprint(sum(1 for _ in Path('.').rglob('*.py')))"


def _script_task(*, status=TaskStatus.COMPLETED, code=SCRIPT, output="42", request="How many .py files are in my project folder?"):
    history = [
        {"tool_name": "filesystem.manage", "input": {"operation": "inspect_folder"}, "status": "succeeded", "output_summary": "ok"},
        {"tool_name": "code.interpreter", "input": {"operation": "run_python", "code": code}, "status": "succeeded", "output_summary": output},
    ]
    return TaskRecord(objective=request, status=status, metadata={"original_message_text": request, "operator_history": history})


def test_the_script_that_produced_the_answer_is_found_in_the_history():
    assert helper_script(_script_task()) == SCRIPT
    assert helper_script(_script_task(output="")) is None
    assert helper_script(TaskRecord(objective="x", metadata={})) is None
    failed = _script_task()
    failed.metadata["operator_history"][-1]["status"] = "failed"
    assert helper_script(failed) is None


def test_a_helper_is_named_from_the_requests_own_words_so_the_same_kind_of_request_finds_it():
    assert helper_name("How many .py files are in my project folder?") == "helper-py-files-project-folder"
    assert helper_name("???") == "helper-script"


def test_a_finished_task_that_a_script_answered_leaves_a_helper_skill_with_the_script_in_it(tmp_path):
    config = SkillsAdapterConfig(root_dir=str(tmp_path), helpers_enabled=True)
    saved = save_helper(config, _script_task())
    assert saved["name"] == "helper-py-files-project-folder" and saved["tools"] == ["code.interpreter"]
    assert SCRIPT in saved["body"] and "run_python" in saved["body"]
    assert [s["name"] for s in _load_skills(str(tmp_path))] == ["helper-py-files-project-folder"]


def test_the_first_script_that_worked_is_kept_not_overwritten_by_a_later_run(tmp_path):
    config = SkillsAdapterConfig(root_dir=str(tmp_path), helpers_enabled=True)
    save_helper(config, _script_task())
    assert save_helper(config, _script_task(code="print('a worse way')")) is None
    assert SCRIPT in _load_skills(str(tmp_path))[0]["body"]


@pytest.mark.parametrize("config_kwargs, status", [
    ({"helpers_enabled": False}, TaskStatus.COMPLETED),
    ({"helpers_enabled": True, "enabled": False}, TaskStatus.COMPLETED),
    ({"helpers_enabled": True}, TaskStatus.FAILED),
])
def test_nothing_is_saved_when_helpers_are_off_or_the_task_did_not_finish(tmp_path, config_kwargs, status):
    config = SkillsAdapterConfig(root_dir=str(tmp_path), **config_kwargs)
    assert save_helper(config, _script_task(status=status)) is None
    assert _load_skills(str(tmp_path)) == []


def test_the_number_of_helpers_is_capped(tmp_path):
    config = SkillsAdapterConfig(root_dir=str(tmp_path), helpers_enabled=True)
    for number in range(MAX_HELPERS):
        assert save_helper(config, _script_task(request=f"Task number{number} alpha")) is not None
    assert save_helper(config, _script_task(request="One more unique request")) is None


def test_a_long_request_is_shortened_at_a_word_boundary_in_the_helper_description(tmp_path):
    config = SkillsAdapterConfig(root_dir=str(tmp_path), helpers_enabled=True)
    request = "How many lines of Python code are there in total in all the .py files under the folder called orchestration please"
    saved = save_helper(config, _script_task(request=request))
    assert saved["description"].endswith("...") and "orchestra..." not in saved["description"] and len(saved["description"]) <= 135
