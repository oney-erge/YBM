"""Telling the person: the notification a task's channel gets, and what is remembered about a finished task."""
from __future__ import annotations

import asyncio
import logging

from agent_control.channels.memory import ConversationMemoryService
from agent_control.orchestration.task_record import task_memory_summary
from agent_control.schemas import (
    AuditEventType,
    TaskRecord,
    TaskStatus,
)

logger = logging.getLogger(__name__)

NOTIFIABLE_STATUSES = {
    TaskStatus.AWAITING_APPROVAL,
    TaskStatus.AWAITING_EXTERNAL,
    TaskStatus.BLOCKED,
    TaskStatus.CANCELLED,
    TaskStatus.CLARIFYING,
    TaskStatus.COMPLETED,
    TaskStatus.FAILED,
    TaskStatus.RETRYING,
    TaskStatus.RUNNING,
}


class ReplyStage:
    async def _send_notification(self, task: TaskRecord) -> None:
        """Tell the person what the task is doing, without making the task wait for it.

        A progress line ("Working on it: ...") is an edit of one message and is not needed before the next step can start, yet it
        was awaited: about a second of network on every step. It is sent in the background now, behind the previous one for the same
        task so that the lines arrive in order. Everything else (a question, an approval, the answer) is still awaited, and waits for
        any progress line still on its way first, so the answer never overtakes the bubble it deletes.
        """
        sink = self.notification_sink
        assert sink is not None
        pending = self._progress_sends.get(task.id)
        if task.status in {TaskStatus.RUNNING, TaskStatus.RETRYING}:
            async def send(previous: asyncio.Future[None] | None = pending) -> None:
                if previous is not None:
                    await asyncio.gather(previous, return_exceptions=True)
                try:
                    await sink.notify(task)
                except Exception:  # noqa: BLE001 - a missed progress line is not worth a failed task
                    logger.debug("progress notification failed", exc_info=True)

            future = asyncio.ensure_future(send())
            self._progress_sends[task.id] = future
            future.add_done_callback(lambda done, task_id=task.id: self._progress_sends.pop(task_id, None) if self._progress_sends.get(task_id) is done else None)
            return
        if pending is not None:
            await asyncio.gather(pending, return_exceptions=True)
        await sink.notify(task)

    async def _notify_if_needed(self, task: TaskRecord) -> None:
        if task.status not in NOTIFIABLE_STATUSES:
            return
        # CLARIFYING can happen more than once per task; key by question number
        # so the second question is not swallowed by the dedupe set.
        status_key = task.status.value
        if task.status == TaskStatus.CLARIFYING:
            status_key = f"clarifying:{task.metadata.get('clarify_count', 0)}"
        elif task.status in {TaskStatus.RETRYING, TaskStatus.RUNNING}:
            # Key on how many steps the operator has taken, so a long task
            # sends a progress update per step instead of one "working on it"
            # and then silence. This used to key on metadata["attempt_history"]
            # + current_step_id - both plan-era fields with zero writers since
            # P3, which collapsed the key to the constant "running" and killed
            # progress reporting entirely. See docs/archive/HISTORY.md §3.3.
            history = task.metadata.get("operator_history")
            if isinstance(history, list) and history:
                status_key = f"{task.status.value}:steps:{len(history)}"
        notified = set(task.metadata.get("notified_statuses", []))
        if self.notification_sink is not None and status_key not in notified:
            await self._send_notification(task)
        await self._remember_task_completion(task)
        latest = self.repositories.tasks.get(task.id)
        if latest is None:
            return
        updated_notified = sorted({*latest.metadata.get("notified_statuses", []), status_key})
        self.repositories.tasks.update_metadata(
            task.id,
            {**latest.metadata, "notified_statuses": updated_notified},
        )

    async def _remember_task_completion(self, task: TaskRecord) -> None:
        if not task.conversation_id or task.status not in {TaskStatus.COMPLETED, TaskStatus.BLOCKED, TaskStatus.FAILED}:
            return
        summary = task_memory_summary(task)
        if not summary:
            return
        try:
            await ConversationMemoryService(self.repositories).update_from_task_summary(task.conversation_id, summary)
        except Exception as exc:
            self.audit.append(
                AuditEventType.ERROR,
                actor="memory",
                task_id=task.id,
                payload={"error": "task_memory_update_failed", "reason": str(exc)},
            )
