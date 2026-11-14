"""Say what is spoken in an audio file: a voicemail, a meeting recording, a voice note saved as a file.

The speech model already runs for voice notes sent in Telegram (adapters.stt, Whisper on this computer). A recording that arrives as a file, or
sits in a folder, had no way to it: the model that runs the assistant reads text only, and the file tools can only say "a WAV, 2 MB". This
is the same speech model behind a file path. Nothing leaves this computer, and it is a read: no approval, no change.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from agent_control.config import AppSettings
from agent_control.schemas import Capability, ToolCallRequest, ToolCallResult, ToolResultStatus
from agent_control.tools.contracts import AudioTranscribeInput, AudioTranscribeOutput
from agent_control.tools.spec import Adapters, Definitions, RegistryDeps, ToolDefinition, capability_enabled, failed_result, same_output_schema
from agent_control.tools.stt import build_stt_adapter

AUDIO_SUFFIXES = frozenset({".wav", ".mp3", ".m4a", ".ogg", ".oga", ".opus", ".flac", ".aac", ".webm", ".mp4", ".wma"})
MAX_AUDIO_BYTES = 60 * 1024 * 1024
_MIME = {".wav": "audio/wav", ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".ogg": "audio/ogg", ".oga": "audio/ogg", ".opus": "audio/ogg",
         ".flac": "audio/flac", ".aac": "audio/aac", ".webm": "audio/webm", ".mp4": "audio/mp4", ".wma": "audio/x-ms-wma"}


class AudioTranscribeAdapter:
    def __init__(self, settings: AppSettings, path_guard: Callable[[str], Path], stt: Any | None = None) -> None:
        self.settings = settings
        self._path_guard = path_guard
        self._stt = stt or build_stt_adapter(settings.adapters.stt)

    async def execute(self, request: ToolCallRequest) -> ToolCallResult:
        try:
            path = self._path_guard(str(request.input["path"]))
        except ValueError as exc:
            return failed_result(request, str(exc))
        if path.suffix.lower() not in AUDIO_SUFFIXES or not path.is_file():
            return failed_result(request, f"{path.name} is not an audio file I can listen to (wav, mp3, m4a, ogg, flac, aac, webm)")
        if path.stat().st_size > MAX_AUDIO_BYTES:
            return failed_result(request, f"{path.name} is larger than {MAX_AUDIO_BYTES // (1024 * 1024)} MB; send a shorter recording")
        try:
            transcript = await self._stt.transcribe(path.read_bytes(), file_name=path.name, mime_type=_MIME.get(path.suffix.lower()))
        except Exception as exc:  # noqa: BLE001 - say what is wrong, in words
            return failed_result(request, f"the speech model could not listen to {path.name}: {' '.join(str(exc).split())[:240] or type(exc).__name__}")
        text = str(getattr(transcript, "text", "") or "").strip()
        if not text:
            return failed_result(request, f"no speech was heard in {path.name}")
        output: dict[str, Any] = {
            "operation": "transcribe", "summary": text[:240], "transcript": text, "file": str(path),
            "language": getattr(transcript, "language", None),
            "terminal_output": [{"content": text, "exit_code": 0, "is_final": True}],
        }
        return ToolCallResult(request_id=request.id, status=ToolResultStatus.SUCCEEDED, output=output)


def register(deps: RegistryDeps, definitions: Definitions, adapters: Adapters) -> None:
    settings = deps.settings
    guard = getattr(adapters.get("filesystem.manage"), "resolve_allowed", None)
    enabled = settings.adapters.stt.enabled and guard is not None and capability_enabled(settings, Capability.FILESYSTEM_READ)
    definitions.append(
        ToolDefinition(
            name="audio.transcribe",
            group="voice",
            provides=("transcribe", "recording", "voicemail", "meeting", "audio", "mp3", "wav", "listen", "speech", "minutes", "spoken", "said"),
            capability=Capability.FILESYSTEM_READ,
            enabled=enabled,
            named_when_disabled=False,
            description=(
                "listen to an audio file (wav, mp3, m4a, ogg, flac) and write down what is said in it, with a speech model that runs on this "
                "computer; use it for a voicemail, a meeting recording or a voice note saved as a file"
            ),
            operations=("transcribe",),
            input_schema=AudioTranscribeInput,
            output_schema=AudioTranscribeOutput,
            operation_output_schemas=same_output_schema(("transcribe",), AudioTranscribeOutput),
            default_operation="transcribe",
            examples=({"operation": "transcribe", "path": "{{recording_path}}"},),
        )
    )
    if enabled:
        adapters["audio.transcribe"] = AudioTranscribeAdapter(settings, guard)  # type: ignore[arg-type]
