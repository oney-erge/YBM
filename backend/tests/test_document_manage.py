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
