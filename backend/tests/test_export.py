"""`ybm export`: what is yours, as text and JSON, with no passwords or keys in it (export.py)."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from agent_control import projects_memory as pm
from agent_control.export import TABLES, export_all
from agent_control.schemas import ChannelType, InboundMessage, MemoryFact, MemorySource
from helpers import make_repos

SECRET = "hunter2-the-password-typed-into-a-form"


def _data(tmp_path):
    repos, _ = make_repos(tmp_path / "db")
    conversation = repos.conversations.get_or_create(ChannelType.TELEGRAM, "42")
    repos.messages.create(InboundMessage(id="m1", channel=ChannelType.TELEGRAM, kind="text", sender_id="7", chat_id="42", text="We are building a pool.", raw={"token": SECRET}), conversation)
    task = repos.tasks.create("Plan the pool", conversation_id=conversation, metadata={"synthesized_answer": "Here is the plan.", "operator_history": [{"input": {"password": SECRET}}]})
    repos.memory_facts.create(MemoryFact(category="life", content="Lives in Pearland, Texas", source=MemorySource.USER_STATED))
    pm.note_to_project(repos, "pool", ["backyard"], "decision", "Decided to build a pool in the backyard.")
    return repos, task


def test_what_is_yours_is_written_as_text_and_json_and_what_is_secret_is_not(tmp_path):
    repos, task = _data(tmp_path)
    skills = tmp_path / "skills"
    skills.mkdir()
    (skills / "tell-me-when.md").write_text("# a learned procedure\n", encoding="utf-8")
    folder = export_all(repos, tmp_path / "out", skills_dir=skills, now=datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc))
    assert folder.name == "ybm-export-20261119-120000"
    names = {path.relative_to(folder).as_posix() for path in folder.rglob("*") if path.is_file()}
    assert set(TABLES) | {"answers.jsonl", "README.md", "memory/INDEX.md", "memory/about-you.md", "memory/projects/pool/PROJECT.md", "skills/tell-me-when.md"} <= names

    messages = [json.loads(line) for line in (folder / "messages.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [m["text"] for m in messages] == ["We are building a pool."] and "raw_json" not in messages[0]
    assert [json.loads(line)["content"] for line in (folder / "facts.jsonl").read_text(encoding="utf-8").splitlines()] == ["Lives in Pearland, Texas"]
    assert json.loads((folder / "answers.jsonl").read_text(encoding="utf-8")) == {"task_id": task.id, "answer": "Here is the plan."}
    assert "Decided to build a pool in the backyard." in (folder / "memory" / "projects" / "pool" / "PROJECT.md").read_text(encoding="utf-8")
    assert "Lives in Pearland, Texas" in (folder / "memory" / "about-you.md").read_text(encoding="utf-8")
    readme = (folder / "README.md").read_text(encoding="utf-8")
    assert "Not here: passwords and keys" in readme and "messages.jsonl 1" in readme

    everything = "".join(path.read_text(encoding="utf-8") for path in folder.rglob("*") if path.is_file())
    assert SECRET not in everything  # not in the raw message, not in what a tool was given: neither leaves


def test_an_empty_database_exports_empty_files_and_a_second_export_does_not_overwrite_the_first(tmp_path):
    repos, _ = make_repos(tmp_path / "db")
    first = export_all(repos, tmp_path / "out", now=datetime(2026, 11, 19, 12, 0, 0, tzinfo=timezone.utc))
    second = export_all(repos, tmp_path / "out", now=datetime(2026, 11, 19, 12, 0, 1, tzinfo=timezone.utc))
    assert first != second and (first / "messages.jsonl").read_text(encoding="utf-8") == ""
    assert "Nothing yet." in (first / "memory" / "about-you.md").read_text(encoding="utf-8")


def test_the_command_is_known_to_the_cli_and_the_launcher():
    import importlib.util
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    assert '"export"' in (root / "backend" / "src" / "agent_control" / "cli.py").read_text(encoding="utf-8")
    script = (root / "scripts" / "ybm.ps1").read_text(encoding="utf-8")
    assert '"backup", "export"' in script and 'agent_control.cli export' in script
    assert importlib.util.find_spec("agent_control.export") is not None
