"""Shared "what happened" text for a task's notification (docs/archive/UI_UX_AUDIT.md
Phase 16) - pure formatting over `TaskRecord.metadata`, no channel API calls
anywhere in this file. Extracted out of `telegram_notifications.py` once
WhatsApp existed as a real second consumer to validate the extraction
against, the same reasoning `channels/base.py` already applied to the
classify/task stages.

Channel-specific richness (Telegram's inline Approve/Reject keyboard, its
screenshot-as-a-separate-photo delivery) stays in each channel's own
notifier module, layered on top of `format_task_message()`'s plain text.
"""

from __future__ import annotations

from pathlib import Path

from agent_control.error_text import explain_for_user
from agent_control.text import trim_text as _trim
from agent_control.schemas import TaskRecord, TaskStatus
from agent_control.storage.redaction import redact_text
from agent_control.tools.mcp_client import mcp_output_text


def format_task_message(task: TaskRecord) -> str:
    """Channel-agnostic outbound text for a task, with credentials stripped.

    Every channel's notifier renders this, so it is the one place that can
    guarantee a credential read out of a user's file does not leave the system
    in a chat message (docs/archive/E2E_FINDINGS.md P0-1). The audit sink is covered
    separately by `AuditLogger.append`; a task's answer reaches the user through
    here regardless of which tool produced it.
    """
    return redact_text(_format_task_message(task))


def _format_task_message(task: TaskRecord) -> str:
    if task.status == TaskStatus.RECEIVED:
        return "Got your message, working on it now…"
    if task.status == TaskStatus.RUNNING:
        attempt = _latest_attempt_summary(task)
        if attempt:
            return f"Working on another step.\n{attempt}"
        return "On it."
    if task.status == TaskStatus.RETRYING:
        attempt = _latest_attempt_summary(task)
        if attempt:
            return f"That attempt did not work, so I am trying a different approach now.\n{attempt}"
        return "That attempt did not work, so I am trying a different approach now."
    if task.status == TaskStatus.AWAITING_APPROVAL:
        preview = str(task.metadata.get("pending_approval_preview") or "").strip()
        lines = ["I need approval before I can continue:"]
        if preview:
            lines.append(preview)
        else:
            lines.append("(no preview available for this step)")
        lines.append("")
        lines.append("Reply 'approve' here to continue, or say 'cancel'.")
        return _trim("\n".join(lines), 3900)
    if task.status == TaskStatus.AWAITING_EXTERNAL:
        session = task.metadata.get("awaiting_external") if isinstance(task.metadata, dict) else None
        if isinstance(session, dict) and session.get("provider"):
            return f"{session['provider']} is working in the background. I will report back when it finishes."
        return "An external tool is working in the background. I will report back when it finishes."
    if task.status == TaskStatus.CANCELLED:
        return "Cancelled."
    if task.status == TaskStatus.CLARIFYING:
        question = str(task.metadata.get("clarifying_question") or "").strip()
        if question:
            return _trim(f"{question}\n\n(Reply here to continue this task, or say 'cancel'.)", 3900)
        return "I need more input to continue this task - reply here with details, or say 'cancel'."
    if task.status in {TaskStatus.BLOCKED, TaskStatus.FAILED}:
        error = _last_error(task)
        first_line = _failure_headline(task, error)
        lines = [first_line]
        lines.extend(_failure_lines(task))
        # "What was the last thing you actually tried?" is the first question a
        # user asks about a failure, and the answer is right there in
        # operator_history.
        attempt = _latest_attempt_summary(task)
        if attempt:
            lines.append(attempt)
        return _trim("\n".join(lines), 3900)
    if task.status != TaskStatus.COMPLETED:
        return f"Status: {task.status.value}."

    # Synthesizer-produced focused answer wins over raw tool output.
    # The synthesizer was specifically prompted with the user's objective and
    # extracts ONLY what was asked for - sending the raw page dump instead
    # defeats the entire purpose of synthesis.
    synthesized = str(task.metadata.get("synthesized_answer") or "").strip()
    if synthesized:
        return _with_result_links(task, synthesized)

    answer = _completed_answer(task)
    if answer:
        return _with_result_links(task, answer)
    output = _last_output(task)
    if output:
        return _with_result_links(task, output)
    return _with_result_links(task, "Done.")


def _with_result_links(task: TaskRecord, message: str) -> str:
    """Append the URL/path a completed task produced, if the answer text does
    not already contain it.

    "Launch a web app" tasks stash the address in task.metadata["preview_url"],
    but the per-tool answer builders only look inside the tool's own output
    dict - so the reply was "The local workspace is ready." with no link, and
    the user had no way to reach the thing they asked for. The old plan-era
    formatter surfaced these via _result_lines(); nothing called it after the
    Operator migration, so the behavior was silently lost.
    """
    extras = []
    for label, value in (
        ("Open it here", task.metadata.get("preview_url") or _output_value(task, "preview_url")),
        ("Workspace", task.metadata.get("workspace_dir") or _output_value(task, "workspace_dir")),
        ("Pull request", task.metadata.get("pull_request_url") or _output_value(task, "pull_request_url")),
    ):
        text = str(value or "").strip()
        if text and text not in message:
            extras.append(f"{label}: {text}")
    if not extras:
        return _trim(message, 3900)
    return _trim(message + "\n" + "\n".join(extras), 3900)


def _completed_answer(task: TaskRecord) -> str | None:
    result = task.metadata.get("last_tool_result")
    output = result.get("output") if isinstance(result, dict) else None
    if not isinstance(output, dict):
        return None
    tool_name = str(task.metadata.get("last_tool_name") or result.get("tool_name") or "")
    operation = str(output.get("operation") or "")

    if tool_name == "filesystem.manage":
        return _filesystem_answer(operation, output)
    if tool_name == "document.manage":
        return _document_answer(output)
    if tool_name in {"browser.open", "browser.control"}:
        return _browser_answer(operation, output)
    if tool_name == "computer.use":
        return _computer_answer(output)
    if tool_name == "code.interpreter":
        return _code_interpreter_answer(output)
    if tool_name == "workspace.manage":
        return _workspace_answer(output)
    if tool_name == "artifact.deliver":
        return _artifact_answer(output)
    if tool_name == "http.request":
        return _http_answer(output)
    if tool_name == "task.status":
        return str(output.get("summary") or output.get("text") or _last_output(task) or "").strip() or None
    if tool_name == "mcp.client":
        return _mcp_answer(output)
    return None


def _mcp_answer(output: dict) -> str:
    text = mcp_output_text(output).strip()
    if text:
        return _trim(text, 3600)
    return str(output.get("summary") or "MCP request completed.")


def _filesystem_answer(operation: str, output: dict) -> str:
    root = output.get("root")
    path = output.get("path")
    entries = output.get("entries") if isinstance(output.get("entries"), list) else []
    if operation == "read_file":
        text = str(output.get("text") or output.get("content_preview") or "").strip()
        header = f"I read {Path(str(path)).name if path else 'the file'}."
        if text:
            return f"{header}\n\n{text[:3400]}"
        return f"{header}\n\nNo readable text was extracted."
    if operation in {"inspect_folder", "collect_folder_snapshot"}:
        lines = [f"I found {len(entries)} item(s)" + (f" in {root}." if root else ".")]
        lines.extend(_entry_lines(entries, include_preview=False, limit=80))
        return "\n".join(lines)
    if operation == "search":
        lines = [f"I found {len(entries)} matching item(s)" + (f" under {root}." if root else ".")]
        lines.extend(_entry_lines(entries, include_preview=True, limit=12))
        return "\n".join(lines)
    if operation == "describe_folder":
        lines = [str(output.get("summary") or f"I described {len(entries)} file(s).")]
        lines.extend(_entry_lines(entries, include_preview=True, limit=20))
        return "\n".join(lines)
    if operation == "write_text_file":
        return f"I created the file:\n{path}"
    changed = output.get("changed_paths") if isinstance(output.get("changed_paths"), list) else []
    if changed:
        lines = [str(output.get("summary") or f"Changed {len(changed)} path(s).")]
        lines.extend(f"- {item}" for item in changed[:40])
        return "\n".join(lines)
    return str(output.get("summary") or "Filesystem request completed.")


def _entry_lines(entries: list, *, include_preview: bool, limit: int) -> list[str]:
    lines: list[str] = []
    for item in entries[:limit]:
        if not isinstance(item, dict):
            continue
        kind = "folder" if item.get("is_dir") else "file"
        name = item.get("relative_path") or item.get("path") or ""
        size = item.get("size_bytes")
        size_text = f" ({size} bytes)" if size is not None and kind == "file" else ""
        lines.append(f"- [{kind}] {name}{size_text}")
        if include_preview:
            preview = item.get("content_summary") or item.get("content_preview") or item.get("ocr_text")
            if preview:
                lines.append(f"  {str(preview)[:900]}")
    if len(entries) > limit:
        lines.append(f"- ... {len(entries) - limit} more item(s)")
    return lines


def _document_answer(output: dict) -> str:
    path = output.get("path")
    summary = output.get("summary") or output.get("text")
    if path and summary:
        return f"Here is what I found in {Path(str(path)).name}:\n\n{str(summary)[:3400]}"
    if summary:
        return str(summary)
    if path:
        return f"Document output created:\n{path}"
    return "Document task completed."


def _browser_answer(operation: str, output: dict) -> str:
    title = output.get("page_title") or "Untitled page"
    url = output.get("url") or output.get("browser_url")
    summary = str(output.get("summary") or "").strip()
    lower_summary = summary.lower()
    if "chatgpt" in str(url).lower() and any(marker in lower_summary for marker in ("log in", "sign up", "sign in", "login")):
        return (
            f"I opened ChatGPT, but the page appears to require login before I can send the prompt.\n\n"
            f"Page: {title}\nURL: {url}\n\n"
            "Log in in the opened browser session, then ask me to continue, or tell me to use normal web search instead."
        )
    lines = [f"Page: {title}"]
    if url:
        lines.append(f"URL: {url}")
    if output.get("visited_urls"):
        lines.append(f"Visited {len(output['visited_urls'])} page(s).")
    if summary:
        lines.append("")
        lines.append(summary[:3200])
    screenshot = output.get("screenshot_path")
    if screenshot:
        lines.append("")
        lines.append(f"Screenshot saved locally: {screenshot}")
    return "\n".join(lines)


def _computer_answer(output: dict) -> str:
    summary = str(output.get("final_summary") or output.get("summary") or "").strip()
    observation = output.get("observation") if isinstance(output.get("observation"), dict) else {}
    lines = []
    if summary:
        lines.append(summary)
    active = observation.get("active_window") if isinstance(observation, dict) else None
    if isinstance(active, dict) and active.get("title"):
        lines.append(f"Active window: {active['title']}")
    windows = observation.get("visible_windows") if isinstance(observation, dict) else None
    if isinstance(windows, list) and windows:
        lines.append("Visible windows:")
        for item in windows[:10]:
            if isinstance(item, dict) and item.get("title"):
                lines.append(f"- {item['title']}")
    screenshot = output.get("screenshot_path") or output.get("screenshot_uri")
    if screenshot:
        lines.append(f"Screenshot captured: {screenshot}")
    return "\n".join(lines) or "I inspected the desktop."


def _code_interpreter_answer(output: dict) -> str:
    summary = str(output.get("summary") or "").strip()
    stdout = str(output.get("stdout") or "").strip()
    stderr = str(output.get("stderr") or "").strip()
    workspace = output.get("workspace_dir")
    files = output.get("files_created") if isinstance(output.get("files_created"), list) else []

    lines: list[str] = []
    if stdout:
        lines.append(stdout[:3000])
    elif summary:
        lines.append(summary)
    else:
        lines.append("The script ran successfully.")

    if files:
        lines.append("")
        lines.append("Created files:")
        lines.extend(f"- {item}" for item in files[:40])
    if workspace:
        lines.append(f"Workspace: {workspace}")
    if stderr:
        lines.append("")
        lines.append("Errors:")
        lines.append(stderr[:1200])
    if stdout and summary and summary not in stdout:
        lines.append("")
        lines.append(summary)
    return "\n".join(lines)


def _workspace_answer(output: dict) -> str:
    url = output.get("preview_url") or output.get("url")
    workspace = output.get("workspace_dir")
    lines = [str(output.get("summary") or "The local workspace is ready.")]
    if url:
        lines.append(f"Open it here: {url}")
    if workspace:
        lines.append(f"Workspace: {workspace}")
    return "\n".join(lines)


def _artifact_answer(output: dict) -> str:
    if output.get("delivered"):
        path = output.get("path")
        return "I sent the file or screenshot." + (f"\n{path}" if path else "")
    return str(output.get("summary") or "Artifact delivery completed.")


def _http_answer(output: dict) -> str:
    status_code = output.get("status_code")
    url = output.get("url")
    body = output.get("json")
    if body is None:
        body = str(output.get("text") or "").strip()
    lines = [f"HTTP {status_code} from {url}"]
    if body:
        if isinstance(body, str):
            lines.append(body[:3400])
        else:
            import json

            lines.append(json.dumps(body, ensure_ascii=False, indent=2, default=str)[:3400])
    return "\n\n".join(lines)


def _failure_headline(task: TaskRecord, error: str | None) -> str:
    if not error:
        return "I could not complete this request."
    low = error.lower()
    if "chrome" in low or "devtools" in low or "browser" in low or "websocket" in low:
        return (
            "Browser task failed - Chrome is not running with remote debugging enabled.\n"
            "Start Chrome with: chrome --remote-debugging-port=9222 --remote-allow-origins=*"
        )
    if "planning" in low or "plan failed" in low or "no plan" in low:
        return "I could not plan this task."
    if "capability" in low or "disabled" in low or "not enabled" in low:
        return "A required capability is not enabled for this task."
    return "I could not complete this request."


def _failure_lines(task: TaskRecord) -> list[str]:
    lines = []
    gap = task.metadata.get("fulfillment_gap")
    if gap:
        lines.append(f"Gap: {gap}")
    retry_count = task.metadata.get("retry_count") or task.metadata.get("fulfillment_retry_count")
    if retry_count:
        lines.append(f"Retries: {retry_count}")
    error = _last_error(task)
    if error:
        lines.append(f"Error: {_trim(error, 900)}")
    return lines


def _latest_attempt_summary(task: TaskRecord) -> str | None:
    """Last real step the operator took, for the "what went wrong" message.

    Reads operator_history. This used to read metadata["attempt_history"], a
    plan-era field with zero writers since P3 - so it silently returned None
    and every failure message lost its explanation. See docs/archive/HISTORY.md §3.3.
    """
    history = task.metadata.get("operator_history")
    if not isinstance(history, list) or not history:
        return None
    latest = next(
        (
            item
            for item in reversed(history)
            if isinstance(item, dict) and not str(item.get("tool_name") or "").startswith("_")
        ),
        None,
    )
    if not latest:
        return None
    tool = latest.get("tool_name") or "tool"
    status = latest.get("status") or "unknown"
    # Name the operation and the thing it acted on. "filesystem.manage ended
    # with succeeded" tells the operator nothing they can follow; "read_file
    # on budget.csv" is the same information they would have watched over
    # someone's shoulder.
    tool_input = latest.get("input") if isinstance(latest.get("input"), dict) else {}
    operation = str(tool_input.get("operation") or "").strip()
    target = next(
        (
            str(tool_input[key])
            for key in ("path", "root", "query", "url", "name", "folder_path", "objective")
            if tool_input.get(key)
        ),
        "",
    )
    described = f"{tool} {operation}".strip()
    if target:
        described += f" on {_trim(target, 120)}"
    line = f"Latest attempt: {described} ended with {status}."
    # Why it chose this, which the model already produced and nothing kept.
    reasoning = str(latest.get("reasoning") or "").strip()
    if reasoning:
        line += f"\nWhy: {_trim(reasoning, 300)}"
    detail = str(latest.get("error") or "").strip()
    if detail and status != "succeeded":
        line += f"\nReason: {_trim(detail, 500)}"
    return _trim(line, 900)


def _output_value(task: TaskRecord, key: str) -> str | None:
    result = task.metadata.get("last_tool_result")
    if not isinstance(result, dict):
        return None
    output = result.get("output")
    if isinstance(output, dict) and output.get(key):
        return str(output[key])
    return None


def _last_output(task: TaskRecord) -> str | None:
    result = task.metadata.get("last_tool_result")
    if not isinstance(result, dict):
        return None
    output = result.get("output")
    if isinstance(output, dict):
        terminal_output = output.get("terminal_output")
        if isinstance(terminal_output, list) and terminal_output:
            last = terminal_output[-1]
            if isinstance(last, dict) and last.get("content"):
                return str(last["content"]).strip()
        for key in ("final_summary", "summary", "stdout", "response", "text"):
            if output.get(key):
                return str(output[key]).strip()
    return None


def _last_error(task: TaskRecord) -> str | None:
    # `planning_error` / `last_replan_reason` used to be chained in here as
    # further fallbacks; both were plan-era keys with zero writers left after
    # P3, so they could only ever contribute None (docs/archive/HISTORY.md Part 2 §4
    # item 10). Same for `intervention_summary` in _failure_lines() above.
    fallback = task.metadata.get("last_worker_error")
    result = task.metadata.get("last_tool_result")
    if not isinstance(result, dict):
        return fallback
    value = result.get("error_message") or fallback
    if not value:
        return None
    # Stored worker errors are describe_exception strings - correct for the
    # trace, unreadable in a chat message. Anything already written for a
    # person passes through untouched.
    return explain_for_user(str(value))
