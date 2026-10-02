"""ybm trace <task_id> - a one-command task post-mortem that reads the DB
directly, no running backend required (docs/archive/HISTORY.md §2.4)."""

from __future__ import annotations

import json

import pytest

from agent_control import cli
from agent_control.storage import AuditLogger, Database, Repositories


def _repos(tmp_path) -> Repositories:
    database = Database(f"sqlite:///{tmp_path / 'trace.db'}")
    database.initialize()
    return Repositories.for_database(database)


@pytest.fixture
def patched_repositories(tmp_path, monkeypatch):
    repositories = _repos(tmp_path)
    monkeypatch.setattr(cli, "build_repositories", lambda: (repositories, AuditLogger(repositories.audit)))
    return repositories


def test_trace_task_prints_operator_history_and_error(patched_repositories, capsys) -> None:
    task = patched_repositories.tasks.create("find my resume and send it to me")
    patched_repositories.tasks.update_metadata(
        task.id,
        {
            "operator_history": [
                {
                    "tool_name": "filesystem.manage",
                    "input": {"operation": "search", "query": "resume"},
                    "status": "succeeded",
                    "output_summary": "Found 1 result: resume.txt",
                    "error": None,
                },
                {
                    "tool_name": "artifact.deliver",
                    "input": {"operation": "send_file", "path": "resume.txt"},
                    "status": "failed",
                    "output_summary": None,
                    "error": "chat_id is required",
                },
            ],
            "last_worker_error": "chat_id is required",
        },
    )

    exit_code = cli.trace_task(task.id)

    assert exit_code == 0
    out = capsys.readouterr().out
    assert task.id in out
    assert "filesystem.manage" in out
    assert "Found 1 result: resume.txt" in out
    assert "artifact.deliver" in out
    assert "chat_id is required" in out


def test_trace_task_prints_token_usage_breakdown(patched_repositories, capsys) -> None:
    """docs/archive/HISTORY.md Part 4 T1.4: ybm trace is the no-running-backend
    post-mortem tool, so this is where cost visibility matters most."""
    task = patched_repositories.tasks.create("what is the invoice total?")
    patched_repositories.tasks.update_metadata(
        task.id,
        {
            "token_usage": {
                "calls": 3,
                "prompt_tokens": 300,
                "completion_tokens": 45,
                "total_tokens": 345,
                "by_source": {
                    "operator": {"calls": 2, "prompt_tokens": 250, "completion_tokens": 30, "total_tokens": 280},
                    "auditor": {"calls": 1, "prompt_tokens": 50, "completion_tokens": 15, "total_tokens": 65},
                },
                "last_model": "gpt-4.1-mini",
            }
        },
    )

    exit_code = cli.trace_task(task.id)

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "345 total over 3 call(s)" in out
    assert "operator=280" in out
    assert "auditor=65" in out


def test_trace_task_omits_token_line_when_no_usage_recorded(patched_repositories, capsys) -> None:
    task = patched_repositories.tasks.create("trivial task, no LLM usage recorded")

    exit_code = cli.trace_task(task.id)

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "tokens" not in out


def test_trace_task_json_mode_round_trips(patched_repositories, capsys) -> None:
    task = patched_repositories.tasks.create("trivial task")

    exit_code = cli.trace_task(task.id, as_json=True)

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["task"]["id"] == task.id
    assert payload["operator_history"] == []


def test_trace_task_returns_1_for_unknown_task(patched_repositories, capsys) -> None:
    exit_code = cli.trace_task("task_does_not_exist")

    assert exit_code == 1
    assert "no task found" in capsys.readouterr().out
