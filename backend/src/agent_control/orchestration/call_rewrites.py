"""The rules that read a model's tool call before it runs, and what each one does to it.

The Operator is a small local model, and its calls arrive with habits that a person would call careless: a made-up file name inside a folder
the user named, a plain write where the request said to add to the end, a PDF read where the request asked what it is about, a path
relative to a folder it had just listed. Each rule here fixes one such habit, from the user's request and what the task has already seen,
and leaves every call that does not show it exactly as it was. They were written one at a time, each after a live failure, and lived inline in the
worker; this module is where they are, and `orchestration/call_policy.py` is the one place that says in what order they run.

Every rule has the same shape: it is given the task, the tool and the input (and the history or the tool list when it needs them) and
returns the tool and input to use. A rule that refuses (a delete asked for by words only, a coding agent nobody asked for) returns a verdict
for `call_policy` instead of changing the call.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
from typing import Any
from agent_control.accounts import PLACEHOLDER as _VAULT_PLACEHOLDER
from agent_control.orchestration.request_facts import RequestFacts, own_words
from agent_control.orchestration.fulfillment import (
    validate_fulfillment,
)
from agent_control.schemas import (
    PostconditionType,
    TaskRecord,
    ToolResultStatus,
)
from agent_control.tools.contracts import SCHEDULE_ID_OPERATIONS


_OPERATOR_TOOL_ALIASES = {
    "filesystem": "filesystem.manage",
    "file_system": "filesystem.manage",
    "filesystem_manager": "filesystem.manage",
    "workspace": "workspace.manage",
    "workspace_manager": "workspace.manage",
    "coding_agent": "coding.agent",
    "coding-agent": "coding.agent",
    "codingagent": "coding.agent",
}


_OPERATOR_OPERATION_ALIASES = {
    "filesystem.manage": {
        "create_file": "write_text_file",
        "save_file": "write_text_file",
        "write_file": "write_text_file",
        "read_text_file": "read_file",
        "list_directory": "inspect_folder",
        "list_directory_with_sizes": "inspect_folder",
        "list_folder": "inspect_folder",
        "directory_tree": "inspect_folder",
        "tree": "inspect_folder",
        "search_files": "search",
        "find_files": "search",
        "search_file": "search",
    },
}


def _canonical_operator_tool_call(
    requested_name: str | None,
    requested_input: dict[str, Any] | None,
    definitions: dict[str, Any],
) -> tuple[str | None, dict[str, Any]]:
    """Recover common model dialects without weakening the tool catalog.

    Only aliases whose canonical target is actually registered are accepted,
    and operation aliases are applied only when that operation exists on the
    registered definition. Policy, schema validation, approval, and allowed
    roots therefore still run against the canonical request exactly as they
    do for a model that emitted the catalog spelling on its first try.
    """
    tool_name = requested_name.strip() if isinstance(requested_name, str) else requested_name
    tool_input = dict(requested_input or {})
    if tool_name not in definitions and isinstance(tool_name, str):
        alias = _OPERATOR_TOOL_ALIASES.get(tool_name.casefold())
        if alias in definitions:
            tool_name = alias

    definition = definitions.get(tool_name) if tool_name is not None else None
    operation = tool_input.get("operation")
    if definition is not None and isinstance(operation, str):
        operation_alias = _OPERATOR_OPERATION_ALIASES.get(str(tool_name), {}).get(operation.casefold())
        supported = set(getattr(definition, "operations", ()) or ())
        if operation_alias and operation_alias in supported:
            tool_input["operation"] = operation_alias
            if operation_alias == "inspect_folder" and "root" not in tool_input and "path" in tool_input:
                tool_input["root"] = tool_input.pop("path")
    if tool_name == "document.manage" and "path" not in tool_input and "artifact_id" not in tool_input:
        # The file tool calls it file_path; a model moving between the two sends the wrong one.
        for alias in ("file_path", "filepath", "file", "document_path"):
            if tool_input.get(alias):
                tool_input["path"] = tool_input.pop(alias)
                break
    if tool_name == "filesystem.manage":
        operation = tool_input.get("operation")
        if operation in {"inspect_folder", "search"} and "root" not in tool_input:
            for alias in ("folder_path", "root_folder", "path"):
                if tool_input.get(alias):
                    tool_input["root"] = tool_input.pop(alias)
                    break
        if operation == "search" and "query" not in tool_input:
            pattern = str(tool_input.pop("pattern", "") or "").strip()
            if pattern and pattern not in {"*", "**", "*.*", "**/*", "**\\*"}:
                tool_input["query"] = pattern
        if operation == "read_file" and "path" not in tool_input:
            file_path = tool_input.pop("file_path", None)
            if file_path:
                tool_input["path"] = file_path
            else:
                folder_path = tool_input.pop("folder_path", None)
                file_name = tool_input.pop("file_name", None)
                if folder_path and file_name:
                    tool_input["path"] = str(Path(str(folder_path)) / str(file_name))
    return tool_name, tool_input


def _coding_agent_input_with_explicit_workspace(
    task: TaskRecord,
    tool_name: str | None,
    tool_input: dict[str, Any],
) -> dict[str, Any]:
    """Carry an explicitly requested folder into the coding-agent session.

    The classifier already extracts ``folder_path``. Using it is safe only
    when that exact path also appears in the user's original message; this
    prevents a model-invented path from expanding terminal scope. Without the
    handoff, the CLI writes in YBM's default task workspace while the prompt
    and later verification refer to a different directory.
    """
    if tool_name != "coding.agent":
        return tool_input
    intent = task.metadata.get("orchestration_intent")
    folder_path = None
    if isinstance(intent, dict):
        # Classifier schemas use ``folder_path`` for filesystem routes but
        # commonly use the generic ``path`` field for workspace.manage. Both
        # are bounded by the same exact-user-text check below.
        folder_path = intent.get("folder_path") or intent.get("path")
    original = str(task.metadata.get("original_message_text") or "")
    if isinstance(folder_path, str) and folder_path.strip():
        if folder_path.casefold() not in original.casefold():
            return tool_input
        if any(
            folder_path.casefold() in match.group(0).casefold()
            for match in re.finditer(r"\{\{.*?\}\}", original)
        ):
            return tool_input
        return {**tool_input, "workspace_dir": folder_path}

    # Classifiers sometimes recognize the coding route but omit the literal
    # path even though it appears in the message. Recover only when the user's
    # text contains exactly one existing absolute directory. Existence plus
    # exact source-text provenance avoids guessing where code may be written;
    # the coding adapter's own allowed-root and policy checks still apply.
    candidates = _existing_absolute_directories(original)
    if len(candidates) != 1:
        return tool_input
    return {**tool_input, "workspace_dir": candidates[0]}


def _coding_agent_input_with_task_defaults(
    task: TaskRecord,
    tool_name: str | None,
    tool_input: dict[str, Any],
) -> dict[str, Any]:
    """Fill schema-required coding fields already explicit in the task.

    This is recovery from an incomplete tool dialect, not model invention:
    a provider is filled only when exactly one supported provider is named in
    the user/task text, and the task's own normalized objective supplies a
    missing start prompt.
    """
    if tool_name != "coding.agent" or tool_input.get("operation") != "start":
        return tool_input
    enriched = dict(tool_input)
    if not enriched.get("provider"):
        providers = _named_coding_providers(task)
        if len(providers) == 1:
            enriched["provider"] = providers[0]
    if not enriched.get("prompt") and not enriched.get("objective"):
        enriched["objective"] = task.objective
    return enriched


# Operations that start or continue work in an outside coding agent. The rest only look or stop.
_CODING_AGENT_STARTING_OPERATIONS = frozenset({"start", "plan", "run_step", "run_goal", "resume"})


def _coding_agent_requested(task: TaskRecord, tool_input: dict[str, Any]) -> bool:
    """Whether the user asked for an outside coding agent: a provider named, or a coding agent asked for."""
    if str(tool_input.get("operation") or "start") not in _CODING_AGENT_STARTING_OPERATIONS:
        return True
    offer = task.metadata.get("claude_code_offer")
    if isinstance(offer, dict) and offer.get("state") == "accepted" and tool_input.get("provider") == "claude_code":
        return True  # the user said yes to handing this task to Claude Code (_offer_claude_code)
    return RequestFacts.from_task(task).names_a_coding_agent


def _named_coding_providers(task: TaskRecord) -> list[str]:
    return list(RequestFacts.from_task(task).providers)


# Where an absolute path can begin. Matching only drive letters meant this
# recovery was dead on Linux and macOS - a user could name a workspace
# explicitly and the coding agent would still write to YBM's default task
# directory.
#
# The POSIX root alternative is enabled only off Windows, and deliberately so:
# on Windows a bare "/Users" resolves against the current drive, so scanning
# for it there would turn any forward-slash fragment in a message into a real
# existing directory and hand the coding agent a folder the user never named.
# The lookbehind keeps "https://", "and/or", "./foo" and "../bar" from starting
# a match.
_ABSOLUTE_PATH_START = re.compile(
    r"(?i)\b[a-z]:\\" if os.name == "nt" else r"(?i)(?:\b[a-z]:\\|(?<![\w:/.])/(?=[^\s/]))"
)


_ABSOLUTE_PATH_SHAPE = re.compile(r"(?i)(?:[a-z]:\\.+|/.+)")


def _existing_absolute_directories(text: str) -> list[str]:
    candidates: list[str] = []
    for match in _ABSOLUTE_PATH_START.finditer(text):
        segment = text[match.start() : match.start() + 600].splitlines()[0]
        resolved: str | None = None
        for end in range(len(segment), 1, -1):
            candidate = segment[:end].rstrip(" \t\r\n.,;:!?)]}\"'")
            if not _ABSOLUTE_PATH_SHAPE.fullmatch(candidate):
                continue
            try:
                path = Path(candidate)
                if path.is_absolute() and path.is_dir():
                    resolved = str(path.resolve())
                    break
            except OSError:
                continue
        if resolved and resolved.casefold() not in {item.casefold() for item in candidates}:
            candidates.append(resolved)
    return candidates


def _filesystem_append_when_asked(
    task: TaskRecord, tool_name: str | None, tool_input: dict[str, Any]
) -> dict[str, Any]:
    """Add to the end of a file that exists when the request says to add to it.

    "Add the word pears on a new line" wrote list_2.txt: write_text_file saves a copy when the path exists, and there was no
    way to add to a file. Only a request that says to append (or to add on a new line, at the end, underneath) and only a path
    that exists turns `append` on. A model that sends the whole file again (the old text plus the new line) meant to replace it,
    so that is written over instead of doubling the old lines."""
    if tool_name != "filesystem.manage" or tool_input.get("operation") != "write_text_file":
        return tool_input
    if tool_input.get("append") or tool_input.get("overwrite"):
        return tool_input
    path = str(tool_input.get("path") or "").strip()
    if not path or not Path(path).is_file() or not RequestFacts.from_task(task).appends:
        return tool_input
    try:
        existing = Path(path).read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return tool_input
    content = str(tool_input.get("content") or "")
    if existing and content.strip().startswith(existing):
        return {**tool_input, "overwrite": True}
    return {**tool_input, "append": True}


def _filesystem_overwrite_when_asked_to_change(
    task: TaskRecord, tool_name: str | None, tool_input: dict[str, Any]
) -> dict[str, Any]:
    """Write over a file that exists when the request is to change it.

    write_text_file keeps an existing file and saves a copy (greeting_2.txt) unless overwrite is set, which is the right
    default for "create a file". For "change it to say goodbye" the copy is not what was asked: the model wrote the copy, read the
    original (still "hello") and repeated the same call until the task was blocked. Only a request that names a change, and only
    a path that exists, turns overwrite on.
    """
    if (
        tool_name != "filesystem.manage" or tool_input.get("operation") != "write_text_file"
        or tool_input.get("overwrite") or tool_input.get("append")
    ):
        return tool_input
    path = str(tool_input.get("path") or "").strip()
    if not path or not Path(path).is_file():
        return tool_input
    if not RequestFacts.from_task(task).changes:
        return tool_input
    return {**tool_input, "overwrite": True}


# The operations that take one file by path and are given a listing's relative path: reading it, opening a document, sending it.
_READS_OF_A_LISTED_FILE = {
    "filesystem.manage": frozenset({"read_file", "open_file"}),
    "document.manage": frozenset({"inspect_document", "extract_text", "summarize_pdf"}),
    "artifact.deliver": frozenset({"send_file"}),
}


_FOLDER_ALIASES = frozenset({"desktop", "documents", "downloads", "home", "pictures"})


def _filesystem_path_relative_to_a_listed_root(
    tool_name: str | None, tool_input: dict[str, Any], history: list[dict[str, Any]]
) -> dict[str, Any]:
    """Read a file the model names the way a listing showed it: relative to the folder that was listed.

    ``search`` and ``inspect_folder`` list entries relative to the folder they looked in (``resumes/oney-resume-notes.txt``), and
    the model passes that to ``read_file``. The tool resolved it against the program's own folder and said the file did not
    exist, three times, until the task was stopped. The path is completed with the most recent listed folder it exists under;
    anything that does not exist there is left exactly as given.
    """
    if tool_input.get("operation") not in _READS_OF_A_LISTED_FILE.get(str(tool_name), frozenset()):
        return tool_input
    raw = str(tool_input.get("path") or "").strip().strip("\"'")
    if not raw or Path(raw).is_absolute() or Path(raw).exists():
        return tool_input
    # The filesystem tool also takes "desktop/AgentControlE2E/a.pdf", with the folder alias first; the document tool does not.
    parts = Path(raw).parts
    without_alias = str(Path(*parts[1:])) if len(parts) > 1 and parts[0].casefold() in _FOLDER_ALIASES else None
    for entry in reversed(history):
        root = entry.get("output_root") if isinstance(entry, dict) else None
        if isinstance(root, str) and root:
            for name in (raw, without_alias):
                candidate = Path(root) / name if name else None
                if candidate is not None and candidate.is_file():
                    return {**tool_input, "path": str(candidate)}
    return tool_input


_LISTINGS_WITHOUT_DATES = frozenset({"describe_folder", "collect_folder_snapshot"})
_PASSWORD_LABEL = re.compile(r"pass(?:word|wd|code)|\bpwd\b", re.IGNORECASE)


def _new_account_password_from_the_vault(task: TaskRecord, tool_name: str | None, tool_input: dict[str, Any]) -> dict[str, Any]:
    """A password the model made up for a new account becomes the vault's `{{new_password}}`.

    A live request to create an account ended with the model's own guessable password written in the form and then in a file and in the
    chat. The history says to use the placeholder (orchestration/ladder.py), and this is the same thing done when it did not: in a form
    that makes an account, a password field filled with text the user never wrote gets a long random password from the vault instead, and
    the model never learns it. A password the user gave in their own words is left alone.
    """
    if tool_name != "browser.control" or tool_input.get("operation") not in {"fill_form", "fill_form_step"}:
        return tool_input
    fields = tool_input.get("fields")
    if not isinstance(fields, dict) or not RequestFacts.from_task(task).wants_a_new_account:
        return tool_input
    said = own_words(task)
    swapped = {
        key: "{{new_password}}"
        for key, value in fields.items()
        if _PASSWORD_LABEL.search(str(key)) and isinstance(value, str) and value.strip()
        and not _VAULT_PLACEHOLDER.search(value) and value not in said
    }
    return {**tool_input, "fields": {**fields, **swapped}} if swapped else tool_input


def _filesystem_listing_input_with_dates_intent(
    task: TaskRecord,
    tool_name: str | None,
    tool_input: dict[str, Any],
) -> dict[str, Any]:
    """Show modified times in a folder listing or search when the request is about which file is newest or oldest.

    The listing used to carry sizes only, so "which file was modified most recently?" could not be answered and the model
    looked for a tool that reports timestamps. Dates are added only for such a request: every other listing, and every
    prompt built from one, stays exactly as it was.
    """
    if tool_name != "filesystem.manage" or tool_input.get("operation") not in {"inspect_folder", "search", *_LISTINGS_WITHOUT_DATES}:
        return tool_input
    if not RequestFacts.from_task(task).wants_recency:
        return tool_input
    if tool_input.get("operation") in _LISTINGS_WITHOUT_DATES:
        # describe_folder and collect_folder_snapshot list names and sizes and cannot show when a file changed, so a question about the
        # newest or oldest file asked through them could never be answered (a fresh-suite case ended "blocked" on exactly this).
        # The folder is listed with inspect_folder, which can.
        if not tool_input.get("root"):
            return tool_input
        return {"operation": "inspect_folder", "root": tool_input["root"], "with_dates": True}
    return {**tool_input, "with_dates": True}


def _filesystem_search_input_with_content_intent(
    task: TaskRecord,
    tool_name: str | None,
    tool_input: dict[str, Any],
) -> dict[str, Any]:
    """Preserve an explicit request to search inside files.

    ``filesystem.manage:search`` supports content search, but an operator can
    omit the boolean and repeatedly search filenames for a marker the user
    explicitly said was in the file contents. Enrich only that declared
    intent; ordinary filename searches retain their cheaper default.
    """
    if tool_name != "filesystem.manage" or tool_input.get("operation") != "search":
        return tool_input
    request_text = " ".join(
        str(value or "")
        for value in (task.metadata.get("original_message_text"), task.objective)
    ).casefold()
    content_intent = bool(
        re.search(
            r"\b(?:contents?|text|body)\b[^.\n]{0,50}\b(?:contain|contains|include|includes|has|have|match|matches)\b",
            request_text,
        )
        or re.search(r"\b(?:search|find|look)\b[^.\n]{0,50}\b(?:inside|within|in)\s+(?:the\s+)?(?:file|files|contents?|text)\b", request_text)
    )
    if not content_intent:
        return tool_input
    enriched = {**tool_input, "include_content": True}
    if not enriched.get("query"):
        query = _explicit_content_query(request_text)
        if query:
            enriched["query"] = query
    return enriched


def _explicit_content_query(request_text: str) -> str | None:
    for pattern in (
        r"\bcontents?\s+(?:contain|contains|include|includes|match|matches)\s+[\"']?([a-z0-9][a-z0-9_.:-]{2,})",
        r"\bmarker\s+(?:is|named|called)\s+[\"']?([a-z0-9][a-z0-9_.:-]{2,})",
    ):
        match = re.search(pattern, request_text, flags=re.IGNORECASE)
        if match:
            return match.group(1).rstrip(".,;!?")
    return None


def _stale_read_recovery_call(
    task: TaskRecord,
    tool_name: str | None,
    tool_input: dict[str, Any],
    history: list[dict[str, Any]],
) -> tuple[str | None, dict[str, Any]]:
    """Switch an unchanged missing-file retry to a bounded parent search."""
    if tool_name != "filesystem.manage" or tool_input.get("operation") != "read_file":
        return tool_name, tool_input
    path = str(tool_input.get("path") or "").strip()
    if not path:
        return tool_name, tool_input
    repeated_failure = any(
        isinstance(entry, dict)
        and entry.get("tool_name") == "filesystem.manage"
        and entry.get("status") == ToolResultStatus.FAILED.value
        and isinstance(entry.get("input"), dict)
        and entry["input"].get("operation") == "read_file"
        and str(entry["input"].get("path") or "").casefold() == path.casefold()
        for entry in history
    )
    if not repeated_failure:
        return tool_name, tool_input
    request_text = str(task.metadata.get("original_message_text") or task.objective)
    if not re.search(r"\b(?:search|find|recover|renamed|moved|no longer)\b", request_text, re.IGNORECASE):
        return tool_name, tool_input
    if path.casefold() not in request_text.casefold():
        return tool_name, tool_input
    stale_path = Path(path).expanduser()
    recovery_root = next(
        (
            parent
            for parent in stale_path.parents
            if parent.is_dir() and str(parent) != stale_path.anchor
        ),
        None,
    )
    if recovery_root is None:
        return tool_name, tool_input
    query = Path(path).stem.strip()
    if not query:
        return tool_name, tool_input
    return "filesystem.manage", {
        "operation": "search",
        "root": str(recovery_root.resolve()),
        "query": query,
        "include_content": False,
    }


def _required_named_coding_agent_call(
    task: TaskRecord,
    tool_name: str | None,
    tool_input: dict[str, Any],
    definitions: dict[str, Any],
) -> tuple[str | None, dict[str, Any]]:
    """Honor an explicit named-provider delegation before fallback tools."""
    definition = definitions.get("coding.agent")
    if definition is None or not getattr(definition, "enabled", False):
        return tool_name, tool_input
    providers = _named_coding_providers(task)
    if len(providers) != 1:
        return tool_name, tool_input
    if PostconditionType.CODING_AGENT_STEP not in set(validate_fulfillment(task).missing):
        return tool_name, tool_input
    if tool_name == "coding.agent":
        return tool_name, tool_input
    return "coding.agent", {
        "operation": "start",
        "provider": providers[0],
        "objective": task.objective,
    }


def _document_summary_call(
    task: TaskRecord,
    tool_name: str | None,
    tool_input: dict[str, Any],
    definitions: dict[str, Any],
) -> tuple[str | None, dict[str, Any]]:
    """Read a PDF with the document tool when the request is to understand it.

    filesystem.manage:read_file also extracts a PDF's text, so a model that picks it is not wrong,
    but "find the PDF and tell me what it is about" then gets no summary and no artifact (the live
    pdf_open_summary run read the file three times and produced neither). When the target is a PDF
    and the request asks what it says, document.manage:summarize_pdf is the tool for that job; its
    normal schema, root and policy checks still apply. "Read me the PDF" keeps the full-text read.
    """
    if tool_name != "filesystem.manage" or tool_input.get("operation") != "read_file":
        return tool_name, tool_input
    path = str(tool_input.get("path") or "")
    if not path.lower().endswith(".pdf"):
        return tool_name, tool_input
    definition = definitions.get("document.manage")
    if (
        definition is None
        or not getattr(definition, "enabled", False)
        or "summarize_pdf" not in set(getattr(definition, "operations", ()) or ())
    ):
        return tool_name, tool_input
    if not RequestFacts.from_task(task).asks_to_understand_a_document:
        return tool_name, tool_input
    return "document.manage", {"operation": "summarize_pdf", "path": path}


_SCHEDULE_ID = re.compile(r"\bschedule_[0-9a-f]{32}\b")


_PLAN_OPERATIONS = frozenset({"organize_plan", "rename_plan"})


def _apply_manifest_from_plan(
    latest: TaskRecord,
    tool_name: str | None,
    tool_input: dict[str, Any],
    history: list[dict[str, Any]],
) -> dict[str, Any]:
    """Apply the plan this task just made, which the model never saw in full.

    The operator sees a plan's one-line summary, not its moves (one per file, each with two
    absolute paths). Told to pass the manifest on, a 9B model copied 4 of 24 moves and planned
    again in one live run, and in the next wrote 26 moves of its own, most for files that do not
    exist. So an apply_manifest that follows an unused organize_plan or rename_plan of this task
    gets that plan's manifest: when it leaves the manifest out (what the catalog and the plan's
    summary tell it to do), and when what it wrote is not an exact part of the plan. An exact part
    (moves the plan has, with the same destinations) is kept: that is a deliberate selection. An
    apply that ran after the plan spends it (its own output replaces the stored manifest). The
    folder is the plan's when the call names none. The user approves whatever runs.
    """
    if tool_name != "filesystem.manage" or tool_input.get("operation") != "apply_manifest":
        return tool_input
    plan_entry = None
    for entry in reversed(history):
        if not isinstance(entry, dict) or entry.get("tool_name") != "filesystem.manage":
            continue
        entry_input = entry.get("input") if isinstance(entry.get("input"), dict) else {}
        operation = entry_input.get("operation")
        if operation == "apply_manifest" and entry.get("status") == ToolResultStatus.SUCCEEDED.value:
            return tool_input
        if operation in _PLAN_OPERATIONS and entry.get("status") == ToolResultStatus.SUCCEEDED.value:
            plan_entry = entry
            break
    plan = latest.metadata.get("file_manifest") if isinstance(latest.metadata, dict) else None
    if plan_entry is None or not isinstance(plan, list) or not plan:
        return tool_input
    written = tool_input.get("manifest")
    if isinstance(written, list) and written and _moves(written) <= _moves(plan):
        return tool_input
    plan_input = plan_entry.get("input") if isinstance(plan_entry.get("input"), dict) else {}
    root = tool_input.get("root") or plan_entry.get("output_root") or plan_input.get("root")
    return {**tool_input, "root": root, "manifest": plan}


def _moves(manifest: list[Any]) -> set[tuple[str, str]]:
    """A manifest's (source, destination) pairs, compared the way the file system would."""
    pairs = set()
    for item in manifest:
        if isinstance(item, dict):
            pairs.add(tuple(os.path.normcase(os.path.normpath(str(item.get(key) or ""))) for key in ("source", "destination")))
        else:
            pairs.add(("", repr(item)))
    return pairs


def _schedule_id_from_this_task(
    tool_name: str | None,
    tool_input: dict[str, Any],
    history: list[dict[str, Any]],
) -> dict[str, Any]:
    """Fill a missing schedule_id with the one schedule this task's own results name.

    The model refers to a schedule it has just created by a name it made up ("task_name": "Daily
    Episode Summary Checker") instead of the id the create result gave it, and repeats that call
    until the no-progress guard stops the task: a live run created its schedule and still ended
    blocked. When the successful schedule.manage results in this task name exactly one schedule,
    that is the one meant. With none or several, the call goes through unchanged and the tool asks
    for the id.
    """
    if tool_name != "schedule.manage" or tool_input.get("schedule_id"):
        return tool_input
    if tool_input.get("operation") not in SCHEDULE_ID_OPERATIONS:
        return tool_input
    named = {
        schedule_id
        for entry in history
        if isinstance(entry, dict)
        and entry.get("tool_name") == "schedule.manage"
        and entry.get("status") in {ToolResultStatus.SUCCEEDED.value, ALREADY_DONE_STATUS}
        for schedule_id in _SCHEDULE_ID.findall(str(entry.get("output_summary") or ""))
    }
    if len(named) != 1:
        return tool_input
    return {**tool_input, "schedule_id": next(iter(named))}


def _schedule_objective_with_request_urls(
    task: TaskRecord,
    tool_name: str | None,
    tool_input: dict[str, Any],
) -> dict[str, Any]:
    """Keep the user's URLs in the job a schedule will run.

    The scheduled run sees only this objective. A live run asked for a daily check of an episode
    page and created "Check for new episodes daily at 9 AM and send summaries.", a job with nothing
    to check. A URL from the user's message that the objective leaves out is added back.
    """
    if tool_name != "schedule.manage" or tool_input.get("operation") != "create":
        return tool_input
    objective = str(tool_input.get("objective") or "").strip()
    if not objective:
        return tool_input
    missing = [url for url in RequestFacts.from_task(task).urls if url not in objective]
    if not missing:
        return tool_input
    return {**tool_input, "objective": f"{objective} [from the request: {' '.join(missing)}]"}


# Operations that take an existing file at `path`. Writes are left out on purpose: a path that does
# not exist yet is what they are for.
_EXISTING_FILE_OPERATIONS = {
    "document.manage": frozenset({"inspect_document", "extract_text", "summarize_pdf"}),
    "filesystem.manage": frozenset({"read_file", "open_file"}),
    "artifact.deliver": frozenset({"send_file"}),
}


def _file_in_named_folder(
    task: TaskRecord,
    tool_name: str | None,
    tool_input: dict[str, Any],
) -> dict[str, Any]:
    """Use the one matching file when the model made up a file name inside a folder the user named.

    Asked to "find the PDF inside C:\\...\\AgentControlE2E", the model called summarize_pdf on
    AgentControlE2E\\AgentControlE2E.pdf, a name built from the folder's, retried it, and only then
    listed the folder. The failed guess stayed in the evidence, and the auditor took it for the file
    the user wanted. When the path does not exist, the user's words name its folder but not the file,
    and that folder holds exactly one file with the same extension, that file is the one meant. In
    every other case the call goes through unchanged.
    """
    if tool_input.get("operation") not in _EXISTING_FILE_OPERATIONS.get(str(tool_name), ()):
        return tool_input
    raw_path = str(tool_input.get("path") or "").strip()
    if not raw_path:
        return tool_input
    path = Path(raw_path).expanduser()
    if not path.is_absolute() or not path.suffix or path.exists():
        return tool_input
    request_text = str(task.metadata.get("original_message_text") or task.objective).casefold()
    folder = path.parent
    # The folder itself, not the start of a deeper path the user named.
    named_folder = re.escape(str(folder).rstrip("\\/").casefold()) + r"(?![\\/\w])"
    if path.name.casefold() in request_text or not re.search(named_folder, request_text):
        return tool_input
    try:
        matches = [
            child for child in folder.iterdir()
            if child.is_file() and child.suffix.casefold() == path.suffix.casefold()
        ]
    except OSError:
        return tool_input
    if len(matches) != 1:
        return tool_input
    return {**tool_input, "path": str(matches[0])}


def _ordered_artifact_delivery_call(
    task: TaskRecord,
    tool_name: str | None,
    tool_input: dict[str, Any],
    history: list[dict[str, Any]],
    definitions: dict[str, Any],
) -> tuple[str | None, dict[str, Any]]:
    """Honor an explicit delivery request before local/open follow-up work.

    A model repeatedly tried to create a schedule before sending the file in
    "only after the file is delivered, create a schedule", then fabricated
    that delivery and asked for confirmation. Once a successful write gives
    an exact path, no semantic choice remains: route the attempted schedule
    step through the registered delivery tool first. Its normal schema,
    policy, root, channel, and approval checks still apply; only ordering is
    made deterministic.
    """
    delivery_definition = definitions.get("artifact.deliver")
    delivery_operations = set(getattr(delivery_definition, "operations", ()) or ())
    delivery_missing = PostconditionType.ARTIFACT_DELIVERED in set(
        validate_fulfillment(task).missing
    )

    # `filesystem.manage:open_file` means open the file in a local desktop
    # application. Small models can mistake that for sending an already found
    # file to the user. When delivery is an explicit unmet postcondition and
    # the model already supplied the exact path, no semantic choice remains:
    # route the same path through the delivery adapter. Its normal root,
    # channel, schema, policy, and approval checks still apply.
    if (
        tool_name == "filesystem.manage"
        and tool_input.get("operation") == "open_file"
        and tool_input.get("path")
        and delivery_missing
        and "send_file" in delivery_operations
    ):
        return "artifact.deliver", {
            "operation": "send_file",
            "path": tool_input["path"],
            "caption": "Requested file",
        }

    if tool_name != "schedule.manage":
        return tool_name, tool_input
    request_text = str(task.metadata.get("original_message_text") or task.objective)
    ordered_request = re.search(
        r"\bonly\s+after\b[^.\n]{0,120}\b(?:deliver\w*|send|sent)\b"
        r"[^.\n]{0,120}\b(?:schedule\w*|scheduled)\b",
        request_text,
        flags=re.IGNORECASE,
    )
    if not ordered_request:
        return tool_name, tool_input
    if not delivery_missing:
        return tool_name, tool_input
    output_path = _known_written_file_path(task, history)
    if not output_path or delivery_definition is None:
        return tool_name, tool_input
    if "send_file" not in delivery_operations:
        return tool_name, tool_input
    return "artifact.deliver", {
        "operation": "send_file",
        "path": output_path,
        "caption": "Requested completed file",
    }


_FILE_REARRANGING_OPERATIONS = frozenset({"organize_plan", "rename_plan", "apply_manifest"})


def _is_blind_persona_replace(tool_name: str | None, tool_input: dict[str, Any], history: list[dict[str, Any]]) -> bool:
    """A persona update (a replacement of the whole document) in a task that has not read it first."""
    if tool_name != "persona.manage" or str(tool_input.get("operation") or "") != "update":
        return False
    return not any(
        isinstance(entry, dict) and entry.get("tool_name") == "persona.manage"
        and isinstance(entry.get("input"), dict) and entry["input"].get("operation") == "get" and entry.get("status") == "succeeded"
        for entry in history
    )


def _is_simulated_delete(task: TaskRecord, tool_name: str | None, tool_input: dict[str, Any]) -> bool:
    """A move or rename planned for a request that only asks to delete.

    Asked to "delete all the files in the junk folder", the local model organized them into folders called "other", "deleted"
    and "trash", renamed them, and reported "Renamed 3 file(s)": the files were altered and not deleted, and nobody was told
    there is no delete operation. A request that also asks to move, organize or rename keeps those operations.
    """
    if tool_name != "filesystem.manage" or tool_input.get("operation") not in _FILE_REARRANGING_OPERATIONS:
        return False
    return RequestFacts.from_task(task).only_deletes


def _known_written_file_path(task: TaskRecord, history: list[dict[str, Any]]) -> str | None:
    for entry in reversed(history):
        if not isinstance(entry, dict) or entry.get("status") != ToolResultStatus.SUCCEEDED.value:
            continue
        tool_input = entry.get("input") if isinstance(entry.get("input"), dict) else {}
        if (
            entry.get("tool_name") == "filesystem.manage"
            and tool_input.get("operation") == "write_text_file"
            and tool_input.get("path")
        ):
            return str(tool_input["path"])
    for key in ("changed_paths", "files_created", "files_modified", "changed_files"):
        values = task.metadata.get(key)
        if isinstance(values, list) and values:
            return str(values[-1])
    return None


#: History status of a side-effecting call that was not run again because it already succeeded.
ALREADY_DONE_STATUS = "already_done"
