from __future__ import annotations

import json
from pathlib import Path
from typing import TypeVar

import pytest
from pydantic import BaseModel

from agent_control.schemas import Capability, ToolCallRequest, ToolCallResult, ToolResultStatus
from agent_control.tools.filesystem_manage import FilesystemManageAdapter, _verify_apply_manifest

T = TypeVar("T", bound=BaseModel)


class FakeVisionProvider:
    async def generate_text(self, system_prompt: str, user_prompt: str) -> str:
        return ""

    async def generate_multimodal_text(self, system_prompt: str, user_prompt: str, image_paths: list[str]) -> str:
        return "Visible text: OCR SAMPLE. The image looks like a small document screenshot."

    async def generate_structured(self, system_prompt: str, user_prompt: str, output_model: type[T], **_ignored_kwargs) -> T:
        raise AssertionError("structured generation is not used")


def _request(root: Path, operation: str, **payload):
    return ToolCallRequest(
        task_id="task_fs",
        tool_name="filesystem.manage",
        capability=Capability.FILESYSTEM_WRITE,
        input={"operation": operation, "root": str(root), **payload},
    )


@pytest.mark.asyncio
async def test_filesystem_manage_inspect_search_plan_and_apply(tmp_path) -> None:
    root = tmp_path / "downloads"
    root.mkdir()
    note = root / "notes.txt"
    image = root / "photo.png"
    note.write_text("resume draft", encoding="utf-8")
    image.write_bytes(b"fake")
    adapter = FilesystemManageAdapter([str(tmp_path)])

    inspect_result = await adapter.execute(_request(root, "inspect_folder"))
    search_result = await adapter.execute(_request(root, "search", query="resume", include_content=True))
    plan_result = await adapter.execute(_request(root, "organize_plan", strategy="by_type"))
    apply_result = await adapter.execute(
        _request(root, "apply_manifest", manifest=plan_result.output["manifest"], dry_run=False)
    )

    assert inspect_result.status.value == "succeeded"
    assert len(inspect_result.output["entries"]) == 2
    assert search_result.output["entries"][0]["path"] == str(note.resolve())
    assert len(plan_result.output["manifest"]) == 2
    assert apply_result.status.value == "succeeded"
    assert "Moved 2 file(s)" in apply_result.output["summary"]
    assert "changed 2 path(s)" in apply_result.output["summary"]
    assert sorted(Path(path).parent.name for path in apply_result.output["changed_paths"]) == ["documents", "images"]
    assert not note.exists()
    assert (root / "documents" / "notes.txt").exists()


@pytest.mark.asyncio
async def test_apply_manifest_resolves_relative_paths_from_requested_root(tmp_path) -> None:
    root = tmp_path / "documents"
    root.mkdir()
    source = root / "budget.csv"
    source.write_text("name,amount\nsample,10\n", encoding="utf-8")
    adapter = FilesystemManageAdapter([str(tmp_path)])

    result = await adapter.execute(
        _request(
            root,
            "apply_manifest",
            manifest=[
                {
                    "operation": "move",
                    "source": "budget.csv",
                    "destination": "spreadsheets/budget.csv",
                }
            ],
        )
    )

    assert result.status.value == "succeeded"
    assert not source.exists()
    assert (root / "spreadsheets" / "budget.csv").exists()


@pytest.mark.asyncio
async def test_organize_plan_terminal_output_carries_manifest_for_next_operator_step(tmp_path) -> None:
    root = tmp_path / "documents"
    root.mkdir()
    (root / "notes.txt").write_text("hello", encoding="utf-8")
    adapter = FilesystemManageAdapter([str(tmp_path)])

    result = await adapter.execute(_request(root, "organize_plan"))

    terminal_text = result.output["terminal_output"][0]["content"]
    assert "Manifest for the next apply_manifest call" in terminal_text
    emitted_manifest = json.loads(terminal_text.rsplit("\n", 1)[-1])
    assert emitted_manifest[0]["source"] == str((root / "notes.txt").resolve())
    assert emitted_manifest[0]["destination"] == str((root / "documents" / "notes.txt").resolve())


@pytest.mark.asyncio
async def test_filesystem_manage_inspection_order_is_deterministic(tmp_path) -> None:
    root = tmp_path / "downloads"
    root.mkdir()
    for name in ("resume.txt", "invoice_2026.txt", "notes.txt"):
        (root / name).write_text(name, encoding="utf-8")
    adapter = FilesystemManageAdapter([str(tmp_path)])

    result = await adapter.execute(_request(root, "inspect_folder"))

    assert [
        entry["relative_path"] for entry in result.output["entries"]
    ] == ["invoice_2026.txt", "notes.txt", "resume.txt"]


@pytest.mark.asyncio
async def test_filesystem_manage_rename_plan_and_apply(tmp_path) -> None:
    root = tmp_path / "docs"
    root.mkdir()
    source = root / "untitled.txt"
    source.write_text("Quarterly invoice notes for Alpha project", encoding="utf-8")
    adapter = FilesystemManageAdapter([str(tmp_path)])

    plan_result = await adapter.execute(_request(root, "rename_plan"))
    apply_result = await adapter.execute(
        _request(root, "apply_manifest", manifest=plan_result.output["manifest"], dry_run=False)
    )

    assert plan_result.status.value == "succeeded"
    assert plan_result.output["rename_manifest"][0]["before"] == "untitled.txt"
    assert plan_result.output["rename_manifest"][0]["after"].endswith(".txt")
    assert apply_result.status.value == "succeeded"
    assert "Renamed 1 file(s)" in apply_result.output["summary"]
    assert apply_result.output["rename_manifest"][0]["before"] == "untitled.txt"
    assert not source.exists()


@pytest.mark.asyncio
async def test_filesystem_manage_write_text_file_inside_allowed_root(tmp_path) -> None:
    root = tmp_path / "docs"
    root.mkdir()
    adapter = FilesystemManageAdapter([str(tmp_path)])

    result = await adapter.execute(
        ToolCallRequest(
            task_id="task_fs",
            tool_name="filesystem.manage",
            capability=Capability.FILESYSTEM_WRITE,
            input={
                "operation": "write_text_file",
                "path": str(root / "e2e-output.txt"),
                "content": "hello\n",
            },
        )
    )

    assert result.status.value == "succeeded"
    assert (root / "e2e-output.txt").read_text(encoding="utf-8") == "hello\n"
    assert result.output["path"] == str((root / "e2e-output.txt").resolve())
    assert result.output["changed_paths"] == [str((root / "e2e-output.txt").resolve())]


@pytest.mark.asyncio
async def test_filesystem_manage_search_by_name_includes_readable_content_preview(tmp_path) -> None:
    root = tmp_path / "desktop"
    root.mkdir()
    report = root / "resume-notes.txt"
    report.write_text("Oney resume notes include Python, orchestration, and local automation.", encoding="utf-8")
    adapter = FilesystemManageAdapter([str(tmp_path)])

    result = await adapter.execute(_request(root, "search", query="resume", include_content=True))

    assert result.status.value == "succeeded"
    assert result.output["entries"][0]["relative_path"] == "resume-notes.txt"
    assert "Python, orchestration" in result.output["entries"][0]["content_preview"]
    assert "resume-notes.txt" in result.output["terminal_output"][0]["content"]


@pytest.mark.asyncio
async def test_filesystem_manage_search_supports_natural_wildcard_filename_queries(tmp_path) -> None:
    root = tmp_path / "notes"
    root.mkdir()
    renamed = root / "career-master-current.txt"
    renamed.write_text("marker: RECOVERED", encoding="utf-8")
    adapter = FilesystemManageAdapter([str(tmp_path)])

    result = await adapter.execute(_request(root, "search", query="career*"))
    all_result = await adapter.execute(_request(root, "search", query="*"))

    assert [entry["path"] for entry in result.output["entries"]] == [str(renamed.resolve())]
    assert [entry["path"] for entry in all_result.output["entries"]] == [str(renamed.resolve())]


@pytest.mark.asyncio
async def test_filesystem_manage_read_file_returns_contents(tmp_path) -> None:
    root = tmp_path / "desktop"
    root.mkdir()
    report = root / "readme.txt"
    report.write_text("This file explains the desktop automation test fixture.", encoding="utf-8")
    adapter = FilesystemManageAdapter([str(tmp_path)])

    result = await adapter.execute(
        ToolCallRequest(
            task_id="task_fs",
            tool_name="filesystem.manage",
            capability=Capability.FILESYSTEM_WRITE,
            input={"operation": "read_file", "path": str(report), "max_chars": 1000},
        )
    )

    assert result.status.value == "succeeded"
    assert "desktop automation test fixture" in result.output["text"]
    assert "Content:" in result.output["terminal_output"][0]["content"]


@pytest.mark.asyncio
async def test_filesystem_manage_describe_folder_extracts_file_content_and_image_ocr(tmp_path) -> None:
    root = tmp_path / "mixed"
    root.mkdir()
    (root / "project_notes.txt").write_text("Alpha project notes about desktop automation.", encoding="utf-8")
    (root / "budget.csv").write_text("name,amount\nhosting,25\n", encoding="utf-8")
    image = root / "ocr-note.png"
    try:
        from PIL import Image, ImageDraw

        canvas = Image.new("RGB", (240, 80), color="white")
        draw = ImageDraw.Draw(canvas)
        draw.text((12, 28), "OCR SAMPLE", fill="black")
        canvas.save(image)
    except Exception:
        image.write_bytes(b"not a real image")
    adapter = FilesystemManageAdapter([str(tmp_path)], provider=FakeVisionProvider())

    result = await adapter.execute(_request(root, "describe_folder", include_ocr=True))

    assert result.status.value == "succeeded"
    descriptions = {Path(item["path"]).name: item for item in result.output["file_descriptions"]}
    assert "desktop automation" in descriptions["project_notes.txt"]["content_preview"]
    assert "hosting" in descriptions["budget.csv"]["content_preview"]
    assert descriptions["ocr-note.png"]["ocr_status"] == "completed"
    assert "OCR SAMPLE" in descriptions["ocr-note.png"]["ocr_text"]
    assert "Described 3 file(s)" in result.output["summary"]


@pytest.mark.asyncio
async def test_filesystem_manage_rejects_path_escape(tmp_path) -> None:
    root = tmp_path / "allowed"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    adapter = FilesystemManageAdapter([str(root)])

    result = await adapter.execute(_request(outside, "inspect_folder"))

    assert result.status.value == "failed"
    assert "outside allowed roots" in (result.error_message or "")


@pytest.mark.asyncio
async def test_filesystem_manage_resolves_desktop_alias_prefix(monkeypatch, tmp_path) -> None:
    fake_home = tmp_path / "home"
    desktop = fake_home / "Desktop"
    desktop.mkdir(parents=True)
    (desktop / "invoice.txt").write_text("invoice content", encoding="utf-8")
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    adapter = FilesystemManageAdapter([str(fake_home)])

    result = await adapter.execute(
        ToolCallRequest(
            task_id="task_fs",
            tool_name="filesystem.manage",
            capability=Capability.FILESYSTEM_WRITE,
            input={"operation": "search", "root": "desktop", "query": "invoice", "include_content": True},
        )
    )

    assert result.status.value == "succeeded"
    assert result.output["entries"][0]["path"] == str((desktop / "invoice.txt").resolve())


# ---- _verify_apply_manifest (docs/ROADMAP.md "Proof": re-check the disk,
# don't trust the adapter's own claim) -------------------------------------

@pytest.mark.asyncio
async def test_verify_apply_manifest_confirms_every_moved_file(tmp_path) -> None:
    root = tmp_path / "downloads"
    root.mkdir()
    (root / "a.txt").write_text("a", encoding="utf-8")
    (root / "b.txt").write_text("b", encoding="utf-8")
    adapter = FilesystemManageAdapter([str(tmp_path)])
    request = _request(
        root,
        "apply_manifest",
        manifest=[
            {"operation": "move", "source": "a.txt", "destination": "docs/a.txt"},
            {"operation": "move", "source": "b.txt", "destination": "docs/b.txt"},
        ],
    )

    result = await adapter.execute(request)
    verification = _verify_apply_manifest(request, result)

    assert verification is not None
    assert verification.checked == 2
    assert verification.verified == 2
    assert verification.missing == []
    assert verification.ok is True


async def _apply_two_file_move(tmp_path: Path) -> tuple[ToolCallRequest, ToolCallResult, Path, Path]:
    root = tmp_path / "downloads"
    root.mkdir()
    (root / "a.txt").write_text("a", encoding="utf-8")
    adapter = FilesystemManageAdapter([str(tmp_path)])
    request = _request(root, "apply_manifest", manifest=[{"operation": "move", "source": "a.txt", "destination": "docs/a.txt"}])
    result = await adapter.execute(request)
    return request, result, root / "a.txt", root / "docs" / "a.txt"


@pytest.mark.asyncio
async def test_verify_apply_manifest_flags_a_destination_missing_after_the_fact(tmp_path) -> None:
    """The scenario the plan's "0 missing" line is meant to catch: the
    adapter reported success, but the destination isn't actually there
    (here, simulated by removing it right after) - verification must not
    take the adapter's own claim at face value.
    """
    request, result, _source, destination = await _apply_two_file_move(tmp_path)
    assert destination.exists()
    destination.unlink()

    verification = _verify_apply_manifest(request, result)

    assert verification is not None
    assert verification.checked == 1
    assert verification.verified == 0
    assert verification.missing == [f"destination not found: {destination}"]
    assert verification.ok is False


@pytest.mark.asyncio
async def test_verify_apply_manifest_flags_a_source_left_behind_after_move(tmp_path) -> None:
    request, result, source, _destination = await _apply_two_file_move(tmp_path)
    # The move already removed the source; recreate it to simulate something
    # else re-populating that path after the fact.
    source.write_text("still here", encoding="utf-8")

    verification = _verify_apply_manifest(request, result)

    assert verification is not None
    assert verification.verified == 0
    assert any("source still present" in item for item in verification.missing)


@pytest.mark.asyncio
async def test_verify_apply_manifest_returns_none_for_a_dry_run(tmp_path) -> None:
    root = tmp_path / "downloads"
    root.mkdir()
    (root / "a.txt").write_text("a", encoding="utf-8")
    adapter = FilesystemManageAdapter([str(tmp_path)])
    request = _request(
        root, "apply_manifest", manifest=[{"operation": "move", "source": "a.txt", "destination": "docs/a.txt"}], dry_run=True,
    )

    result = await adapter.execute(request)

    assert _verify_apply_manifest(request, result) is None
    # Confirms this is genuinely a dry run and not a false pass: nothing moved.
    assert (root / "a.txt").exists()


def test_verify_apply_manifest_returns_none_when_the_output_has_no_manifest() -> None:
    request = ToolCallRequest(
        task_id="task_fs",
        tool_name="filesystem.manage",
        capability=Capability.FILESYSTEM_WRITE,
        input={"operation": "apply_manifest", "root": "/tmp", "manifest": [{"operation": "move", "source": "a", "destination": "b"}]},
    )
    result = ToolCallResult(request_id=request.id, status=ToolResultStatus.SUCCEEDED, output={})

    assert _verify_apply_manifest(request, result) is None


# ---- read_file on files that are not plain text -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_file_gives_the_text_of_a_word_file_not_its_bytes(tmp_path) -> None:
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "e2e"))
    import gap_fixtures

    docx = tmp_path / "brief.docx"
    gap_fixtures.write_docx(docx, ["Project brief", f"Reference marker: {gap_fixtures.DOCX_MARKER}."])
    adapter = FilesystemManageAdapter([str(tmp_path)])
    result = await adapter.execute(_request(tmp_path, "read_file", path=str(docx)))

    assert result.status.value == "succeeded"
    assert gap_fixtures.DOCX_MARKER in result.output["text"] and not result.output["text"].startswith("PK")


@pytest.mark.asyncio
async def test_read_file_on_a_binary_file_says_there_is_nothing_to_read(tmp_path) -> None:
    blob = tmp_path / "photo.png"
    blob.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + bytes(range(256)))
    adapter = FilesystemManageAdapter([str(tmp_path)])
    result = await adapter.execute(_request(tmp_path, "read_file", path=str(blob)))

    assert result.status.value == "succeeded"
    assert result.output["text"] == "" and "No readable text" in result.output["summary"]



# ---- search accepts the glob patterns a model writes -----------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("query", [
    "budget-2026.csv", "**/budget-2026.csv", "**/budget*.csv", "**/*.csv", "*.CSV", "finance/archive/*.csv",
    "finance\\archive\\budget*.csv",
])
async def test_a_search_pattern_with_a_folder_prefix_finds_the_file(tmp_path, query) -> None:
    nested = tmp_path / "finance" / "archive"
    nested.mkdir(parents=True)
    (nested / "budget-2026.csv").write_text("month,income\n", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("not a csv", encoding="utf-8")
    adapter = FilesystemManageAdapter([str(tmp_path)])

    result = await adapter.execute(_request(tmp_path, "search", query=query))

    assert [Path(entry["path"]).name for entry in result.output["entries"]] == ["budget-2026.csv"]


@pytest.mark.asyncio
async def test_a_pattern_that_matches_nothing_still_finds_nothing(tmp_path) -> None:
    (tmp_path / "notes.txt").write_text("x", encoding="utf-8")
    adapter = FilesystemManageAdapter([str(tmp_path)])

    result = await adapter.execute(_request(tmp_path, "search", query="**/*.csv"))

    assert result.output["entries"] == []


# ---- a word that no file name contains is looked for in the files' text ----------------------------------------------


@pytest.mark.asyncio
async def test_a_search_by_word_looks_in_file_text_when_no_name_matches(tmp_path) -> None:
    (tmp_path / "alpha").mkdir()
    (tmp_path / "beta").mkdir()
    (tmp_path / "alpha" / "plan.txt").write_text("Alpha: ship the parser.\n", encoding="utf-8")
    (tmp_path / "beta" / "kitchen-plan.txt").write_text("Beta: repaint the kitchen, tangerine walls.\n", encoding="utf-8")
    adapter = FilesystemManageAdapter([str(tmp_path)])

    result = await adapter.execute(_request(tmp_path, "search", query="tangerine"))

    assert [Path(entry["path"]).name for entry in result.output["entries"]] == ["kitchen-plan.txt"]
    assert "found in the text" in result.output["summary"] and "tangerine" in result.output["entries"][0]["content_preview"]


@pytest.mark.asyncio
async def test_a_name_match_does_not_trigger_the_text_search(tmp_path) -> None:
    (tmp_path / "tangerine-notes.txt").write_text("nothing about fruit", encoding="utf-8")
    (tmp_path / "other.txt").write_text("a tangerine here", encoding="utf-8")
    adapter = FilesystemManageAdapter([str(tmp_path)])

    result = await adapter.execute(_request(tmp_path, "search", query="tangerine"))

    assert [Path(entry["path"]).name for entry in result.output["entries"]] == ["tangerine-notes.txt"]
    assert "found in the text" not in result.output["summary"]


@pytest.mark.asyncio
async def test_a_large_file_is_not_read_by_the_text_search(tmp_path) -> None:
    (tmp_path / "huge.txt").write_text("tangerine " + "x" * 1_100_000, encoding="utf-8")
    adapter = FilesystemManageAdapter([str(tmp_path)])

    result = await adapter.execute(_request(tmp_path, "search", query="tangerine"))

    assert result.output["entries"] == []



# ---- modified times, shown only when the request is about which file is newest or oldest -----------------------------


@pytest.mark.asyncio
async def test_a_listing_shows_modified_times_only_when_asked(tmp_path) -> None:
    import os

    old, new = tmp_path / "old.md", tmp_path / "latest.md"
    old.write_text("a", encoding="utf-8")
    new.write_text("b", encoding="utf-8")
    os.utime(old, (1_577_836_800, 1_577_836_800))  # 2020-01-01
    os.utime(new, (1_767_225_600, 1_767_225_600))  # 2026-01-01
    adapter = FilesystemManageAdapter([str(tmp_path)])

    plain = await adapter.execute(_request(tmp_path, "inspect_folder"))
    dated = await adapter.execute(_request(tmp_path, "inspect_folder", with_dates=True))

    plain_text = plain.output["terminal_output"][0]["content"]
    dated_text = dated.output["terminal_output"][0]["content"]
    assert ", modified " not in plain_text and "- [file] latest.md (1 bytes)" in plain_text  # the text every recording was made from
    assert "- [file] old.md (1 bytes, modified 2020-01-01 00:00 UTC)" in dated_text
    assert "- [file] latest.md (1 bytes, modified 2026-01-01 00:00 UTC)" in dated_text


def test_only_a_recency_request_turns_the_dates_on() -> None:
    from agent_control.orchestration.worker import _filesystem_listing_input_with_dates_intent as enrich
    from agent_control.schemas import TaskRecord

    listing = {"operation": "inspect_folder", "root": "C:/x"}
    for text in ("Which file in the folder was modified most recently?", "What is the newest file in Downloads?",
                 "when was budget.csv last modified"):
        task = TaskRecord(objective=text, metadata={"original_message_text": text})
        assert enrich(task, "filesystem.manage", listing) == {**listing, "with_dates": True}, text
    for text in ("List the files in the folder", "Read notes.txt and tell me what it says"):
        task = TaskRecord(objective=text, metadata={"original_message_text": text})
        assert enrich(task, "filesystem.manage", listing) == listing, text
    other = {"operation": "read_file", "path": "C:/x/a.txt"}
    task = TaskRecord(objective="the newest file", metadata={})
    assert enrich(task, "filesystem.manage", other) == other
    assert enrich(task, "browser.open", listing) == listing
