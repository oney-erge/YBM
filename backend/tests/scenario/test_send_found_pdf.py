"""Scenario: "send me the PDF at this path" through the real Operator loop -
a single artifact.deliver send_file call -> the fake Telegram client. Ports
e2e/all_cases.json's `send_found_pdf` case down to the deterministic tier
(docs/archive/HISTORY.md P2) - the simplest possible delivery-only case (single
step, a known literal path, no filesystem search first), chosen deliberately
after `output_delivery` turned out to need an unusually flaky multi-attempt
plan under the old plan-based path; this one locks in the harness's
FakeTelegramClient path with minimal surface area for LLM non-determinism.
Fixture re-recorded 2026-07-28 (`ybm scenario record send_found_pdf`,
localdeploy_qwen3vl_8b).
"""

from __future__ import annotations

import pytest

from .harness import assert_completed, assert_rejected, build_scenario, filesystem_settings, isolated_settings, run_task_to_completion, scenario_scratch_dir


def _write_minimal_pdf(path, text: str) -> None:
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream = f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET"
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
    output.extend(
        f"trailer << /Root 1 0 R /Size {len(objects) + 1} >>\nstartxref\n{xref_at}\n%%EOF\n".encode("latin-1")
    )
    path.write_bytes(output)




@pytest.mark.asyncio
async def test_send_found_pdf_delivers_the_file(tmp_path, monkeypatch) -> None:
    desktop_dir = scenario_scratch_dir("send_found_pdf")
    pdf_path = desktop_dir / "agent-control-sample.pdf"
    _write_minimal_pdf(pdf_path, "Agent Control E2E PDF sample content.")

    # Deliberately NOT filesystem_settings(): delivering a file at a known
    # path only needs artifact.deliver's own allowed-root check
    # (computer_use.allowed_roots below), not the filesystem.manage tool.
    # Granting filesystem.write here let the model wander into
    # filesystem.manage's open_file operation, which for real calls
    # os.startfile() on the PDF - a genuine host side effect (launches
    # whatever PDF viewer is installed) that has no business happening
    # during a test run, recorded or replayed.
    settings = isolated_settings(
        monkeypatch, tmp_path,
        adapters={"computer_use": {"enabled": True, "allowed_roots": [str(desktop_dir)]}},
    )
    scenario = build_scenario(settings, tmp_path=tmp_path, fixture_name="send_found_pdf")

    task = await run_task_to_completion(scenario, f"Send me the PDF file at {pdf_path}.")

    assert_completed(task)
    tool_calls = scenario.repositories.tool_invocations.list_for_task(task.id)
    assert any(call["tool_name"] == "artifact.deliver" for call in tool_calls)
    assert scenario.telegram.documents
    assert any("agent-control-sample.pdf" in path for _chat_id, path, _caption in scenario.telegram.documents)


@pytest.mark.asyncio
async def test_send_found_pdf_rejects_path_outside_allowed_roots(tmp_path, monkeypatch) -> None:
    desktop_dir = scenario_scratch_dir("send_found_pdf")
    pdf_path = desktop_dir / "agent-control-sample.pdf"
    _write_minimal_pdf(pdf_path, "Agent Control E2E PDF sample content.")

    settings = filesystem_settings(monkeypatch, tmp_path, str(tmp_path / "somewhere_else"))
    scenario = build_scenario(settings, tmp_path=tmp_path, fixture_name="send_found_pdf")

    task = await run_task_to_completion(scenario, f"Send me the PDF file at {pdf_path}.")

    assert_rejected(task)
    assert not scenario.telegram.documents
