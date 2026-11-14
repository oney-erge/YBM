from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any
from urllib.parse import unquote, urlparse
from pathlib import Path

from agent_control.channels.task_notify import format_task_message
from agent_control.text import trim_text as _trim
from agent_control.channels.telegram import TelegramBotApi
from agent_control.schemas import ApprovalStatus, ChannelType, TaskRecord, TaskStatus, channel_chat_id
from agent_control.storage.repositories import ApprovalRepository


def _approval_inline_keyboard(approval_id: str) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "Approve", "callback_data": f"approval:{approval_id}:approve"},
                {"text": "Reject", "callback_data": f"approval:{approval_id}:reject"},
            ],
            # One decision for the whole job: a web sign-up asks to go to the page, fill the form and click the button.
            [{"text": "Allow for this task", "callback_data": f"approval:{approval_id}:approve_task"}],
        ]
    }


def _stop_keyboard(task_id: str) -> dict[str, Any]:
    """Under the progress line: one tap stops the task (an edit must carry the keyboard again, or Telegram removes it)."""
    return {"inline_keyboard": [[{"text": "Stop", "callback_data": f"task:{task_id}:cancel"}]]}


def _undo_keyboard(task: TaskRecord) -> dict[str, Any] | None:
    """Under the answer to a move or rename: put the files back. Not offered once it was undone or when nothing was recorded."""
    if task.status != TaskStatus.COMPLETED or not task.metadata.get("undo_moves") or task.metadata.get("undo_task_id"):
        return None
    if task.metadata.get("undo_of_task_id"):
        return None  # the answer to an undo is not itself undoable
    return {"inline_keyboard": [[{"text": "Undo", "callback_data": f"undo:{task.id}"}]]}


def _claude_code_offer_keyboard(task: TaskRecord) -> dict[str, Any] | None:
    """Yes / No under the question "shall I hand this to Claude Code?", so answering it is one tap (typing still works).

    "No" sends "No thanks", not "no": a bare "no" is one of the words that cancel a task, and declining the offer should end the
    task with what was found, not cancel it.
    """
    offer = task.metadata.get("claude_code_offer")
    if task.status != TaskStatus.CLARIFYING or not isinstance(offer, dict) or offer.get("state") != "pending":
        return None
    return {
        "inline_keyboard": [[
            {"text": "Yes, ask Claude Code", "callback_data": f"answer:{task.id}:yes"},
            {"text": "No thanks", "callback_data": f"answer:{task.id}:no"},
        ]]
    }


#: Telegram allows an edit about once a second per chat; two seconds keeps well inside that and is
#: still quicker than anyone reads.
PROGRESS_EDIT_INTERVAL_SECONDS = 2.0
_PROGRESS_STATUSES = {TaskStatus.RUNNING, TaskStatus.RETRYING}
_FINISHED_STATUSES = {TaskStatus.COMPLETED, TaskStatus.BLOCKED, TaskStatus.FAILED, TaskStatus.CANCELLED}


class TelegramTaskNotifier:
    def __init__(
        self, client: TelegramBotApi, approvals: ApprovalRepository | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.client = client
        self._clock = clock
        # Optional: lets an AWAITING_APPROVAL notification carry a real
        # Approve/Reject inline keyboard instead of only the plain-text
        # "reply 'approve'" instruction. None (e.g. in tests) just skips it.
        self.approvals = approvals
        # task id -> (time of the last edit, the text shown), so progress can be rate limited and an
        # unchanged line is not sent again (Telegram rejects an edit that changes nothing).
        self._progress_shown: dict[str, tuple[float, str]] = {}

    async def notify(self, task: TaskRecord) -> None:
        chat_id = channel_chat_id(task, ChannelType.TELEGRAM)
        if not chat_id:
            return

        message_text = format_task_message(task)
        anchor = task.metadata.get("progress_message_id")
        if task.status in _PROGRESS_STATUSES and anchor:
            # Progress is one bubble that changes, not a new message per step.
            await self._edit_progress(chat_id, anchor, task.id, message_text)
            return

        reply_markup = self._pending_approval_keyboard(task) or _claude_code_offer_keyboard(task) or _undo_keyboard(task)
        await self.client.send_message(chat_id, message_text, reply_markup=reply_markup)
        if anchor and task.status in _FINISHED_STATUSES:
            # The answer is its own message (so the phone buzzes); the progress bubble has done its job.
            self._progress_shown.pop(task.id, None)
            try:
                await self.client.delete_message(chat_id, anchor)
            except Exception:  # noqa: BLE001 - tidying the chat must never lose the answer
                pass

        # Send screenshot as separate photo if available
        screenshot_path = _get_screenshot_path(task)
        if screenshot_path:
            try:
                caption = f"Screenshot - {_trim(task.objective, 80)}"
                await self.client.send_photo_file(chat_id, screenshot_path, caption)
            except Exception as exc:
                await self.client.send_message(
                    chat_id,
                    "Screenshot was captured, but Telegram photo delivery failed.\n"
                    f"Local file: {screenshot_path}\n"
                    f"Error: {_trim(str(exc), 600)}",
                )

    async def _edit_progress(self, chat_id: str, anchor: Any, task_id: str, text: str) -> None:
        now = self._clock()
        last = self._progress_shown.get(task_id)
        if last is not None and (last[1] == text or now - last[0] < PROGRESS_EDIT_INTERVAL_SECONDS):
            return
        try:
            await self.client.edit_message_text(chat_id, anchor, text, reply_markup=_stop_keyboard(task_id))
        except Exception:  # noqa: BLE001 - a missed progress line is not worth failing a task over
            return
        self._progress_shown[task_id] = (now, text)

    def _pending_approval_keyboard(self, task: TaskRecord) -> dict[str, Any] | None:
        if self.approvals is None or task.status != TaskStatus.AWAITING_APPROVAL:
            return None
        pending = [a for a in self.approvals.list_for_task(task.id) if a.status == ApprovalStatus.PENDING]
        if not pending:
            return None
        # The operator loop stashes exactly one pending tool call at a time
        # (metadata["operator_pending_call"]), so there is always at most one
        # approval to act on here even if list_for_task returns older ones.
        return _approval_inline_keyboard(pending[-1].id)


def _get_screenshot_path(task: TaskRecord) -> str | None:
    for value in (
        task.metadata.get("screenshot_path"),
        _output_value(task, "screenshot_path"),
        task.metadata.get("screenshot_uri"),
        _output_value(task, "screenshot_uri"),
    ):
        path = _path_from_screenshot_value(value)
        if path is not None:
            return str(path)
    return None


def _output_value(task: TaskRecord, key: str) -> str | None:
    result = task.metadata.get("last_tool_result")
    if not isinstance(result, dict):
        return None
    output = result.get("output")
    if isinstance(output, dict) and output.get(key):
        return str(output[key])
    return None


def _path_from_screenshot_value(value: object) -> Path | None:
    if not value:
        return None
    text = str(value)
    if text.startswith("file:///"):
        parsed = urlparse(text)
        raw_path = unquote(parsed.path)
        if raw_path.startswith("/") and len(raw_path) > 3 and raw_path[2] == ":":
            raw_path = raw_path[1:]
        path = Path(raw_path)
    else:
        path = Path(text)
    return path if path.exists() and path.is_file() else None
