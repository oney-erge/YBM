"""A bare answer is said the way a person would, and only ever with the facts it already had."""

from __future__ import annotations

import pytest

from agent_control.config import AppSettings, CapabilityPolicy
from agent_control.orchestration import StaticToolAdapter, TaskWorker, ToolExecutor
from agent_control.orchestration.voice import VOICE_SYSTEM_PROMPT, adds_nothing, needs_voice, speak
from agent_control.policy import PolicyEngine
from agent_control.schemas import Capability, OperatorAction, OperatorDecision, RiskLevel, TaskStatus
from agent_control.tools.spec import ToolDefinition
from helpers import make_repos
from test_worker_operator_loop import QueueOperator


@pytest.mark.parametrize("answer", [
    "13655", "42", "527.5 GB", "Wrote text file C:\\x\\a.txt.", "Found 3 result(s) for 'x' under C:\\y.",
    "Moved 3 file(s); changed 3 path(s).", "done\nWorkspace: /x/y", "Disk space:\n- C:\\ 527 GB free",
])
def test_a_bare_value_or_a_tools_own_phrasing_needs_wording(answer):
    assert needs_voice(answer) is True


@pytest.mark.parametrize("answer", [
    "", "   ", None, "There are 13,655 lines of Python in that folder.", "Yes", "I could not find that file on any drive.",
    "The file is called budget.xlsx", "x" * 800,
])
def test_a_sentence_is_left_alone(answer):
    assert needs_voice(answer) is False


def test_the_rewrite_may_not_add_a_number():
    assert adds_nothing("There are 13,655 lines.", "13655", "How many lines?") is True
    assert adds_nothing("About 14,000 lines.", "13655", "How many lines?") is False
    assert adds_nothing("The 3 files you asked about are done.", "Moved 3 file(s)", "Move the 3 files") is True
    assert adds_nothing("It has 12 words.", "13", "How many words in file?") is False


class _Provider:
    def __init__(self, reply=None, error=None):
        self.reply, self.error, self.calls = reply, error, []

    async def generate_text(self, system_prompt, user_prompt):
        self.calls.append((system_prompt, user_prompt))
        if self.error:
            raise self.error
        return self.reply


@pytest.mark.asyncio
async def test_a_bare_answer_is_said_in_a_sentence_using_only_its_facts():
    provider = _Provider("There are 13,655 lines of Python in that folder.")
    spoken = await speak(provider, "How many lines of Python are in it?", "13655")
    assert spoken == "There are 13,655 lines of Python in that folder."
    system, user = provider.calls[0]
    assert system == VOICE_SYSTEM_PROMPT and "The answer: 13655" in user and "How many lines" in user


@pytest.mark.asyncio
@pytest.mark.parametrize("reply, error", [
    ("About 14,000 lines of Python.", None), ("", None), ("13655", None), (None, RuntimeError("model down")), ("x" * 900, None),
])
async def test_the_original_is_kept_when_the_rewrite_is_invented_empty_the_same_too_long_or_fails(reply, error):
    assert await speak(_Provider(reply, error), "How many lines?", "13655") is None


@pytest.mark.asyncio
async def test_a_sentence_never_reaches_the_model_and_no_provider_means_no_change():
    provider = _Provider("anything")
    assert await speak(provider, "q", "There are 5 files here.") is None and provider.calls == []
    assert await speak(None, "q", "13655") is None


def _worker(repos, audit, operator, *, voice=True):
    settings = AppSettings(
        _env_file=None,
        capabilities={Capability.LLM_GENERATE: CapabilityPolicy(enabled=True, requires_approval=False, max_risk_level=RiskLevel.LOW)},
    )
    definitions = {"llm": ToolDefinition(name="llm", capability=Capability.LLM_GENERATE, enabled=True, description="t")}
    executor = ToolExecutor(PolicyEngine(settings, audit), repos, audit, adapters={"llm": StaticToolAdapter({"answer": "x"})},
                            tool_definitions=definitions)
    return TaskWorker(repos, audit, executor=executor, operator=operator, voice_pass=voice)


class _Operator(QueueOperator):
    def __init__(self, decisions, provider):
        super().__init__(decisions)
        self.provider = provider


@pytest.mark.asyncio
async def test_the_final_answer_of_a_task_is_said_in_words_when_it_was_bare(tmp_path):
    repos, audit = make_repos(tmp_path)
    provider = _Provider("It has 13,655 lines.")
    operator = _Operator([OperatorDecision(action=OperatorAction.DONE, final_answer="13655")], provider)
    task = repos.tasks.create("How many lines of Python?", metadata={"original_message_text": "How many lines of Python?"})
    done = await _worker(repos, audit, operator).process_task(task.id)
    assert done.status == TaskStatus.COMPLETED and done.metadata["synthesized_answer"] == "It has 13,655 lines."


@pytest.mark.asyncio
async def test_with_the_pass_off_or_an_invented_number_the_answer_is_unchanged(tmp_path):
    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("How many lines of Python?", metadata={"original_message_text": "How many lines of Python?"})
    off = await _worker(repos, audit, _Operator([OperatorDecision(action=OperatorAction.DONE, final_answer="13655")], _Provider("It has 13,655 lines.")), voice=False).process_task(task.id)
    assert off.metadata["synthesized_answer"] == "13655"
    task2 = repos.tasks.create("How many lines of Python?", metadata={"original_message_text": "How many lines of Python?"})
    invented = await _worker(repos, audit, _Operator([OperatorDecision(action=OperatorAction.DONE, final_answer="13655")], _Provider("Roughly 14,000 lines."))).process_task(task2.id)
    assert invented.metadata["synthesized_answer"] == "13655"
