"""Make what YBM says in a chat read like a person wrote it.

Everything a task reports (an answer, a progress line, a failure) is built from tool
output, so it arrives full of things only a program cares about: `C:\\Users\\name\\...`
paths, `task_<hex>` ids, `[file]` tags with byte counts, tool names and error codes.
This module is the one place that turns those into plain language. It is pure text
in, text out, with no model call, so it adds no delay and cannot invent anything:
it only rewrites or shortens what is already there, and a path that does not exist
on this machine is left exactly as written.

    - `friendly_path`    `C:\\Users\\me\\OneDrive\\CV\\resume.docx` -> `resume.docx (in OneDrive > CV)`
    - `humanize`         the whole message: paths, ids, long listings, error codes
    - `progress_phrase`  one step of work -> "looking through your files"
"""

from __future__ import annotations

import os
import re
from collections import Counter
from pathlib import Path, PureWindowsPath
from typing import Any

# A Windows path as it appears inside a sentence. Spaces are legal in a Windows path, so the
# match is greedy up to the end of the line and then cut back to the longest prefix that
# exists on disk (`_resolve_prefix`); free text after the path is never swallowed.
_WINDOWS_PATH = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:\\[^\r\n\"<>|*?`]*")
# A POSIX path (macOS, Linux). Only rewritten when it exists on this computer, so "and/or", a URL or a stray
# slash is never touched: the lookbehind refuses a slash that follows a word character, a colon or a dot.
_POSIX_PATH = re.compile(r"(?<![\w:/.\-])(/(?=[^\s/])[^\r\n\"<>|*?`]*)")
_TASK_ID = re.compile(r"\btask_[0-9a-f]{8,}\b")
_LISTING_LINE = re.compile(r"^(\s*)- \[(?:file|folder|dir)\] (.+?)(?: \((\d+) bytes\))?\s*$")

#: Folders a person names by their everyday word, matched case-insensitively against the first
#: segment under the user profile.
_KNOWN_FOLDERS = {
    "desktop": "Desktop",
    "documents": "Documents",
    "downloads": "Downloads",
    "pictures": "Pictures",
    "music": "Music",
    "videos": "Videos",
}

#: Reason codes the worker records, as a sentence. Anything not listed is left as it was.
_REASONS = {
    "risk_exceeds_capability_policy": "your safety settings do not allow that kind of action",
    "approval_required": "it needs your approval first",
    "already_done": "I had already done that",
}

MAX_LISTED = 8


def _home() -> Path:
    return Path(os.path.expanduser("~"))


_TRAILING = ".,;:!?)]}'\"`"


def _existing_prefix(candidate: str) -> str | None:
    """The longest leading part of `candidate` that is a real path on this computer, if any."""
    trimmed = candidate.rstrip().rstrip(_TRAILING)
    words = trimmed.split(" ")
    for count in range(len(words), 0, -1):
        part = " ".join(words[:count]).rstrip(_TRAILING)
        try:
            if part and os.path.exists(part):
                return part
        except (OSError, ValueError):
            continue
    return None


def _resolve_prefix(candidate: str) -> str:
    """The part of a Windows-style `candidate` that is the path, without the sentence after it.

    `C:\\Users\\me\\notes.txt and it has 3 lines` -> `C:\\Users\\me\\notes.txt`. A path that exists is cut back
    to the longest prefix that does. One that does not (a file that was moved, or one on another computer)
    ends at its first file extension that does not run on into more path, or, for a folder, at the first
    quote or sentence punctuation, so what follows it is kept.
    """
    existing = _existing_prefix(candidate)
    if existing is not None:
        return existing
    trimmed = candidate.rstrip().rstrip(_TRAILING)
    # Not on this computer: end the path at the first file extension that does not run on into more path.
    match = re.match(r"^(.*?\\[^\\/]*?\.[A-Za-z0-9]{1,6})(?![A-Za-z0-9_\\/-])", trimmed)
    if match:
        return match.group(1)
    # A folder with no extension ends at the first quote or sentence punctuation.
    return re.match(r"^(.*?)(?=[`'\"]|[.,;:!?)\]]+(?:\s|$)|$)", trimmed).group(1)


def friendly_path(path: str) -> str:
    """A place described the way a person would say it, with the file or folder name first."""
    raw = str(path).strip().strip('"')
    if not raw:
        return raw
    parts = [p for p in re.split(r"[\\/]+", raw) if p]
    drive = ""
    if parts and re.fullmatch(r"[A-Za-z]:", parts[0]):
        drive, parts = parts[0].upper(), parts[1:]
    if not parts:
        return raw
    # Anything under YBM's own scratch space is "the project folder made for this task".
    lowered = [p.lower() for p in parts]
    if ".agent_control" in lowered:
        tail = parts[lowered.index(".agent_control") + 1:]
        if tail and tail[0].lower() == "workspaces":
            if len(tail) > 2:
                return f"{parts[-1]} (in the project folder I made for this task)"
            return "the project folder I made for this task"
        return parts[-1] + " (in my working folder)"
    home = _home()
    home_parts = [p.lower() for p in home.parts[1:]] if home.parts else []
    inner = parts
    where = ""
    if home_parts and lowered[: len(home_parts)] == home_parts:
        inner = parts[len(home_parts):]
        if inner and inner[0].lower() == "onedrive":
            where, inner = "OneDrive", inner[1:]
        elif inner and inner[0].lower() in _KNOWN_FOLDERS:
            where, inner = _KNOWN_FOLDERS[inner[0].lower()], inner[1:]
        elif not inner:
            return "your user folder"
        else:
            where = "your user folder"
    elif lowered and lowered[0] in {"program files", "program files (x86)"}:
        where = parts[0]
        inner = parts[1:]
    elif drive and drive != _home().drive.upper():
        # A file on another drive is not found by its folders alone: "D:" is part of where it is.
        where, inner = f"drive {drive}", parts
    else:
        where = f"the {parts[0]} folder" if len(parts) > 1 else ""
        inner = parts[1:] if len(parts) > 1 else parts
    if not inner:
        return where or raw
    name = inner[-1]
    folders = inner[:-1]
    if len(folders) > 2:
        folders = ["…", *folders[-2:]]
    place = " > ".join([p for p in [where, *folders] if p])
    return f"{name} (in {place})" if place else name


def _replace_paths(text: str) -> str:
    def swap_windows(match: re.Match[str]) -> str:
        raw = match.group(0)
        cut = _resolve_prefix(raw)
        return friendly_path(cut) + raw[len(cut):]

    def swap_posix(match: re.Match[str]) -> str:
        raw = match.group(1)
        cut = _existing_prefix(raw)
        # Two folders or more: "/tmp" alone is a word, not a place worth rewriting.
        if cut is None or len([part for part in cut.split("/") if part]) < 2:
            return match.group(0)
        return friendly_path(cut) + raw[len(cut):]

    return _POSIX_PATH.sub(swap_posix, _WINDOWS_PATH.sub(swap_windows, text))


def _tidy_listing(text: str) -> str:
    """`- [file] name (123 bytes)` runs become short name lists, long ones are cut off."""
    lines = text.split("\n")
    out: list[str] = []
    run: list[str] = []

    def flush() -> None:
        if not run:
            return
        shown = run[:MAX_LISTED]
        out.extend(f"- {name}" for name in shown)
        if len(run) > MAX_LISTED:
            out.append(f"…and {len(run) - MAX_LISTED} more.")
        run.clear()

    for line in lines:
        match = _LISTING_LINE.match(line)
        if match:
            run.append(match.group(2))
        else:
            flush()
            out.append(line)
    flush()
    return "\n".join(out)


def humanize(text: str) -> str:
    """Rewrite a message so a person can read it. Idempotent."""
    if not text:
        return text
    out = _replace_paths(text)
    out = _TASK_ID.sub("an earlier task", out)
    out = _tidy_listing(out)
    # "Workspace: <the project folder>" is a label for a program; a person is told where the files are.
    out = re.sub(r"^Workspace: (the project folder I made for this task)\s*$", r"The files are in \1.", out, flags=re.MULTILINE)
    for code, sentence in _REASONS.items():
        out = out.replace(code, sentence)
    out = re.sub(r"[ \t]+\n", "\n", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


# What a step is, in the words a person uses. Keyed by (tool, operation); "" matches any operation.
_PHRASES: dict[tuple[str, str], str] = {
    ("filesystem.manage", "search"): "looking through your files",
    ("filesystem.manage", "inspect_folder"): "looking through a folder",
    ("filesystem.manage", "describe_folder"): "looking through a folder",
    ("filesystem.manage", "read_file"): "reading a file",
    ("filesystem.manage", "write_text_file"): "writing a file",
    ("filesystem.manage", "apply_manifest"): "moving and organising files",
    ("filesystem.manage", ""): "working with your files",
    ("document.manage", ""): "reading a document",
    ("browser.open", "open"): "opening a web page",
    ("browser.open", "search"): "searching the web",
    ("browser.open", ""): "browsing the web",
    ("browser.control", "fill_form"): "filling in a form",
    ("browser.control", "fill_form_step"): "filling in a form",
    ("browser.control", ""): "using the web page",
    ("web.search", ""): "searching the web",
    ("http.request", ""): "fetching information from the web",
    ("code.interpreter", ""): "running a calculation",
    ("workspace.manage", "write_files"): "writing the app",
    ("workspace.manage", "launch_static"): "starting the app",
    ("workspace.manage", ""): "setting up the project",
    ("computer.use", ""): "looking at your screen",
    ("memory.manage", ""): "checking what I know about you",
    ("schedule.manage", ""): "setting up the reminder",
    ("artifact.deliver", ""): "getting the file ready to send",
    ("task.status", ""): "checking on your tasks",
    ("knowledge.search", ""): "searching your notes",
    ("system.inventory", ""): "checking what is on this computer",
}
_CODING_AGENTS = {"codex": "Codex", "claude_code": "Claude Code", "github_copilot": "Copilot"}


def progress_phrase(tool: str, operation: str = "", tool_input: dict[str, Any] | None = None) -> str:
    """What one step means to a person, e.g. `looking through your files`."""
    tool = (tool or "").strip()
    operation = (operation or "").strip()
    if tool == "coding.agent":
        name = _CODING_AGENTS.get(str((tool_input or {}).get("provider") or ""), "a coding assistant")
        return f"working with {name}"
    if tool in {"delegate", ""} and not operation:
        return "splitting the work into smaller parts" if tool == "delegate" else "thinking it through"
    return _PHRASES.get((tool, operation)) or _PHRASES.get((tool, "")) or "working on it"


_SECRET_FIELD = re.compile(r"pass(word|code)?|secret|token|card|cvv|cvc|ssn|pin\b|iban", re.IGNORECASE)


def _quoted(value: object, limit: int = 80) -> str:
    text = " ".join(str(value).split())
    return '"' + (text if len(text) <= limit else text[: limit - 3].rstrip() + "...") + '"'


def _field_value(field: object, value: object) -> str:
    return "(hidden)" if _SECRET_FIELD.search(str(field)) else _quoted(value)


def _files(count: int) -> str:
    return f"{count} file" if count == 1 else f"{count} files"


def describe_manifest(data: dict[str, Any]) -> str:
    """What a move/copy/rename manifest does, from its own entries.

    An approval on a phone read "Move or rename 24 file(s)": nothing about which folder, or where
    the files would go. The entries say both: the folder they are in now, and the folders they go
    to (or that they keep their folder and change name).
    """
    moves = data.get("moves") or data.get("entries") or data.get("manifest")
    if not isinstance(moves, list) or not moves:
        return "Move or rename files"
    items = [item for item in moves if isinstance(item, dict) and item.get("source") and item.get("destination")]
    if len(items) != len(moves):
        return f"Move or rename {len(moves)} file(s)"
    # PureWindowsPath reads both separators, so Windows and POSIX paths split the same way.
    pairs = [(PureWindowsPath(str(item["source"])), PureWindowsPath(str(item["destination"])), item) for item in items]
    home = Counter(source.parent.name for source, _, _ in pairs).most_common(1)[0][0]
    groups: dict[str, set[str]] = {"move": set(), "copy": set()}
    counts = Counter()
    for source, destination, item in pairs:
        kind = str(item.get("operation") or "move")
        if kind != "copy" and source.parent == destination.parent:
            kind = "rename"
        counts[kind] += 1
        if kind in groups:
            groups[kind].add(destination.parent.name)
    parts = []
    for kind, verb in (("move", "move"), ("copy", "copy")):
        if counts[kind]:
            folders = sorted(groups[kind])
            # "and 1 more" is longer than the one name it hides: list up to seven, then abbreviate.
            shown = ", ".join(folders) if len(folders) <= 7 else ", ".join(folders[:6]) + f" and {len(folders) - 6} more"
            where = f"into {folders[0]}" if len(folders) == 1 else f"into {len(folders)} folders: {shown}"
            parts.append(f"{verb} {_files(counts[kind])} {where}")
    if counts["rename"]:
        parts.append(f"rename {_files(counts['rename'])}")
    if home and parts:
        verb, _, rest = parts[0].partition(" ")
        count_word, _, after = rest.partition(" ")
        noun, _, tail = after.partition(" ")
        parts[0] = f"{verb} {count_word} {noun} in {home}" + (f" {tail}" if tail else "")
    sentence = " and ".join(parts)
    return sentence[:1].upper() + sentence[1:]


def describe_action(tool: str, tool_input: dict[str, Any] | None) -> str:
    """What a step will do, as a sentence a person can approve or refuse. Secrets are never shown."""
    data = tool_input or {}
    operation = str(data.get("operation") or "")
    if tool == "browser.control":
        if operation in {"fill_form_step", "fill_form"}:
            fields = dict(data["fields"]) if isinstance(data.get("fields"), dict) else {}
            if data.get("field") is not None:
                fields[str(data["field"])] = data.get("value", "")
            if len(fields) == 1:
                ((name, value),) = fields.items()
                return f'Type {_field_value(name, value)} into the "{name}" field'
            if fields:
                filled = ", ".join(f'"{name}" = {_field_value(name, value)}' for name, value in list(fields.items())[:6])
                return f"Fill in the form: {filled}"
            return "Fill in the form"
        if operation == "click":
            target = data.get("text") or data.get("selector")
            return f"Click {_quoted(target)}" if target else "Click on the page"
        if operation == "navigate":
            return f"Go to {data['url']}" if data.get("url") else "Go to another page"
        if operation in {"extract_page_state", "summarize_page"}:
            return "Read what is on the page"
        if operation == "chain":
            return "Do several things on the web page, one after another"
        return "Use the web page"
    if tool == "browser.open":
        return f"Open {data['url']}" if data.get("url") else "Open a web page"
    if tool == "computer.use":
        action = data.get("action") if isinstance(data.get("action"), dict) else {}
        goal = action.get("goal") or data.get("goal")
        return f"Control your computer: {goal}" if goal else "Control your computer (mouse and keyboard)"
    if tool == "filesystem.manage":
        if operation == "write_text_file":
            return f"Write the file {humanize(str(data.get('path') or ''))}".rstrip()
        if operation == "apply_manifest":
            return describe_manifest(data)
    if tool == "artifact.deliver":
        return f"Send you the file {humanize(str(data.get('path') or 'you asked for'))}"
    if tool == "schedule.manage" and operation == "create":
        return f"Set up a reminder: {data.get('objective') or data.get('prompt') or ''}".rstrip(": ")
    if tool == "http.request":
        return f"Contact {data['url']}" if data.get("url") else "Contact a website"
    if tool == "coding.agent":
        name = _CODING_AGENTS.get(str(data.get("provider") or ""), "a coding assistant")
        prompt = " ".join(str(data.get("prompt") or "").split())
        return f"Start {name}" + (f" on: {prompt[:100]}" if prompt else "")
    if tool == "code.interpreter":
        return "Run a short Python script"
    if tool == "ability.acquire":
        from agent_control.tools.ability_acquire import approval_sentence

        return approval_sentence(data)
    if tool == "image.describe":
        name = str(data.get("path") or "").replace("\\", "/").rsplit("/", 1)[-1] or "a picture"
        return f"Load a vision model to look at {name} (about 3 GB of GPU memory; the picture stays on this computer)"
    phrase = progress_phrase(tool, operation, data)
    return phrase[:1].upper() + phrase[1:]
