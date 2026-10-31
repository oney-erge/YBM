"""Score one model answer for one recorded Operator step.

A step passes only when all of these hold:
  valid      the answer parses, names a tool YBM has, and (after YBM's own call normalization) the tool's
             input schema accepts it
  grounded   every file path, URL and id it uses appears in what the model was shown. A JSON grammar keeps
             the *shape* valid; it cannot stop a plausible file name from being made up
  agrees     on a step that went well, it picks the same action (and the same tool and operation)
  avoids     on a step that went badly, it does not make the same call again
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable

# Only these fields name something that must already exist. Output names, new file contents and free text
# are the model's to write, so they are not checked.
REFERENCE_KEYS = {
    "path", "root", "folder_path", "file_path", "source", "src", "directory", "workspace_dir", "url",
    "schedule_id", "task_id", "artifact_id", "session_id", "request_id", "approval_id",
}
CREATING_OPERATIONS = ("write", "create", "save", "move", "copy", "rename", "scaffold", "materialize", "prepare", "mkdir")
_ID = re.compile(r"^[a-z]+_[0-9a-f]{16,}$")
_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")


def _flatten(value: Any, key: str = "") -> list[tuple[str, str]]:
    if isinstance(value, dict):
        return [pair for k, v in value.items() for pair in _flatten(v, str(k))]
    if isinstance(value, list):
        return [pair for item in value for pair in _flatten(item, key)]
    return [(key, value)] if isinstance(value, str) and value.strip() else []


def _norm(text: str) -> str:
    return text.replace("\\", "/").casefold()


def ungrounded_references(tool_input: dict[str, Any], shown: str) -> list[str]:
    """References in the call that the model was never shown."""
    haystack = _norm(shown)
    operation = str(tool_input.get("operation") or "").casefold()
    creating = operation.startswith(CREATING_OPERATIONS)
    missing: list[str] = []
    for key, value in _flatten(tool_input):
        if key not in REFERENCE_KEYS:
            continue
        text = value.strip()
        norm = _norm(text)
        if _ID.match(text) or text.startswith(("http://", "https://")):
            if norm.rstrip("/") not in haystack:
                missing.append(text)
        elif _DRIVE.match(text) or text.startswith(("/", "\\\\", "~")) or "/" in norm:
            if creating and key == "path":
                continue  # a path that is about to be created cannot have been shown
            name = norm.rstrip("/").rsplit("/", 1)[-1]
            if norm.rstrip("/") not in haystack and (not name or name not in haystack):
                missing.append(text)
    return missing


def _canonical(
    decision: dict[str, Any], definitions: dict[str, Any], canonicalize: Callable[..., tuple[str | None, dict[str, Any]]]
) -> tuple[str | None, dict[str, Any]]:
    name, tool_input = canonicalize(decision.get("tool_name"), dict(decision.get("tool_input") or {}), definitions)
    return name, tool_input


def call_is_valid(
    decision: dict[str, Any], definitions: dict[str, Any], canonicalize: Callable[..., tuple[str | None, dict[str, Any]]]
) -> tuple[bool, str]:
    name, tool_input = _canonical(decision, definitions, canonicalize)
    definition = definitions.get(name or "")
    if definition is None:
        return False, f"unknown tool {name!r}"
    try:
        definition.validate_input(tool_input)
    except ValueError as exc:
        return False, str(exc).splitlines()[0][:160]
    return True, ""


def same_call(
    left: dict[str, Any], right: dict[str, Any], definitions: dict[str, Any], canonicalize: Callable[..., tuple[str | None, dict[str, Any]]]
) -> tuple[bool, bool]:
    """(same tool and operation, identical canonical input)."""
    left_name, left_input = _canonical(left, definitions, canonicalize)
    right_name, right_input = _canonical(right, definitions, canonicalize)
    same_tool = left_name == right_name and left_input.get("operation") == right_input.get("operation")
    return same_tool, same_tool and json.dumps(left_input, sort_keys=True, default=str) == json.dumps(right_input, sort_keys=True, default=str)


def grade(
    answer: dict[str, Any] | None,
    step: Any,
    definitions: dict[str, Any],
    canonicalize: Callable[..., tuple[str | None, dict[str, Any]]],
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "parsed": answer is not None, "action": (answer or {}).get("action"), "valid": None, "grounded": None,
        "agrees": None, "avoids": None, "problem": "",
    }
    if answer is None:
        result["problem"] = "no valid answer"
        result["passed"] = False
        return result
    action = answer.get("action")
    if action == "call_tool":
        valid, problem = call_is_valid(answer, definitions, canonicalize)
        result["valid"], result["problem"] = valid, problem
        _name, tool_input = _canonical(answer, definitions, canonicalize)
        missing = ungrounded_references(tool_input, step.system + "\n" + step.user)
        result["grounded"] = not missing
        if missing:
            result["problem"] = result["problem"] or "made up: " + "; ".join(missing)[:160]
    recorded = step.recorded
    if step.label == "good":
        agrees = action == recorded.get("action")
        if agrees and action == "call_tool":
            agrees = same_call(answer, recorded, definitions, canonicalize)[0]
        result["agrees"] = agrees
    else:
        repeats = action == recorded.get("action")
        if repeats and action == "call_tool":
            repeats = same_call(answer, recorded, definitions, canonicalize)[1]
        result["avoids"] = not repeats
    result["passed"] = bool(
        result["valid"] is not False and result["grounded"] is not False
        and result["agrees"] is not False and result["avoids"] is not False
    )
    return result
