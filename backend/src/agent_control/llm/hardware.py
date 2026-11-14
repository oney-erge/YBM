"""Best-effort local hardware detection, so model recommendations are earned.

The preset labels used to say "runs on this machine" without anyone having
looked at the machine. On a 4 GB GPU that is false and the user finds out by
watching it fail.

This is deliberately conservative. Every field is optional and "we could not
tell" is a first-class answer - a wrong recommendation is worse than none,
because the user acts on it. In particular, inside a container the host GPU is
usually invisible, so `detected` comes back False rather than reporting the
container's own view as if it were the machine's.

LocalDeploy has a much better version of this and it *is* reachable over HTTP:
`GET /system/hardware` (per-GPU name, vendor, backend, total **and free** VRAM,
driver, utilization, multi-GPU grouping) and `POST /system/fit-check`, backed by
per-GPU VRAM calibration learned from real runs. `probe()` asks it first and
only falls back to the detection below when LocalDeploy is not running.

Preferring it matters for more than richness: it reports *free* VRAM, so a card
already busy with something else does not get a model recommended into memory
it does not have.
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Hardware:
    detected: bool
    gpu_name: str | None = None
    #: The graphics memory a model could use now (free when the server says so): what decides which local model fits.
    vram_gb: float | None = None
    #: The card's own total. "Can this computer run a 70B model?" is about this, not about what is free right now: the live answer quoted
    #: 1.7 GB free as if it were the card, on an 8 GB card.
    vram_total_gb: float | None = None
    ram_gb: float | None = None
    #: Why detection gave up, shown to the user rather than swallowed.
    reason: str | None = None
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "detected": self.detected,
            "gpu_name": self.gpu_name,
            "vram_gb": self.vram_gb,
            "vram_total_gb": self.vram_total_gb,
            "ram_gb": self.ram_gb,
            "reason": self.reason,
            "notes": list(self.notes),
        }


def _in_container() -> bool:
    if os.environ.get("YBM_HEADLESS"):
        return True
    try:
        return Path("/.dockerenv").exists()
    except OSError:
        return False


def _run(command: list[str], timeout: int = 6) -> str | None:
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell
            command, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


def _nvidia_gpu() -> tuple[str, float] | None:
    """Ask nvidia-smi for the first GPU's name and total memory."""
    if not shutil.which("nvidia-smi"):
        return None
    out = _run(
        ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"]
    )
    if not out:
        return None
    first = out.strip().splitlines()[0] if out.strip() else ""
    parts = [p.strip() for p in first.split(",")]
    if len(parts) < 2:
        return None
    try:
        # nvidia-smi reports MiB with nounits.
        return parts[0], round(int(float(parts[1])) / 1024, 1)
    except ValueError:
        return None


def _system_ram_gb() -> float | None:
    try:
        if platform.system() == "Linux":
            text = Path("/proc/meminfo").read_text(encoding="utf-8")
            match = re.search(r"MemTotal:\s+(\d+) kB", text)
            if match:
                return round(int(match.group(1)) / (1024 * 1024), 1)
        elif platform.system() == "Windows":
            out = _run(["wmic", "computersystem", "get", "TotalPhysicalMemory"])
            if out:
                digits = re.findall(r"\d{6,}", out)
                if digits:
                    return round(int(digits[0]) / (1024**3), 1)
        elif platform.system() == "Darwin":
            out = _run(["sysctl", "-n", "hw.memsize"])
            if out and out.strip().isdigit():
                return round(int(out.strip()) / (1024**3), 1)
    except (OSError, ValueError):
        return None
    return None


#: Where LocalDeploy usually listens. The container entry is second because a
#: containerised YBM cannot reach the host on loopback.
_LOCALDEPLOY_BASE_URLS = ("http://127.0.0.1:8000", "http://host.docker.internal:8000")


def _from_localdeploy(payload: dict) -> Hardware | None:
    """Map LocalDeploy's /system/hardware payload onto our shape.

    It reports every GPU; we want the one a model would actually load onto, so
    the pick is the largest *free* VRAM, falling back to total when free is
    unknown (integrated GPUs often report only total).
    """
    if not payload.get("success"):
        return None
    gpus = [g for g in (payload.get("gpus") or []) if isinstance(g, dict)]
    # LocalDeploy calls it ram_total_mb; memory_total_mb was a guess and it
    # silently produced ram_gb=None against the real server.
    system = payload.get("system") if isinstance(payload.get("system"), dict) else {}
    ram_mb = system.get("ram_total_mb") or system.get("memory_total_mb")
    ram_gb = round(ram_mb / 1024, 1) if isinstance(ram_mb, (int, float)) and ram_mb else None

    if not gpus:
        return Hardware(
            detected=True,
            ram_gb=ram_gb,
            notes=["No GPU found - local models will run on the CPU and be slow."],
        )

    def _usable_mb(gpu: dict) -> float:
        free = gpu.get("vram_free_mb")
        total = gpu.get("vram_total_mb")
        return float(free if isinstance(free, (int, float)) else (total or 0))

    best = max(gpus, key=_usable_mb)
    total_mb = best.get("vram_total_mb")
    free_mb = best.get("vram_free_mb")
    notes: list[str] = []
    if isinstance(free_mb, (int, float)) and isinstance(total_mb, (int, float)) and total_mb:
        notes.append(f"{round(free_mb / 1024, 1):g} GB of {round(total_mb / 1024, 1):g} GB free right now.")
    if best.get("vram_estimated"):
        notes.append("Graphics memory is an estimate on this device.")
    if len(gpus) > 1:
        notes.append(f"{len(gpus)} GPUs found; using {best.get('name') or 'the largest'}.")

    # Fit is judged against what is actually available, not what exists.
    usable_gb = round(_usable_mb(best) / 1024, 1) if _usable_mb(best) else None
    return Hardware(
        detected=True,
        gpu_name=best.get("name"),
        vram_gb=usable_gb,
        vram_total_gb=round(total_mb / 1024, 1) if isinstance(total_mb, (int, float)) and total_mb else None,
        ram_gb=ram_gb,
        notes=notes,
    )


def probe_localdeploy(timeout: float = 2.5) -> Hardware | None:
    """Ask LocalDeploy about the machine. None when it is not running."""
    import httpx

    for base in _LOCALDEPLOY_BASE_URLS:
        try:
            response = httpx.get(f"{base}/system/hardware", timeout=timeout)
        except httpx.HTTPError:
            continue
        if response.status_code != 200:
            continue
        try:
            mapped = _from_localdeploy(response.json() or {})
        except ValueError:
            continue
        if mapped is not None:
            return mapped
    return None


def probe() -> Hardware:
    """Look at the machine, and be honest when we cannot.

    LocalDeploy first - it knows about free VRAM, multiple GPUs, and its own
    calibration data. Everything below is the fallback for when it is not
    running.
    """
    notes: list[str] = []

    from_localdeploy = probe_localdeploy()
    if from_localdeploy is not None:
        return from_localdeploy

    if _in_container():
        # The container's view is not the host's. Reporting it would be the
        # same class of mistake as the label this module exists to fix.
        return Hardware(
            detected=False,
            reason="Running in a container, so we cannot see this computer's graphics card.",
            notes=["Use an API key, or point YBM at a model server running on the host."],
        )

    gpu = _nvidia_gpu()
    ram = _system_ram_gb()

    if gpu is None:
        if ram is None:
            return Hardware(detected=False, reason="We could not detect this computer's hardware.")
        notes.append("No NVIDIA GPU found - local models will run on the CPU and be slow.")
        return Hardware(detected=True, ram_gb=ram, notes=notes)

    name, vram = gpu
    return Hardware(detected=True, gpu_name=name, vram_gb=vram, vram_total_gb=vram, ram_gb=ram, notes=notes)


#: Rough VRAM needed per preset, in GB. Weights plus KV cache plus overhead at
#: the context sizes these presets ship with. Deliberately generous: telling
#: someone a model fits when it does not is the failure being prevented.
PRESET_VRAM_GB: dict[str, float] = {
    "localdeploy_qwen3vl_8b": 7.0,
    "localdeploy_qwen3vl_8b_container": 7.0,
    "localdeploy_gemma3_12b": 10.0,
    "localdeploy_gemma3_4b": 4.0,
    "localdeploy_gemma3_4b_container": 4.0,
}


def fit(preset_key: str, hardware: Hardware) -> dict:
    """Whether a preset can run here, and the reason either way.

    Three outcomes, and "unknown" is a real one - it renders as a plain choice
    with no claim attached, rather than a recommendation we did not earn.
    """
    needed = PRESET_VRAM_GB.get(preset_key)
    if needed is None or not hardware.detected or hardware.vram_gb is None:
        return {"status": "unknown", "needed_gb": needed, "reason": None}
    if hardware.vram_gb + 0.001 >= needed:
        return {
            "status": "fits",
            "needed_gb": needed,
            "reason": f"Needs about {needed:g} GB; you have {hardware.vram_gb:g} GB.",
        }
    return {
        "status": "too_big",
        "needed_gb": needed,
        "reason": f"Needs about {needed:g} GB of graphics memory; this machine has {hardware.vram_gb:g} GB.",
    }
