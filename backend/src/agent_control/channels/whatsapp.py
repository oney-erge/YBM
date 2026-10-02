"""WhatsApp channel (docs/archive/UI_UX_AUDIT.md Phase 16) - the second real
consumer of `channels/base.py`'s channel-agnostic core, and deliberately
thin because of it: no `/command` slash syntax, no inline buttons, no voice
transcription - plain text only, going through the exact same
`classify_and_spawn_task`/`resume_clarifying_reply`/`status_summary`/
`approve_latest_pending` functions Telegram's own intake service calls.

Talks to WhatsApp via a local Node.js sidecar (`whatsapp-bridge/`, Baileys -
see channels/whatsapp_bridge_process.py for why), never to WhatsApp
directly - `WhatsAppBridgeClient` below is the local-HTTP equivalent of
`TelegramBotApi`, and the sidecar's messages are already a clean,
pre-normalized envelope (`{id, from, chat_id, text, timestamp}`), so
`WhatsAppAdapter` never touches Baileys' raw protocol objects.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from agent_control.channels.base import (
    ACKNOWLEDGMENT_TEXT,
    ChannelUpdateResult,
    approve_latest_pending,
    classify_and_spawn_task,
    resume_clarifying_reply,
    status_summary,
)
from agent_control.channels.memory import ConversationMemoryService, detect_remember_request
from agent_control.channels.responder import ChatResponder
from agent_control.config import AppSettings, WhatsAppConfig
from agent_control.llm.classifier import MessageClassifier
from agent_control.schemas import (
    AuditEventType,
    ChannelType,
    InboundMessage,
    MemoryFact,
    MemorySource,
    MessageKind,
    OutboundMessage,
)
from agent_control.storage.audit import AuditLogger
from agent_control.storage.repositories import Repositories


logger = logging.getLogger(__name__)


class WhatsAppBridgeClient:
    """Local-HTTP equivalent of `TelegramBotApi`, pointed at the sidecar
    (127.0.0.1:<port>) instead of a cloud API. `base_url`/`secret` are taken
    directly rather than a `WhatsAppBridgeProcess` reference, so this stays
    trivially fakeable in tests without spawning anything."""

    def __init__(self, base_url: str, secret: str) -> None:
        self.base_url = base_url.rstrip("/")
        self._headers = {"X-Bridge-Secret": secret}

    async def get_updates(self, offset: int = 0) -> tuple[list[dict[str, Any]], int]:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(f"{self.base_url}/updates", params={"offset": offset}, headers=self._headers)
            response.raise_for_status()
            data = response.json()
        return list(data.get("messages", [])), int(data.get("next_offset", offset))

    async def send_message(self, chat_id: str, text: str) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"{self.base_url}/send", json={"chat_id": chat_id, "text": text}, headers=self._headers
            )
            response.raise_for_status()
            return response.json()

    async def health(self) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(f"{self.base_url}/health", headers=self._headers)
            response.raise_for_status()
            return response.json()


def _number_from_jid(jid: str) -> str:
    """"15551234567:12@s.whatsapp.net" -> "15551234567" (the ":12" device
    suffix appears for multi-device linked accounts)."""
    return jid.split("@", 1)[0].split(":", 1)[0]


class WhatsAppAdapter:
    """Implements the `ChannelAdapter` Protocol (channels/base.py) - the
    intake half of WhatsApp support. Unlike Telegram's adapter, there is no
    raw wire format to parse here: the bridge already hands over a clean
    `{id, from, chat_id, text, timestamp}` envelope, so this is mostly the
    allowlist decision."""

    channel: ChannelType = ChannelType.WHATSAPP

    def __init__(self, config: WhatsAppConfig, audit: AuditLogger | None = None) -> None:
        self.config = config
        self.audit = audit

    def normalize_update(self, update: dict[str, Any]) -> ChannelUpdateResult:
        sender_jid = str(update.get("from") or "")
        chat_id = str(update.get("chat_id") or sender_jid)
        text = update.get("text")
        number = _number_from_jid(sender_jid)
        is_lid = bool(update.get("is_lid"))

        allowed, reason = self._authorization_decision(number, is_lid)
        if not allowed:
            self._audit_access(False, reason, number, chat_id, text)
            return ChannelUpdateResult(authorized=False, denial_reason="unauthorized")

        inbound = InboundMessage(
            id=f"whatsapp_{update.get('id')}",
            channel=ChannelType.WHATSAPP,
            kind=MessageKind.TEXT,
            sender_id=number,
            chat_id=chat_id,
            text=str(text) if text is not None else None,
            raw=update,
        )
        if self.audit:
            self.audit.append(
                AuditEventType.MESSAGE_RECEIVED,
                actor=f"whatsapp:user:{number}",
                correlation_id=inbound.correlation_id,
                payload={
                    "message_id": inbound.id,
                    "kind": inbound.kind.value,
                    "sender_id": inbound.sender_id,
                    "chat_id": inbound.chat_id,
                    "text": inbound.text,
                },
            )
        return ChannelUpdateResult(authorized=True, inbound_message=inbound)

    def _authorization_decision(self, number: str, is_lid: bool = False) -> tuple[bool, str]:
        if not self.config.enabled:
            return False, "whatsapp_disabled"
        if not self.config.allowed_numbers:
            return False, "allowlist_empty"
        if number not in self.config.allowed_numbers:
            if is_lid:
                # WhatsApp's privacy-preserving LID addressing sends an
                # opaque id in place of the real phone number - this can
                # never match allowed_numbers no matter how it's
                # configured. Labeled distinctly from a genuine
                # unknown-number denial so the audit trail doesn't read as
                # "add this number to the allowlist" when there is no
                # resolvable number to add.
                return False, "lid_jid_no_resolvable_number"
            return False, "number_not_allowed"
        return True, "allowed"

    def _audit_access(self, allowed: bool, reason: str, number: str, chat_id: str, text: Any) -> None:
        if self.audit:
            self.audit.append(
                AuditEventType.CHANNEL_ACCESS_DECISION,
                actor="whatsapp",
                payload={
                    "channel": ChannelType.WHATSAPP.value,
                    "allowed": allowed,
                    "reason": reason,
                    "sender_number": number,
                    "chat_id": chat_id,
                    "allowed_numbers": self.config.allowed_numbers,
                    "text_preview": str(text)[:240] if text else None,
                },
            )


class WhatsAppIntakeService:
    """Mirrors `TelegramIntakeService`'s shape, but thin - the classify/task
    heavy lifting all lives in channels/base.py."""

    def __init__(
        self,
        adapter: WhatsAppAdapter,
        repositories: Repositories,
        audit: AuditLogger,
        *,
        settings: AppSettings | None = None,
        bridge_client: WhatsAppBridgeClient | None = None,
        classifier: MessageClassifier | None = None,
        responder: ChatResponder | None = None,
        memory_service: ConversationMemoryService | None = None,
    ) -> None:
        self.adapter = adapter
        self.repositories = repositories
        self.audit = audit
        self.settings = settings
        self.bridge_client = bridge_client
        self.classifier = classifier
        self.responder = responder
        self.memory_service = memory_service or ConversationMemoryService(repositories)

    async def handle_update_async(self, update: dict[str, Any]) -> ChannelUpdateResult:
        result = self.adapter.normalize_update(update)
        if not result.authorized or result.inbound_message is None:
            return result

        inbound = result.inbound_message
        conversation_id = self.repositories.conversations.get_or_create(inbound.channel, inbound.chat_id)
        if not self.repositories.messages.try_create(inbound, conversation_id):
            return ChannelUpdateResult(authorized=True, inbound_message=inbound)
        if inbound.text:
            await self.memory_service.update_from_user_message(conversation_id, inbound.text)

        plain_response = self._plain_text_command_response(inbound)
        if plain_response is not None:
            return ChannelUpdateResult(authorized=True, inbound_message=inbound, outbound_message=plain_response)

        clarify_response = resume_clarifying_reply(self.repositories, self.audit, inbound, conversation_id)
        if clarify_response is not None:
            return ChannelUpdateResult(authorized=True, inbound_message=inbound, outbound_message=clarify_response)

        return await classify_and_spawn_task(
            inbound, conversation_id,
            repositories=self.repositories, audit=self.audit,
            classifier=self.classifier, responder=self.responder, settings=self.settings,
            send_progress=self._send_progress,
        )

    def _plain_text_command_response(self, inbound: InboundMessage) -> OutboundMessage | None:
        # "Remember that ..." (docs/archive/UI_UX_AUDIT.md Phase 15) is checked here,
        # at the runtime level, before the LLM classifier ever sees the
        # message - same precedence and provenance guarantee Telegram's own
        # plain-text layer already established.
        remember_content = detect_remember_request(inbound.text or "")
        if remember_content is not None:
            self.repositories.memory_facts.create(
                MemoryFact(category="user_note", content=remember_content, source=MemorySource.USER_STATED)
            )
            return _reply(inbound, f"Got it, I'll remember: {remember_content}")
        text = (inbound.text or "").strip().lower()
        if text in {"approve", "approved", "approve it", "yes approve", "yes, approve"}:
            return approve_latest_pending(self.repositories, self.audit, inbound)
        if text in {"status", "task status", "tasks status", "what is the status"}:
            return _reply(inbound, status_summary(self.repositories))
        return None

    async def _send_progress(self, chat_id: str, text: str) -> None:
        if self.bridge_client is None:
            return
        if text == ACKNOWLEDGMENT_TEXT:
            # Skip the pre-classification "got your message" filler
            # specifically (docs/archive/UI_UX_AUDIT.md Phase 16 review) - it has
            # no lasting information: the real chat reply, or the
            # task-started message that follows it, arrives within the
            # same handling of this update regardless. WhatsApp's own
            # account-flagging risk (why a secondary number is recommended
            # over a primary one) makes trimming outbound volume worth
            # more here than the "still alive" reassurance is worth -
            # unlike Telegram's TelegramIntakeService._send_progress, which
            # sends both unchanged over the official bot API.
            return
        try:
            await self.bridge_client.send_message(chat_id, text)
        except Exception as exc:
            logger.warning("failed to send WhatsApp progress message", exc_info=True)
            self.audit.append(
                AuditEventType.ERROR, actor="whatsapp_bridge",
                payload={"error": "send_progress_failed", "reason": str(exc)},
            )


def _reply(inbound: InboundMessage, text: str) -> OutboundMessage:
    return OutboundMessage(channel=ChannelType.WHATSAPP, chat_id=inbound.chat_id, text=text)


class WhatsAppPollingRunner:
    """Mirrors `TelegramPollingRunner.poll_once` - polls the LOCAL bridge
    instead of Telegram's cloud API, everything else identical in shape."""

    def __init__(self, client: WhatsAppBridgeClient, intake: WhatsAppIntakeService) -> None:
        self.client = client
        self.intake = intake

    async def poll_once(self, offset: int = 0) -> tuple[int, list[ChannelUpdateResult]]:
        updates, next_offset = await self.client.get_updates(offset=offset)
        results: list[ChannelUpdateResult] = []
        for update in updates:
            try:
                result = await self.intake.handle_update_async(update)
                results.append(result)
                if result.outbound_message and result.outbound_message.text:
                    await self.client.send_message(result.outbound_message.chat_id, result.outbound_message.text)
            except Exception as exc:
                self.intake.audit.append(
                    AuditEventType.ERROR,
                    actor="whatsapp_polling",
                    payload={"error": "update_processing_failed", "message_id": update.get("id"), "reason": str(exc)},
                )
        return next_offset, results
