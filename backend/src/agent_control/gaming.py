"""Gaming mode: the person says they are about to play, and the assistant gives the GPU back and stays off it until they say they are done.

The model that runs the assistant is loaded into the video memory of a laptop that is also the person's games machine. Unloading it on an
idle timer is not enough when they know they are about to play: they say so, and the assistant frees the GPU at once and does not load a
model again (not for a message, not to warm up, not for a scheduled job) until they say they are done. It is only ever turned on by the
person saying it: nothing here watches for a game.

The state is a file, because the intake, the worker and the scheduler are separate processes: while it exists, gaming mode is on. It lists
what was sent meanwhile, so the person is told when they come back and nothing is silently dropped.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import httpx

FLAG_PATH = Path(".agent_control/gaming_mode.json")
#: What is kept of the messages that arrive while it is on: the number and the length of each, so the flag file stays small.
MAX_HELD = 20
MAX_HELD_CHARS = 200


def _read(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def is_on(path: Path = FLAG_PATH) -> bool:
    return _read(path) is not None


def turn_on(path: Path = FLAG_PATH) -> bool:
    """True when it was off and is now on; False when it already was on (the state, and what is held, are kept)."""
    if is_on(path):
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"held": [], "gpu_freed": False}), encoding="utf-8")
    return True


def turn_off(path: Path = FLAG_PATH) -> list[str]:
    """Back to normal. Returns the messages that were sent while it was on."""
    state = _read(path) or {}
    path.unlink(missing_ok=True)
    return [str(item) for item in state.get("held", [])]


def hold(text: str, path: Path = FLAG_PATH) -> int:
    """Keep a message that arrived while it is on, so the person is told about it later. Returns how many are held."""
    state = _read(path)
    if state is None:
        return 0
    held = [*state.get("held", []), " ".join(text.split())[:MAX_HELD_CHARS]][-MAX_HELD:]
    path.write_text(json.dumps({**state, "held": held}), encoding="utf-8")
    return len(held)


def gpu_still_to_free(path: Path = FLAG_PATH) -> bool:
    state = _read(path)
    return state is not None and not state.get("gpu_freed")


def mark_gpu_freed(path: Path = FLAG_PATH) -> None:
    state = _read(path)
    if state is not None:
        path.write_text(json.dumps({**state, "gpu_freed": True}), encoding="utf-8")


def localdeploy_root(base_url: str | None) -> str | None:
    """LocalDeploy's address from the default model's base_url (it ends in /v1)."""
    return (re.sub(r"/v1/?$", "", base_url.rstrip("/")) or None) if base_url else None


def free_gpu(root: str | None, *, client: httpx.Client | None = None) -> str:
    """Ask LocalDeploy to unload every model it has loaded, and say in words what happened. Never raises: the person is told either way."""
    if not root:
        return "I do not know where the model server is, so I could not ask it to free the GPU."
    own = client is None
    http = client or httpx.Client(timeout=30)
    try:
        freed = http.post(f"{root}/models/free", json={}).json()
        if not freed.get("success"):
            return f"The model server could not free the GPU: {str(freed.get('error') or 'unknown error')[:160]}"
        loaded = freed.get("unloaded") or 0
        try:
            llamacpp = (http.get(f"{root}/health").json().get("llamacpp") or {}).get("reachable")
        except (httpx.HTTPError, ValueError):
            llamacpp = None
        if llamacpp:
            return "I asked the model server to free the GPU, but its language model is still running (an older LocalDeploy cannot stop it)."
        return f"I unloaded {loaded} model{'s' if loaded != 1 else ''}; the GPU is free." if loaded else "No model was loaded, so the GPU was already free."
    except (httpx.HTTPError, ValueError):
        return "I could not reach the model server, so I do not know whether the GPU is free."
    finally:
        if own:
            http.close()
