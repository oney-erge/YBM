"""Undoing a move takes away the folders that move made, once they are empty, and nothing else."""

from __future__ import annotations

import pytest

from agent_control.channels.undo_command import merge_undo_moves, remove_empty_folders_the_move_made, undo_moves_from_output
from agent_control.channels.task_notify import format_task_message
from agent_control.schemas import Capability, TaskRecord, TaskStatus, ToolCallRequest
from agent_control.tools.filesystem_manage import FilesystemManageAdapter


def _request(root, manifest):
    return ToolCallRequest(
        task_id="task_fs", tool_name="filesystem.manage", capability=Capability.FILESYSTEM_WRITE,
        input={"operation": "apply_manifest", "root": str(root), "manifest": manifest},
    )


@pytest.mark.asyncio
async def test_a_move_reports_the_folders_it_made_shallowest_first_and_not_the_ones_that_were_there(tmp_path):
    root = tmp_path / "photos"
    (root / "existing").mkdir(parents=True)
    for name in ("a.png", "b.png"):
        (root / name).write_text("x", encoding="utf-8")
    result = await FilesystemManageAdapter([str(tmp_path)]).execute(_request(root, [
        {"operation": "move", "source": "a.png", "destination": "pictures/2026/a.png"},
        {"operation": "move", "source": "b.png", "destination": "pictures/2026/b.png"},
        {"operation": "move", "source": "pictures/2026/b.png", "destination": "existing/b.png"},
    ]))
    assert result.status.value == "succeeded"
    assert result.output["created_folders"] == [str(root / "pictures"), str(root / "pictures" / "2026")]


@pytest.mark.asyncio
async def test_undoing_a_move_puts_the_files_back_and_removes_the_empty_folders_it_made(tmp_path):
    root = tmp_path / "photos"
    root.mkdir()
    for name in ("a.png", "b.png"):
        (root / name).write_text(name, encoding="utf-8")
    adapter = FilesystemManageAdapter([str(tmp_path)])
    moved = await adapter.execute(_request(root, [
        {"operation": "move", "source": "a.png", "destination": "pictures/a.png"},
        {"operation": "move", "source": "b.png", "destination": "pictures/b.png"},
    ]))
    record = undo_moves_from_output(moved.output)
    assert record is not None and record["created_folders"] == [str(root / "pictures")]

    back = [{"operation": "move", "source": m["from"], "destination": m["to"]} for m in record["moves"]]
    await adapter.execute(_request(root, back))
    assert (root / "a.png").exists() and (root / "b.png").exists()

    assert remove_empty_folders_the_move_made(record) == [str(root / "pictures")]
    assert not (root / "pictures").exists()
    assert (root / "a.png").read_text(encoding="utf-8") == "a.png"


def test_a_folder_that_has_something_in_it_now_stays(tmp_path):
    root = tmp_path / "photos"
    (root / "pictures").mkdir(parents=True)
    (root / "pictures" / "mine.txt").write_text("kept", encoding="utf-8")
    assert remove_empty_folders_the_move_made({"root": str(root), "created_folders": [str(root / "pictures")]}) == []
    assert (root / "pictures" / "mine.txt").exists()


def test_only_folders_inside_the_root_the_move_was_made_in_are_touched(tmp_path):
    root = tmp_path / "photos"
    root.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    record = {"root": str(root), "created_folders": [str(outside), str(root), str(tmp_path)]}
    assert remove_empty_folders_the_move_made(record) == []
    assert outside.exists() and root.exists()


def test_nested_folders_go_deepest_first(tmp_path):
    root = tmp_path / "photos"
    deep = root / "pictures" / "2026"
    deep.mkdir(parents=True)
    record = {"root": str(root), "created_folders": [str(root / "pictures"), str(deep)]}
    assert remove_empty_folders_the_move_made(record) == [str(deep), str(root / "pictures")]


def test_a_record_that_is_missing_or_malformed_removes_nothing():
    for record in (None, {}, {"root": ""}, {"root": "x", "created_folders": "not a list"}, "text"):
        assert remove_empty_folders_the_move_made(record) == []


def test_two_moves_in_one_task_keep_every_folder_they_made():
    first = {"root": "/r", "moves": [{"from": "/r/p/a", "to": "/r/a"}], "created_folders": ["/r/p"]}
    second = {"root": "/r", "moves": [{"from": "/r/q/b", "to": "/r/b"}], "created_folders": ["/r/p", "/r/q"]}
    assert merge_undo_moves(first, second)["created_folders"] == ["/r/p", "/r/q"]
    other_root = {"root": "/other", "moves": [{"from": "/other/z", "to": "/other/y"}], "created_folders": ["/other/z"]}
    assert merge_undo_moves(first, other_root) == other_root


def test_the_message_says_which_folder_was_removed():
    task = TaskRecord(
        objective="Undo: put 3 file(s) back where they were", status=TaskStatus.COMPLETED,
        metadata={"synthesized_answer": "Put 3 files back.", "removed_empty_folders": [r"C:\photos\pictures"]},
    )
    assert "I also removed the empty folder the move had made: pictures" in format_task_message(task)
