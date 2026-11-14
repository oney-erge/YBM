from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4
import wave

from agent_control.config import TTSAdapterConfig
from agent_control.schemas import Capability, ToolCallRequest, ToolCallResult, ToolResultStatus
from agent_control.tools.contracts import TTSSynthesizeInput, TTSSynthesizeOutput
from agent_control.tools.spec import Adapters, Definitions, RegistryDeps, ToolDefinition, capability_enabled, failed_result


class TTSAdapter(Protocol):
    async def synthesize(self, text: str, *, voice: str | None = None, output_name: str | None = None) -> dict[str, Any]:
        ...

    async def execute(self, request: ToolCallRequest) -> ToolCallResult:
        ...


class DisabledTTSAdapter:
    async def synthesize(self, text: str, *, voice: str | None = None, output_name: str | None = None) -> dict[str, Any]:
        raise RuntimeError("TTS adapter is disabled")

    async def execute(self, request: ToolCallRequest) -> ToolCallResult:
        return failed_result(request, "TTS adapter is disabled")


class KokoroOnnxTTSAdapter:
    def __init__(self, config: TTSAdapterConfig) -> None:
        self.config = config
        self._engine: Any | None = None

    async def execute(self, request: ToolCallRequest) -> ToolCallResult:
        try:
            output = await self.synthesize(
                str(request.input.get("text") or ""),
                voice=request.input.get("voice"),
                output_name=request.input.get("output_name"),
            )
        except Exception as exc:
            return failed_result(request, str(exc))
        output["operation"] = "synthesize"
        output["terminal_output"] = [
            {
                "content": output.get("summary") or f"Synthesized speech to {output.get('path')}.",
                "is_final": True,
                "exit_code": 0,
            }
        ]
        return ToolCallResult(request_id=request.id, status=ToolResultStatus.SUCCEEDED, output=output)

    async def synthesize(self, text: str, *, voice: str | None = None, output_name: str | None = None) -> dict[str, Any]:
        text = text.strip()
        if not text:
            raise ValueError("text is required")
        return await asyncio.wait_for(
            asyncio.to_thread(self._synthesize_sync, text, voice, output_name),
            timeout=self.config.timeout_seconds,
        )

    def _synthesize_sync(self, text: str, voice: str | None, output_name: str | None) -> dict[str, Any]:
        engine = self._get_engine()
        selected_voice = voice or self.config.voice
        samples, sample_rate = engine.create(
            text,
            voice=selected_voice,
            speed=self.config.speed,
            lang=self.config.language,
        )
        output_dir = Path(self.config.output_dir).expanduser().resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        filename = _safe_wav_name(output_name or f"tts_{uuid4().hex}.wav")
        path = output_dir / filename
        _write_wav(path, samples, int(sample_rate))
        return {
            "path": str(path),
            "voice": selected_voice,
            "provider": "kokoro_onnx",
            "sample_rate": int(sample_rate),
            "summary": f"Synthesized {len(text)} character(s) to {path.name}.",
        }

    def _get_engine(self) -> Any:
        if self._engine is not None:
            return self._engine
        if not self.config.model_path or not self.config.voices_path:
            raise RuntimeError("Kokoro ONNX TTS requires adapters.tts.model_path and adapters.tts.voices_path")
        try:
            from kokoro_onnx import Kokoro
        except ImportError as exc:
            raise RuntimeError(
                "TTS provider kokoro_onnx is enabled, but the kokoro-onnx package is not installed. "
                "Install the backend voice extra or run: python -m pip install kokoro-onnx"
            ) from exc
        self._engine = Kokoro(self.config.model_path, self.config.voices_path)
        return self._engine


def build_tts_adapter(config: TTSAdapterConfig) -> TTSAdapter:
    if not config.enabled:
        return DisabledTTSAdapter()
    provider = config.provider.strip().lower()
    if provider in {"kokoro_onnx", "kokoro-onnx", "kokoro"}:
        return KokoroOnnxTTSAdapter(config)
    raise RuntimeError(f"Unsupported TTS provider: {config.provider}")


def _write_wav(path: Path, samples: Any, sample_rate: int) -> None:
    try:
        import numpy as np
    except ImportError as exc:
        raise RuntimeError("Kokoro ONNX TTS output writing requires numpy") from exc

    array = np.asarray(samples)
    if array.ndim > 1:
        array = array.reshape(-1)
    if array.dtype.kind == "f":
        array = np.clip(array, -1.0, 1.0)
        array = (array * 32767.0).astype(np.int16)
    else:
        array = array.astype(np.int16)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(array.tobytes())


def _safe_wav_name(value: str) -> str:
    name = Path(value).name.strip() or f"tts_{uuid4().hex}.wav"
    if not name.lower().endswith(".wav"):
        name = f"{name}.wav"
    return "".join(char if char.isalnum() or char in {"-", "_", "."} else "_" for char in name)




def register(deps: RegistryDeps, definitions: Definitions, adapters: Adapters) -> None:
    settings = deps.settings
    enabled = capability_enabled(settings, Capability.TTS_SYNTHESIZE) and settings.adapters.tts.enabled
    definitions.append(
        ToolDefinition(
            name="tts.synthesize",
            group="voice",
            provides=('speak', 'voice', 'read', 'aloud', 'audio', 'speech', 'say', 'narrate',),
            capability=Capability.TTS_SYNTHESIZE,
            enabled=enabled,
            description="synthesize local speech audio with the configured Kokoro ONNX runtime",
            operations=("synthesize",),
            input_schema=TTSSynthesizeInput,
            output_schema=TTSSynthesizeOutput,
            default_operation="synthesize",
        )
    )
    if settings.adapters.tts.enabled:
        adapters["tts.synthesize"] = build_tts_adapter(settings.adapters.tts)
