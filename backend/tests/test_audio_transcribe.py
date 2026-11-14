"""Say what is spoken in an audio file (tools/audio_transcribe.py)."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_control.config import AppSettings
from agent_control.schemas import TranscriptionResult, ToolCallRequest, ToolResultStatus
from agent_control.tools.audio_transcribe import AudioTranscribeAdapter, MAX_AUDIO_BYTES
from agent_control.tools.registry import build_tool_registry


class _Stt:
    def __init__(self, text: str = "Please call Marta back on 555 0142.", error: Exception | None = None) -> None:
        self.text, self.error, self.heard = text, error, []

    async def transcribe(self, audio: bytes, file_name: str | None = None, mime_type: str | None = None) -> TranscriptionResult:
        self.heard.append((len(audio), file_name, mime_type))
        if self.error:
            raise self.error
        return TranscriptionResult(text=self.text, language="en")


def _guard(root: Path):
    def resolve(value: str) -> Path:
        path = Path(value).resolve()
        if root.resolve() not in path.parents:
            raise ValueError("that folder is not one I may read")
        return path

    return resolve


def _request(path: Path) -> ToolCallRequest:
    return ToolCallRequest(task_id="t", tool_name="audio.transcribe", capability="filesystem.read", risk_level="low", input={"operation": "transcribe", "path": str(path)})


@pytest.mark.asyncio
async def test_a_recording_is_listened_to_and_written_down(tmp_path) -> None:
    clip = tmp_path / "voicemail.wav"
    clip.write_bytes(b"RIFF" + b"\0" * 2000)
    stt = _Stt()
    result = await AudioTranscribeAdapter(AppSettings(_env_file=None), _guard(tmp_path), stt).execute(_request(clip))
    assert result.status == ToolResultStatus.SUCCEEDED and result.output["transcript"] == "Please call Marta back on 555 0142."
    assert stt.heard == [(2004, "voicemail.wav", "audio/wav")] and result.output["language"] == "en"


@pytest.mark.asyncio
async def test_what_is_not_audio_not_allowed_too_big_silent_or_broken_is_a_failure_in_words(tmp_path) -> None:
    adapter = AudioTranscribeAdapter(AppSettings(_env_file=None), _guard(tmp_path), _Stt())
    text = tmp_path / "notes.txt"
    text.write_text("hi")
    assert "not an audio file" in ((await adapter.execute(_request(text))).error_message or "")
    assert "not one I may read" in ((await adapter.execute(_request(Path(tmp_path.anchor) / "elsewhere.wav"))).error_message or "")
    big = tmp_path / "long.mp3"
    with big.open("wb") as handle:
        handle.truncate(MAX_AUDIO_BYTES + 1)
    assert "larger than" in ((await adapter.execute(_request(big))).error_message or "")
    quiet = tmp_path / "quiet.wav"
    quiet.write_bytes(b"x" * 100)
    assert "no speech was heard" in ((await AudioTranscribeAdapter(AppSettings(_env_file=None), _guard(tmp_path), _Stt(text="  ")).execute(_request(quiet))).error_message or "")
    broken = await AudioTranscribeAdapter(AppSettings(_env_file=None), _guard(tmp_path), _Stt(error=RuntimeError("model missing"))).execute(_request(quiet))
    assert "could not listen" in (broken.error_message or "") and "model missing" in (broken.error_message or "")


def test_the_tool_exists_only_when_speech_recognition_is_on() -> None:
    off = build_tool_registry(AppSettings(_env_file=None), "http://127.0.0.1:8765")
    assert "audio.transcribe" not in {d.name for d in off.definitions if d.enabled} and "audio.transcribe" not in off.context()
