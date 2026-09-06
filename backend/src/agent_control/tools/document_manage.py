from __future__ import annotations

import logging
from pathlib import Path
import re
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile

from agent_control.config import AppSettings
from agent_control.llm.providers import LLMProvider
from agent_control.schemas import (
    Artifact,
    ArtifactType,
    Capability,
    RiskLevel,
    ToolCallRequest,
    ToolCallResult,
    ToolResultStatus,
)
from agent_control.storage.repositories import ArtifactRepository
from agent_control.tools.contracts import DocumentManageInput, DocumentManageOutput
from agent_control.tools.spec import (
    UNTRUSTED_EXTERNAL,
    Adapters,
    Definitions,
    RegistryDeps,
    ToolDefinition,
    capability_enabled,
    failed_result,
    same_output_schema,
)


logger = logging.getLogger(__name__)


class DocumentManageAdapter:
    def __init__(
        self,
        artifacts: ArtifactRepository,
        *,
        provider: LLMProvider | None = None,
        allowed_roots: list[str] | None = None,
    ) -> None:
        self.artifacts = artifacts
        self.provider = provider
        self.allowed_roots = [Path(root).expanduser().resolve() for root in (allowed_roots or [])]

    async def execute(self, request: ToolCallRequest) -> ToolCallResult:
        operation = str(request.input.get("operation") or "inspect_document")
        try:
            if operation == "inspect_document":
                output = self._inspect_document(request)
            elif operation == "extract_text":
                output = self._extract_text_output(request)
            elif operation == "summarize_pdf":
                output = self._summarize_pdf(request)
            elif operation == "create_presentation":
                output = self._create_presentation(request)
            elif operation == "update_presentation":
                output = self._update_presentation(request)
            else:
                return failed_result(request, f"unsupported document operation: {operation}")
        except Exception as exc:
            return failed_result(request, f"document operation failed: {exc}")

        output["operation"] = operation
        output["terminal_output"] = [_terminal_output(operation, output)]
        return ToolCallResult(request_id=request.id, status=ToolResultStatus.SUCCEEDED, output=output)

    def _inspect_document(self, request: ToolCallRequest) -> dict[str, Any]:
        path = self._resolve_input_path(request)
        stat = path.stat()
        return {
            "path": str(path),
            "metadata": {
                "name": path.name,
                "suffix": path.suffix.lower(),
                "size_bytes": stat.st_size,
            },
            "summary": f"{path.name} is a {path.suffix.lower() or 'file'} document with {stat.st_size} byte(s).",
        }

    def _extract_text_output(self, request: ToolCallRequest) -> dict[str, Any]:
        path = self._resolve_input_path(request)
        text = _extract_text(path)
        return {
            "path": str(path),
            "text": text,
            "summary": f"Extracted {len(text)} character(s) from {path.name}.",
        }

    def _summarize_pdf(self, request: ToolCallRequest) -> dict[str, Any]:
        path = self._resolve_input_path(request)
        if path.suffix.lower() != ".pdf":
            raise ValueError(f"summarize_pdf requires a PDF file: {path}")
        text = _extract_text(path)
        summary = _simple_summary(text, fallback=f"{path.name} appears to be a PDF, but no extractable text was found.")
        artifact = self.artifacts.create(
            Artifact(
                task_id=request.task_id,
                type=ArtifactType.TEXT_LOG,
                uri=None,
                content_preview=summary,
                metadata={"source": "document.manage", "document_path": str(path), "operation": "summarize_pdf"},
            )
        )
        return {
            "path": str(path),
            "artifact_id": artifact.id,
            "artifact_ids": [artifact.id],
            "text": text[:4000],
            "summary": summary,
            "metadata": {"source_path": str(path), "text_chars": len(text)},
        }

    def _create_presentation(self, request: ToolCallRequest) -> dict[str, Any]:
        title = str(request.input.get("title") or request.input.get("objective") or "Presentation").strip()
        content = str(request.input.get("content") or request.input.get("instructions") or request.input.get("objective") or "").strip()
        output_path = self._output_path(request, request.input.get("output_name"), default_stem=_safe_stem(title), suffix=".pptx")
        slides = _slides_from_content(title, content)
        _write_minimal_pptx(output_path, slides)
        artifact = self.artifacts.create(
            Artifact(
                task_id=request.task_id,
                type=ArtifactType.DOCUMENT,
                uri=str(output_path),
                content_preview=f"PowerPoint presentation: {title}",
                metadata={"source": "document.manage", "operation": "create_presentation", "slide_count": len(slides)},
            )
        )
        return {
            "path": str(output_path),
            "artifact_id": artifact.id,
            "artifact_ids": [artifact.id],
            "slide_count": len(slides),
            "summary": f"Created PowerPoint presentation with {len(slides)} slide(s): {output_path}",
        }

    def _update_presentation(self, request: ToolCallRequest) -> dict[str, Any]:
        source = self._resolve_input_path(request)
        instructions = str(request.input.get("instructions") or request.input.get("content") or "Updated presentation").strip()
        title = str(request.input.get("title") or f"Updated {source.stem}").strip()
        output_path = self._output_path(request, request.input.get("output_name"), default_stem=f"{source.stem}_revision", suffix=".pptx")
        slides = _slides_from_content(title, instructions)
        _write_minimal_pptx(output_path, slides)
        artifact = self.artifacts.create(
            Artifact(
                task_id=request.task_id,
                type=ArtifactType.DOCUMENT,
                uri=str(output_path),
                content_preview=f"Revised PowerPoint presentation: {title}",
                metadata={
                    "source": "document.manage",
                    "operation": "update_presentation",
                    "source_path": str(source),
                    "slide_count": len(slides),
                },
            )
        )
        return {
            "path": str(output_path),
            "artifact_id": artifact.id,
            "artifact_ids": [artifact.id],
            "slide_count": len(slides),
            "summary": f"Created revised PowerPoint with {len(slides)} slide(s): {output_path}",
            "metadata": {"source_path": str(source)},
        }

    def _resolve_input_path(self, request: ToolCallRequest) -> Path:
        artifact_id = request.input.get("artifact_id")
        if artifact_id:
            artifact = self.artifacts.get(str(artifact_id))
            if artifact is None or not artifact.uri:
                raise ValueError(f"artifact not found or has no file: {artifact_id}")
            return self._safe_path(artifact.uri)
        path = request.input.get("path")
        if not path:
            raise ValueError("path or artifact_id is required")
        resolved = self._safe_path(str(path))
        if not resolved.exists() or not resolved.is_file():
            raise ValueError(f"document file does not exist: {resolved}")
        return resolved

    def _output_path(self, request: ToolCallRequest, requested: Any, *, default_stem: str, suffix: str) -> Path:
        if requested:
            path = self._safe_path(str(requested))
            if path.suffix.lower() != suffix:
                path = path.with_suffix(suffix)
            path.parent.mkdir(parents=True, exist_ok=True)
            return _dedupe(path)
        root = next((item for item in self.allowed_roots if ".agent_control" in str(item)), None) or self.allowed_roots[0]
        path = root / f"task_{request.task_id}" / f"{default_stem}{suffix}"
        path.parent.mkdir(parents=True, exist_ok=True)
        return _dedupe(path)

    def _safe_path(self, value: str) -> Path:
        path = Path(value).expanduser().resolve()
        if self.allowed_roots and not any(root == path or root in path.parents for root in self.allowed_roots):
            raise ValueError(f"path is outside configured document roots: {path}")
        return path


def _extract_text(path: Path) -> str:
    if path.suffix.lower() == ".pdf":
        try:
            from pypdf import PdfReader

            reader = PdfReader(str(path))
            return "\n\n".join((page.extract_text() or "").strip() for page in reader.pages).strip()
        except Exception:
            # The fallback decodes a binary PDF as if it were text, which
            # produces near-garbage for a real PDF - this is a real quality
            # degradation on the "summarize this PDF" path, not routine noise.
            logger.warning("pypdf extraction failed for %s; falling back to raw bytes decode", path, exc_info=True)
            return path.read_bytes().decode("utf-8", errors="ignore")
    return path.read_text(encoding="utf-8", errors="ignore")


def _simple_summary(text: str, *, fallback: str) -> str:
    cleaned = re.sub(r"\s+", " ", text).strip()
    if not cleaned:
        return fallback
    sentences = re.split(r"(?<=[.!?])\s+", cleaned)
    selected = " ".join(sentences[:4]).strip()
    return selected[:1200] if selected else cleaned[:1200]


def _slides_from_content(title: str, content: str) -> list[tuple[str, list[str]]]:
    bullets = [line.strip(" -*\t") for line in content.splitlines() if line.strip()]
    if not bullets and content:
        bullets = [item.strip() for item in re.split(r"(?<=[.!?])\s+", content) if item.strip()]
    bullets = bullets[:12] or ["Draft content"]
    slides = [(title, bullets[:4])]
    for index in range(4, len(bullets), 5):
        slides.append((f"{title} - Part {len(slides) + 1}", bullets[index : index + 5]))
    return slides


def _write_minimal_pptx(path: Path, slides: list[tuple[str, list[str]]]) -> None:
    rels = "\n".join(
        f'<Relationship Id="rId{index + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" Target="slides/slide{index + 1}.xml"/>'
        for index in range(len(slides))
    )
    overrides = "\n".join(
        f'<Override PartName="/ppt/slides/slide{index + 1}.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>'
        for index in range(len(slides))
    )
    with ZipFile(path, "w", ZIP_DEFLATED) as pptx:
        pptx.writestr("[Content_Types].xml", _content_types_xml(overrides))
        pptx.writestr("_rels/.rels", _root_rels_xml())
        pptx.writestr("ppt/presentation.xml", _presentation_xml(len(slides)))
        pptx.writestr("ppt/_rels/presentation.xml.rels", _presentation_rels_xml(rels))
        for index, (title, bullets) in enumerate(slides, 1):
            pptx.writestr(f"ppt/slides/slide{index}.xml", _slide_xml(title, bullets))


def _content_types_xml(overrides: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>
{overrides}
</Types>"""


def _root_rels_xml() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="ppt/presentation.xml"/>
</Relationships>"""


def _presentation_xml(count: int) -> str:
    slide_ids = "\n".join(f'<p:sldId id="{255 + index}" r:id="rId{index}"/>' for index in range(1, count + 1))
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
<p:sldIdLst>{slide_ids}</p:sldIdLst>
</p:presentation>"""


def _presentation_rels_xml(rels: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
{rels}
</Relationships>"""


def _slide_xml(title: str, bullets: list[str]) -> str:
    body = "".join(f"<a:p><a:r><a:t>{_xml_escape(item)}</a:t></a:r></a:p>" for item in bullets)
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
<p:cSld><p:spTree>
<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr/>
<p:sp><p:nvSpPr><p:cNvPr id="2" name="Title"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr><p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:t>{_xml_escape(title)}</a:t></a:r></a:p></p:txBody></p:sp>
<p:sp><p:nvSpPr><p:cNvPr id="3" name="Content"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr><p:txBody><a:bodyPr/><a:lstStyle/>{body}</p:txBody></p:sp>
</p:spTree></p:cSld>
</p:sld>"""


def _xml_escape(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _safe_stem(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._")[:80] or "presentation"


def _dedupe(path: Path) -> Path:
    candidate = path
    index = 2
    while candidate.exists():
        candidate = path.with_name(f"{path.stem}_{index}{path.suffix}")
        index += 1
    return candidate


def _terminal_output(operation: str, output: dict[str, Any]) -> dict[str, Any]:
    return {
        "content": output.get("summary") or f"document.manage {operation} completed.",
        "is_final": True,
        "exit_code": 0,
    }




def register(deps: RegistryDeps, definitions: Definitions, adapters: Adapters) -> None:
    settings = deps.settings
    enabled = capability_enabled(settings, Capability.FILESYSTEM_WRITE)
    definitions.append(
        ToolDefinition(
            name="document.manage",
            capability=Capability.FILESYSTEM_WRITE,
            enabled=enabled,
            description="inspect documents, summarize PDFs, and create or revise PowerPoint files as task artifacts",
            operations=("inspect_document", "extract_text", "summarize_pdf", "create_presentation", "update_presentation"),
            input_schema=DocumentManageInput,
            output_schema=DocumentManageOutput,
            operation_output_schemas=same_output_schema(
                ("inspect_document", "extract_text", "summarize_pdf", "create_presentation", "update_presentation"),
                DocumentManageOutput,
            ),
            default_operation="inspect_document",
            # A document's content is not authored by this machine or
            # necessarily by the user who pointed at it - e.g. a downloaded
            # PDF (docs/THREAT_MODEL.md). create_presentation/
            # update_presentation are writes, not inbound content.
            operation_content_trust={
                "inspect_document": UNTRUSTED_EXTERNAL,
                "extract_text": UNTRUSTED_EXTERNAL,
                "summarize_pdf": UNTRUSTED_EXTERNAL,
            },
            operation_risks={
                "inspect_document": RiskLevel.LOW,
                "extract_text": RiskLevel.LOW,
                "summarize_pdf": RiskLevel.LOW,
                "create_presentation": RiskLevel.HIGH,
                "update_presentation": RiskLevel.HIGH,
            },
            examples=(
                {"operation": "summarize_pdf", "path": "{{last_entry_path}}"},
                {"operation": "create_presentation",
                 "title": "Weekly Update",
                 "content": "Status: green. Blockers: none."},
            ),
        )
    )
    if deps.artifact_repository is not None:
        adapters["document.manage"] = DocumentManageAdapter(
            deps.artifact_repository,  # type: ignore[arg-type]
            provider=deps.provider,
            allowed_roots=_document_roots(settings),
        )


def _document_roots(settings: AppSettings) -> list[str]:
    return [
        settings.storage.artifact_dir,
        settings.adapters.workspace.root_dir,
        *settings.adapters.computer_use.allowed_roots,
    ]
