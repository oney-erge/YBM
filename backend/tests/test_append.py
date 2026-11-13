"""Adding to the end of a file: "add the word pears on a new line" must not make list_2.txt."""

from __future__ import annotations

import asyncio

import pytest

from agent_control.orchestration.worker import _filesystem_append_when_asked as append_rule
from agent_control.orchestration.worker import _filesystem_overwrite_when_asked_to_change as overwrite_rule
from agent_control.schemas import Capability, TaskRecord, ToolCallRequest
from agent_control.tools.contracts import FilesystemWriteTextFileInput
from agent_control.tools.filesystem_manage import FilesystemManageAdapter


def _write(adapter, path, content, **extra):
    request = ToolCallRequest(
        task_id="task_append", tool_name="filesystem.manage", capability=Capability.FILESYSTEM_WRITE,
        input={"operation": "write_text_file", "path": str(path), "content": content, **extra},
    )
    return asyncio.run(adapter.execute(request))


def _task(message: str) -> TaskRecord:
    return TaskRecord(objective=message, metadata={"original_message_text": message})


def test_append_adds_to_the_end_on_its_own_line_and_keeps_the_rest(tmp_path):
    target = tmp_path / "list.txt"
    target.write_text("apples", encoding="utf-8")
    result = _write(FilesystemManageAdapter([str(tmp_path)]), target, "pears", append=True)
    assert result.status.value == "succeeded"
    assert target.read_text(encoding="utf-8") == "apples\npears"
    assert [p.name for p in tmp_path.iterdir()] == ["list.txt"]  # no list_2.txt
    assert result.output["appended"] is True and "existing text was kept" in result.output["summary"]


def test_append_does_not_add_a_second_newline(tmp_path):
    target = tmp_path / "list.txt"
    target.write_text("apples\n", encoding="utf-8")
    _write(FilesystemManageAdapter([str(tmp_path)]), target, "pears", append=True)
    assert target.read_text(encoding="utf-8") == "apples\npears"


def test_append_to_a_file_that_does_not_exist_creates_it(tmp_path):
    target = tmp_path / "new.txt"
    result = _write(FilesystemManageAdapter([str(tmp_path)]), target, "first", append=True)
    assert result.status.value == "succeeded" and target.read_text(encoding="utf-8") == "first"


def test_without_append_an_existing_file_is_still_kept_and_a_copy_saved(tmp_path):
    target = tmp_path / "list.txt"
    target.write_text("apples", encoding="utf-8")
    _write(FilesystemManageAdapter([str(tmp_path)]), target, "pears")
    assert target.read_text(encoding="utf-8") == "apples" and (tmp_path / "list_2.txt").exists()


def test_the_schema_accepts_append_and_defaults_it_off():
    assert FilesystemWriteTextFileInput.model_validate({"path": "a.txt"}).append is False
    assert FilesystemWriteTextFileInput.model_validate({"path": "a.txt", "append": True}).append is True


@pytest.mark.parametrize("message", [
    "Add the word pears on a new line.", "append pears to list.txt", "add pears to the end of the list", "add another line saying hi",
])
def test_a_request_to_add_turns_append_on_for_a_file_that_exists(tmp_path, message):
    target = tmp_path / "list.txt"
    target.write_text("apples", encoding="utf-8")
    write = {"operation": "write_text_file", "path": str(target), "content": "pears"}
    assert append_rule(_task(message), "filesystem.manage", write) == {**write, "append": True}


@pytest.mark.parametrize("message", ["Change it to say goodbye", "Create a file called list.txt with apples", "Write the cubes to cubes.txt"])
def test_other_requests_are_left_alone(tmp_path, message):
    target = tmp_path / "list.txt"
    target.write_text("apples", encoding="utf-8")
    write = {"operation": "write_text_file", "path": str(target), "content": "pears"}
    assert append_rule(_task(message), "filesystem.manage", write) == write


def test_a_file_that_does_not_exist_is_not_appended_to(tmp_path):
    write = {"operation": "write_text_file", "path": str(tmp_path / "missing.txt"), "content": "x"}
    assert append_rule(_task("add x on a new line"), "filesystem.manage", write) == write


def test_a_model_that_sends_the_whole_file_again_replaces_it_instead_of_doubling_it(tmp_path):
    target = tmp_path / "list.txt"
    target.write_text("apples", encoding="utf-8")
    write = {"operation": "write_text_file", "path": str(target), "content": "apples\npears"}
    assert append_rule(_task("Add the word pears on a new line."), "filesystem.manage", write)["overwrite"] is True


def test_append_wins_over_the_change_rule_when_the_request_has_both_words(tmp_path):
    target = tmp_path / "list.txt"
    target.write_text("apples", encoding="utf-8")
    task = _task("Update the list: add pears to the end")
    write = {"operation": "write_text_file", "path": str(target), "content": "pears"}
    enriched = overwrite_rule(task, "filesystem.manage", append_rule(task, "filesystem.manage", write))
    assert enriched["append"] is True and "overwrite" not in enriched
