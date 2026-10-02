"""Choosing the folders YBM may work in.

The README's headline task is "organize my Downloads folder", and on a fresh
install it could not be done: file access was off and the only folder the agent
could touch was its own scratch workspace. Turning that on meant finding the
Access page, picking a mode, and separately editing a list of allowed roots in a
config file - three things, none of them named in the first run.

This is the one action that does all of it: pick folders, pick how careful to be,
done. The rules are deliberately conservative, because this is the point at which
a person hands an agent real access to their machine:

* only folders that exist and are real directories, by absolute path;
* never a whole drive, the home folder itself (or anything above it), or an
  operating-system directory - a request for "everything" is almost never meant;
* only the two gentle modes (Read-only, or Write with approval). Full autonomy is
  a deliberate choice on the Access page, not something a setup shortcut grants.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from agent_control.config import AppSettings
from agent_control.policy.access_modes import apply_access_modes_to_config, summarize_access_modes
from agent_control.schemas import CapabilityAccessMode

#: The agent's own scratch area. Always kept first in the allowed roots.
DEFAULT_ROOT = ".agent_control/workspaces"

#: The two modes a setup shortcut may grant.
GENTLE_MODES = (CapabilityAccessMode.READ_ONLY, CapabilityAccessMode.WRITE_ACCESS)

# (label, folder names to look for under the home directory, in order). OneDrive
# redirects Desktop, Documents and Pictures on many Windows machines.
_STANDARD_FOLDERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Downloads", ("Downloads",)),
    ("Documents", ("Documents", "OneDrive/Documents")),
    ("Desktop", ("Desktop", "OneDrive/Desktop")),
    ("Pictures", ("Pictures", "OneDrive/Pictures")),
)


class FolderRejected(ValueError):
    """A folder that should not be granted, with a sentence a person can act on."""


def suggested_folders(home: Path | None = None) -> list[dict[str, str]]:
    """The usual places people keep things, that actually exist on this machine."""
    home = home or Path.home()
    found: list[dict[str, str]] = []
    for label, relatives in _STANDARD_FOLDERS:
        for relative in relatives:
            candidate = home / relative
            if candidate.is_dir():
                found.append({"name": label, "path": str(candidate)})
                break
    return found


def _protected_directories() -> list[Path]:
    if sys.platform == "win32":
        names = ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramData")
        return [Path(value).resolve() for value in (os.environ.get(name) for name in names) if value]
    return [
        Path(p)
        for p in ("/etc", "/usr", "/bin", "/sbin", "/lib", "/lib64", "/var", "/boot", "/dev", "/proc", "/sys",
                  "/System", "/Library", "/private")
    ]


def validate_work_folder(raw: str, *, home: Path | None = None) -> Path:
    """Resolve ``raw`` to a folder YBM may be given, or say plainly why not."""
    home = (home or Path.home()).resolve()
    text = (raw or "").strip().strip('"')
    if not text:
        raise FolderRejected("Choose a folder.")
    path = Path(text).expanduser()
    if not path.is_absolute():
        raise FolderRejected(f"Give the full path to the folder (for example {home / 'Downloads'}).")
    resolved = path.resolve()
    if not resolved.exists():
        raise FolderRejected(f"{text} does not exist.")
    if not resolved.is_dir():
        raise FolderRejected(f"{text} is a file, not a folder.")
    if resolved.parent == resolved:
        raise FolderRejected("A whole drive is too much to hand over. Pick a folder inside it.")
    if resolved == home or resolved in home.parents:
        raise FolderRejected("Your whole home folder is too broad. Pick the folder you actually want organised, such as Downloads.")
    for protected in _protected_directories():
        if resolved == protected or protected in resolved.parents:
            raise FolderRejected(f"{text} is a system folder, which YBM should not change.")
    return resolved


def apply_work_folders(config: dict[str, Any], folders: list[Path], mode: CapabilityAccessMode) -> dict[str, Any]:
    """Write the chosen folders and file-access mode into a config dict.

    Folders are added to the existing allowed roots (never replacing ones already
    there) with the scratch workspace kept first, and the file system access mode
    is set the way the Access page sets it, so the two cannot disagree.
    """
    if mode not in GENTLE_MODES:
        raise ValueError(f"work folders can only be granted as read-only or write-with-approval, not {mode.value}")
    computer_use = config.setdefault("adapters", {}).setdefault("computer_use", {})
    existing = [str(root) for root in (computer_use.get("allowed_roots") or [DEFAULT_ROOT])]
    roots = [DEFAULT_ROOT, *(root for root in existing if root != DEFAULT_ROOT)]
    seen = {_key(root) for root in roots}
    for folder in folders:
        if _key(str(folder)) not in seen:
            roots.append(str(folder))
            seen.add(_key(str(folder)))
    computer_use["allowed_roots"] = roots
    apply_access_modes_to_config(config, {"filesystem": mode})
    return config


def _key(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))


def current_state(settings: AppSettings) -> dict[str, Any]:
    """Where file access stands right now, for the console to show and decide on."""
    roots = [str(root) for root in settings.adapters.computer_use.allowed_roots]
    return {
        "file_access": summarize_access_modes(settings)["filesystem"].mode.value,
        "allowed_roots": roots,
        "work_folders": [root for root in roots if _key(root) != _key(DEFAULT_ROOT)],
    }
