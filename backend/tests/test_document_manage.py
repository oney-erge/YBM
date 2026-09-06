from __future__ import annotations

from pathlib import Path
from zipfile import ZipFile

import pytest

from agent_control.config import AppSettings, CapabilityPolicy
from agent_control.schemas import ArtifactType, Capability, RiskLevel, ToolCallRequest
from agent_control.tools.document_manage import DocumentManageAdapter
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
