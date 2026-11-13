from __future__ import annotations

import pytest

from agent_control.schemas import Capability, ChannelType, TaskStatus, ToolCallRequest
from agent_control.storage import Database, Repositories
from agent_control.tools.registry import build_tool_registry
from agent_control.tools.task_status import TaskStatusAdapter
from agent_control.config import AppSettings, CapabilityPolicy


@pytest.mark.asyncio
async def test_task_status_adapter_reports_recent_active_and_blocked_tasks(tmp_path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'agent.db'}")
    database.initialize()
    repos = Repositories.for_database(database)
    conversation_id = repos.conversations.get_or_create(ChannelType.TELEGRAM, "100")
    active = repos.tasks.create("Run active task", conversation_id=conversation_id)
    repos.tasks.update_status(active.id, TaskStatus.RUNNING)
    failed = repos.tasks.create("Failed task", conversation_id=conversation_id)
    repos.tasks.update_status(failed.id, TaskStatus.FAILED)

    result = await TaskStatusAdapter(repos).execute(
        ToolCallRequest(
            task_id="task_status",
            tool_name="task.status",
            capability=Capability.TELEGRAM_RECEIVE,
            input={"operation": "status", "limit": 10},
        )
    )

    assert result.status.value == "succeeded"
    assert result.output["task_status"]["active_count"] == 1
    rows = {row["id"]: row for row in result.output["task_status"]["recent_tasks"]}
    assert rows[active.id]["allowed_actions"] == ["pause", "cancel"]
    assert rows[failed.id]["allowed_actions"] == []
    assert "Blocked or failed recently: 1" in result.output["summary"]
    assert result.output["terminal_output"]


@pytest.mark.asyncio
async def test_task_status_reports_awaiting_external_and_coding_sessions(tmp_path) -> None:
    import json

    database = Database(f"sqlite:///{tmp_path / 'agent.db'}")
    database.initialize()
    repos = Repositories.for_database(database)
    task = repos.tasks.create("Use Codex", metadata={"awaiting_external": {"session_id": "codex_live"}})
    repos.tasks.update_status(task.id, TaskStatus.AWAITING_EXTERNAL)
    session_root = tmp_path / "sessions"
    session_root.mkdir()
    (session_root / "codex_live.json").write_text(
        json.dumps(
            {
                "session_id": "codex_live",
                "provider": "codex",
                "status": "running",
                "task_id": task.id,
                "workspace_dir": str(tmp_path),
                "started_at": "2026-07-05T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    settings = AppSettings(
        _env_file=None,
        adapters={"coding_agent": {"session_root": str(session_root)}},
        mcp={"enabled": True, "servers": {}},
    )

    result = await TaskStatusAdapter(repos, settings).execute(
        ToolCallRequest(
            task_id="task_status",
            tool_name="task.status",
            capability=Capability.TELEGRAM_RECEIVE,
            input={"operation": "status", "limit": 10},
        )
    )

    assert result.output["task_status"]["awaiting_external_count"] == 1
    assert result.output["task_status"]["active_coding_sessions"][0]["session_id"] == "codex_live"
    assert result.output["task_status"]["mcp"]["enabled"] is True


@pytest.mark.asyncio
async def test_task_status_finds_stale_waiting_task_beyond_recency_limit(tmp_path) -> None:
    """A task waiting for approval/clarification must stay visible even after
    more than `limit` newer tasks have been created since it started waiting."""
    database = Database(f"sqlite:///{tmp_path / 'agent.db'}")
    database.initialize()
    repos = Repositories.for_database(database)
    conversation_id = repos.conversations.get_or_create(ChannelType.TELEGRAM, "100")

    stuck_approval = repos.tasks.create("Old task needing approval", conversation_id=conversation_id)
    repos.tasks.update_status(stuck_approval.id, TaskStatus.AWAITING_APPROVAL)
    stuck_clarify = repos.tasks.create("Old task needing clarification", conversation_id=conversation_id)
    repos.tasks.update_status(stuck_clarify.id, TaskStatus.CLARIFYING)

    # Bury both behind more tasks than the status query's `limit` window.
    for index in range(5):
        newer = repos.tasks.create(f"Newer completed task {index}", conversation_id=conversation_id)
        repos.tasks.update_status(newer.id, TaskStatus.COMPLETED)

    result = await TaskStatusAdapter(repos).execute(
        ToolCallRequest(
            task_id="task_status",
            tool_name="task.status",
            capability=Capability.TELEGRAM_RECEIVE,
            input={"operation": "status", "limit": 3},
        )
    )

    output = result.output["task_status"]
    assert output["waiting_approval_count"] == 1
    assert output["waiting_clarification_count"] == 1
    row_ids = {row["id"] for row in output["recent_tasks"]}
    assert stuck_approval.id in row_ids
    assert stuck_clarify.id in row_ids


@pytest.mark.asyncio
async def test_task_status_exposes_redacted_recovery_question_for_agents(tmp_path) -> None:
    import json

    from agent_control.orchestration.worker import _tool_output_text

    database = Database(f"sqlite:///{tmp_path / 'agent.db'}")
    database.initialize()
    repos = Repositories.for_database(database)
    task = repos.tasks.create("investigate an interrupted operation")
    # Older stored rows may predate answer-field redaction.
    metadata = {
        "clarifying_question": "Check whether it took effect; API_KEY=do-not-expose",
        "interrupted_step": {"input": {"private": "do-not-copy"}},
    }
    repos.tasks.update_status(task.id, TaskStatus.CLARIFYING)
    with database.connect() as connection:
        connection.execute("UPDATE tasks SET metadata_json = ? WHERE id = ?", (json.dumps(metadata), task.id))

    result = await TaskStatusAdapter(repos).execute(ToolCallRequest(
        task_id="task_status", tool_name="task.status", capability=Capability.AGENT_CORE,
        input={"operation": "status"},
    ))

    row = next(row for row in result.output["task_status"]["recent_tasks"] if row["id"] == task.id)
    assert "Check whether it took effect" in row["clarifying_question"]
    assert "do-not-expose" not in row["clarifying_question"]
    assert "interrupted_step" not in row
    terminal_text = result.output["terminal_output"][0]["content"]
    assert "Check whether it took effect" in terminal_text
    assert "do-not-expose" not in terminal_text
    assert "Check whether it took effect" in _tool_output_text(result)
    assert repos.tasks.get(task.id).status == TaskStatus.CLARIFYING


def test_registry_exposes_task_status_when_repositories_are_available(tmp_path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'agent.db'}")
    database.initialize()
    repos = Repositories.for_database(database)
    settings = AppSettings(
        _env_file=None,
        capabilities={Capability.TELEGRAM_RECEIVE: CapabilityPolicy(enabled=True, requires_approval=False)},
    )

    registry = build_tool_registry(settings, "http://127.0.0.1:8765", repositories=repos)
    definitions = {definition.name: definition for definition in registry.definitions}

    assert definitions["task.status"].enabled is True
    assert "task.status" in registry.adapters
