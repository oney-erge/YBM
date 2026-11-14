"""Photos and files sent to the bot are saved and handed to the task; a voice note is acknowledged, transcribed and echoed back."""

from __future__ import annotations

import pytest

from agent_control.config import AppSettings, TelegramConfig
from agent_control.llm import StaticMessageClassifier
from agent_control.schemas import AuditEventType, MessageKind, TranscriptionResult
from test_telegram import _service


class FakeBot:
    def __init__(self, files: dict[str, bytes] | None = None) -> None:
        self.files = files or {}
        self.sent: list[tuple[str, str]] = []
        self.actions: list[str] = []
        self.downloaded: list[str] = []

    async def get_file(self, file_id: str) -> dict:
        return {"file_path": f"documents/{file_id}.bin"} if file_id in self.files else {}

    async def download_file(self, file_path: str) -> bytes:
        self.downloaded.append(file_path)
        return self.files[file_path.split("/")[-1].removesuffix(".bin")]

    async def send_message(self, chat_id, text, reply_markup=None) -> dict:
        self.sent.append((str(chat_id), text))
        return {"ok": True, "result": {"message_id": 7}}

    async def send_chat_action(self, chat_id, action="typing") -> dict:
        self.actions.append(action)
        return {"ok": True}


class FakeSTT:
    async def transcribe(self, audio, file_name=None, mime_type=None):
        return TranscriptionResult(text="how many steam games do I have", language="en")


def _config(tmp_path, **extra) -> TelegramConfig:
    return TelegramConfig(enabled=True, allowed_user_ids=[42], allowed_chat_ids=[100], inbox_dir=str(tmp_path / "inbox"), **extra)


def _service_with(tmp_path, bot, config=None, stt=None):
    service, repos = _service(tmp_path, config or _config(tmp_path), settings=AppSettings(_env_file=None), classifier=StaticMessageClassifier(), bot_api=bot)
    if stt is not None:
        service.stt = stt
    return service, repos


def _message(**fields) -> dict:
    return {"update_id": 1, "message": {"message_id": 5, "from": {"id": 42}, "chat": {"id": 100}, **fields}}


def test_a_photo_is_seen_as_an_image_with_its_biggest_size_chosen(tmp_path):
    service, _ = _service_with(tmp_path, FakeBot())
    result = service.adapter.normalize_update(_message(photo=[
        {"file_id": "small", "file_size": 900, "width": 90, "height": 90}, {"file_id": "big", "file_size": 90000, "width": 900, "height": 900},
    ], caption="what is this"))
    inbound = result.inbound_message
    assert inbound.kind == MessageKind.IMAGE and inbound.text == "what is this"
    assert [a.file_id for a in inbound.attachments] == ["big"]


def test_a_document_is_seen_with_its_name_and_type(tmp_path):
    service, _ = _service_with(tmp_path, FakeBot())
    inbound = service.adapter.normalize_update(_message(document={"file_id": "d1", "file_name": "report.pdf", "mime_type": "application/pdf", "file_size": 1234})).inbound_message
    assert inbound.kind == MessageKind.DOCUMENT
    attachment = inbound.attachments[0]
    assert (attachment.file_name, attachment.mime_type, attachment.size_bytes) == ("report.pdf", "application/pdf", 1234)


@pytest.mark.asyncio
async def test_a_file_is_saved_in_the_inbox_and_the_task_is_told_where(tmp_path):
    bot = FakeBot({"d1": b"%PDF-1.4 hello"})
    service, repos = _service_with(tmp_path, bot)
    result = await service.handle_update_async(_message(document={"file_id": "d1", "file_name": "tax return (2025).pdf", "file_size": 14}, caption="summarize this"))
    text = result.inbound_message.text
    assert text.startswith("summarize this\n[The user sent a file: ") and "tax return _2025_.pdf" in text
    saved = text.split("[The user sent a file: ")[1].rstrip("]")
    assert open(saved, "rb").read() == b"%PDF-1.4 hello"
    assert str(tmp_path / "inbox") in saved


@pytest.mark.asyncio
async def test_a_photo_without_a_caption_still_gives_the_task_something_to_read(tmp_path):
    service, _ = _service_with(tmp_path, FakeBot({"p1": b"\xff\xd8jpeg"}))
    result = await service.handle_update_async(_message(photo=[{"file_id": "p1", "file_size": 6, "width": 10, "height": 10}]))
    assert result.inbound_message.text.startswith("I am sending you a picture.\n[The user sent a picture: ")


@pytest.mark.asyncio
async def test_two_files_with_the_same_name_in_one_second_do_not_overwrite_each_other(tmp_path):
    service, _ = _service_with(tmp_path, FakeBot({"a": b"one", "b": b"two"}))
    first = await service.handle_update_async(_message(document={"file_id": "a", "file_name": "n.txt"}))
    inbound = first.inbound_message.model_copy(update={"attachments": [first.inbound_message.attachments[0].model_copy(update={"file_id": "b"})], "text": None})
    second = await service._receive_files(inbound)
    paths = {first.inbound_message.text.split(": ")[1].rstrip("]"), second.text.split(": ")[1].rstrip("]")}
    assert len(paths) == 2


@pytest.mark.asyncio
async def test_a_file_that_is_too_big_or_will_not_download_is_answered_in_words_and_nothing_is_spawned(tmp_path):
    service, repos = _service_with(tmp_path, FakeBot({"big": b"x"}), config=_config(tmp_path, max_file_mb=1))
    too_big = await service.handle_update_async(_message(document={"file_id": "big", "file_name": "huge.bin", "file_size": 5 * 1024 * 1024}))
    assert "up to 1 MB" in too_big.outbound_message.text and too_big.inbound_message.attachments
    gone = await service.handle_update_async(_message(document={"file_id": "missing", "file_name": "gone.bin", "file_size": 10}))
    assert "would not give me that file" in gone.outbound_message.text
    assert repos.tasks.list_recent(5) == []
    assert any(e.payload.get("error") == "file_download_failed" for e in repos.audit.list_recent(10) if e.type == AuditEventType.ERROR)


@pytest.mark.asyncio
async def test_a_voice_note_shows_typing_then_says_what_it_heard(tmp_path):
    bot = FakeBot({"v1": b"ogg"})
    service, _ = _service_with(tmp_path, bot, stt=FakeSTT())
    result = await service.handle_update_async(_message(voice={"file_id": "v1", "duration": 3, "mime_type": "audio/ogg", "file_size": 3}))
    assert bot.actions[0] == "typing"
    assert ("100", "I heard: how many steam games do I have") in bot.sent
    assert result.inbound_message.text == "how many steam games do I have"


@pytest.mark.asyncio
async def test_what_it_heard_is_not_sent_when_the_echo_is_turned_off(tmp_path):
    bot = FakeBot({"v1": b"ogg"})
    service, _ = _service_with(tmp_path, bot, config=_config(tmp_path, echo_voice_transcript=False), stt=FakeSTT())
    await service.handle_update_async(_message(voice={"file_id": "v1", "duration": 3, "file_size": 3}))
    assert not any(text.startswith("I heard") for _, text in bot.sent)
    assert bot.actions[0] == "typing"


def test_the_inbox_is_readable_by_the_file_and_document_tools():
    from agent_control.tools.document_manage import _document_roots
    from agent_control.tools.registry import build_tool_registry

    settings = AppSettings(_env_file=None)
    assert settings.channels.telegram.inbox_dir == ".agent_control/inbox"
    assert ".agent_control/inbox" in _document_roots(settings)
    registry = build_tool_registry(settings, "http://127.0.0.1:8765")
    adapter = registry.adapters.get("filesystem.manage")
    if adapter is not None:  # off in a default config without file access
        assert any(str(root).replace("\\", "/").endswith(".agent_control/inbox") for root in adapter._also_allowed)
        assert not any(str(root).replace("\\", "/").endswith(".agent_control/inbox") for root in adapter.allowed_roots)
