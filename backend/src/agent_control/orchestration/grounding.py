"""Putting the final answer back in line with what the task read: a file name the model made up becomes the file it read."""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from agent_control.schemas import (
    ToolResultStatus,
)


def ground_operator_final_answer(answer: str | None, history: list[dict[str, Any]]) -> str | None:
    """Keep the identity of a file-read result in the user-facing answer.

    Models occasionally return only the file contents even though the user
    asked which file was found and read. The tool history has the authoritative
    path, so append it when neither the full path nor basename survived the
    synthesis. This does not infer a path or alter non-file answers.
    """
    if not answer:
        return answer
    answer = _answer_with_the_file_read(answer, history)
    source_path = ""
    for entry in reversed(history):
        tool_input = entry.get("input") if isinstance(entry, dict) else None
        if (
            entry.get("status") == ToolResultStatus.SUCCEEDED.value
            and isinstance(tool_input, dict)
            and tool_input.get("operation") == "read_file"
            and tool_input.get("path")
        ):
            source_path = str(tool_input["path"])
            break
    if not source_path:
        return answer
    filename = source_path.replace("\\", "/").rsplit("/", 1)[-1]
    lowered = answer.lower()
    if source_path.lower() in lowered or (filename and filename.lower() in lowered):
        return answer
    return f"{answer.rstrip()}\n\nSource file: {source_path}"


_FILE_READ_OPERATIONS = frozenset({"read_file", "inspect_document", "extract_text", "summarize_pdf"})


def _answer_with_the_file_read(answer: str, history: list[dict[str, Any]]) -> str:
    """Name the file that was read where the answer names one that does not exist beside it.

    A live run read the right PDF (its made-up name had been replaced before the call) and then
    told the user about the made-up one. A path in the answer that does not exist, has the same
    extension and sits in the folder of the last file read is that file.
    """
    source = next(
        (
            Path(str(entry["input"]["path"]))
            for entry in reversed(history)
            if isinstance(entry, dict)
            and entry.get("status") == ToolResultStatus.SUCCEEDED.value
            and isinstance(entry.get("input"), dict)
            and entry["input"].get("operation") in _FILE_READ_OPERATIONS
            and entry["input"].get("path")
        ),
        None,
    )
    if source is None:
        return answer
    if not source.is_absolute() or not source.suffix:
        return answer

    # Match only the known source directory, including spaces, rather than
    # guessing where an arbitrary unquoted path ends in a prose sentence.
    directory = r"[\\/]".join(re.escape(part) for part in re.split(r"[\\/]", str(source.parent)))
    path_in_source_directory = re.compile(
        r"(?<![\w/\\])" + directory + r"[\\/][^\\/\r\n'\"<>|*?,;]+?"
        + re.escape(source.suffix) + r"(?=$|[\s.,;:!?\)\]\"'])",
        re.IGNORECASE if os.name == "nt" else 0,
    )

    def real_file(match: re.Match[str]) -> str:
        text = match.group(0).rstrip(".:)")
        named = Path(text)
        if (
            os.path.normcase(str(named.parent)) == os.path.normcase(str(source.parent))
            and os.path.normcase(str(named)) != os.path.normcase(str(source))
            and named.suffix.casefold() == source.suffix.casefold()
            and not named.exists()
        ):
            return str(source) + match.group(0)[len(text):]
        return match.group(0)

    return path_in_source_directory.sub(real_file, answer)
