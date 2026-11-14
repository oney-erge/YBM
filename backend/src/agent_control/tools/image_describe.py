"""See what is in a picture: the assistant's local text model cannot, so a vision model is asked, with the user's approval.

The model that runs the assistant on this machine reads text only. A photo of an invoice, a screenshot of an error, a picture the user
sends from their phone were answered with "a JPEG, 8 KB". A small vision model (Qwen3-VL 4B through LocalDeploy and Ollama reads
"INVOICE 1234 TOTAL 56.78 USD" off a test picture in three seconds once loaded) can look. It is not the assistant's own model and it
takes a few gigabytes of an 8 GB GPU, so each use is an approval the user sees ("Load a vision model to look at invoice.jpg") and can grant
for the task. Nothing is sent outside this machine: the picture goes to LocalDeploy on this computer.

The model is found in LocalDeploy, not named in code: the smallest vision profile it lists, or the one the user chose (`vision.profile`).
"""

from __future__ import annotations

import asyncio
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

import httpx
import structlog

from agent_control.config import AppSettings, LLMProfileConfig
from agent_control.llm.providers import OpenAICompatibleProvider
from agent_control.schemas import Capability, ToolCallRequest, ToolCallResult, ToolResultStatus
from agent_control.tools.contracts import ImageDescribeInput, ImageDescribeOutput
from agent_control.tools.spec import Adapters, Definitions, RegistryDeps, ToolDefinition, capability_enabled, failed_result, same_output_schema

logger = structlog.get_logger(__name__)

IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"})
MAX_IMAGE_BYTES = 10 * 1024 * 1024
#: A scanned document is a PDF that is only pictures of pages; its pages are looked at one by one, the first few, because each is a model call.
MAX_PDF_BYTES = 40 * 1024 * 1024
MAX_PDF_PAGES = 3
MAX_PAGE_PIXELS = 2000
SCRATCH_DIR = Path(".agent_control/tmp/vision")
SYSTEM_PROMPT = (
    "You describe pictures accurately for someone who cannot see them. Say what is shown, quote any text exactly as written, and say plainly "
    "what you cannot tell. Do not guess names, numbers or details that are not visible."
)
DEFAULT_QUESTION = "Describe this picture and quote any text that is in it."
#: How long the first call may take: the model has to be loaded from disk, and the main model may be using the GPU.
VISION_TIMEOUT_SECONDS = 240
OLLAMA_START_WAIT_SECONDS = 25.0

Listing = Callable[[str], Awaitable[list[dict[str, Any]]]]


class ModelSwap(Protocol):
    async def make_room(self, root: str) -> None: ...

    async def release(self, root: str, profile: str) -> None: ...


class LocalDeploySwap:
    """Give the vision model the whole GPU for the look, then give it back.

    An 8 GB GPU holds the assistant's model (6.5 GB) or a vision model (3 GB) with room to work, not both: loading the second made the two evict
    each other, so each look took one to two minutes (the first challenge run: a screen description took 9 minutes) and a message that arrived
    meanwhile waited more than three minutes to be taken up. LocalDeploy is asked to free the GPU first (it stops the main model), and to unload
    the vision model after, so the main model loads once into free memory at the next step that needs it. Best effort: if LocalDeploy cannot do
    either, the look goes ahead as it did.
    """

    async def make_room(self, root: str) -> None:
        await self._post(f"{root}/models/free", {})

    async def release(self, root: str, profile: str) -> None:
        await self._post(f"{root}/models/stop", {"profile": profile})

    @staticmethod
    async def _post(url: str, body: dict[str, Any]) -> None:
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                await client.post(url, json=body)
        except httpx.HTTPError:
            logger.info("vision_model_swap_unavailable", url=url, exc_info=True)


def localdeploy_root(settings: AppSettings) -> str | None:
    """LocalDeploy's address, from the default model's profile (its base_url ends in /v1)."""
    profile = settings.llm.profiles.get(settings.llm.default_profile)
    url = (profile.base_url or "").rstrip("/") if profile else ""
    return re.sub(r"/v1$", "", url) or None


async def list_vision_profiles(root: str) -> list[dict[str, Any]]:
    """The profiles LocalDeploy offers that can look at pictures, smallest first (a 4B model before a 12B one: the GPU is shared).

    LocalDeploy lists a profile whether or not its model has been downloaded, so each Ollama profile says whether its model is installed:
    True, False, or None when Ollama could not be asked (it is not running).
    """
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(f"{root}/v1/models")
        response.raise_for_status()
        installed: set[str] | None = None
        try:
            models = (await client.get(f"{root}/models")).json().get("ollama", {})
            if not models.get("error"):
                installed = {str(name) for name in models.get("models", [])}
        except (httpx.HTTPError, ValueError):
            pass
    found = []
    for entry in response.json().get("data", []):
        details = entry.get("localdeploy") or {}
        if not details.get("supports_vision") or details.get("kind") == "decision":
            continue
        model = str(details.get("model_id") or "")
        backend = str(details.get("backend") or "")
        found.append({
            "profile": entry.get("id"), "model": model, "backend": backend,
            "installed": (model in installed if installed is not None else None) if backend == "ollama" else True,
        })
    return sorted(found, key=lambda item: (_size_in_billions(item["model"]), item["profile"]))


def scanned_pages(path: Path) -> tuple[list[Path], Path]:
    """The page pictures of a scanned PDF, as PNG files in a scratch folder (which the caller removes), first few pages only.

    A scanner makes a PDF whose every page is one picture, so the picture is already inside the file and is read out of it (no renderer is
    needed). A PDF whose pages are text has no such picture: its text is read with the document tool, and this says so.
    """
    from pypdf import PdfReader

    try:
        reader = PdfReader(str(path))
        pages = list(reader.pages[:MAX_PDF_PAGES])
    except Exception as exc:  # noqa: BLE001 - a damaged or locked PDF is said, not raised
        raise ValueError(f"{path.name} could not be opened as a PDF ({type(exc).__name__})") from exc
    scratch = SCRATCH_DIR / uuid4().hex
    scratch.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []
    for number, page in enumerate(pages, 1):
        try:
            images = list(page.images)
        except Exception:  # noqa: BLE001 - a page whose pictures cannot be read has none to look at
            images = []
        if not images:
            continue
        # The picture of the page is the biggest one on it (a logo or a stamp is not).
        biggest = max(images, key=lambda item: item.image.width * item.image.height)
        picture = biggest.image.convert("RGB")
        picture.thumbnail((MAX_PAGE_PIXELS, MAX_PAGE_PIXELS))
        target = scratch / f"page-{number}.png"
        picture.save(target, "PNG")
        saved.append(target)
    if not saved:
        shutil.rmtree(scratch, ignore_errors=True)
        raise ValueError(f"{path.name} has no scanned page pictures to look at. If it has text, read it with document.manage extract_text.")
    return saved, scratch


def _size_in_billions(model: str) -> float:
    match = re.search(r"(\d+(?:\.\d+)?)b\b", model.lower())
    return float(match.group(1)) if match else 99.0


class ImageDescribeAdapter:
    def __init__(
        self,
        settings: AppSettings,
        path_guard: Callable[[str], Path],
        *,
        listing: Listing = list_vision_profiles,
        provider_factory: Callable[[LLMProfileConfig], Any] = OpenAICompatibleProvider,
        swap: ModelSwap | None = None,
    ) -> None:
        self.settings = settings
        self._swap = swap if settings.vision.swap_models else None
        self._path_guard = path_guard
        self._listing = listing
        self._provider_factory = provider_factory

    async def execute(self, request: ToolCallRequest) -> ToolCallResult:
        try:
            path = self._path_guard(str(request.input["path"]))
        except ValueError as exc:
            return failed_result(request, str(exc))
        scanned = path.suffix.lower() == ".pdf"
        if (path.suffix.lower() not in IMAGE_SUFFIXES and not scanned) or not path.is_file():
            return failed_result(request, f"{path.name} is not a picture file I can look at (png, jpg, gif, webp, bmp, or a scanned PDF)")
        limit = MAX_PDF_BYTES if scanned else MAX_IMAGE_BYTES
        if path.stat().st_size > limit:
            return failed_result(request, f"{path.name} is larger than {limit // (1024 * 1024)} MB; send a smaller copy")
        pictures, scratch = [path], None
        if scanned:
            try:
                pictures, scratch = await asyncio.to_thread(scanned_pages, path)
            except ValueError as exc:
                return failed_result(request, str(exc))
        root = localdeploy_root(self.settings)
        if root is None:
            return failed_result(request, "no vision model server is configured: the default model has no address to ask LocalDeploy at")
        try:
            profile_name = await self._profile_name(root)
        except ValueError as exc:
            return failed_result(request, str(exc))
        except Exception as exc:  # noqa: BLE001 - say what is wrong, in words the user can act on
            return failed_result(request, f"I could not ask LocalDeploy which vision models it has ({type(exc).__name__}). Is it running?")
        question = str(request.input.get("question") or "").strip() or DEFAULT_QUESTION
        provider = self._provider_factory(
            LLMProfileConfig(model=profile_name, base_url=f"{root}/v1", timeout_seconds=VISION_TIMEOUT_SECONDS, max_tokens=700, temperature=0.1)
        )
        if self._swap is not None:
            await self._swap.make_room(root)
        try:
            seen = [(await self._ask(provider, question, picture, root)).strip() for picture in pictures]
        except Exception as exc:  # noqa: BLE001
            return failed_result(request, f"the vision model ({profile_name}) could not look at {path.name}: {_reason(exc)}")
        finally:
            if scratch is not None:
                shutil.rmtree(scratch, ignore_errors=True)
            if self._swap is not None:
                await self._swap.release(root, profile_name)
        text = "\n\n".join(f"Page {number}: {page}" for number, page in enumerate(seen, 1)) if scanned else seen[0]
        text = text.strip()
        if not text:
            return failed_result(request, f"the vision model ({profile_name}) returned nothing for {path.name}")
        output: dict[str, Any] = {
            "operation": "describe", "summary": text[:240], "description": text, "model": profile_name, "file": str(path),
            "terminal_output": [{"content": text, "exit_code": 0, "is_final": True}],
        }
        return ToolCallResult(request_id=request.id, status=ToolResultStatus.SUCCEEDED, output=output)

    async def _profile_name(self, root: str) -> str:
        """The profile to use: the one the user chose, or the smallest whose model is installed. Says what is missing when there is none."""
        chosen = self.settings.vision.profile.strip()
        if chosen:
            return chosen
        profiles = await self._listing(root)
        if any(p["installed"] is None for p in profiles) and self.settings.vision.start_ollama and await _start_ollama():
            profiles = await self._listing(root)  # Ollama could not be asked because it was not running; it is now
        if not profiles:
            raise ValueError("LocalDeploy has no vision model set up. Add a vision profile for a model such as qwen3-vl:4b-instruct.")
        usable = [p for p in profiles if p["installed"] is not False]
        if not usable:
            names = ", ".join(sorted({p["model"] for p in profiles})[:3])
            raise ValueError(f"none of the vision models LocalDeploy lists is installed in Ollama ({names}). Pull one, for example: ollama pull qwen3-vl:4b-instruct")
        return str(usable[0]["profile"])

    async def _ask(self, provider: Any, question: str, path: Path, root: str) -> str:
        try:
            return await provider.generate_multimodal_text(SYSTEM_PROMPT, question, [str(path)])
        except Exception as exc:
            # The model server behind LocalDeploy is not running: start it once, as part of loading the model the user just approved.
            if self.settings.vision.start_ollama and "ollama is not running" in str(exc).lower() and await _start_ollama():
                return await provider.generate_multimodal_text(SYSTEM_PROMPT, question, [str(path)])
            raise


def _reason(exc: Exception) -> str:
    text = " ".join(str(exc).split())
    return text[:240] or type(exc).__name__


async def _start_ollama() -> bool:
    """Start `ollama serve` detached, when it is installed and not running, and wait until it answers. True if it came up."""
    executable = shutil.which("ollama") or str(Path.home() / "AppData" / "Local" / "Programs" / "Ollama" / "ollama.exe")
    if not Path(executable).exists():
        return False
    flags = (subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS) if sys.platform == "win32" else 0  # type: ignore[attr-defined]
    try:
        subprocess.Popen([executable, "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)  # noqa: S603
    except OSError:
        return False
    deadline = time.monotonic() + OLLAMA_START_WAIT_SECONDS
    async with httpx.AsyncClient(timeout=2) as client:
        while time.monotonic() < deadline:
            try:
                if (await client.get("http://127.0.0.1:11434/api/tags")).status_code == 200:
                    return True
            except httpx.HTTPError:
                pass
            await asyncio.sleep(1)
    return False


def register(deps: RegistryDeps, definitions: Definitions, adapters: Adapters) -> None:
    settings = deps.settings
    guard = getattr(adapters.get("filesystem.manage"), "resolve_allowed", None)
    enabled = settings.vision.enabled and guard is not None and capability_enabled(settings, Capability.FILESYSTEM_READ)
    shown = settings.vision.profile or "the smallest vision model LocalDeploy has"
    definitions.append(
        ToolDefinition(
            name="image.describe",
            group="vision",
            provides=("photo", "picture", "image", "screenshot", "scan", "jpg", "png", "look", "see", "read", "text", "diagram", "chart"),
            capability=Capability.FILESYSTEM_READ,
            enabled=enabled,
            named_when_disabled=False,
            description=(
                "look at a picture file (png, jpg, gif, webp) or a scanned PDF (a PDF that is only pictures of pages, which document.manage cannot read) "
                "and say what is in it, or answer a question about it, reading any text in it; "
                "the assistant's own model cannot see, so a local vision model is loaded for this and the user approves each use"
            ),
            operations=("describe",),
            input_schema=ImageDescribeInput,
            output_schema=ImageDescribeOutput,
            operation_output_schemas=same_output_schema(("describe",), ImageDescribeOutput),
            default_operation="describe",
            approval_required_operations=("describe",),
            approval_reasons={
                "describe": lambda value: (
                    f"Load a vision model ({shown}, about 3 GB of GPU memory) to look at {Path(str(value.get('path') or 'a picture')).name}. "
                    "The picture stays on this computer; the main model may pause while it runs."
                )
            },
            examples=(
                {"operation": "describe", "path": "{{picture_path}}", "question": "What does the text say?"},
                {"operation": "describe", "path": "{{picture_path}}"},
            ),
        )
    )
    if enabled:
        adapters["image.describe"] = ImageDescribeAdapter(settings, guard, swap=LocalDeploySwap())  # type: ignore[arg-type]
