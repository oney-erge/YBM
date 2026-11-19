"""Monitors: a script the assistant wrote and saved, run on a schedule by a watch (jobs/watch.py, target "script:<name>"), speaking only when what it prints changes.

"Connect to every device on my network and tell me when a new one shows up" has no tool. The assistant writes the script that does it. Saving one is a step with rules:
the script is Python, small, written into this folder under a plain name, and **run once at once as a test**: it must finish within the time limit, exit cleanly and print
at least one line. A script that fails its test is not kept, and the person is told why. Afterwards the watch only ever runs a script from this folder, by name, with a time
limit and a cap on what it may print; nothing outside the folder is run. The script is the assistant's own code on the owner's own PC: it can do what Python can do, so the
rule that matters is the one in the skill (read only: it looks, it never changes anything) and that the person approved the watch.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

MAX_SCRIPT_CHARS = 20_000
RUN_SECONDS = 60
MAX_OUTPUT_CHARS = 40_000
_NAME = re.compile(r"[^a-z0-9_-]+")
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def clean_name(name: str) -> str:
    return _NAME.sub("-", (name or "").lower()).strip("-")[:60]


def script_path(folder: str | Path, name: str) -> Path:
    cleaned = clean_name(name)
    if not cleaned:
        raise ValueError("a monitor needs a plain name (letters, digits, dashes)")
    return Path(folder) / f"{cleaned}.py"


def run_script(folder: str | Path, name: str, *, seconds: int = RUN_SECONDS) -> str:
    """Run a saved monitor script and return what it printed. Raises with the end of its error text when it fails, prints nothing, or takes too long."""
    path = script_path(folder, name)
    if not path.is_file():
        raise RuntimeError(f"the monitor script {path.name} does not exist")
    try:
        done = subprocess.run(  # noqa: S603 - this interpreter and a file in the monitors folder, no shell
            [sys.executable, str(path)], capture_output=True, timeout=seconds, cwd=str(path.parent), creationflags=_NO_WINDOW, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"the script took longer than {seconds} seconds") from exc
    out = (done.stdout or b"").decode("utf-8", errors="replace")[:MAX_OUTPUT_CHARS]
    if done.returncode != 0:
        tail = (done.stderr or b"").decode("utf-8", errors="replace").strip().splitlines()[-4:]
        raise RuntimeError(f"the script failed (exit {done.returncode}): {' | '.join(tail)[:400]}")
    if not out.strip():
        raise RuntimeError("the script printed nothing: it must print one line for each thing it sees")
    return out


def save_and_test(folder: str | Path, name: str, code: str) -> tuple[Path, str]:
    """Write the script and run it once. (the path, what it printed). A script that fails its test is removed and the reason is raised."""
    if len(code) > MAX_SCRIPT_CHARS:
        raise ValueError(f"the script is too long ({len(code)} characters; at most {MAX_SCRIPT_CHARS})")
    if not code.strip():
        raise ValueError("the script is empty")
    path = script_path(folder, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    existed = path.exists()
    previous = path.read_text(encoding="utf-8") if existed else None
    path.write_text(code, encoding="utf-8")
    try:
        output = run_script(folder, name)
    except Exception:
        if previous is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(previous, encoding="utf-8")
        raise
    return path, output
