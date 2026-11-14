"""Reading a task's own record: its history, results, effects and evidence, for the audit, the memory summary and the checks."""
from __future__ import annotations

import json
from typing import Any

from agent_control.orchestration.asking import users_own_words
from agent_control.orchestration.auditor import RAW_OUTPUT_CHARS, AuditorService
from agent_control.orchestration.check_entries import CHECK_ENTRY_NAMES
from agent_control.orchestration.repeats import REPEAT_SENSITIVE_EFFECTS
from agent_control.schemas import (
    OperatorAction,
    OperatorDecision,
    RiskLevel,
    TaskRecord,
    ToolCallResult,
    ToolResultStatus,
)
from agent_control.tools.effects import classify_effect
from agent_control.tools.mcp_client import mcp_output_text


def merge_unique_list(existing: Any, incoming: Any) -> list[Any]:
    result = list(existing) if isinstance(existing, list) else []
    values = incoming if isinstance(incoming, list) else [incoming]
    for value in values:
        if value not in result:
            result.append(value)
    return result


def normalization_history_fields(
    requested_name: str | None,
    requested_input: dict[str, Any],
    tool_name: str | None,
    tool_input: dict[str, Any],
) -> dict[str, Any]:
    if requested_name == tool_name and requested_input == tool_input:
        return {}
    return {
        "normalized_from": {
            "tool_name": requested_name,
            "operation": requested_input.get("operation"),
        }
    }


def answer_from_results(history: list[dict[str, Any]], question: str | None) -> str:
    """What the task did, in its own successful results, and the question the model still had.

    What it changed (the effect table in tools/effects.py), or its last result when it only read.
    """
    succeeded = [
        entry for entry in history
        if isinstance(entry, dict)
        and entry.get("tool_name") not in CHECK_ENTRY_NAMES
        and entry.get("status") == ToolResultStatus.SUCCEEDED.value
        and entry.get("output_summary")
    ]
    changed = [
        entry for entry in succeeded
        if classify_effect(
            str(entry.get("tool_name")), str((entry.get("input") or {}).get("operation") or "") or None
        ) in REPEAT_SENSITIVE_EFFECTS
    ]
    done = list(dict.fromkeys(
        " ".join(str(entry["output_summary"]).split())[:300] for entry in (changed or succeeded[-1:])
    ))
    lines = ["Done:", *(f"- {line}" for line in done[-5:])]
    asked = " ".join(str(question or "").split())
    if asked:
        lines += ["", f"Not settled: {asked}"]
    return "\n".join(lines)


def auditor_response_context(task: TaskRecord, memory_context: str) -> str | None:
    parts: list[str] = []
    if memory_context.strip():
        parts.append(memory_context.strip())
    question = str(task.metadata.get("answered_clarifying_question") or "").strip()
    answer = str(task.metadata.get("clarification_answer") or "").strip()
    if answer:
        clarification = f"Latest user clarification: {answer}"
        if question:
            clarification = f"Latest clarifying question: {question}\n{clarification}"
        parts.append(clarification)
    return "\n\n".join(parts) or None


def effective_operator_risk(tool_definition: Any, tool_input: dict[str, Any], declared: RiskLevel) -> RiskLevel:
    """Use the runtime-owned risk for a model-authored tool call.

    The executor still rejects understated requests from any other caller.
    Operator decisions are normalized first so a model cannot bypass policy
    by understating risk *or* derail a valid call by conservatively
    overstating it above the capability ceiling. Tool definitions and their
    operation-specific resolvers are the authoritative risk classifiers.
    Invalid inputs stay untouched and are rejected by the executor's normal
    schema validation path.
    """
    try:
        validated_input = tool_definition.validate_input(tool_input)
    except ValueError:
        return declared
    return tool_definition.required_risk(validated_input)


def is_background_external_tool_result(tool_name: str | None, result: ToolCallResult) -> bool:
    if tool_name != "coding.agent" or result.status != ToolResultStatus.SUCCEEDED:
        return False
    output = result.output if isinstance(result.output, dict) else {}
    return output.get("status") == "running" and bool(output.get("session_id"))


#: What a tool that wrote or sent something leaves as a receipt: the changed paths and the delivery record.
_RECEIPTED_EFFECTS = frozenset({"created", "modified", "moved", "message_sent"})


_RECEIPT_TOOLS = frozenset({"filesystem.manage", "document.manage", "artifact.deliver"})


def only_receipted_writes(steps: list[dict[str, Any]]) -> bool:
    """Whether every step was a file write, move or send that succeeded, so the answer reports actions and holds no content to check.

    The Auditor exists to say whether an answer is grounded in what the tools returned (a count, a quotation, a summary). An answer
    that only says "I made the three files" is checked against the receipts by script (the changed paths, the delivery record, and
    the claim-against-evidence check), which is faster and exact; the model call took 1 to 8 seconds. Any read, any search, any
    script, any browser step or any step that failed keeps the audit.
    """
    if not steps or any(str(step.get("status") or "") != "succeeded" for step in steps):
        return False
    for step in steps:
        tool_input = step.get("input")
        operation = tool_input.get("operation") if isinstance(tool_input, dict) else None
        if step.get("tool_name") not in _RECEIPT_TOOLS or classify_effect(str(step.get("tool_name")), operation) not in _RECEIPTED_EFFECTS:
            return False
    return True


def last_content_tool_history_entry(history: list[dict[str, Any]]) -> dict[str, Any] | None:
    for entry in reversed(history):
        if entry.get("status") == "succeeded" and AuditorService.is_content_tool(entry.get("tool_name")):
            return entry
    return None


def operator_audit_evidence(
    history: list[dict[str, Any]],
    last_content_entry: dict[str, Any],
    last_raw_output: str,
) -> str:
    """Build observable evidence for auditing a multi-tool objective.

    A one-tool task retains the full raw output byte-for-byte (important for
    long list/count audits). For a multi-tool task, include every recorded
    outcome so the auditor can see that earlier inspection, write, delivery,
    and scheduling steps happened instead of judging the entire objective
    from only the final tool's output.
    """
    real_entries = [entry for entry in history if entry.get("tool_name") not in CHECK_ENTRY_NAMES]
    if len(real_entries) <= 1:
        return last_raw_output
    steps: list[tuple[str, str, str]] = []
    for index, entry in enumerate(real_entries, 1):
        tool_name = str(entry.get("tool_name") or "unknown")
        status = str(entry.get("status") or "unknown")
        if entry is last_content_entry:
            evidence = last_raw_output
        else:
            evidence = str(entry.get("output_summary") or entry.get("error") or "")
        tool_input = entry.get("input")
        input_text = json.dumps(tool_input, ensure_ascii=False, default=str) if tool_input else ""
        steps.append((f"{index}. {tool_name} ({status})", evidence, input_text))
    # The auditor sees the first RAW_OUTPUT_CHARS of this. What each step returned is what grounds an
    # answer, so outputs come first and inputs share the room left over (they still carry what a write
    # wrote). Laid out input-first with 8,000 characters per input, an apply_manifest's 24-move input
    # pushed its own "Moved 24 file(s)" out of view, and the auditor reported that nothing had moved.
    inputs = sum(1 for _, _, input_text in steps if input_text)
    used = sum(len(head) + len(evidence) + 20 for head, evidence, _ in steps)
    room = min(8000, max(400, (RAW_OUTPUT_CHARS - used) // max(1, inputs)))
    parts: list[str] = []
    for head, evidence, input_text in steps:
        details = []
        if evidence:
            details.append(f"Output: {evidence}")
        if input_text:
            shown = input_text if len(input_text) <= room else f"{input_text[:room]}...[{len(input_text) - room} more characters]"
            details.append(f"Input: {shown}")
        parts.append(head + "\n" + "\n".join(details))
    return "\n\n".join(parts)


def steps_used(history: list[dict[str, Any]]) -> int:
    """Real tool calls only, excluding the check pseudo-entries.

    The fulfillment/audit gap paths append CHECK_* rows to `history` so the
    next decide() call sees why `done` was rejected. Those are bookkeeping,
    not work: counting them against operator_max_steps meant every gap cycle
    stole a slot from the tool calls the model needs to actually close the
    gap - with the default budget of 8, two fulfillment plus two audit gaps
    burned half of it, and a task could exhaust its budget having called zero
    tools. See docs/archive/HISTORY.md §3.1.
    """
    return len([entry for entry in history if entry.get("tool_name") not in CHECK_ENTRY_NAMES])


TOOL_RUNNING_ACTIONS = frozenset({OperatorAction.CALL_TOOL, OperatorAction.CALL_TOOLS_PARALLEL, OperatorAction.DELEGATE})


def claude_code_decision(task: TaskRecord, history: list[dict[str, Any]]) -> OperatorDecision:
    """The call the user said yes to: Claude Code gets the request and a plain account of what was already tried."""
    tried = []
    for entry in history:
        if not isinstance(entry, dict) or entry.get("tool_name") in CHECK_ENTRY_NAMES:
            continue
        operation = (entry.get("input") or {}).get("operation") if isinstance(entry.get("input"), dict) else None
        outcome = str(entry.get("error") or entry.get("output_summary") or entry.get("status") or "")[:140].replace("\n", " ")
        tried.append(f"{entry.get('tool_name')}{':' + str(operation) if operation else ''}: {outcome}")
    account = "\n".join(f"- {line}" for line in tried[-10:]) or "- nothing yet"
    objective = (
        f"{users_own_words(task)}\n\nA smaller local assistant already tried this on the user's computer and could not finish "
        f"it. What it tried:\n{account}\nDo the task yourself on this computer and report plainly what you did and found."
    )
    return OperatorDecision(
        action=OperatorAction.CALL_TOOL, tool_name="coding.agent",
        tool_input={"operation": "run_goal", "provider": "claude_code", "objective": objective},
        risk_level=RiskLevel.HIGH, reasoning="The user agreed to hand this task to Claude Code.",
    )


def tool_output_text(result: ToolCallResult) -> str:
    output = result.output
    if not isinstance(output, dict):
        return ""
    if _looks_like_mcp_output(output):
        return mcp_output_text(output)
    if _looks_like_http_output(output):
        if output.get("json") is not None:
            return json.dumps(output["json"], ensure_ascii=False, indent=2, default=str)
        if output.get("text"):
            if output.get("page_title") or "The page text starts:" in str(output.get("summary") or ""):
                # An HTML page: what a person would read (the exact title, the visible text) comes first. The model sees only
                # the first couple of thousand characters of a result, and those were scripts and meta tags, so it could not
                # find the title it was asked for and gave up after six steps.
                return f"{output['summary']}\n\n--- page source ---\n{output['text']}"
            return str(output["text"])
    terminal_output = output.get("terminal_output")
    if isinstance(terminal_output, list):
        chunks = []
        for item in terminal_output:
            if isinstance(item, dict) and item.get("content"):
                chunks.append(str(item["content"]))
        if chunks:
            return "\n\n".join(chunks)
    for key in ("final_summary", "summary", "text", "message", "content"):
        if output.get(key):
            return str(output[key])
    return ""


def _looks_like_mcp_output(output: dict[str, Any]) -> bool:
    if output.get("operation") in {"discover", "list_tools", "call_tool", "health"} and (
        "servers" in output or "tools" in output or "result" in output
    ):
        return True
    return bool(output.get("catalog_path") and ("servers" in output or "tools" in output))


def _looks_like_http_output(output: dict[str, Any]) -> bool:
    return output.get("operation") == "request" and "status_code" in output and (
        "json" in output or "text" in output
    )


def trim_result(result: ToolCallResult) -> dict:
    payload = result.model_dump(mode="json")
    return _trim_value(payload)


def _trim_value(value):
    if isinstance(value, str):
        return value if len(value) <= 4000 else f"{value[:3997]}..."
    if isinstance(value, list):
        return [_trim_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _trim_value(item) for key, item in value.items()}
    return value


def task_memory_summary(task: TaskRecord) -> str:
    metadata = task.metadata
    parts = [
        f"Task {task.id} {task.status.value}: {task.objective}",
    ]
    for label, key in (
        ("tool", "last_tool_name"),
        ("output", "last_tool_output_text"),
        ("desktop_observation", "desktop_observation"),
        ("screenshot_path", "screenshot_path"),
        ("browser_url", "browser_url"),
        ("page_title", "page_title"),
        ("document_path", "document_path"),
        ("document_summary", "document_summary"),
        ("workspace_dir", "workspace_dir"),
        ("preview_url", "preview_url"),
        ("changed_paths", "changed_paths"),
        ("organized_paths", "organized_paths"),
        ("file_manifest", "file_manifest"),
        ("rename_manifest", "rename_manifest"),
        ("artifact_ids", "last_artifact_ids"),
    ):
        value = metadata.get(key)
        if value:
            parts.append(f"{label}: {_compact_jsonish(value)}")
    error = _last_error_from_task(task)
    if error:
        parts.append(f"error: {error}")
    return _trim_value(" | ".join(parts))  # type: ignore[return-value]


def _compact_jsonish(value: Any, limit: int = 900) -> str:
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False, default=str)
    text = " ".join(text.split())
    return text if len(text) <= limit else f"{text[: limit - 3]}..."


def _last_error_from_task(task: TaskRecord) -> str | None:
    result = task.metadata.get("last_tool_result")
    if isinstance(result, dict) and result.get("error_message"):
        return str(result["error_message"])
    if task.metadata.get("last_worker_error"):
        return str(task.metadata["last_worker_error"])
    if task.metadata.get("fulfillment_gap"):
        return str(task.metadata["fulfillment_gap"])
    return None
