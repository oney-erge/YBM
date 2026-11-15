from __future__ import annotations

from pathlib import Path
from zipfile import ZipFile

import pytest

from agent_control.config import AppSettings, CapabilityPolicy
from agent_control.schemas import ArtifactType, Capability, RiskLevel, ToolCallRequest
from agent_control.tools.document_manage import DocumentManageAdapter, _verify_document_manage
from agent_control.tools.registry import build_tool_registry
from helpers import make_repos


def test_registry_exposes_document_manage_when_filesystem_write_is_enabled(tmp_path) -> None:
    repos, _audit = make_repos(tmp_path)
    settings = AppSettings(
        _env_file=None,
        adapters={"computer_use": {"allowed_roots": [str(tmp_path)]}},
        capabilities={
            Capability.FILESYSTEM_WRITE: CapabilityPolicy(
                enabled=True,
                requires_approval=False,
                max_risk_level=RiskLevel.HIGH,
            )
        },
    )

    registry = build_tool_registry(
        settings,
        "http://127.0.0.1:8765",
        artifact_repository=repos.artifacts,
        task_repository=repos.tasks,
    )
    definitions = {definition.name: definition for definition in registry.definitions}

    assert definitions["document.manage"].enabled is True
    assert "summarize_pdf" in definitions["document.manage"].operations
    assert "create_presentation" in definitions["document.manage"].operations
    assert "document.manage" in registry.adapters
    # docs/THREAT_MODEL.md: reading a document's content is untrusted input;
    # writing a presentation is not inbound content.
    assert definitions["document.manage"].operation_content_trust == {
        "inspect_document": "untrusted_external",
        "extract_text": "untrusted_external",
        "summarize_pdf": "untrusted_external",
        "compare_documents": "untrusted_external",
    }

@pytest.mark.asyncio
async def test_document_manage_summarizes_pdf_text_and_records_artifact(tmp_path) -> None:
    repos, _audit = make_repos(tmp_path)
    task = repos.tasks.create("summarize pdf")
    pdf = tmp_path / "notes.pdf"
    pdf.write_text("Ferrets are curious animals. They sleep often. This document is about care.", encoding="utf-8")
    adapter = DocumentManageAdapter(repos.artifacts, allowed_roots=[str(tmp_path)])

    result = await adapter.execute(
        ToolCallRequest(
            task_id=task.id,
            tool_name="document.manage",
            capability=Capability.FILESYSTEM_WRITE,
            input={"operation": "summarize_pdf", "path": str(pdf)},
        )
    )

    artifacts = repos.artifacts.list_for_task(task.id)
    assert result.status.value == "succeeded"
    assert "Ferrets are curious animals" in result.output["summary"]
    assert result.output["artifact_ids"] == [artifacts[0].id]
    assert artifacts[0].type == ArtifactType.TEXT_LOG

@pytest.mark.asyncio
async def test_document_manage_creates_and_updates_powerpoint_artifacts(tmp_path) -> None:
    repos, _audit = make_repos(tmp_path)
    task = repos.tasks.create("create powerpoint")
    adapter = DocumentManageAdapter(repos.artifacts, allowed_roots=[str(tmp_path)])

    created = await adapter.execute(
        ToolCallRequest(
            task_id=task.id,
            tool_name="document.manage",
            capability=Capability.FILESYSTEM_WRITE,
            input={
                "operation": "create_presentation",
                "title": "Duck Launch",
                "content": "One\nTwo\nThree",
                "output_name": str(tmp_path / "duck.pptx"),
            },
        )
    )
    revised = await adapter.execute(
        ToolCallRequest(
            task_id=task.id,
            tool_name="document.manage",
            capability=Capability.FILESYSTEM_WRITE,
            input={
                "operation": "update_presentation",
                "path": created.output["path"],
                "instructions": "Make it more visual\nAdd a final recommendation",
                "output_name": str(tmp_path / "duck_revision.pptx"),
            },
        )
    )

    assert Path(created.output["path"]).exists()
    assert Path(revised.output["path"]).exists()
    with ZipFile(created.output["path"]) as pptx:
        assert "ppt/presentation.xml" in pptx.namelist()
    assert created.output["slide_count"] >= 1
    assert revised.output["artifact_id"] != created.output["artifact_id"]
    assert len(repos.artifacts.list_for_task(task.id)) == 2


@pytest.mark.asyncio
async def test_document_manage_compares_documents_into_verified_workbook_and_report(tmp_path) -> None:
    repos, _audit = make_repos(tmp_path)
    task = repos.tasks.create("compare proposals")
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("Cost is $10. Timeline is two weeks.", encoding="utf-8")
    second.write_text("Cost is $12. Timeline is one week.", encoding="utf-8")
    adapter = DocumentManageAdapter(repos.artifacts, allowed_roots=[str(tmp_path)])

    request = ToolCallRequest(
        task_id=task.id,
        tool_name="document.manage",
        capability=Capability.FILESYSTEM_WRITE,
        input={
            "operation": "compare_documents",
            "source_paths": [str(first), str(second)],
            "criteria": ["cost", "timeline"],
            "output_name": str(tmp_path / "comparison.xlsx"),
        },
    )
    result = await adapter.execute(request)

    assert result.status.value == "succeeded"
    assert Path(result.output["workbook_path"]).is_file()
    assert Path(result.output["report_path"]).is_file()
    with ZipFile(result.output["workbook_path"]) as workbook:
        assert "xl/worksheets/sheet1.xml" in workbook.namelist()
    verification = _verify_document_manage(request, result)
    assert verification is not None and verification.ok
    assert len(repos.artifacts.list_for_task(task.id)) == 2


# ---- _verify_document_manage (docs/ROADMAP.md "Finish the Proof": re-open
# the .pptx and count real slides, don't trust the adapter's own claim) ----

@pytest.mark.asyncio
async def test_verify_document_manage_confirms_a_created_presentation(tmp_path) -> None:
    repos, _audit = make_repos(tmp_path)
    task = repos.tasks.create("create powerpoint")
    adapter = DocumentManageAdapter(repos.artifacts, allowed_roots=[str(tmp_path)])
    request = ToolCallRequest(
        task_id=task.id,
        tool_name="document.manage",
        capability=Capability.FILESYSTEM_WRITE,
        input={"operation": "create_presentation", "title": "Duck Launch", "content": "One\nTwo\nThree"},
    )

    result = await adapter.execute(request)
    verification = _verify_document_manage(request, result)

    assert verification is not None
    assert verification.checked == 1
    assert verification.verified == 1
    assert verification.missing == []
    assert verification.ok is True


@pytest.mark.asyncio
async def test_verify_document_manage_flags_a_presentation_missing_after_the_fact(tmp_path) -> None:
    repos, _audit = make_repos(tmp_path)
    task = repos.tasks.create("create powerpoint")
    adapter = DocumentManageAdapter(repos.artifacts, allowed_roots=[str(tmp_path)])
    request = ToolCallRequest(
        task_id=task.id,
        tool_name="document.manage",
        capability=Capability.FILESYSTEM_WRITE,
        input={"operation": "create_presentation", "title": "Duck Launch", "content": "One"},
    )

    result = await adapter.execute(request)
    Path(result.output["path"]).unlink()
    verification = _verify_document_manage(request, result)

    assert verification is not None
    assert verification.verified == 0
    assert "presentation not found" in verification.missing[0]


@pytest.mark.asyncio
async def test_verify_document_manage_flags_a_slide_count_mismatch(tmp_path) -> None:
    """The adapter's own claim (`slide_count`) is re-derived from the
    file's actual ppt/slides/slideN.xml entries, not trusted back - the
    scenario this verifier exists to catch if the two ever disagree."""
    repos, _audit = make_repos(tmp_path)
    task = repos.tasks.create("create powerpoint")
    adapter = DocumentManageAdapter(repos.artifacts, allowed_roots=[str(tmp_path)])
    request = ToolCallRequest(
        task_id=task.id,
        tool_name="document.manage",
        capability=Capability.FILESYSTEM_WRITE,
        input={"operation": "create_presentation", "title": "Duck Launch", "content": "One\nTwo"},
    )

    result = await adapter.execute(request)
    # Simulate the adapter overclaiming: the file really has 2 slides.
    tampered = result.model_copy(update={"output": {**result.output, "slide_count": 5}})
    verification = _verify_document_manage(request, tampered)

    assert verification is not None
    assert verification.verified == 0
    assert "slide count mismatch" in verification.missing[0]
    assert "claimed 5" in verification.missing[0]


@pytest.mark.asyncio
async def test_verify_document_manage_returns_none_for_a_read_only_operation(tmp_path) -> None:
    """summarize_pdf's `path` names the source it read, not something it
    created - nothing to re-check."""
    repos, _audit = make_repos(tmp_path)
    task = repos.tasks.create("summarize pdf")
    pdf = tmp_path / "notes.pdf"
    pdf.write_text("Ferrets are curious.", encoding="utf-8")
    adapter = DocumentManageAdapter(repos.artifacts, allowed_roots=[str(tmp_path)])
    request = ToolCallRequest(
        task_id=task.id,
        tool_name="document.manage",
        capability=Capability.FILESYSTEM_WRITE,
        input={"operation": "summarize_pdf", "path": str(pdf)},
    )

    result = await adapter.execute(request)

    assert _verify_document_manage(request, result) is None


async def _make(tmp_path, **payload):
    repos, _audit = make_repos(tmp_path)
    task = repos.tasks.create("make a file")
    adapter = DocumentManageAdapter(repos.artifacts, allowed_roots=[str(tmp_path)])
    request = ToolCallRequest(task_id=task.id, tool_name="document.manage", capability=Capability.FILESYSTEM_WRITE, input=payload)
    return await adapter.execute(request), request, repos, task


@pytest.mark.asyncio
async def test_a_word_document_is_made_from_a_title_and_lines_and_reads_back(tmp_path) -> None:
    """Verdict 2 fin2_a3: "a packing checklist as a Word document" ended as a PowerPoint file, because the tool made only presentations."""
    from agent_control.tools.office_text import office_text

    content = "# Clothes\n- Rain jacket\n- Two pairs of socks\n[ ] Map & compass\nTake water for 3 days."
    result, request, repos, task = await _make(tmp_path, operation="create_document", title="Three-day hiking checklist", content=content)
    assert result.status.value == "succeeded" and result.output["path"].endswith(".docx") and result.output["paragraph_count"] == 6
    text = office_text(Path(result.output["path"]))
    assert "Three-day hiking checklist" in text and "Clothes" in text and "\u2022 Rain jacket" in text and "\u2022 Map & compass" in text and "Take water for 3 days." in text
    with ZipFile(result.output["path"]) as docx:
        assert {"[Content_Types].xml", "_rels/.rels", "word/document.xml"} <= set(docx.namelist())
        assert b"<w:b/>" in docx.read("word/document.xml")  # the heading is bold
    assert repos.artifacts.list_for_task(task.id)[0].type == ArtifactType.DOCUMENT
    verification = _verify_document_manage(request, result)
    assert verification.checked == 1 and verification.verified == 1
    missing = _verify_document_manage(request, result.model_copy(update={"output": {**result.output, "path": str(tmp_path / "nope.docx")}}))
    assert missing.verified == 0 and "not found" in missing.missing[0]


@pytest.mark.asyncio
async def test_a_spreadsheet_is_made_from_rows_with_numbers_as_numbers_and_a_total_row_summed_here(tmp_path) -> None:
    """Verdict 2 fin2_a1: four invoices into one spreadsheet with a total row. The 9B's own Python failed; the sum is now worked out by the tool."""
    from xml.etree import ElementTree

    rows = [["Vendor", "Date", "Amount"], ["Brightwater Plumbing", "2026-03-15", "120.50"], ["Delta Office Supplies", "2026-04-15", 89.9], ["Corner Electric", "2026-05-15", 310], ["Alder Print Shop", "2026-06-15", "45.60"]]
    result, request, repos, task = await _make(tmp_path, operation="create_spreadsheet", title="Invoices", rows=rows, total_row=True)
    assert result.status.value == "succeeded" and result.output["path"].endswith(".xlsx") and result.output["totals"] == {"Amount": 566.0}
    assert "The total row says Amount 566" in result.output["summary"]
    with ZipFile(result.output["path"]) as xlsx:
        sheet = ElementTree.fromstring(xlsx.read("xl/worksheets/sheet1.xml"))
    ns = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    sheet_rows = sheet.findall("x:sheetData/x:row", ns)
    assert len(sheet_rows) == 6  # the header, four invoices and the total
    total = {cell.get("r"): cell for cell in sheet_rows[-1].findall("x:c", ns)}
    assert total["C6"].find("x:f", ns).text == "SUM(C2:C5)" and total["C6"].find("x:v", ns).text == "566"
    amount = {cell.get("r"): cell for cell in sheet_rows[1].findall("x:c", ns)}
    assert amount["C2"].get("t") is None and amount["C2"].find("x:v", ns).text == "120.5"  # a number, not text
    assert amount["B2"].get("t") == "inlineStr"  # a date stays text
    assert _verify_document_manage(request, result).verified == 1


@pytest.mark.asyncio
async def test_no_total_for_a_column_that_is_not_all_numbers_and_the_inputs_are_checked(tmp_path) -> None:
    result, _request, _repos, _task = await _make(tmp_path, operation="create_spreadsheet", title="Mixed", rows=[["Name", "Qty", "Note"], ["a", 2, "ok"], ["b", "3", "later"]], total_row=True)
    assert result.output["totals"] == {"Qty": 5.0}  # Note has words in it, Name has words in it
    plain, *_ = await _make(tmp_path, operation="create_spreadsheet", title="Plain", rows=[["A"], ["x"]])
    assert plain.output["totals"] == {}
    with pytest.raises(ValueError, match="requires 'rows'"):
        from agent_control.tools.contracts import DocumentManageInput

        DocumentManageInput(operation="create_spreadsheet", title="x")
    with pytest.raises(ValueError, match="create_document requires"):
        DocumentManageInput(operation="create_document")


@pytest.mark.asyncio
async def test_a_file_goes_where_the_model_said_whichever_field_it_used_and_outside_the_roots_is_an_error_not_a_silent_move(tmp_path) -> None:
    """Live 11/15: asked to save a Word file in a named folder, the model wrote "output_path"; the tool ignored it and the answer claimed the folder."""
    wanted = tmp_path / "trip" / "list.docx"
    by_path, *_ = await _make(tmp_path, operation="create_document", title="Trip", lines=["- Tent"], output_path=str(wanted))
    assert by_path.status.value == "succeeded" and Path(by_path.output["path"]) == wanted.resolve() and wanted.exists()
    by_name, *_ = await _make(tmp_path, operation="create_document", title="Trip", lines=["- Tent"], output_name=str(tmp_path / "other.docx"))
    assert Path(by_name.output["path"]) == (tmp_path / "other.docx").resolve()
    elsewhere, *_ = await _make(tmp_path, operation="create_document", title="Trip", lines=["- Tent"], output_path=str(tmp_path.parent / "outside" / "x.docx"))
    assert elsewhere.status.value == "failed" and "outside configured document roots" in (elsewhere.error_message or "")


@pytest.mark.asyncio
async def test_the_text_of_a_word_document_can_be_a_list_of_lines_and_a_document_with_only_a_title_is_refused(tmp_path) -> None:
    """Live 11/15: the model wrote the checklist as "lines", the tool read only "content", and a Word file holding just its title was made and sent."""
    from agent_control.tools.office_text import office_text

    result, *_ = await _make(tmp_path, operation="create_document", title="Camping", lines=["# Gear", "- Tent", "- Stove", "Pack the night before."])
    assert result.status.value == "succeeded" and result.output["paragraph_count"] == 5
    text = office_text(Path(result.output["path"]))
    assert "Gear" in text and "\u2022 Tent" in text and "Pack the night before." in text
    empty, *_ = await _make(tmp_path, operation="create_document", title="Only a title")
    assert empty.status.value == "failed" and "needs the text of the document" in (empty.error_message or "")
