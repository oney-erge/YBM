"""Undo the last move: "undo that" puts the files back through the ordinary approved replay."""

from __future__ import annotations

import asyncio

import pytest

from agent_control.channels.undo_command import MAX_UNDO_MOVES, undo_command_reply, undo_moves_from_output
from agent_control.orchestration import TaskWorker
from agent_control.schemas import ChannelType, InboundMessage, MessageKind, TaskStatus, ToolCallResult, ToolResultStatus
from agent_control.tools.filesystem_manage import FilesystemManageAdapter
from helpers import make_repos


def _say(repos, audit, conv, text="undo that", channel=ChannelType.TELEGRAM):
    inbound = InboundMessage(channel=channel, chat_id="42", sender_id="1", text=text, kind=MessageKind.TEXT)
    return undo_command_reply(repos, audit, inbound, conv)


@pytest.fixture
def env(tmp_path):
    repos, audit = make_repos(tmp_path)
    return repos, audit, repos.conversations.get_or_create(ChannelType.TELEGRAM, "42"), tmp_path


def _moved_task(repos, conv, tmp_path, names=("a.pdf", "b.pdf")):
    """A finished task that moved files into a folder, recorded the way the worker records it."""
    sorted_dir = tmp_path / "pdfs"
    sorted_dir.mkdir(exist_ok=True)
    moves = []
    for name in names:
        new = sorted_dir / name
        new.write_text("x")
        moves.append({"from": str(new), "to": str(tmp_path / name)})
    task = repos.tasks.create("sort my pdfs", conversation_id=conv, metadata={"source_chat_id": "42"})
    return repos.tasks.update_metadata(
        task.id, {**task.metadata, "undo_moves": {"root": str(tmp_path), "moves": moves}}, TaskStatus.COMPLETED,
    )


def test_what_to_record_after_apply_manifest():
    output = {
        "root": "/r", "dry_run": False,
        "manifest": [{"source": "/r/a.pdf", "destination": "/r/pdfs", "operation": "move"},
                     {"source": "/r/b.pdf", "destination": "/r/pdfs", "operation": "copy"},
                     {"source": "/r/c.pdf", "destination": "/r/pdfs"}],
        "changed_paths": ["/r/pdfs/a.pdf", "/r/pdfs/b_2.pdf", "/r/pdfs/c.pdf"],
    }
    assert undo_moves_from_output(output) == {"root": "/r", "moves": [
        {"from": "/r/pdfs/a.pdf", "to": "/r/a.pdf"}, {"from": "/r/pdfs/c.pdf", "to": "/r/c.pdf"}]}


@pytest.mark.parametrize("output", [
    {"dry_run": True, "manifest": [{"source": "a"}], "changed_paths": ["b"]},
    {"manifest": [{"source": "a"}], "changed_paths": []},
    {"manifest": [], "changed_paths": []},
    {},
])
def test_nothing_is_recorded_when_nothing_moved_or_it_cannot_be_matched(output):
    assert undo_moves_from_output(output) is None


def test_the_record_is_capped():
    manifest = [{"source": f"/r/{n}.txt", "destination": "/r/x"} for n in range(MAX_UNDO_MOVES + 50)]
    undo = undo_moves_from_output({"root": "/r", "manifest": manifest, "changed_paths": [f"/r/x/{n}.txt" for n in range(len(manifest))]})
    assert len(undo["moves"]) == MAX_UNDO_MOVES


def test_undo_makes_a_replay_task_that_moves_each_file_back(env):
    repos, audit, conv, tmp_path = env
    source = _moved_task(repos, conv, tmp_path)
    reply = _say(repos, audit, conv)
    assert reply.text.startswith("Putting 2 file(s) back where they were")
    task = next(t for t in repos.tasks.list_recent(10) if t.metadata.get("undo_of_task_id") == source.id)
    step = task.metadata["replay_plan"][0]
    assert step["tool_name"] == "filesystem.manage" and step["tool_input"]["operation"] == "apply_manifest"
    assert {(m["source"], m["destination"]) for m in step["tool_input"]["manifest"]} == {
        (str(tmp_path / "pdfs" / "a.pdf"), str(tmp_path / "a.pdf")), (str(tmp_path / "pdfs" / "b.pdf"), str(tmp_path / "b.pdf"))}
    assert task.metadata["source_channel"] == "telegram" and task.metadata["source_chat_id"] == "42" and task.conversation_id == conv


def test_the_replay_really_puts_the_files_back(env):
    repos, audit, conv, tmp_path = env
    _moved_task(repos, conv, tmp_path)
    _say(repos, audit, conv)
    plan = next(t for t in repos.tasks.list_recent(10) if t.metadata.get("undo_of_task_id")).metadata["replay_plan"][0]["tool_input"]
    from agent_control.schemas import Capability, ToolCallRequest

    result = asyncio.run(FilesystemManageAdapter([str(tmp_path)]).execute(ToolCallRequest(
        task_id="t", tool_name="filesystem.manage", capability=Capability.FILESYSTEM_WRITE, input=plan)))
    assert result.status.value == "succeeded"
    assert (tmp_path / "a.pdf").exists() and (tmp_path / "b.pdf").exists() and not (tmp_path / "pdfs" / "a.pdf").exists()


def test_undo_twice_does_not_do_it_twice(env):
    repos, audit, conv, tmp_path = env
    _moved_task(repos, conv, tmp_path)
    _say(repos, audit, conv)
    assert _say(repos, audit, conv).text == "I already put those back."


def test_files_that_are_not_where_they_were_left_are_skipped_and_said(env):
    repos, audit, conv, tmp_path = env
    _moved_task(repos, conv, tmp_path)
    (tmp_path / "pdfs" / "a.pdf").unlink()
    assert "(1 are not where I left them, so I'll leave those.)" in _say(repos, audit, conv).text


def test_nothing_to_undo_says_so(env):
    repos, audit, conv, tmp_path = env
    assert _say(repos, audit, conv).text == "I don't have a move to undo."
    _moved_task(repos, conv, tmp_path)
    for name in ("a.pdf", "b.pdf"):
        (tmp_path / "pdfs" / name).unlink()
    assert "nothing to undo" in _say(repos, audit, conv).text


def test_a_task_that_did_not_finish_is_not_undone(env):
    repos, audit, conv, tmp_path = env
    task = _moved_task(repos, conv, tmp_path)
    repos.tasks.update_metadata(task.id, task.metadata, TaskStatus.BLOCKED)
    assert _say(repos, audit, conv).text == "I don't have a move to undo."


@pytest.mark.parametrize("text", ["undo", "Undo that.", "put them back", "revert the last move", "move them back"])
def test_the_ways_to_ask(env, text):
    repos, audit, conv, tmp_path = env
    assert _say(repos, audit, conv, text) is not None


@pytest.mark.parametrize("text", ["undo the changes in my git repo please and then run the tests", "find my resume", "what is an undo button", ""])
def test_other_messages_are_left_alone(env, text):
    repos, audit, conv, _ = env
    assert _say(repos, audit, conv, text) is None


def test_other_channels_are_left_alone(env):
    repos, audit, conv, _ = env
    assert _say(repos, audit, conv, channel=ChannelType.WHATSAPP) is None


def test_the_worker_records_the_moves_when_a_manifest_is_applied(env):
    from test_worker_operator_loop import QueueOperator, _executor, _settings

    repos, audit, conv, tmp_path = env
    task = repos.tasks.create("sort", conversation_id=conv)
    worker = TaskWorker(repos, audit, executor=_executor(_settings(), audit, repos), operator=QueueOperator([]))
    updated = worker._record_tool_result(task.id, "filesystem.manage", ToolCallResult(
        request_id="r", status=ToolResultStatus.SUCCEEDED,
        output={"operation": "apply_manifest", "root": str(tmp_path), "dry_run": False,
                "manifest": [{"source": str(tmp_path / "a.pdf"), "destination": str(tmp_path / "pdfs")}],
                "changed_paths": [str(tmp_path / "pdfs" / "a.pdf")]},
    ))
    assert updated.metadata["undo_moves"]["moves"] == [{"from": str(tmp_path / "pdfs" / "a.pdf"), "to": str(tmp_path / "a.pdf")}]
