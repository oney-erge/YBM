"""MCP server exposing YBM to MCP clients (Claude Code, Codex, etc.).

Run from the repo root (so config/config.yaml and the SQLite DB resolve):

    PYTHONPATH=backend/src python -m agent_control.mcp_server

The repo-root .mcp.json registers it for Claude Code automatically. Tools
operate on the same database the Telegram/worker stack uses, so a task
created here is picked up by the running worker like any other task.
"""

from __future__ import annotations

from functools import lru_cache

from mcp.server.fastmcp import FastMCP

from agent_control.clarification import objective_after_answer
from agent_control.config import load_settings
from agent_control.schemas import TaskRecord, TaskStatus
from agent_control.storage import Database, Repositories
from agent_control.tools.coding_agent import (
    load_session,
    load_sessions,
    read_log_tail,
    stop_session_process,
)

mcp = FastMCP("ybm")


@lru_cache(maxsize=1)
def _state() -> tuple[Repositories, str]:
    settings = load_settings()
    database = Database(settings.storage.database_url)
    database.initialize()
    return Repositories.for_database(database), settings.adapters.coding_agent.session_root


@mcp.tool()
def create_task(objective: str) -> dict:
    """Create a YBM task; the running worker plans and executes it."""
    repositories, _ = _state()
    task = repositories.tasks.create(objective, metadata={"source_channel": "mcp"})
    return _task_brief(task)


@mcp.tool()
def get_task(task_id: str) -> dict:
    """Get a task's status, last output, pending question, and error."""
    repositories, _ = _state()
    task = repositories.tasks.get(task_id)
    if task is None:
        return {"error": f"task not found: {task_id}"}
    return _task_brief(task)


@mcp.tool()
def list_tasks(limit: int = 10) -> list[dict]:
    """List the most recent YBM tasks."""
    repositories, _ = _state()
    return [_task_brief(task) for task in repositories.tasks.list_recent(limit)]


@mcp.tool()
def answer_task_question(task_id: str, answer: str) -> dict:
    """Answer a task that is waiting in 'clarifying' state and resume it."""
    repositories, _ = _state()
    task = repositories.tasks.get(task_id)
    if task is None:
        return {"error": f"task not found: {task_id}"}
    if task.status != TaskStatus.CLARIFYING:
        return {"error": f"task is not waiting for an answer (status: {task.status.value})"}
    repositories.tasks.update_objective(task.id, objective_after_answer(task, answer))
    answers = list(task.metadata.get("clarification_answers") or [])
    answers.append(
        {
            "question": task.metadata.get("clarifying_question"),
            "answer": answer,
            "source": "mcp",
        }
    )
    metadata = {
        **task.metadata,
        "clarification_answer": answer,
        "clarification_answers": answers,
        "answered_clarifying_question": task.metadata.get("clarifying_question"),
        "operator_history_offset_after_clarification": len(task.metadata.get("operator_history") or []),
        "retry_count": 0,
        "replan_count": 0,
        "evaluator_repair_count": 0,
        "fulfillment_retry_count": 0,
    }
    metadata.pop("clarifying_question", None)
    updated = repositories.tasks.update_metadata(task.id, metadata, TaskStatus.RECEIVED)
    return _task_brief(updated)


@mcp.tool()
def coding_sessions(limit: int = 10) -> list[dict]:
    """List recent background coding-agent sessions (codex/claude_code/github_copilot)."""
    _, session_root = _state()
    return [_session_brief(session) for session in load_sessions(session_root, limit=limit)]


@mcp.tool()
def coding_session_log(session_id: str, max_chars: int = 4000) -> str:
    """Return the tail of a coding session's live log."""
    _, session_root = _state()
    session = load_session(session_root, session_id)
    if session is None:
        return f"session not found: {session_id}"
    return read_log_tail(str(session.get("log_path") or ""), max_chars=max_chars) or "(log is empty)"


@mcp.tool()
def stop_coding_session(session_id: str) -> dict:
    """Request termination of a running coding session."""
    _, session_root = _state()
    session = load_session(session_root, session_id)
    if session is None:
        return {"error": f"session not found: {session_id}"}
    if session.get("status") != "running":
        return {"session_id": session_id, "status": session.get("status"), "note": "session is not running"}
    stopped = stop_session_process(session)
    return {"session_id": session_id, "stop_requested": stopped}


def _task_brief(task: TaskRecord) -> dict:
    metadata = task.metadata
    return {
        "task_id": task.id,
        "status": task.status.value,
        "objective": task.objective,
        "question": metadata.get("clarifying_question"),
        "answer": metadata.get("synthesized_answer"),
        "last_tool": metadata.get("last_tool_name"),
        "last_output": str(metadata.get("last_tool_output_text") or "")[:2000] or None,
        "error": metadata.get("last_worker_error") or metadata.get("fulfillment_gap"),
        "workspace_dir": metadata.get("workspace_dir"),
        "preview_url": metadata.get("preview_url"),
        "created_at": task.created_at.isoformat(),
        "updated_at": task.updated_at.isoformat(),
    }


def _session_brief(session: dict) -> dict:
    return {
        key: session.get(key)
        for key in (
            "session_id",
            "provider",
            "status",
            "task_id",
            "workspace_dir",
            "returncode",
            "started_at",
            "ended_at",
            "changed_files",
            "summary",
        )
    }


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
