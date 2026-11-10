"""document.manage reads Word, Excel and PowerPoint files and lists zip archives, and shows the model the text it extracted."""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

import pytest

from agent_control.schemas import Capability, ToolCallRequest
from agent_control.tools import office_text as office
from agent_control.tools.document_manage import DocumentManageAdapter, _extraction_summary
from helpers import make_repos

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "e2e"))
import gap_fixtures  # noqa: E402


@pytest.fixture()
def files(tmp_path):
    return gap_fixtures.prepare_gap_fixtures(tmp_path / "fixtures", gap_fixtures.remove_tree)


def test_a_docx_gives_its_paragraphs(files) -> None:
    text = office.office_text(Path(files["gap_docx"]))
    assert text.splitlines() == ["Project brief", f"Reference marker: {gap_fixtures.DOCX_MARKER}.", "The team ships on Friday."]


def test_an_xlsx_gives_every_cell_with_its_reference(files) -> None:
    text = office.office_text(Path(files["gap_xlsx"]))
    assert text.startswith("Sheet Orders:")
    assert "A1=item  B1=quantity  C1=unit_price" in text
    assert "A3=monitor  B3=2  C3=215.5" in text  # the arithmetic the model has to do is possible from this


def test_an_xlsx_with_shared_strings_resolves_them(tmp_path) -> None:
    path = tmp_path / "shared.xlsx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("xl/workbook.xml", '<workbook><sheets><sheet name="Q3 &amp; Q4" sheetId="1"/></sheets></workbook>')
        archive.writestr("xl/sharedStrings.xml", "<sst><si><t>region</t></si><si><t>north &amp; south</t></si></sst>")
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            '<worksheet><sheetData><row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1"><v>7</v></c></row>'
            '<row r="2"><c r="A2" t="s"><v>1</v></c></row></sheetData></worksheet>',
        )
    assert office.office_text(path) == "Sheet Q3 & Q4:\n  A1=region  B1=7\n  A2=north & south"


def test_a_pptx_gives_each_slides_text(tmp_path) -> None:
    path = tmp_path / "deck.pptx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("ppt/slides/slide2.xml", "<p><a:t>Second</a:t><a:t>point</a:t></p>")
        archive.writestr("ppt/slides/slide1.xml", "<p><a:t>Title</a:t></p>")
    assert office.office_text(path) == "Slide 1: Title\nSlide 2: Second | point"


def test_a_zip_is_listed_and_nothing_is_extracted(files, tmp_path) -> None:
    before = {p.name for p in Path(files["gap_zip"]).parent.iterdir()}
    text = office.office_text(Path(files["gap_zip"]))

    assert text.splitlines()[0] == f"Archive with {len(gap_fixtures.ZIP_MEMBERS)} entries (not extracted):"
    assert all(member in text for member in gap_fixtures.ZIP_MEMBERS)
    assert {p.name for p in Path(files["gap_zip"]).parent.iterdir()} == before


def test_other_suffixes_are_left_to_the_plain_reader(tmp_path) -> None:
    assert office.office_text(tmp_path / "notes.txt") is None


def test_a_corrupt_file_says_so(tmp_path) -> None:
    path = tmp_path / "broken.docx"
    path.write_bytes(b"this is not a zip")
    with pytest.raises(ValueError, match="not a valid .docx file"):
        office.office_text(path)


def test_a_huge_member_is_skipped_not_read(tmp_path) -> None:
    path = tmp_path / "bomb.docx"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", "<w:p><w:t>x</w:t></w:p>" * 600_000)  # about 14 MB, tiny once compressed
    assert zipfile.ZipFile(path).getinfo("word/document.xml").file_size > office.MAX_MEMBER_BYTES
    assert office.office_text(path) == ""


def test_an_xml_entity_bomb_is_not_expanded(tmp_path) -> None:
    path = tmp_path / "entities.docx"
    bomb = '<!DOCTYPE d [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">]><w:p><w:t>&b;</w:t></w:p>'
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", bomb)
    assert office.office_text(path) == "&b;"  # read as text, never expanded


def test_the_model_is_shown_the_text_not_only_its_length() -> None:
    text = "Reference marker: HARBOR-LAMP-318."
    assert _extraction_summary("brief.docx", text) == f"Extracted {len(text)} character(s) from brief.docx. Text:\n{text}"
    assert _extraction_summary("empty.txt", "  ") == "Extracted 2 character(s) from empty.txt."


def test_a_long_text_is_shown_in_part_and_says_so() -> None:
    summary = _extraction_summary("log.txt", "x" * 5000)
    preview = summary.split("Text:\n", 1)[1].split("\n[...", 1)[0]
    assert preview == "x" * 3000 and "2000 more character(s)" in summary


@pytest.mark.asyncio
async def test_extract_text_on_a_docx_through_the_tool_returns_the_marker(files, tmp_path) -> None:
    repos, _audit = make_repos(tmp_path)
    task = repos.tasks.create("read the brief")
    adapter = DocumentManageAdapter(repos.artifacts, allowed_roots=[str(Path(files["gap_docx"]).parent)])

    result = await adapter.execute(ToolCallRequest(
        task_id=task.id, tool_name="document.manage", capability=Capability.FILESYSTEM_WRITE,
        input={"operation": "extract_text", "path": files["gap_docx"]},
    ))

    assert result.status.value == "succeeded"
    assert gap_fixtures.DOCX_MARKER in result.output["summary"] and gap_fixtures.DOCX_MARKER in result.output["text"]
