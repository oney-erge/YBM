from __future__ import annotations

from agent_control.channels.task_notify import format_task_message
from agent_control.channels.whatsapp import WhatsAppBridgeClient
from agent_control.schemas import ChannelType, TaskRecord, channel_chat_id


class WhatsAppTaskNotifier:
    """Implements `TaskNotificationSink` (orchestration/worker.py) - the
    WhatsApp half of notify, mirroring `TelegramTaskNotifier`'s shape but
    plain-text only (no inline keyboard, no separate screenshot delivery -
    docs/archive/UI_UX_AUDIT.md Phase 16's disclosed v1 scope)."""

    def __init__(self, client: WhatsAppBridgeClient) -> None:
        self.client = client

    async def notify(self, task: TaskRecord) -> None:
        chat_id = channel_chat_id(task, ChannelType.WHATSAPP)
        if not chat_id:
            return
        await self.client.send_message(chat_id, format_task_message(task))
