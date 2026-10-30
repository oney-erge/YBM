"""A hosted model is never called as a fallback until the user says yes (llm/cloud_gate.py)."""

from __future__ import annotations

import httpx
import pytest
from test_worker_operator_loop import QueueOperator, _executor, _executor_with_adapter, _settings

from agent_control.clarification import resume_clarifying_task
from agent_control.llm.cloud_gate import CloudFallbackNeeded, hosted_allowed, is_allowed, is_hosted, is_yes
from agent_control.llm.providers import ChainLLMProvider, FailoverLLMProvider, _ChainEntry
from agent_control.orchestration import StaticToolAdapter, TaskWorker
from agent_control.orchestration.worker import _coding_agent_requested
from agent_control.schemas import OperatorAction, OperatorDecision, TaskStatus, utc_now
from helpers import make_repos


class _Provider:
    def __init__(self, name: str, fail: Exception | None = None) -> None:
        self.name, self.fail, self.calls = name, fail, 0

    async def generate_text(self, system_prompt: str, user_prompt: str) -> str:
        self.calls += 1
        if self.fail is not None:
            raise self.fail
        return self.name


DOWN = httpx.ConnectError("refused")


@pytest.mark.parametrize(
    "provider, url, hosted",
    [
        ("openai_compatible", "http://127.0.0.1:8000/v1", False),
        ("openai_compatible", "http://localhost:11434/v1", False),
        ("openai_compatible", "http://host.docker.internal:8000/v1", False),
        ("openai_compatible", "http://192.168.1.20:8000/v1", False),
        ("openai_compatible", "https://api.openai.com/v1", True),
        ("openai_compatible", "https://openrouter.ai/api/v1", True),
        ("openai_compatible", "http://8.8.8.8/v1", True),
        ("openai_compatible", None, True),
        ("anthropic", "http://127.0.0.1:9000", True),
    ],
)
def test_a_profile_is_hosted_when_it_leaves_the_machine_and_its_network(provider, url, hosted) -> None:
    assert is_hosted(provider, url) is hosted


@pytest.mark.parametrize("answer", ["yes", "Yes", "YES!", "yes please", "Yes, send it.", "ok", "go ahead"])
def test_a_plain_yes_approves(answer) -> None:
    assert is_yes(answer)


@pytest.mark.parametrize(
    "answer",
    [
        "no", "", None, "maybe", "yes but keep my files off it", "not now",
        # the E2E harness answers every question with this; it must never approve leaving the machine
        "Yes, please go ahead with what you proposed, staying within the constraints in my original message.",
    ],
)
def test_anything_but_a_plain_yes_sends_nothing(answer) -> None:
    assert not is_yes(answer)


@pytest.mark.asyncio
async def test_failover_to_a_hosted_profile_asks_instead_of_calling_it() -> None:
    local, hosted = _Provider("local", DOWN), _Provider("openai")
    provider = FailoverLLMProvider(local, hosted, fallback_name="openai_saved", fallback_model="gpt-4.1", fallback_hosted=True)

    with pytest.raises(CloudFallbackNeeded) as raised:
        await provider.generate_text("s", "u")

    assert hosted.calls == 0
    assert (raised.value.profile, raised.value.model) == ("openai_saved", "gpt-4.1")
    assert "could not be reached" in raised.value.reason


@pytest.mark.asyncio
async def test_failover_uses_the_hosted_profile_only_inside_an_approval() -> None:
    hosted = _Provider("openai")
    provider = FailoverLLMProvider(_Provider("local", DOWN), hosted, fallback_name="openai_saved", fallback_model="gpt-4.1", fallback_hosted=True)

    with hosted_allowed({"openai_saved"}):
        assert await provider.generate_text("s", "u") == "openai"
    assert not is_allowed("openai_saved")  # the approval ended with the block
    with pytest.raises(CloudFallbackNeeded):
        await provider.generate_text("s", "u")


@pytest.mark.asyncio
async def test_a_local_fallback_still_works_without_asking() -> None:
    backup = _Provider("backup-on-this-machine")
    provider = FailoverLLMProvider(_Provider("local", DOWN), backup, fallback_name="spare", fallback_hosted=False)
    assert await provider.generate_text("s", "u") == "backup-on-this-machine"


@pytest.mark.asyncio
async def test_the_chain_skips_to_a_local_backup_but_asks_before_a_hosted_one() -> None:
    local, spare, hosted = _Provider("local", DOWN), _Provider("spare"), _Provider("openai")
    chain = ChainLLMProvider([
        _ChainEntry("local", local), _ChainEntry("spare", spare), _ChainEntry("openai", hosted, hosted=True, model="gpt-4.1"),
    ])
    assert await chain.generate_text("s", "u") == "spare"

    spare.fail = DOWN
    chain._cooldown_until.clear()
    with pytest.raises(CloudFallbackNeeded):
        await chain.generate_text("s", "u")
    assert hosted.calls == 0

    chain._cooldown_until.clear()
    with hosted_allowed({"openai"}):
        assert await chain.generate_text("s", "u") == "openai"


@pytest.mark.asyncio
async def test_a_hosted_primary_the_user_chose_is_not_gated() -> None:
    chain = ChainLLMProvider([_ChainEntry("openai", _Provider("openai"), hosted=True, model="gpt-4.1")])
    assert await chain.generate_text("s", "u") == "openai"


# ---- the worker asks, and only a plain yes lets the task go on ---------------


class _Operator(QueueOperator):
    """Raises the scripted exception for the first call, then answers; records whether a hosted profile was allowed."""

    def __init__(self, decisions, first_error: Exception | None) -> None:
        super().__init__(decisions)
        self.first_error = first_error
        self.allowed_when_called: list[bool] = []

    async def decide(self, *args, **kwargs):
        self.allowed_when_called.append(is_allowed("openai_saved"))
        if self.first_error is not None:
            error, self.first_error = self.first_error, None
            raise error
        return await super().decide(*args, **kwargs)


def _needed() -> CloudFallbackNeeded:
    return CloudFallbackNeeded("openai_saved", "gpt-4.1", "it timed out")


DONE = OperatorDecision(action=OperatorAction.DONE, final_answer="finished locally and then on the approved model")


async def _ask(tmp_path):
    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("What is two plus two")
    operator = _Operator([DONE], _needed())
    worker = TaskWorker(repos, audit, executor=_executor(_settings(), audit, repos), operator=operator)
    asked = await worker.process_task(task.id)
    return repos, audit, worker, operator, asked


@pytest.mark.asyncio
async def test_a_refused_fallback_pauses_the_task_and_asks_the_user(tmp_path) -> None:
    repos, _audit, _worker, operator, asked = await _ask(tmp_path)

    assert asked.status == TaskStatus.CLARIFYING
    question = asked.metadata["clarifying_question"]
    assert "Nothing has been sent anywhere" in question and "gpt-4.1" in question and "it timed out" in question
    assert asked.metadata["cloud_fallback_pending"]["profile"] == "openai_saved"
    assert not asked.metadata.get("cloud_allowed_profiles")
    assert operator.allowed_when_called == [False]


@pytest.mark.asyncio
async def test_a_plain_yes_lets_the_task_use_the_hosted_model_and_finish(tmp_path) -> None:
    repos, audit, worker, operator, asked = await _ask(tmp_path)

    resume_clarifying_task(repos, audit, asked, text="yes", actor="test", message_id="m1", received_at=utc_now())
    finished = await worker.process_task(asked.id)

    assert finished.status == TaskStatus.COMPLETED
    assert finished.metadata["cloud_allowed_profiles"] == ["openai_saved"]
    assert finished.objective == "What is two plus two"  # the permission question and answer are not part of the request
    assert operator.allowed_when_called == [False, True]  # the retry ran inside the approval
    assert finished.metadata.get("cloud_fallback_pending") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", ["maybe later", "Yes, please go ahead with what you proposed, staying within the constraints in my original message."])
async def test_any_other_reply_stops_the_task_without_sending(tmp_path, reply) -> None:
    repos, audit, worker, operator, asked = await _ask(tmp_path)

    resume_clarifying_task(repos, audit, asked, text=reply, actor="test", message_id="m1", received_at=utc_now())
    stopped = await worker.process_task(asked.id)

    assert stopped.status == TaskStatus.BLOCKED
    assert "nothing was sent" in stopped.metadata["last_worker_error"]
    assert operator.allowed_when_called == [False]  # the operator was never called again
    assert not stopped.metadata.get("cloud_allowed_profiles")


@pytest.mark.asyncio
async def test_no_cancels_the_task(tmp_path) -> None:
    repos, audit, _worker, _operator, asked = await _ask(tmp_path)

    resume_clarifying_task(repos, audit, asked, text="no", actor="test", message_id="m1", received_at=utc_now())

    assert repos.tasks.get(asked.id).status == TaskStatus.CANCELLED


# ---- Codex and Claude Code run only when the user asked for one -----------------


def _start(**extra) -> OperatorDecision:
    return OperatorDecision(
        action=OperatorAction.CALL_TOOL, tool_name="coding.agent",
        tool_input={"operation": "start", "provider": "claude_code", "objective": "build it", **extra},
    )


@pytest.mark.parametrize(
    "message, operation, requested",
    [
        ("Build me a todo app", "start", False),
        ("Build me a todo app", "run_goal", False),
        ("Build me a todo app with Claude Code", "start", True),
        ("have Codex write the parser", "start", True),
        ("ask GitHub Copilot to fix the tests", "start", True),
        ("use a coding agent for this", "start", True),
        ("Build me a todo app", "status", True),  # looking at a running session is not starting one
        ("Build me a todo app", "stop", True),
        ("my clause code is 4", "start", False),
    ],
)
def test_a_coding_agent_is_requested_only_when_the_user_names_one(message, operation, requested) -> None:
    from agent_control.schemas import TaskRecord

    task = TaskRecord(objective=message, metadata={"original_message_text": message})
    assert _coding_agent_requested(task, {"operation": operation}) is requested


@pytest.mark.asyncio
async def test_the_model_cannot_start_a_coding_agent_the_user_did_not_ask_for(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("Build me a todo app", metadata={"original_message_text": "Build me a todo app"})
    adapter = StaticToolAdapter({"summary": "started"})
    executor = _executor_with_adapter(_settings(), audit, repos, adapter, tool_name="coding.agent")
    worker = TaskWorker(repos, audit, executor=executor, operator=QueueOperator([_start()]))

    result = await worker.process_task(task.id)

    assert adapter.requests == []
    entry = result.metadata["operator_history"][-1]
    assert entry["status"] == "denied" and "only when the user asks" in entry["error"]


@pytest.mark.asyncio
async def test_a_named_coding_agent_still_starts(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    message = "Build me a todo app with Claude Code"
    task = repos.tasks.create(message, metadata={"original_message_text": message})
    adapter = StaticToolAdapter({"summary": "started"})
    executor = _executor_with_adapter(_settings(), audit, repos, adapter, tool_name="coding.agent")
    worker = TaskWorker(repos, audit, executor=executor, operator=QueueOperator([_start()]))

    await worker.process_task(task.id)

    assert len(adapter.requests) == 1


@pytest.mark.asyncio
async def test_insisting_on_an_unrequested_coding_agent_stops_the_task(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("Build me a todo app", metadata={"original_message_text": "Build me a todo app"})
    executor = _executor_with_adapter(_settings(), audit, repos, StaticToolAdapter({"summary": "started"}), tool_name="coding.agent")
    worker = TaskWorker(repos, audit, executor=executor, operator=QueueOperator([_start() for _ in range(5)]))

    result = task
    for _ in range(3):
        result = await worker.process_task(result.id)

    assert result.status == TaskStatus.BLOCKED
    assert "coding agent was not requested" in result.metadata["last_worker_error"]
