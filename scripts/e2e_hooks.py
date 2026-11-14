"""Things a case can ask the E2E runner to do to the machine around a message: the situations a person's computer really is in.

A case lists them as {"hook": <name>, ...} in "before" (sent before the first message) or "after_initial" (after the first turn
finishes). The runner knows `wait_for_message` itself, because it owns the audit log; everything else is here and touches only the
fixture folders, the model server process, and one file handle.
"""

from __future__ import annotations

import ctypes
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

LOCALDEPLOY_HEALTH = "http://127.0.0.1:8000/health"


def _healthy(url: str, timeout: float = 3.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310 - loopback only
            return 200 <= response.status < 300
    except Exception:  # noqa: BLE001 - down means down
        return False


def stop_localdeploy(spec: dict, fixtures: dict[str, str]) -> str:
    """Kill the model server and its model process, as a crash would. The launcher's supervisor may bring it back; that is the test."""
    script = (
        "$ids = @(); "
        "$ids += (Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess); "
        "$ids += (Get-Process -Name llama-server -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Id); "
        "$ids | Sort-Object -Unique | ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }; $ids.Count"
    )
    done = subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True, text=True, timeout=60)
    time.sleep(2)
    return f"stopped the model server ({done.stdout.strip() or '0'} process(es)); healthy now: {_healthy(LOCALDEPLOY_HEALTH)}"


def start_localdeploy(spec: dict, fixtures: dict[str, str]) -> str:
    """Wait for the model server to be back; if the supervisor has not brought it up, ask the launcher to."""
    deadline = time.monotonic() + float(spec.get("seconds", 240))
    nudged = False
    started = time.monotonic()
    while time.monotonic() < deadline:
        if _healthy(LOCALDEPLOY_HEALTH):
            return f"model server healthy again after {time.monotonic() - started:.0f}s" + (" (the launcher was asked)" if nudged else "")
        if not nudged and time.monotonic() - started > 45:
            root = Path(__file__).resolve().parents[1]
            subprocess.run(["powershell", "-NoProfile", "-File", str(root / "scripts" / "ybm.ps1"), "start"], capture_output=True, timeout=300)
            nudged = True
        time.sleep(5)
    return "the model server did not come back in time"


def drop_file(spec: dict, fixtures: dict[str, str]) -> str:
    path = Path(spec["path"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(spec.get("text") or "x"), encoding="utf-8")
    return f"wrote {path.name}"


def hold_file_open(spec: dict, fixtures: dict[str, str]) -> str:
    """Hold a file open with no sharing, as Excel does, for a number of seconds, in the background."""
    if sys.platform != "win32":
        return "not on Windows: nothing held"
    path = str(spec["path"])
    seconds = float(spec.get("seconds", 60))
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    kernel32.CreateFileW.restype = ctypes.c_void_p
    generic_read_write, open_existing, no_share = 0xC0000000, 3, 0
    handle = kernel32.CreateFileW(path, generic_read_write, no_share, None, open_existing, 0x80, None)
    if handle in (None, ctypes.c_void_p(-1).value):
        return f"could not hold {Path(path).name} open"

    def release() -> None:
        time.sleep(seconds)
        kernel32.CloseHandle(ctypes.c_void_p(handle))

    threading.Thread(target=release, daemon=True).start()
    return f"holding {Path(path).name} open for {seconds:.0f}s"


HOOKS = {
    "stop_localdeploy": stop_localdeploy,
    "start_localdeploy": start_localdeploy,
    "drop_file": drop_file,
    "hold_file_open": hold_file_open,
}


def run_hook(spec: dict, fixtures: dict[str, str], render) -> str:
    """Run one hook; `render` fills {{names}} from the fixtures in its string values."""
    name = str(spec.get("hook") or "")
    if name not in HOOKS:
        return f"unknown hook {name!r}"
    rendered = {key: render(value, fixtures) if isinstance(value, str) else value for key, value in spec.items()}
    return HOOKS[name](rendered, fixtures)
