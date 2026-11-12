from __future__ import annotations

import pytest

from agent_control.schemas import Capability, ToolCallRequest
from agent_control.tools.filesystem_manage import FilesystemManageAdapter


@pytest.mark.asyncio
async def test_filesystem_find_by_description_prefers_matching_names(tmp_path) -> None:
    (tmp_path / "resume_final.pdf").write_text("resume", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("notes", encoding="utf-8")
    adapter = FilesystemManageAdapter([str(tmp_path)])

    result = await adapter.execute(
        ToolCallRequest(
            task_id="task_files",
            tool_name="filesystem.manage",
            capability=Capability.FILESYSTEM_WRITE,
            input={"operation": "find_by_description", "root": str(tmp_path), "description": "resume pdf"},
        )
    )

    assert result.status.value == "succeeded"
    assert result.output["entries"][0]["relative_path"] == "resume_final.pdf"


@pytest.mark.asyncio
async def test_filesystem_collect_folder_snapshot_matches_inspection(tmp_path) -> None:
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    adapter = FilesystemManageAdapter([str(tmp_path)])

    result = await adapter.execute(
        ToolCallRequest(
            task_id="task_files",
            tool_name="filesystem.manage",
            capability=Capability.FILESYSTEM_WRITE,
            input={"operation": "collect_folder_snapshot", "root": str(tmp_path)},
        )
    )

    assert result.status.value == "succeeded"
    assert result.output["entries"][0]["relative_path"] == "a.txt"


def test_a_locked_folder_inside_a_search_is_skipped_not_fatal(tmp_path, monkeypatch):
    from pathlib import Path

    from agent_control.tools.filesystem_manage import _walk_limited

    (tmp_path / "locked").mkdir()
    (tmp_path / "open").mkdir()
    (tmp_path / "open" / "needle.txt").write_text("x")
    real = Path.iterdir

    def iterdir(self):
        if self.name == "locked":
            raise PermissionError(13, "Access is denied", str(self))
        return real(self)

    monkeypatch.setattr(Path, "iterdir", iterdir)
    assert [p.name for p in _walk_limited(tmp_path, max_depth=4)] == [tmp_path.name, "locked", "open", "needle.txt"]


def test_the_folder_a_search_was_pointed_at_still_fails_loudly_when_it_cannot_be_opened(tmp_path, monkeypatch):
    from pathlib import Path

    from agent_control.tools.filesystem_manage import _walk_limited

    def iterdir(self):
        raise PermissionError(13, "Access is denied", str(self))

    monkeypatch.setattr(Path, "iterdir", iterdir)
    with pytest.raises(PermissionError):
        list(_walk_limited(tmp_path, max_depth=2))
