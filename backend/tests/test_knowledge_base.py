"""docs/archive/HISTORY.md Part 4 T2.7: local, personal document search - keyword
overlap over a folder of the user's own reference material, not embeddings.
See knowledge_base.py's module docstring for why.
"""

from __future__ import annotations

import pytest

from agent_control.config import AppSettings, Capability, CapabilityPolicy, KnowledgeBaseAdapterConfig, RiskLevel
from agent_control.knowledge_base import list_sources, search
from agent_control.schemas import ToolCallRequest, ToolResultStatus
from agent_control.tools.registry import build_tool_registry
from agent_control.tools.knowledge_base import KnowledgeBaseAdapter


def _request(operation: str, **payload) -> ToolCallRequest:
    return ToolCallRequest(
        task_id="task_kb",
        tool_name="knowledge.search",
        capability=Capability.TELEGRAM_RECEIVE,
        input={"operation": operation, **payload},
    )


def test_list_sources_on_missing_directory_returns_empty(tmp_path) -> None:
    config = KnowledgeBaseAdapterConfig(root_dir=str(tmp_path / "does_not_exist"))

    assert list_sources(config) == []


def test_list_sources_finds_supported_files_recursively(tmp_path) -> None:
    (tmp_path / "notes.md").write_text("some notes", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "more.txt").write_text("more notes", encoding="utf-8")
    (tmp_path / "image.png").write_bytes(b"\x89PNG")  # unsupported, must be skipped
    config = KnowledgeBaseAdapterConfig(root_dir=str(tmp_path))

    sources = list_sources(config)

    # Set comparison, not list equality, to avoid platform path-separator
    # flakiness (relative_to() uses os.sep, sorted() then orders by that).
    assert set(sources) == {"notes.md", str((tmp_path / "sub" / "more.txt").relative_to(tmp_path))}


def test_search_finds_the_relevant_document(tmp_path) -> None:
    (tmp_path / "invoice_policy.md").write_text(
        "Our invoice payment terms are net 30 days from the invoice date.", encoding="utf-8"
    )
    (tmp_path / "unrelated.md").write_text("The weather today is sunny with a light breeze.", encoding="utf-8")
    config = KnowledgeBaseAdapterConfig(root_dir=str(tmp_path))

    results = search(config, "invoice payment terms")

    assert results
    assert results[0]["path"] == "invoice_policy.md"
    assert "net 30 days" in results[0]["excerpt"]
    assert results[0]["score"] > 0


def test_search_with_no_matches_returns_empty_list(tmp_path) -> None:
    (tmp_path / "notes.md").write_text("completely unrelated content about gardening", encoding="utf-8")
    config = KnowledgeBaseAdapterConfig(root_dir=str(tmp_path))

    assert search(config, "quantum physics equations") == []


def test_search_with_empty_query_returns_empty_list(tmp_path) -> None:
    (tmp_path / "notes.md").write_text("some content", encoding="utf-8")
    config = KnowledgeBaseAdapterConfig(root_dir=str(tmp_path))

    assert search(config, "   ") == []


def test_search_respects_max_results(tmp_path) -> None:
    for i in range(10):
        (tmp_path / f"doc_{i}.md").write_text(f"invoice number {i} payment terms", encoding="utf-8")
    config = KnowledgeBaseAdapterConfig(root_dir=str(tmp_path), max_results=3)

    results = search(config, "invoice payment terms")

    assert len(results) == 3


def test_search_splits_long_documents_into_chunks(tmp_path) -> None:
    # Two "hot spots" far apart in one long file - both should be
    # independently findable as separate chunks with their own scores.
    filler = "irrelevant filler text. " * 200
    content = f"{filler}\nthe secret invoice code is ALPHA-7.\n{filler}"
    (tmp_path / "long.md").write_text(content, encoding="utf-8")
    config = KnowledgeBaseAdapterConfig(root_dir=str(tmp_path))

    results = search(config, "secret invoice code ALPHA-7")

    assert results
    assert any("ALPHA-7" in r["excerpt"] for r in results)


def test_search_extracts_pdf_content(tmp_path) -> None:
    stream = "BT /F1 12 Tf 72 720 Td (invoice total 250 dollars) Tj ET"
    objects = [
        "1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n",
        "2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n",
        "3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        "/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >> endobj\n",
        "4 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj\n",
        f"5 0 obj << /Length {len(stream.encode('latin-1'))} >> stream\n{stream}\nendstream endobj\n",
    ]
    output = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for obj in objects:
        offsets.append(len(output))
        output.extend(obj.encode("latin-1"))
    xref_at = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode("latin-1"))
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode("latin-1"))
    output.extend(f"trailer << /Root 1 0 R /Size {len(objects) + 1} >>\nstartxref\n{xref_at}\n%%EOF\n".encode("latin-1"))
    (tmp_path / "invoice.pdf").write_bytes(output)
    config = KnowledgeBaseAdapterConfig(root_dir=str(tmp_path))

    results = search(config, "invoice total dollars")

    assert results
    assert results[0]["path"] == "invoice.pdf"


@pytest.mark.asyncio
async def test_adapter_search_operation(tmp_path) -> None:
    (tmp_path / "notes.md").write_text("invoice payment terms are net 30", encoding="utf-8")
    adapter = KnowledgeBaseAdapter(KnowledgeBaseAdapterConfig(root_dir=str(tmp_path)))

    result = await adapter.execute(_request("search", query="invoice payment"))

    assert result.status == ToolResultStatus.SUCCEEDED
    assert result.output["results"]
    assert "notes.md" in result.output["terminal_output"][0]["content"]


@pytest.mark.asyncio
async def test_adapter_list_sources_operation(tmp_path) -> None:
    (tmp_path / "notes.md").write_text("content", encoding="utf-8")
    adapter = KnowledgeBaseAdapter(KnowledgeBaseAdapterConfig(root_dir=str(tmp_path)))

    result = await adapter.execute(_request("list_sources"))

    assert result.status == ToolResultStatus.SUCCEEDED
    assert result.output["sources"] == ["notes.md"]


@pytest.mark.asyncio
async def test_adapter_search_without_query_fails_validation() -> None:
    from agent_control.tools.contracts import KnowledgeBaseInput
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        KnowledgeBaseInput(operation="search")


@pytest.mark.asyncio
async def test_adapter_unsupported_operation_fails_cleanly(tmp_path) -> None:
    adapter = KnowledgeBaseAdapter(KnowledgeBaseAdapterConfig(root_dir=str(tmp_path)))

    result = await adapter.execute(_request("delete"))

    assert result.status == ToolResultStatus.FAILED
    assert "unsupported" in result.error_message


def test_registry_exposes_knowledge_search_when_telegram_receive_is_enabled() -> None:
    settings = AppSettings(_env_file=None)

    registry = build_tool_registry(settings, "http://127.0.0.1:8765")

    definitions = {d.name: d for d in registry.definitions}
    assert definitions["knowledge.search"].enabled is True
    assert "knowledge.search" in registry.adapters


def test_registry_disables_knowledge_search_when_adapter_disabled_in_config() -> None:
    settings = AppSettings(_env_file=None, adapters={"knowledge_base": {"enabled": False}})

    registry = build_tool_registry(settings, "http://127.0.0.1:8765")

    definitions = {d.name: d for d in registry.definitions}
    assert definitions["knowledge.search"].enabled is False
    assert "knowledge.search" not in registry.adapters


def test_registry_disables_knowledge_search_when_telegram_receive_capability_is_off() -> None:
    settings = AppSettings(
        _env_file=None,
        capabilities={Capability.TELEGRAM_RECEIVE: CapabilityPolicy(enabled=False, max_risk_level=RiskLevel.LOW)},
    )

    registry = build_tool_registry(settings, "http://127.0.0.1:8765")

    definitions = {d.name: d for d in registry.definitions}
    assert definitions["knowledge.search"].enabled is False
