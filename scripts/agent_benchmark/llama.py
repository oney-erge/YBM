"""Start and stop a llama-server for one benchmark configuration, and read what it cost.

Used by fit_speed.py and replay.py. Windows-first (the target machine), but nothing here is Windows-only
except the process-tree kill and the WinGet lookup.
"""

from __future__ import annotations

import glob
import os
import shutil
import subprocess
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ServerConfig:
    """One way of running one model: the file plus the llama-server flags that decide where it runs."""

    name: str
    gguf: str
    args: list[str] = field(default_factory=list)
    context: int = 16384


def find_llama_server(explicit: str | None = None) -> str:
    candidates = [explicit, os.environ.get("LLAMACPP_SERVER_PATH"), shutil.which("llama-server")]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates += sorted(
            glob.glob(os.path.join(local, "Microsoft", "WinGet", "Packages", "ggml.llamacpp*", "llama-server.exe")),
            reverse=True,
        )
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(candidate)
    raise FileNotFoundError("llama-server not found: install llama.cpp (winget install ggml.llamacpp) or pass --server")


def build_command(server: str, config: ServerConfig, port: int) -> list[str]:
    return [
        server, "-m", config.gguf, "--host", "127.0.0.1", "--port", str(port), "-c", str(config.context),
        "--no-webui", *config.args,
    ]


def gpu_used_mib() -> int | None:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=15, check=True,
        ).stdout.strip().splitlines()[0]
        return int(out)
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None


def _healthy(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3) as response:
            return response.status == 200
    except OSError:
        return False


def start(config: ServerConfig, port: int, log_path: Path, server: str | None = None, timeout_s: int = 900):
    """Launch llama-server and wait until it answers /health. Returns (process, seconds_to_ready)."""
    command = build_command(find_llama_server(server), config, port)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = open(log_path, "wb")
    started = time.monotonic()
    process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, creationflags=0x08000000)  # no window
    while time.monotonic() - started < timeout_s:
        if process.poll() is not None:
            log.close()
            tail = log_path.read_text(encoding="utf-8", errors="replace")[-1500:]
            raise RuntimeError(f"llama-server for {config.name} exited with {process.returncode}:\n{tail}")
        if _healthy(port):
            return process, time.monotonic() - started
        time.sleep(1.0)
    stop(process)
    raise TimeoutError(f"llama-server for {config.name} was not ready after {timeout_s}s")


def stop(process: subprocess.Popen) -> None:
    """Kill the server and anything it started."""
    if process.poll() is None:
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, timeout=30)
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            pass
    time.sleep(2.0)  # let the driver release VRAM before the next model loads
