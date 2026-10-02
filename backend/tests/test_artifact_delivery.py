from __future__ import annotations

from pathlib import Path

import pytest

from agent_control.config import AppSettings, CapabilityPolicy
from agent_control.orchestration import ToolExecutor
from agent_control.policy import PolicyEngine
from agent_control.schemas import Artifact, ArtifactType, Capability, ChannelType, RiskLevel, ToolCallRequest, ToolResultStatus
from agent_control.tools.artifact_delivery import ArtifactDeliveryAdapter
from agent_control.tools.registry import build_tool_registry
from helpers import make_repos

class FakeTelegramClient:
    def __init__(self) -> None:
        self.photos: list[tuple[str | int, str, str | None]] = []
        self.documents: list[tuple[str | int, str, str | None]] = []

    async def send_photo_file(self, chat_id: str | int, path: str, caption: str | None = None) -> dict:
        self.photos.append((chat_id, path, caption))
        return {"ok": True, "method": "sendPhoto"}

    async def send_document_file(self, chat_id: str | int, path: str, caption: str | None = None) -> dict:
        self.documents.append((chat_id, path, caption))
        return {"ok": True, "method": "sendDocument"}


def test_registry_exposes_artifact_delivery_when_telegram_send_is_enabled(tmp_path) -> None:
    repos, _audit = make_repos(tmp_path)
    settings = AppSettings(
        _env_file=None,
        capabilities={
            Capability.TELEGRAM_SEND: CapabilityPolicy(
                enabled=True,
                requires_approval=False,
                max_risk_level=RiskLevel.LOW,
            )
        },
    )

    registry = build_tool_registry(
        settings,
        "http://127.0.0.1:8765",
        artifact_repository=repos.artifacts,
        task_repository=repos.tasks,
        telegram_client=FakeTelegramClient(),
    )
    definitions = {definition.name: definition for definition in registry.definitions}

    assert definitions["artifact.deliver"].enabled is True
    assert definitions["artifact.deliver"].operations == ("send_file", "send_latest", "send_screenshot", "list_artifacts")
    assert "artifact.deliver" in registry.adapters

@pytest.mark.asyncio
async def test_artifact_delivery_sends_screenshot_from_task_metadata(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    screenshot = tmp_path / "screen.png"
    screenshot.write_bytes(b"png")
    task = repos.tasks.create(
        "take a screenshot and send it to me",
        metadata={"source_chat_id": "100", "screenshot_path": str(screenshot)},
    )
    client = FakeTelegramClient()
    adapter = ArtifactDeliveryAdapter(
        repos.artifacts,
        repos.tasks,
        telegram_client=client,
        allowed_roots=[str(tmp_path)],
    )

    result = await adapter.execute(
        ToolCallRequest(
            task_id=task.id,
            tool_name="artifact.deliver",
            capability=Capability.TELEGRAM_SEND,
            risk_level=RiskLevel.LOW,
            input={"operation": "send_screenshot", "caption": "desktop"},
        )
    )

    artifacts = repos.artifacts.list_for_task(task.id)
    assert result.status == ToolResultStatus.SUCCEEDED
    assert result.output["delivered"] is True
    assert result.output["delivery_method"] == "telegram.sendPhoto"
    assert client.photos == [("100", str(screenshot.resolve()), "desktop")]
    assert artifacts[0].type == ArtifactType.SCREENSHOT
    assert artifacts[0].uri == str(screenshot.resolve())

@pytest.mark.asyncio
async def test_artifact_delivery_on_a_whatsapp_task_fails_with_a_channel_aware_message(tmp_path) -> None:
    """artifact.deliver is Telegram-only in this version (docs/archive/UI_UX_AUDIT.md
    Phase 16's disclosed scope) - a WhatsApp-sourced task must fail with a
    message naming its actual channel, not a generic Telegram-flavored one
    that would read like a bug to whoever hits it."""
    repos, _audit = make_repos(tmp_path)
    screenshot = tmp_path / "screen.png"
    screenshot.write_bytes(b"png")
    task = repos.tasks.create(
        "take a screenshot and send it to me",
        metadata={
            "source_channel": ChannelType.WHATSAPP.value,
            "source_chat_id": "15551234567@s.whatsapp.net",
            "screenshot_path": str(screenshot),
        },
    )
    adapter = ArtifactDeliveryAdapter(
        repos.artifacts,
        repos.tasks,
        telegram_client=FakeTelegramClient(),
        allowed_roots=[str(tmp_path)],
    )

    result = await adapter.execute(
        ToolCallRequest(
            task_id=task.id,
            tool_name="artifact.deliver",
            capability=Capability.TELEGRAM_SEND,
            risk_level=RiskLevel.LOW,
            input={"operation": "send_screenshot"},
        )
    )

    assert result.status == ToolResultStatus.FAILED
    assert "only supports Telegram" in result.error_message
    assert "whatsapp" in result.error_message


@pytest.mark.asyncio
async def test_artifact_delivery_does_not_send_recent_artifact_by_default(tmp_path) -> None:
    repos, _audit = make_repos(tmp_path)
    document = tmp_path / "report.txt"
    document.write_text("latest output", encoding="utf-8")
    previous = repos.tasks.create("previous", metadata={"source_chat_id": "100"})
    task = repos.tasks.create("send latest output", metadata={"source_chat_id": "100"})
    repos.artifacts.create(Artifact(task_id=previous.id, type=ArtifactType.DOCUMENT, uri=str(document), content_preview="report"))
    client = FakeTelegramClient()
    adapter = ArtifactDeliveryAdapter(
        repos.artifacts,
        repos.tasks,
        telegram_client=client,
        allowed_roots=[str(tmp_path)],
    )

    result = await adapter.execute(
        ToolCallRequest(
            task_id=task.id,
            tool_name="artifact.deliver",
            capability=Capability.TELEGRAM_SEND,
            risk_level=RiskLevel.LOW,
            input={"operation": "send_latest", "caption": "latest"},
        )
    )

    assert result.status == ToolResultStatus.FAILED
    assert "no deliverable artifact" in (result.error_message or "")
    assert client.documents == []

@pytest.mark.asyncio
async def test_artifact_delivery_sends_recent_artifact_when_cache_fallback_enabled(tmp_path) -> None:
    repos, _audit = make_repos(tmp_path)
    document = tmp_path / "report.txt"
    document.write_text("latest output", encoding="utf-8")
    previous = repos.tasks.create("previous", metadata={"source_chat_id": "100"})
    task = repos.tasks.create("send latest output", metadata={"source_chat_id": "100"})
    repos.artifacts.create(Artifact(task_id=previous.id, type=ArtifactType.DOCUMENT, uri=str(document), content_preview="report"))
    client = FakeTelegramClient()
    adapter = ArtifactDeliveryAdapter(
        repos.artifacts,
        repos.tasks,
        telegram_client=client,
        allowed_roots=[str(tmp_path)],
        recent_fallback_enabled=True,
    )

    result = await adapter.execute(
        ToolCallRequest(
            task_id=task.id,
            tool_name="artifact.deliver",
            capability=Capability.TELEGRAM_SEND,
            risk_level=RiskLevel.LOW,
            input={"operation": "send_latest", "caption": "latest"},
        )
    )

    assert result.status == ToolResultStatus.SUCCEEDED
    assert result.output["delivered"] is True
    assert client.documents == [("100", str(document.resolve()), "latest")]

@pytest.mark.asyncio
async def test_artifact_delivery_materializes_latest_tool_text_when_no_file_exists(tmp_path) -> None:
    repos, _audit = make_repos(tmp_path)
    task = repos.tasks.create(
        "send latest output",
        metadata={
            "source_chat_id": "100",
            "last_tool_result": {"output": {"summary": "Browser page summary"}},
        },
    )
    client = FakeTelegramClient()
    adapter = ArtifactDeliveryAdapter(
        repos.artifacts,
        repos.tasks,
        telegram_client=client,
        allowed_roots=[str(tmp_path)],
    )

    result = await adapter.execute(
        ToolCallRequest(
            task_id=task.id,
            tool_name="artifact.deliver",
            capability=Capability.TELEGRAM_SEND,
            risk_level=RiskLevel.LOW,
            input={"operation": "send_latest", "caption": "latest"},
        )
    )

    assert result.status == ToolResultStatus.SUCCEEDED
    assert Path(result.output["path"]).read_text(encoding="utf-8") == "Browser page summary"
    assert client.documents == [("100", result.output["path"], "latest")]

@pytest.mark.asyncio
async def test_artifact_delivery_sends_latest_document_artifact(tmp_path) -> None:
    repos, _audit = make_repos(tmp_path)
    document = tmp_path / "report.pdf"
    document.write_bytes(b"%PDF-1.4")
    task = repos.tasks.create("send me the PDF file", metadata={"source_chat_id": "100"})
    artifact = repos.artifacts.create(
        Artifact(
            task_id=task.id,
            type=ArtifactType.DOCUMENT,
            uri=str(document),
            content_preview="report.pdf",
        )
    )
    client = FakeTelegramClient()
    adapter = ArtifactDeliveryAdapter(
        repos.artifacts,
        repos.tasks,
        telegram_client=client,
        allowed_roots=[str(tmp_path)],
    )

    result = await adapter.execute(
        ToolCallRequest(
            task_id=task.id,
            tool_name="artifact.deliver",
            capability=Capability.TELEGRAM_SEND,
            risk_level=RiskLevel.LOW,
            input={"operation": "send_latest", "artifact_type": "document"},
        )
    )

    assert result.status == ToolResultStatus.SUCCEEDED
    assert result.output["artifact_id"] == artifact.id
    assert result.output["delivery_method"] == "telegram.sendDocument"
    assert client.documents == [("100", str(document.resolve()), "report.pdf")]

@pytest.mark.asyncio
async def test_artifact_delivery_direct_path_rejects_root_escape(tmp_path) -> None:
    repos, _audit = make_repos(tmp_path)
    task = repos.tasks.create("send a file", metadata={"source_chat_id": "100"})
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("nope", encoding="utf-8")
    adapter = ArtifactDeliveryAdapter(
        repos.artifacts,
        repos.tasks,
        telegram_client=FakeTelegramClient(),
        allowed_roots=[str(tmp_path / "allowed")],
    )

    result = await adapter.execute(
        ToolCallRequest(
            task_id=task.id,
            tool_name="artifact.deliver",
            capability=Capability.TELEGRAM_SEND,
            risk_level=RiskLevel.LOW,
            input={"operation": "send_file", "path": str(outside)},
        )
    )

    assert result.status == ToolResultStatus.FAILED
    assert "outside configured delivery roots" in (result.error_message or "")

@pytest.mark.asyncio
async def test_artifact_delivery_resolves_desktop_alias_under_allowed_home(monkeypatch, tmp_path) -> None:
    fake_home = tmp_path / "home"
    desktop = fake_home / "Desktop"
    desktop.mkdir(parents=True)
    document = desktop / "report.txt"
    document.write_text("desktop report", encoding="utf-8")
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    repos, _audit = make_repos(tmp_path)
    task = repos.tasks.create("send file from desktop", metadata={"source_chat_id": "100"})
    client = FakeTelegramClient()
    adapter = ArtifactDeliveryAdapter(
        repos.artifacts,
        repos.tasks,
        telegram_client=client,
        allowed_roots=[str(fake_home)],
    )

    result = await adapter.execute(
        ToolCallRequest(
            task_id=task.id,
            tool_name="artifact.deliver",
            capability=Capability.TELEGRAM_SEND,
            risk_level=RiskLevel.LOW,
            input={"operation": "send_file", "path": "desktop/report.txt"},
        )
    )

    assert result.status == ToolResultStatus.SUCCEEDED
    assert client.documents == [("100", str(document.resolve()), "report.txt")]

@pytest.mark.asyncio
async def test_executor_records_artifact_delivery_output(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    screenshot = tmp_path / "screen.png"
    screenshot.write_bytes(b"png")
    task = repos.tasks.create("send a screenshot", metadata={"source_chat_id": "100", "screenshot_path": str(screenshot)})
    settings = AppSettings(
        _env_file=None,
        capabilities={
            Capability.TELEGRAM_SEND: CapabilityPolicy(
                enabled=True,
                requires_approval=False,
                max_risk_level=RiskLevel.LOW,
            )
        },
    )
    registry = build_tool_registry(
        settings,
        "http://127.0.0.1:8765",
        artifact_repository=repos.artifacts,
        task_repository=repos.tasks,
        telegram_client=FakeTelegramClient(),
    )
    executor = ToolExecutor(
        PolicyEngine(settings, audit),
        repos,
        audit,
        adapters=registry.adapters,
        tool_definitions=registry.definitions,
    )

    result = await executor.execute(
        ToolCallRequest(
            task_id=task.id,
            tool_name="artifact.deliver",
            capability=Capability.TELEGRAM_SEND,
            input={"operation": "send_screenshot"},
        )
    )

    assert result.status == ToolResultStatus.SUCCEEDED
    assert result.output["delivered"] is True
