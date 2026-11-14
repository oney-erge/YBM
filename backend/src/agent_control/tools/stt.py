from __future__ import annotations

import asyncio
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any, Protocol

from agent_control.config import STTAdapterConfig
from agent_control.schemas import TranscriptionResult


class STTAdapter(Protocol):
    async def transcribe(
        self,
        audio: bytes,
        file_name: str | None = None,
        mime_type: str | None = None,
    ) -> TranscriptionResult:
        ...


class DisabledSTTAdapter:
    async def transcribe(
        self,
        audio: bytes,
        file_name: str | None = None,
        mime_type: str | None = None,
    ) -> TranscriptionResult:
        raise RuntimeError("STT adapter is disabled")


class StaticSTTAdapter:
    def __init__(self, text: str) -> None:
        self.text = text

    async def transcribe(
        self,
        audio: bytes,
        file_name: str | None = None,
        mime_type: str | None = None,
    ) -> TranscriptionResult:
        return TranscriptionResult(
            text=self.text,
            metadata={"file_name": file_name, "mime_type": mime_type, "bytes": len(audio)},
        )


class WhisperCliSTTAdapter:
    def __init__(self, config: STTAdapterConfig) -> None:
        self.config = config

    async def transcribe(
        self,
        audio: bytes,
        file_name: str | None = None,
        mime_type: str | None = None,
    ) -> TranscriptionResult:
        temp_root = Path(self.config.temp_dir).expanduser().resolve()
        temp_root.mkdir(parents=True, exist_ok=True)
        suffix = Path(file_name or "voice.ogg").suffix or ".ogg"
        with tempfile.TemporaryDirectory(dir=temp_root) as work_dir:
            input_path = Path(work_dir) / f"voice{suffix}"
            input_path.write_bytes(audio)
            command = self._command(input_path)
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=work_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=self.config.timeout_seconds)
            except asyncio.TimeoutError as exc:
                process.kill()
                await process.communicate()
                raise RuntimeError(f"STT command timed out after {self.config.timeout_seconds} seconds") from exc
            output_text = stdout.decode("utf-8", errors="replace").strip()
            error_text = stderr.decode("utf-8", errors="replace").strip()
            txt_outputs = sorted(Path(work_dir).glob("*.txt"))
            if txt_outputs:
                output_text = txt_outputs[0].read_text(encoding="utf-8", errors="replace").strip()
            if process.returncode != 0:
                raise RuntimeError(f"STT command failed with exit code {process.returncode}: {error_text or output_text}")
            if not output_text:
                raise RuntimeError("STT command completed but returned no transcript")
            return TranscriptionResult(
                text=output_text,
                metadata={
                    "provider": self.config.provider,
                    "model": self.config.model,
                    "file_name": file_name,
                    "mime_type": mime_type,
                    "bytes": len(audio),
                    "stderr": error_text[-1000:],
                },
            )

    def _command(self, input_path: Path) -> list[str]:
        if self.config.command:
            return [part.format(input=str(input_path), model=self.config.model) for part in self.config.command]
        whisper = shutil.which("whisper") or shutil.which("whisper.exe")
        if whisper:
            return [
                whisper,
                str(input_path),
                "--model",
                self.config.model,
                "--output_format",
                "txt",
                "--fp16",
                "False",
            ]
        raise RuntimeError(
            "STT provider local_whisper is enabled, but no whisper CLI was found. "
            "Install/configure Whisper or set adapters.stt.command."
        )


class FasterWhisperSTTAdapter:
    def __init__(self, config: STTAdapterConfig) -> None:
        self.config = config
        self._model: Any | None = None

    async def transcribe(
        self,
        audio: bytes,
        file_name: str | None = None,
        mime_type: str | None = None,
    ) -> TranscriptionResult:
        temp_root = Path(self.config.temp_dir).expanduser().resolve()
        temp_root.mkdir(parents=True, exist_ok=True)
        suffix = Path(file_name or "voice.ogg").suffix or ".ogg"
        with tempfile.TemporaryDirectory(dir=temp_root) as work_dir:
            input_path = Path(work_dir) / f"voice{suffix}"
            input_path.write_bytes(audio)
            return await asyncio.wait_for(
                asyncio.to_thread(self._transcribe_file, input_path, file_name, mime_type, len(audio)),
                timeout=self.config.timeout_seconds,
            )

    def _transcribe_file(
        self,
        input_path: Path,
        file_name: str | None,
        mime_type: str | None,
        byte_count: int,
    ) -> TranscriptionResult:
        model = self._get_model()
        kwargs: dict[str, Any] = {
            "beam_size": self.config.beam_size,
            "vad_filter": self.config.vad_filter,
        }
        if self.config.language:
            kwargs["language"] = self.config.language
        segments, info = model.transcribe(str(input_path), **kwargs)
        segment_list = list(segments)
        text = " ".join(str(segment.text).strip() for segment in segment_list if str(segment.text).strip()).strip()
        if not text:
            raise RuntimeError("faster-whisper completed but returned no transcript")
        duration = getattr(info, "duration", None)
        language = getattr(info, "language", None)
        language_probability = getattr(info, "language_probability", None)
        return TranscriptionResult(
            text=text,
            language=str(language) if language else None,
            duration_seconds=float(duration) if duration is not None else None,
            confidence=float(language_probability) if language_probability is not None else None,
            metadata={
                "provider": "faster_whisper",
                "model": self.config.model,
                "device": self.config.device,
                "compute_type": self.config.compute_type,
                "file_name": file_name,
                "mime_type": mime_type,
                "bytes": byte_count,
                "segment_count": len(segment_list),
            },
        )

    async def warm(self) -> None:
        """Load the speech model now, so the first voice note does not wait for it (several seconds). Never raises."""
        try:
            await asyncio.to_thread(self._get_model)
        except Exception:  # noqa: BLE001 - a model that cannot load is reported when a voice note needs it
            pass

    def _get_model(self) -> Any:
        if self._model is not None:
            return self._model
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise RuntimeError(
                "STT provider faster_whisper is enabled, but the faster-whisper package is not installed. "
                "Install the backend voice extra or run: python -m pip install faster-whisper"
            ) from exc
        self._model = WhisperModel(
            self.config.model,
            device=self.config.device,
            compute_type=self.config.compute_type,
        )
        return self._model


def build_stt_adapter(config: STTAdapterConfig) -> STTAdapter:
    if not config.enabled:
        return DisabledSTTAdapter()
    provider = config.provider.strip().lower()
    if provider == "static":
        transcript = os.getenv(config.static_transcript_env, "").strip()
        if not transcript:
            raise RuntimeError(f"{config.static_transcript_env} is required for static STT")
        return StaticSTTAdapter(transcript)
    if provider in {"local_whisper", "whisper", "whisper_cli"}:
        return WhisperCliSTTAdapter(config)
    if provider in {"faster_whisper", "faster-whisper"}:
        return FasterWhisperSTTAdapter(config)
    raise RuntimeError(f"Unsupported STT provider: {config.provider}")
