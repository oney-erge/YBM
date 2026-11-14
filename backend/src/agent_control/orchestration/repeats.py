"""Calls and reads that were already done: the repeat guards, the 'already done' answer, and the stop when nothing new happens."""
from __future__ import annotations

import json
from typing import Any

from agent_control.human_text import progress_phrase
from agent_control.orchestration.call_rewrites import ALREADY_DONE_STATUS
from agent_control.orchestration.check_entries import CHECK_ENTRY_NAMES
from agent_control.orchestration.fulfillment import (
    fulfillment_guidance,
    validate_fulfillment,
)
from agent_control.schemas import (
    PostconditionType,
    TaskRecord,
    ToolResultStatus,
)
from agent_control.tools.effects import classify_effect

#: Effects a second identical call would repeat (tools/effects.py). Reads, commands and visits are left
#: alone: reading a file twice is harmless, and running the same command again can be the point.
REPEAT_SENSITIVE_EFFECTS = frozenset({"created", "modified", "moved", "deleted", "message_sent"})


def succeeded_side_effect(
    history: list[dict[str, Any]], tool_name: str | None, tool_input: dict[str, Any]
) -> tuple[int, dict[str, Any]] | None:
    """The 1-based step and entry where this exact side-effecting call already succeeded in this task."""
    if not tool_name or not isinstance(tool_input, dict):
        return None
    operation = str(tool_input.get("operation") or "") or None
    if classify_effect(tool_name, operation) not in REPEAT_SENSITIVE_EFFECTS:
        return None
    wanted = json.dumps(tool_input, sort_keys=True, default=str)
    for index, entry in enumerate(history, start=1):
        if (
            isinstance(entry, dict)
            and entry.get("tool_name") == tool_name
            and entry.get("status") == ToolResultStatus.SUCCEEDED.value
            and json.dumps(entry.get("input"), sort_keys=True, default=str) == wanted
        ):
            return index, entry
    return None


def _what_is_missing(task: TaskRecord, history: list[dict[str, Any]]) -> PostconditionType | None:
    """What the request still needs, as an instruction for the next step.

    Only for gaps the step history alone can prove (today: a form filled in and never submitted). The other
    checks read what the tools reported into the task's metadata, which is not yet updated mid-step, so using
    them here would call a finished task unfinished.
    """
    probe = task.model_copy(update={"metadata": {**task.metadata, "operator_history": history}})
    try:
        missing = validate_fulfillment(probe).missing
    except Exception:  # noqa: BLE001 - a hint must never break the loop
        return None
    if PostconditionType.FORM_SUBMITTED in missing:
        return PostconditionType.FORM_SUBMITTED
    return None


def with_what_is_missing(task: TaskRecord, history: list[dict[str, Any]], summary: str) -> str:
    """A repeated step the model should not run again, plus what it should do instead.

    Told only "this already succeeded", a model that has not finished (a form filled but never submitted) repeats the
    step; told what is still missing, it can do that.
    """
    missing = _what_is_missing(task, history)
    if missing is None:
        return summary
    return f"{summary} Still missing for the request: {fulfillment_guidance(f'expected_{missing.value}_missing')}"


#: What a person is told when the request is not finished (the instruction above is for the model, not for them).
_UNFINISHED_IN_WORDS = {PostconditionType.FORM_SUBMITTED: "The form was filled in but never submitted, so the sign-up is not done."}


def repeat_stop_message(task: TaskRecord, history: list[dict[str, Any]], tool_name: str, tool_input: dict[str, Any]) -> str:
    """Why the task stopped, in words: a step that had worked was asked for again and again."""
    step = progress_phrase(tool_name, str(tool_input.get("operation") or ""), tool_input)
    missing = _what_is_missing(task, history)
    return (
        f"I got stuck repeating a step that had already worked ({step}), so I stopped; nothing was done twice."
        + (f" {_UNFINISHED_IN_WORDS[missing]}" if missing in _UNFINISHED_IN_WORDS else "")
    )


def already_done_summary(step_number: int, entry: dict[str, Any], *, read: bool = False) -> str:
    result = str(entry.get("output_summary") or "succeeded")[:600]
    if read:
        return (
            f"Not run again: this exact read already succeeded at step {step_number} and nothing has changed since. "
            f"Its result: {result} Use that result: finish with action done and a final_answer drawn from it, "
            "or call a different tool."
        )
    return (
        f"Not run again: this exact call already succeeded at step {step_number}. "
        f"Its result: {result} "
        "Running it again would do it twice. Continue with what the request still needs, or finish."
    )


# Reads whose answer cannot change unless something was written in between. Browsers, screenshots, HTTP, searches and
# task status are left out on purpose: asking again can be exactly what the user wants.
_STABLE_READS: dict[str, frozenset[str]] = {
    "filesystem.manage": frozenset(
        {"read_file", "inspect_folder", "search", "describe_folder", "find_by_description", "resolve_desktop_item", "collect_folder_snapshot"}
    ),
    "document.manage": frozenset({"inspect_document", "extract_text", "summarize_pdf"}),
    "knowledge.search": frozenset({"search", "list_sources"}),
}


def redundant_read(
    history: list[dict[str, Any]], tool_name: str | None, tool_input: dict[str, Any]
) -> tuple[int, dict[str, Any]] | None:
    """The earlier step where this exact read already succeeded, when nothing written since could change its answer.

    A small model that has the result it needs often asks for it again instead of answering; in live runs a PDF
    summary, a file read and a document extraction each ended blocked that way.
    """
    operation = str(tool_input.get("operation") or "") if isinstance(tool_input, dict) else ""
    if operation not in _STABLE_READS.get(tool_name or "", ()):
        return None
    wanted = json.dumps(tool_input, sort_keys=True, default=str)
    found: tuple[int, dict[str, Any]] | None = None
    for index, entry in enumerate(history, start=1):
        if (
            isinstance(entry, dict)
            and entry.get("tool_name") == tool_name
            and entry.get("status") == ToolResultStatus.SUCCEEDED.value
            and json.dumps(entry.get("input"), sort_keys=True, default=str) == wanted
        ):
            found = (index, entry)
    if found is None:
        return None
    for entry in history[found[0]:]:
        if (
            isinstance(entry, dict)
            and entry.get("status") == ToolResultStatus.SUCCEEDED.value
            and classify_effect(str(entry.get("tool_name")), str((entry.get("input") or {}).get("operation") or "") or None)
            in REPEAT_SENSITIVE_EFFECTS
        ):
            return None  # something was written since: the read may now return something else
    return found


#: Refusals that come from a rule, not from a hiccup: the same call can never succeed.
_POLICY_REFUSALS = ("risk_exceeds_capability_policy", "capability_disabled", "scope_not_allowed", "pattern_denied")


def policy_refusal(history: list[dict[str, Any]]) -> tuple[str, str] | None:
    """The tool and operation when policy has refused the same tool twice in a row.

    A live run asked to fill in a form was refused for exceeding the browser's risk ceiling, tried
    the same step six more times and then said "something went wrong on my side". One refusal is
    information the model can act on (use another way); the second is a wall, so stop and say so.
    """
    real = [e for e in history if isinstance(e, dict) and e.get("tool_name") not in CHECK_ENTRY_NAMES][-2:]
    if len(real) < 2 or real[0].get("tool_name") != real[1].get("tool_name"):
        return None
    for entry in real:
        error = str(entry.get("error") or "")
        if str(entry.get("status") or "") != ToolResultStatus.DENIED.value or not any(c in error for c in _POLICY_REFUSALS):
            return None
    tool_input = real[1].get("input") if isinstance(real[1].get("input"), dict) else {}
    return str(real[1].get("tool_name") or ""), str(tool_input.get("operation") or "")


def repeat_block_message(history: list[dict[str, Any]], called: str) -> str:
    refused = policy_refusal(history)
    if refused is not None:
        what = progress_phrase(*refused)
        return (
            f"I can't go on with {what}: your safety settings do not allow that action, so I stopped "
            "instead of trying it again. You can allow it in the YBM settings, or do that one step yourself."
        )
    return (
        f"Blocked after repeated {called} calls returned the same result; "
        "stopped to avoid a no-progress loop. A different capability or user input is required."
    )


def repeated_no_progress_call(
    history: list[dict[str, Any]],
    *,
    threshold: int = 3,
) -> tuple[str, str] | None:
    """Detect a bounded run of identical observations or failures.

    An operator may vary the prose in a read-only request while continuing to
    receive exactly the same page or status. Treating those calls as progress
    burns the whole step budget and hides the actual limitation. Three
    consecutive identical outcomes are enough evidence to stop safely. This
    also catches a model retrying the same missing path despite receiving the
    same explicit error; writes with changing output do not match this guard.
    """
    refused = policy_refusal(history)
    if refused is not None:
        return refused
    real_entries = [
        entry
        for entry in history
        if isinstance(entry, dict) and entry.get("tool_name") not in CHECK_ENTRY_NAMES
    ]
    if len(real_entries) < threshold:
        return None
    recent = real_entries[-threshold:]
    statuses = {str(entry.get("status") or "") for entry in recent}
    if len(statuses) != 1 or next(iter(statuses)) not in {
        ToolResultStatus.SUCCEEDED.value,
        ToolResultStatus.FAILED.value,
        ToolResultStatus.DENIED.value,
        ALREADY_DONE_STATUS,
    }:
        return None
    tool_names = {str(entry.get("tool_name") or "") for entry in recent}
    operations = {
        str((entry.get("input") or {}).get("operation") or "")
        for entry in recent
        if isinstance(entry.get("input"), dict)
    }
    observation_key = (
        "output_summary"
        if next(iter(statuses)) in {ToolResultStatus.SUCCEEDED.value, ALREADY_DONE_STATUS}
        else "error"
    )
    outputs = {" ".join(str(entry.get(observation_key) or "").split()) for entry in recent}
    operation = next(iter(operations), "")
    # A call without an operation counts too: {"url": ...} and {"query": ...} repeated with the
    # same error used to slip past this guard (ten identical web.search failures in one live run).
    if (
        len(tool_names) != 1
        or len(operations) != 1
        or len(outputs) != 1
        or not next(iter(outputs), "")
    ):
        return None
    return next(iter(tool_names)), operation
