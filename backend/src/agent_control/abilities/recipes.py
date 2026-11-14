"""Ways to get a capability the assistant does not have yet: what is fetched, from where, how big it is, and what is switched on.

A request for something this assistant cannot do yet (read an answer aloud, say what is in a recording, make a video, look at a picture) used
to end in "I can't". Several of those are a small download away. A recipe is that knowledge as data, next to the gaps (`gaps.py`): the
capability it gives, the words people use for it, where each file comes from and its hash, and the configuration that switches the capability
on once the files are there. It is data about a capability, never a second way to run one: `tools/ability_acquire.py` carries a recipe out
with the user's approval, and everything it produces is used through the tools that already exist (`tts.synthesize`, `audio.transcribe`,
`image.describe`, a program run with `code.interpreter`).

Recipes are curated. A file comes from the address written here and is checked against the hash written here; nothing a model or a message
says can name another source. Installing an arbitrary package is `dependencies.install`, with its own allowlist.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from agent_control.abilities.model import NEED_APPROVAL, Ability, AbilityKind, AbilityState
from agent_control.config import AppSettings

#: A recipe at or above this size is always put to the user, whatever the model decides: a download that big is not a small thing to do
#: without being asked. Below it, whether to ask is the model's judgment (`ask_first`).
ASK_ALWAYS_MB = 500


@dataclass(frozen=True)
class Download:
    url: str
    name: str
    sha256: str
    size_bytes: int


@dataclass(frozen=True)
class Recipe:
    id: str
    #: What it is, as a noun phrase a person would say: "a local voice".
    title: str
    #: What it gives, in the words people use for it.
    summary: str
    provides: tuple[str, ...]
    #: A yes/no question about a request, so the notes mention this only when the user's request is about it.
    confirm: str
    size_mb: int
    #: Where it comes from, said to the user: "github.com (the kokoro-onnx model files)".
    source: str
    #: Said to the user when it is done and by the model when it plans the next step.
    after: str
    ready: Callable[[AppSettings], bool]
    downloads: tuple[Download, ...] = ()
    #: Configuration to switch on, as a nested mapping merged into config.yaml, given the folder the downloads went to.
    config: Callable[[Path], dict[str, Any]] | None = None
    #: A command run after the downloads (winget, ollama): the whole argument list, never built from a request.
    command: tuple[str, ...] = ()
    command_timeout_seconds: int = 1800
    #: A service the command needs running first ("ollama"): started if it is not, because `ollama pull` fails without its server.
    needs_service: str = ""
    #: True when it changes the machine outside this assistant's own folders (a program installed for the user).
    changes_system: bool = False
    #: Whether the user must be asked first, whatever the model decides.
    ask_always: bool = False

    @property
    def must_ask(self) -> bool:
        return self.ask_always or self.changes_system or self.size_mb >= ASK_ALWAYS_MB

    def folder(self, models_dir: Path) -> Path:
        return models_dir / self.id.replace(".", "-")


# --- how each one is known to be there ---------------------------------------------------------------------------------------------------


def _voice_ready(settings: AppSettings) -> bool:
    tts = settings.adapters.tts
    return bool(tts.enabled and tts.model_path and tts.voices_path and Path(tts.model_path).exists() and Path(tts.voices_path).exists())


def _speech_ready(settings: AppSettings) -> bool:
    return bool(settings.adapters.stt.enabled)


def winget_ffmpeg() -> Path | None:
    """ffmpeg as winget installs it: a program the running process cannot find on its PATH until it is restarted."""
    base = Path.home() / "AppData" / "Local" / "Microsoft" / "WinGet" / "Packages"
    for candidate in sorted(base.glob("Gyan.FFmpeg*/ffmpeg-*/bin/ffmpeg.exe")) if base.exists() else []:
        return candidate
    return None


def _video_ready(settings: AppSettings) -> bool:
    return shutil.which("ffmpeg") is not None or winget_ffmpeg() is not None


def _ollama_has(prefix: str) -> bool:
    """Whether Ollama, if it is running, has a model with this name. Asked over HTTP: the command line starts Ollama as a side effect."""
    try:
        reply = httpx.get("http://127.0.0.1:11434/api/tags", timeout=1.5)
        return any(str(model.get("name", "")).startswith(prefix) for model in reply.json().get("models", []))
    except (httpx.HTTPError, ValueError):
        return False


def _vision_ready(settings: AppSettings) -> bool:
    return _ollama_has("qwen3-vl")


# --- the recipes ------------------------------------------------------------------------------------------------------------------------

KOKORO_BASE = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"

RECIPES: tuple[Recipe, ...] = (
    Recipe(
        id="voice.kokoro",
        title="a local voice",
        summary="speak: read an answer aloud, say something in a voice, make an audio file of text",
        provides=("speak", "voice", "aloud", "audio", "say", "read", "narrate", "speech", "listen", "voicemessage", "tts"),
        confirm="Does the user want the assistant to speak, read something aloud, or send a voice message or audio of text?",
        size_mb=120,
        source="github.com (the Kokoro model files, run on this computer)",
        after="The voice is set up. Make the audio with tts.synthesize (text in, a .wav file out) and send it with artifact.deliver.",
        ready=_voice_ready,
        downloads=(
            Download(f"{KOKORO_BASE}/kokoro-v1.0.int8.onnx", "kokoro-v1.0.int8.onnx", "6e742170d309016e5891a994e1ce1559c702a2ccd0075e67ef7157974f6406cb", 92361271),
            Download(f"{KOKORO_BASE}/voices-v1.0.bin", "voices-v1.0.bin", "bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d", 28214398),
        ),
        config=lambda folder: {
            "adapters": {"tts": {"enabled": True, "model_path": str(folder / "kokoro-v1.0.int8.onnx"), "voices_path": str(folder / "voices-v1.0.bin")}},
            "capabilities": {"tts.synthesize": {"enabled": True, "requires_approval": False, "max_risk_level": "low"}},
        },
    ),
    Recipe(
        id="speech.whisper",
        title="speech to text for audio files",
        summary="say what is spoken in a recording: a voicemail, a meeting, a voice note saved as a file",
        provides=("transcribe", "recording", "voicemail", "meeting", "audio", "mp3", "wav", "listen", "speech", "minutes", "dictation"),
        confirm="Does the user ask what is said in an audio recording, a voicemail or a meeting recording?",
        size_mb=150,
        source="huggingface.co (the Whisper base model, fetched the first time it is used, run on this computer)",
        after="Speech recognition is on. Use audio.transcribe with the path of the recording.",
        ready=_speech_ready,
        config=lambda folder: {"adapters": {"stt": {"enabled": True}}},
    ),
    Recipe(
        id="video.ffmpeg",
        title="a video and audio tool (ffmpeg)",
        summary="make, convert or edit video and audio: a slideshow from photos, an mp4, an mp3, a clip",
        provides=("video", "slideshow", "mp4", "mp3", "convert", "clip", "movie", "ffmpeg", "gif", "audio", "render"),
        confirm="Does the user want a video made, converted or edited, or audio converted?",
        size_mb=95,
        source="winget (the Gyan.FFmpeg package)",
        after="ffmpeg is installed. Run it from a program with code.interpreter, using the path in the result if `ffmpeg` is not found by name.",
        ready=_video_ready,
        command=("winget", "install", "--id", "Gyan.FFmpeg", "-e", "--silent", "--accept-source-agreements", "--accept-package-agreements"),
        command_timeout_seconds=900,
        changes_system=True,
    ),
    Recipe(
        id="vision.qwen3vl",
        title="a vision model",
        summary="look at pictures: read a scanned page, a screenshot, a photo of a menu or a receipt",
        provides=("picture", "photo", "image", "screenshot", "scan", "scanned", "see", "look", "menu", "receipt", "ocr"),
        confirm="Does the user ask what is in a picture, a photo, a screenshot or a scanned page?",
        size_mb=3300,
        source="ollama.com (the Qwen3-VL 4B model, run on this computer)",
        after="The vision model is installed. Use image.describe with the path of the picture.",
        ready=_vision_ready,
        command=("ollama", "pull", "qwen3-vl:4b-instruct"),
        command_timeout_seconds=3600,
        needs_service="ollama",
    ),
)

_BY_ID = {recipe.id: recipe for recipe in RECIPES}


def recipe_by_id(recipe_id: str) -> Recipe | None:
    return _BY_ID.get(str(recipe_id or "").strip())


def recipe_abilities(settings: AppSettings) -> list[Ability]:
    """One record for each recipe whose capability is not there yet, so the same search that finds a tool finds what could be set up."""
    abilities = []
    for recipe in RECIPES:
        try:
            if recipe.ready(settings):
                continue
        except Exception:  # noqa: BLE001 - a check that cannot run says nothing, it does not break the index
            continue
        asking = (
            "Ask the user first (a large download or a program installed on this computer): set ask_first true."
            if recipe.must_ask
            else "It is a small download into this assistant's own folder. Ask first (ask_first true) if you think the user would want to be asked."
        )
        abilities.append(
            Ability(
                id=f"model:{recipe.id}",
                kind=AbilityKind.MODEL,
                name=recipe.title,
                summary=recipe.summary,
                group="setup",
                state=AbilityState.INSTALLABLE,
                provides=recipe.provides,
                detail=f"about {recipe.size_mb} MB from {recipe.source}",
                call={"tool": "ability.acquire", "input": {"operation": "install", "recipe": recipe.id}},
                needs=(NEED_APPROVAL,),
                guidance=(
                    f"Not set up yet. Say so plainly, then get it with ability.acquire (about {recipe.size_mb} MB from {recipe.source}) and "
                    f"carry on with the request. {asking}"
                ),
                confirm=recipe.confirm,
                confirm_at=0.6,
                source="recipes",
            )
        )
    return abilities
