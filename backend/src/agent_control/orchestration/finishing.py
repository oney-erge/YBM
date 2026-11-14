"""How a task ends: a finish named as a tool, the answer when the budget is spent, a finished coding-agent run, and the wording
of a transition that failed."""
from __future__ import annotations

from typing import Any

from agent_control.orchestration.check_entries import (
    CHECK_ENTRY_NAMES,
    CHECK_ENTRY_STUCK,
)
from agent_control.schemas import (
    OperatorAction,
    OperatorDecision,
    TaskRecord,
    ToolCallResult,
    ToolResultStatus,
)


def operator_blocked_reason(reason: str | None, history: list[dict[str, Any]]) -> str:
    stated = str(reason or "").strip()
    if stated and stated != "operator_blocked":
        return stated
    for entry in reversed(history):
        if entry.get("tool_name") in CHECK_ENTRY_NAMES:
            continue
        if entry.get("status") in {"failed", "denied", "rate_limited"}:
            tool_name = str(entry.get("tool_name") or "the latest capability")
            error = str(entry.get("error") or "it did not succeed").strip()
            return (
                "No available capability completed the request. "
                f"The latest attempt with {tool_name} did not succeed: {error}"
            )
    return "No available tool or capability can complete the request with the current configuration."


def finish_offer_pending(history: list[dict[str, Any]]) -> bool:
    """Whether the last thing in the history is an offer to answer, not yet taken up or refused."""
    return bool(history) and history[-1].get("tool_name") == CHECK_ENTRY_STUCK and history[-1].get("status") == "stuck"


def fulfilled_step_budget_answer(task: TaskRecord, history: list[dict[str, Any]]) -> str:
    """Ground a completion that reached the step cap after fulfillment."""
    adapter_dir = task.metadata.get("adapter_dir")
    if adapter_dir:
        return f"Created the adapter proposal in the cache at {adapter_dir}. It was not loaded automatically."
    workspace_dir = task.metadata.get("workspace_dir")
    if workspace_dir:
        return f"Completed the requested work in {workspace_dir}."
    schedule_id = task.metadata.get("schedule_id")
    if schedule_id:
        return f"Created the requested schedule ({schedule_id})."
    for entry in reversed(history):
        if entry.get("status") == "succeeded" and entry.get("output_summary"):
            return str(entry["output_summary"])
    return "Completed the requested work and recorded the required result."


def operator_transition_error_message(reason: str, max_steps: int) -> str:
    if reason == "operator_step_budget_exhausted":
        return (
            f"Blocked after the bounded {max_steps}-step operator budget was exhausted; "
            "stopped instead of continuing an unproductive loop."
        )
    return reason


_FINISH_TOOL_NAMES = frozenset({"done", "finish", "final_answer", "final", "answer", "respond", "reply"})


_FINISH_TEXT_KEYS = ("final_answer", "answer", "text", "message", "response", "result")


def finish_named_as_a_tool(decision: OperatorDecision) -> OperatorDecision:
    """Treat ``call_tool`` with ``tool_name: "done"`` and the answer in its input as the ``done`` action it plainly is.

    A small model that has the answer sometimes writes the finish as a tool call. That was refused as an unregistered tool,
    twice in one live run, with the right answer already written in the input. Only a finish name carrying a non-empty
    answer is repaired; anything else goes through as before.
    """
    if decision.action != OperatorAction.CALL_TOOL or str(decision.tool_name or "").strip().casefold() not in _FINISH_TOOL_NAMES:
        return decision
    for key in _FINISH_TEXT_KEYS:
        answer = decision.tool_input.get(key)
        if isinstance(answer, str) and answer.strip():
            return decision.model_copy(
                update={"action": OperatorAction.DONE, "final_answer": answer.strip(), "tool_name": None, "tool_input": {}}
            )
    return decision


def completed_coding_agent_answer(tool_name: str, result: ToolCallResult) -> str | None:
    if tool_name != "coding.agent" or result.status != ToolResultStatus.SUCCEEDED:
        return None
    output = result.output if isinstance(result.output, dict) else {}
    if output.get("status") != "completed" or output.get("returncode") not in (None, 0):
        return None
    provider = str(output.get("provider") or "coding agent")
    lines = [f"{provider} completed successfully."]
    workspace = str(output.get("workspace_dir") or "").strip()
    if workspace:
        lines.append(f"Workspace: {workspace}")
    changed_files = [str(item) for item in output.get("changed_files") or [] if str(item).strip()]
    if changed_files:
        lines.append("Changed files:")
        lines.extend(f"- {path}" for path in changed_files[:40])
    summary = str(output.get("summary") or "").strip()
    if summary:
        lines.extend(("", summary[:2_000]))
    return "\n".join(lines)
