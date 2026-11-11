from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from agent_control import cli
from agent_control.tools import coding_agent
from agent_control.orchestration.signals import apply_task_signal
from agent_control.schemas import TaskStatus
from agent_control.tools.coding_agent import CodingAgentAdapter, _coding_session_completion_callback
from helpers import make_repos


class RecordingTelegram:
    def __init__(self) -> None:
        self.messages = []

    async def send_message(self, chat_id, text) -> None:
        self.messages.append((chat_id, text))


def _session(task_id: str, session_id: str) -> dict:
    return {"task_id": task_id, "session_id": session_id, "request_id": f"request-{session_id}",
            "status": "completed", "returncode": 0, "summary": "finished"}


def _awaiting(repos, task_id: str, session_id: str) -> None:
    repos.tasks.update_metadata(task_id, {
        "source_chat_id": "synthetic-chat",
        "awaiting_external": {"session_id": session_id, "step_id": "current-step", "tool_name": "coding.agent"},
        "operator_pending_call": {"tool_name": "coding.agent", "step_id": "current-step"},
    }, TaskStatus.AWAITING_EXTERNAL)


@pytest.mark.asyncio
async def test_stale_session_completion_does_not_resume_or_notify(tmp_path) -> None:
    repos, _ = make_repos(tmp_path)
    task = repos.tasks.create("wait for a coding session")
    _awaiting(repos, task.id, "new-session")
    telegram = RecordingTelegram()

    handled, error = await cli._handle_coding_session_completion(None, repos, _session(task.id, "old-session"), telegram)

    current = repos.tasks.get(task.id)
    assert handled and error is None
    assert current.status == TaskStatus.AWAITING_EXTERNAL
    assert "pending_tool_result" not in current.metadata
    assert current.metadata["awaiting_external"]["session_id"] == "new-session"
    assert telegram.messages == []


@pytest.mark.asyncio
@pytest.mark.parametrize("callback_first", [False, True])
async def test_in_process_callback_and_watcher_share_session_authority(tmp_path, callback_first) -> None:
    repos, _ = make_repos(tmp_path)
    task = repos.tasks.create("wait for a coding session")
    _awaiting(repos, task.id, "new-session")
    telegram = RecordingTelegram()
    callback = _coding_session_completion_callback(SimpleNamespace(task_repository=repos.tasks, telegram_client=telegram))

    await callback(_session(task.id, "old-session"))
    current = repos.tasks.get(task.id)
    assert "coding_agent_session" not in current.metadata
    assert current.status == TaskStatus.AWAITING_EXTERNAL
    assert telegram.messages == []

    session = _session(task.id, "new-session")
    if callback_first:
        await callback(session)
        assert await cli._handle_coding_session_completion(None, repos, session, telegram) == (True, None)
    else:
        assert await cli._handle_coding_session_completion(None, repos, session, telegram) == (True, None)
        await callback(session)
    assert repos.tasks.get(task.id).metadata["pending_tool_result"]["result"]["request_id"] == "request-new-session"
    assert len(telegram.messages) == 1


@pytest.mark.asyncio
async def test_in_process_early_completion_waits_for_watcher_binding(tmp_path) -> None:
    repos, _ = make_repos(tmp_path)
    task = repos.tasks.create("start a coding session")
    repos.tasks.update_metadata(task.id, {"operator_in_flight": {"tool_name": "coding.agent"}}, TaskStatus.RUNNING)
    telegram = RecordingTelegram()
    callback = _coding_session_completion_callback(SimpleNamespace(task_repository=repos.tasks, telegram_client=telegram))
    session = _session(task.id, "session-1")

    await callback(session)
    assert telegram.messages == []
    assert "pending_tool_result" not in repos.tasks.get(task.id).metadata
    assert "coding_agent_session" not in repos.tasks.get(task.id).metadata
    _awaiting(repos, task.id, "session-1")
    assert await cli._handle_coding_session_completion(None, repos, session, telegram) == (True, None)
    assert len(telegram.messages) == 1


@pytest.mark.asyncio
async def test_in_process_adapter_watch_uses_transactional_completion(tmp_path, monkeypatch) -> None:
    repos, _ = make_repos(tmp_path)
    task = repos.tasks.create("wait for a coding session")
    _awaiting(repos, task.id, "new-session")
    telegram = RecordingTelegram()
    callback = _coding_session_completion_callback(SimpleNamespace(task_repository=repos.tasks, telegram_client=telegram))
    adapter = CodingAgentAdapter(SimpleNamespace(timeout_seconds=10, start_wait_seconds=1), on_complete=callback)

    class FinishedProcess:
        async def wait(self):
            return 0

    monkeypatch.setattr(adapter, "_finalize", lambda session_id, *_args, **_kwargs: _session(task.id, session_id))
    await adapter._watch(FinishedProcess(), "old-session")
    assert repos.tasks.get(task.id).status == TaskStatus.AWAITING_EXTERNAL
    assert telegram.messages == []
    await adapter._watch(FinishedProcess(), "new-session")
    assert repos.tasks.get(task.id).metadata["pending_tool_result"]["result"]["request_id"] == "request-new-session"
    assert len(telegram.messages) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ["awaiting_tool", "pending_tool", "step"])
async def test_completion_rejects_mismatched_tool_or_step(tmp_path, mismatch) -> None:
    repos, _ = make_repos(tmp_path)
    task = repos.tasks.create("wait for an external session")
    _awaiting(repos, task.id, "session-1")
    current = repos.tasks.get(task.id)
    metadata = dict(current.metadata)
    if mismatch == "awaiting_tool":
        metadata["awaiting_external"]["tool_name"] = "different.tool"
    elif mismatch == "pending_tool":
        metadata["operator_pending_call"]["tool_name"] = "different.tool"
    else:
        metadata["operator_pending_call"]["step_id"] = "different-step"
    repos.tasks.update_metadata(task.id, metadata)
    telegram = RecordingTelegram()

    assert await cli._handle_coding_session_completion(None, repos, _session(task.id, "session-1"), telegram) == (True, None)
    current = repos.tasks.get(task.id)
    assert current.status == TaskStatus.AWAITING_EXTERNAL
    assert "pending_tool_result" not in current.metadata
    assert telegram.messages == []


@pytest.mark.asyncio
@pytest.mark.parametrize("pause", [False, True])
async def test_matching_session_completion_is_consumed_once_and_preserves_pause(tmp_path, pause) -> None:
    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("wait for a coding session")
    _awaiting(repos, task.id, "session-1")
    if pause:
        apply_task_signal(repos, audit, task.id, "pause", "operator", {})
    telegram = RecordingTelegram()
    session = _session(task.id, "session-1")

    assert await cli._handle_coding_session_completion(None, repos, session, telegram) == (True, None)
    current = repos.tasks.get(task.id)
    assert current.status == (TaskStatus.PAUSED if pause else TaskStatus.RUNNING)
    assert current.metadata["pending_tool_result"]["result"]["request_id"] == "request-session-1"
    assert len(telegram.messages) == 1
    assert await cli._handle_coding_session_completion(None, repos, session, telegram) == (True, None)
    assert len(telegram.messages) == 1
    if pause:
        apply_task_signal(repos, audit, task.id, "resume", "operator", {})
        assert repos.tasks.get(task.id).status == TaskStatus.RUNNING


@pytest.mark.asyncio
@pytest.mark.parametrize("pause", [False, True])
async def test_early_session_completion_is_deferred_until_task_binds_session(tmp_path, pause) -> None:
    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("start a coding session")
    repos.tasks.update_metadata(task.id, {"operator_in_flight": {"tool_name": "coding.agent"}}, TaskStatus.RUNNING)
    if pause:
        apply_task_signal(repos, audit, task.id, "pause", "operator", {})
    telegram = RecordingTelegram()
    session = _session(task.id, "session-1")

    assert await cli._handle_coding_session_completion(None, repos, session, telegram) == (False, None)
    assert repos.tasks.get(task.id).status == (TaskStatus.PAUSED if pause else TaskStatus.RUNNING)
    assert telegram.messages == []
    _awaiting(repos, task.id, "session-1")
    assert await cli._handle_coding_session_completion(None, repos, session, telegram) == (True, None)
    assert repos.tasks.get(task.id).metadata["pending_tool_result"]["result"]["request_id"] == "request-session-1"


@pytest.mark.asyncio
async def test_watcher_retries_a_completion_that_arrives_before_binding(tmp_path, monkeypatch) -> None:
    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("start a coding session")
    repos.tasks.update_metadata(task.id, {"operator_in_flight": {"tool_name": "coding.agent"}}, TaskStatus.RUNNING)
    session = _session(task.id, "session-1")
    settings = SimpleNamespace(adapters=SimpleNamespace(coding_agent=SimpleNamespace(session_root="synthetic")))
    notified = []
    ticks = 0

    async def scan(_root):
        return [session]

    async def next_tick(_delay):
        nonlocal ticks
        ticks += 1
        if ticks == 1:
            assert notified == []
            _awaiting(repos, task.id, "session-1")
        else:
            raise asyncio.CancelledError

    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    monkeypatch.setattr(cli, "build_repositories", lambda: (repos, audit))
    monkeypatch.setattr(cli, "_telegram_client", lambda *_args: None)
    monkeypatch.setattr(cli, "scan_coding_sessions_once", scan)
    monkeypatch.setattr(cli, "mark_session_notified", lambda _root, session_id, **_kwargs: notified.append(session_id))
    monkeypatch.setattr(cli.asyncio, "sleep", next_tick)
    with pytest.raises(asyncio.CancelledError):
        await cli.run_coding_session_watcher()
    assert notified == ["session-1"]
    assert repos.tasks.get(task.id).metadata["pending_tool_result"]["result"]["request_id"] == "request-session-1"


@pytest.mark.asyncio
async def test_completion_rechecks_session_after_result_is_constructed(tmp_path, monkeypatch) -> None:
    repos, _ = make_repos(tmp_path)
    task = repos.tasks.create("wait for a coding session")
    _awaiting(repos, task.id, "old-session")
    original = coding_agent.terminal_session_result

    def switch_session(session):
        result = original(session)
        _awaiting(repos, task.id, "new-session")
        return result

    monkeypatch.setattr(coding_agent, "terminal_session_result", switch_session)
    telegram = RecordingTelegram()
    assert await cli._handle_coding_session_completion(None, repos, _session(task.id, "old-session"), telegram) == (True, None)
    current = repos.tasks.get(task.id)
    assert current.status == TaskStatus.AWAITING_EXTERNAL
    assert current.metadata["awaiting_external"]["session_id"] == "new-session"
    assert "pending_tool_result" not in current.metadata
    assert telegram.messages == []


@pytest.mark.asyncio
async def test_legacy_awaiting_record_uses_its_pending_call_step(tmp_path) -> None:
    repos, _ = make_repos(tmp_path)
    task = repos.tasks.create("wait for a legacy coding session")
    _awaiting(repos, task.id, "session-1")
    current = repos.tasks.get(task.id)
    metadata = dict(current.metadata)
    metadata["awaiting_external"].pop("step_id")
    repos.tasks.update_metadata(task.id, metadata)

    assert await cli._handle_coding_session_completion(None, repos, _session(task.id, "session-1"), None) == (True, None)
    assert repos.tasks.get(task.id).metadata["pending_tool_result"]["step_id"] == "current-step"
