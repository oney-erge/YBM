"""A recording sent as a file (Telegram's "audio") is saved and handed to the task like any other file; it was ignored."""

from __future__ import annotations

import pytest

from agent_control.schemas import MessageKind
from test_telegram_files_and_voice import FakeBot, _message, _service_with


def test_an_audio_file_is_seen_as_a_file_with_its_name_and_type(tmp_path) -> None:
    service, _ = _service_with(tmp_path, FakeBot())
    inbound = service.adapter.normalize_update(
        _message(audio={"file_id": "a1", "file_name": "meeting.wav", "mime_type": "audio/wav", "file_size": 2048, "duration": 40}, caption="what are the action items?")
    ).inbound_message
    assert inbound.kind == MessageKind.DOCUMENT and inbound.text == "what are the action items?"
    attachment = inbound.attachments[0]
    assert (attachment.file_name, attachment.mime_type, attachment.size_bytes) == ("meeting.wav", "audio/wav", 2048)


def test_an_audio_file_with_no_name_gets_one_the_speech_tool_recognises(tmp_path) -> None:
    service, _ = _service_with(tmp_path, FakeBot())
    for mime, suffix in (("audio/mpeg", ".mp3"), ("audio/x-m4a", ".m4a"), ("audio/ogg", ".ogg"), ("audio/weird", ".audio")):
        inbound = service.adapter.normalize_update(_message(audio={"file_id": "a1", "mime_type": mime})).inbound_message
        assert inbound.attachments[0].file_name == f"audio_5{suffix}", mime


@pytest.mark.asyncio
async def test_an_audio_file_is_saved_in_the_inbox_and_the_task_is_told_where(tmp_path) -> None:
    service, _ = _service_with(tmp_path, FakeBot({"a1": b"RIFFxxxxWAVE"}))
    result = await service.handle_update_async(
        _message(audio={"file_id": "a1", "file_name": "voicemail.wav", "mime_type": "audio/wav", "file_size": 12}, caption="who called?")
    )
    text = result.inbound_message.text
    assert text.startswith("who called?\n[The user sent a file: ") and "voicemail.wav" in text
    saved = text.split("[The user sent a file: ")[1].split("]")[0]
    assert open(saved, "rb").read() == b"RIFFxxxxWAVE" and str(tmp_path / "inbox") in saved
