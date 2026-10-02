from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from agent_control.channels.base import (
    ChannelUpdateResult,
    approve_latest_pending,
    classify_and_spawn_task,
    resume_clarifying_reply,
    status_summary,
)
from agent_control.config import AppSettings, TelegramConfig
from agent_control.config_sync import read_env_value
from agent_control.channels.responder import ChatResponder
from agent_control.channels.memory import ConversationMemoryService, detect_remember_request
from agent_control.llm.classifier import MessageClassifier
from agent_control.orchestration.signals import apply_task_signal, requeue_after_approval_decision
from agent_control.schemas import (
    ApprovalRequest,
    ApprovalStatus,
    AuditEventType,
    Artifact,
    ArtifactType,
    Capability,
    ChannelType,
    CommandEnvelope,
    InboundMessage,
    MemoryFact,
    MemorySource,
    MessageKind,
    OutboundMessage,
    TaskSignal,
    VoiceAttachment,
)
from agent_control.error_text import explain_for_user, explain_voice_failure
from agent_control.storage.audit import AuditLogger
from agent_control.storage.repositories import Repositories
from agent_control.observation.screenshot import ScreenshotService
from agent_control.tools.coding_agent import (
    PROVIDERS as CODING_PROVIDERS,
    format_session_status,
    latest_session,
    load_sessions,
    read_log_tail,
    stop_session_process,
)
from agent_control.tools.stt import STTAdapter


def _preview(value: str | None, limit: int = 240) -> str | None:
    if value is None:
        return None
    return value if len(value) <= limit else f"{value[: limit - 3]}..."


def load_telegram_token(config: TelegramConfig) -> str:
    if config.token:
        return config.token.get_secret_value()
    token = read_env_value(config.token_env)
    if not token:
        raise RuntimeError(f"Telegram token not found in {config.token_env}")
    return token


logger = logging.getLogger(__name__)


class TelegramBotApi:
    def __init__(
        self,
        token: str,
        base_url: str = "https://api.telegram.org",
        audit: AuditLogger | None = None,
    ) -> None:
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.audit = audit

    async def get_me(self) -> dict[str, Any]:
        """Cheapest possible "is this token real" probe - used by the admin
        console's Connect flow to verify a pasted token before it is written
        to .env, and to show the operator the bot handle it resolved to."""
        data = await self._post("getMe", {})
        return dict(data.get("result", {}))

    async def get_updates(self, offset: int | None = None, timeout: int = 30) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"timeout": timeout}
        if offset is not None:
            payload["offset"] = offset
        data = await self._post("getUpdates", payload)
        return list(data.get("result", []))

    async def send_message(
        self, chat_id: str | int, text: str, reply_markup: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        data = await self._post("sendMessage", payload)
        self._log_sent(chat_id, kind="text", text=text, response=data)
        return data

    async def send_photo_file(self, chat_id: str | int, path: str, caption: str | None = None) -> dict[str, Any]:
        payload = {"chat_id": str(chat_id)}
        if caption:
            payload["caption"] = caption
        url = f"{self.base_url}/bot{self.token}/sendPhoto"
        try:
            with open(path, "rb") as photo:
                async with httpx.AsyncClient(timeout=60) as client:
                    response = await client.post(url, data=payload, files={"photo": photo})
                    response.raise_for_status()
                    data = response.json()
        except httpx.HTTPError as exc:
            raise _safe_telegram_http_error("sendPhoto", exc) from None
        if not data.get("ok"):
            raise RuntimeError("Telegram Bot API call failed: sendPhoto")
        self._log_sent(chat_id, kind="photo", caption=caption, path=path, response=data)
        return data

    async def send_document_file(self, chat_id: str | int, path: str, caption: str | None = None) -> dict[str, Any]:
        payload = {"chat_id": str(chat_id)}
        if caption:
            payload["caption"] = caption
        url = f"{self.base_url}/bot{self.token}/sendDocument"
        try:
            with open(path, "rb") as document:
                async with httpx.AsyncClient(timeout=60) as client:
                    response = await client.post(url, data=payload, files={"document": document})
                    response.raise_for_status()
                    data = response.json()
        except httpx.HTTPError as exc:
            raise _safe_telegram_http_error("sendDocument", exc) from None
        if not data.get("ok"):
            raise RuntimeError("Telegram Bot API call failed: sendDocument")
        self._log_sent(chat_id, kind="document", caption=caption, path=path, response=data)
        return data

    def _log_sent(
        self,
        chat_id: str | int,
        *,
        kind: str,
        response: dict[str, Any],
        text: str | None = None,
        caption: str | None = None,
        path: str | None = None,
    ) -> None:
        # Durable local record of what YBM actually sent - the E2E runner's
        # truth source, independent of re-reading Telegram's live chat state.
        if self.audit is None:
            return
        result = response.get("result") if isinstance(response.get("result"), dict) else {}
        payload: dict[str, Any] = {
            "chat_id": str(chat_id),
            "kind": kind,
            "telegram_message_id": result.get("message_id"),
        }
        if text is not None:
            payload["text"] = text
        if caption is not None:
            payload["caption"] = caption
        if path is not None:
            payload["path"] = path
        try:
            self.audit.append(AuditEventType.MESSAGE_SENT, actor="telegram_bot_api", payload=payload)
        except Exception:
            # The message already went out to the user - this only means its
            # audit-trail record might be missing, which is worth knowing
            # about given how much this system leans on the audit trail for
            # "what did the agent actually do."
            logger.warning("failed to record MESSAGE_SENT audit event", exc_info=True)

    async def get_file(self, file_id: str) -> dict[str, Any]:
        data = await self._post("getFile", {"file_id": file_id})
        return dict(data.get("result", {}))

    async def download_file(self, file_path: str) -> bytes:
        url = f"{self.base_url}/file/bot{self.token}/{file_path}"
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                response = await client.get(url)
                response.raise_for_status()
                return response.content
        except httpx.HTTPError as exc:
            raise _safe_telegram_http_error("downloadFile", exc) from None

    async def answer_callback_query(self, callback_query_id: str, text: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        return await self._post("answerCallbackQuery", payload)

    async def _post(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.base_url}/bot{self.token}/{method}"
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                response = await client.post(url, json=payload)
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPError as exc:
            raise _safe_telegram_http_error(method, exc) from None
        if not data.get("ok"):
            raise RuntimeError(f"Telegram Bot API call failed: {method}")
        return data


def _safe_telegram_http_error(method: str, exc: httpx.HTTPError) -> RuntimeError:
    """Describe a Telegram transport failure without echoing its token URL."""
    if isinstance(exc, httpx.HTTPStatusError):
        return RuntimeError(
            f"Telegram Bot API request failed: {method} (HTTP {exc.response.status_code})"
        )
    return RuntimeError(f"Telegram Bot API request failed: {method} ({type(exc).__name__})")


class TelegramPollingRunner:
    def __init__(self, client: TelegramBotApi, intake: "TelegramIntakeService") -> None:
        self.client = client
        self.intake = intake

    async def poll_once(self, offset: int | None = None, timeout: int = 30) -> tuple[int | None, list[ChannelUpdateResult]]:
        updates = await self.client.get_updates(offset=offset, timeout=timeout)
        results: list[ChannelUpdateResult] = []
        next_offset = offset
        for update in updates:
            update_id = update.get("update_id")
            try:
                result = await self.intake.handle_update_async(update)
                results.append(result)
                if result.outbound_message and result.outbound_message.text:
                    await self.client.send_message(result.outbound_message.chat_id, result.outbound_message.text)
                if result.outbound_message and result.outbound_message.artifact_ids:
                    await self._send_artifacts(result.outbound_message)
            except Exception as exc:
                if self.intake.audit:
                    self.intake.audit.append(
                        AuditEventType.ERROR,
                        actor="telegram_polling",
                        payload={
                            "error": "update_processing_failed",
                            "update_id": update_id,
                            "reason": str(exc),
                        },
                    )
            finally:
                if isinstance(update_id, int):
                    next_offset = update_id + 1
        return next_offset, results

    async def _send_artifacts(self, outbound_message: "OutboundMessage") -> None:
        for artifact_id in outbound_message.artifact_ids:
            artifact = self.intake.repositories.artifacts.get(artifact_id)
            if artifact and artifact.type == ArtifactType.SCREENSHOT and artifact.uri:
                await self.client.send_photo_file(outbound_message.chat_id, artifact.uri, artifact.content_preview)
            elif artifact and artifact.uri:
                await self.client.send_document_file(outbound_message.chat_id, artifact.uri, artifact.content_preview)


class TelegramAdapter:
    """Implements the `ChannelAdapter` Protocol (channels/base.py,
    docs/archive/UI_UX_AUDIT.md Phase 16): the intake half of Telegram support -
    raw webhook JSON in, `ChannelUpdateResult` out. A future channel's
    adapter (WhatsApp/Discord/...) implements the same shape."""

    channel: ChannelType = ChannelType.TELEGRAM

    def __init__(self, config: TelegramConfig, audit: AuditLogger | None = None) -> None:
        self.config = config
        self.audit = audit

    def normalize_update(self, update: dict[str, Any]) -> ChannelUpdateResult:
        if "callback_query" in update:
            return self._normalize_callback(update["callback_query"], update)
        if "message" in update:
            return self._normalize_message(update["message"], update)
        return ChannelUpdateResult(authorized=False, denial_reason="unsupported_update")

    def _normalize_message(self, message: dict[str, Any], raw_update: dict[str, Any]) -> ChannelUpdateResult:
        sender = message.get("from") or {}
        chat = message.get("chat") or {}
        user_id = sender.get("id")
        chat_id = chat.get("id")

        text = self._message_text(message)
        allowed, reason = self._authorization_decision(user_id, chat_id)
        if not allowed:
            self._audit_telegram_access(False, reason, user_id, chat_id, text, message)
            return ChannelUpdateResult(authorized=False, denial_reason="unauthorized")

        voice = message.get("voice")
        kind = MessageKind.VOICE if voice else MessageKind.TEXT
        attachments = []
        if voice:
            attachments.append(
                VoiceAttachment(
                    file_id=voice.get("file_id"),
                    duration_seconds=voice.get("duration"),
                    mime_type=voice.get("mime_type"),
                    size_bytes=voice.get("file_size"),
                    metadata={"file_unique_id": voice.get("file_unique_id")},
                )
            )

        inbound = InboundMessage(
            id=f"telegram_{message.get('message_id')}",
            channel=ChannelType.TELEGRAM,
            kind=kind,
            sender_id=str(user_id),
            chat_id=str(chat_id),
            text=text,
            attachments=attachments,
            raw=raw_update,
        )

        if text and text.startswith("/"):
            command = self._command_from_text(text, inbound)
            return ChannelUpdateResult(authorized=True, inbound_message=inbound, command=command)

        if self.audit:
            self.audit.append(
                AuditEventType.MESSAGE_RECEIVED,
                actor=f"telegram:user:{user_id}",
                correlation_id=inbound.correlation_id,
                payload={
                    "message_id": inbound.id,
                    "kind": inbound.kind.value,
                    "sender_id": inbound.sender_id,
                    "chat_id": inbound.chat_id,
                    "text": text,
                    "text_preview": _preview(text),
                    "has_forward_origin": "forward_origin" in message or "forward_from" in message,
                    "forward_origin": message.get("forward_origin") or message.get("forward_from"),
                },
            )
        return ChannelUpdateResult(authorized=True, inbound_message=inbound)

    def _normalize_callback(self, callback: dict[str, Any], raw_update: dict[str, Any]) -> ChannelUpdateResult:
        sender = callback.get("from") or {}
        message = callback.get("message") or {}
        chat = message.get("chat") or {}
        user_id = sender.get("id")
        chat_id = chat.get("id")

        allowed, reason = self._authorization_decision(user_id, chat_id)
        if not allowed:
            self._audit_telegram_access(False, reason, user_id, chat_id, None, callback)
            return ChannelUpdateResult(authorized=False, denial_reason="unauthorized")

        data = callback.get("data") or ""
        payload = self._parse_callback_data(data)
        command = CommandEnvelope(
            type="telegram.callback",
            source=f"telegram:{user_id}",
            payload={**payload, "callback_query_id": callback.get("id"), "raw": raw_update},
        )
        return ChannelUpdateResult(authorized=True, command=command)

    @staticmethod
    def _message_text(message: dict[str, Any]) -> str | None:
        value = message.get("text") or message.get("caption")
        return str(value) if value is not None else None

    def _authorization_decision(self, user_id: int | None, chat_id: int | None) -> tuple[bool, str]:
        if not self.config.enabled:
            return False, "telegram_disabled"
        has_user_allowlist = bool(self.config.allowed_user_ids)
        has_chat_allowlist = bool(self.config.allowed_chat_ids)
        if not has_user_allowlist and not has_chat_allowlist:
            return False, "allowlist_empty"
        if has_user_allowlist and user_id not in self.config.allowed_user_ids:
            return False, "user_not_allowed"
        if has_chat_allowlist and chat_id not in self.config.allowed_chat_ids:
            return False, "chat_not_allowed"
        return True, "allowed"

    def _command_from_text(self, text: str, inbound: InboundMessage) -> CommandEnvelope:
        parts = text.strip().split()
        command = parts[0].removeprefix("/").split("@", 1)[0].lower()
        return CommandEnvelope(
            type="telegram.command",
            source=f"telegram:{inbound.sender_id}",
            correlation_id=inbound.correlation_id,
            payload={
                "command": command,
                "args": parts[1:],
                "chat_id": inbound.chat_id,
                "message_id": inbound.id,
            },
        )

    @staticmethod
    def _parse_callback_data(data: str) -> dict[str, Any]:
        parts = data.split(":")
        if len(parts) == 3 and parts[0] == "approval":
            return {"kind": "approval", "approval_id": parts[1], "decision": parts[2]}
        if len(parts) == 3 and parts[0] == "task":
            return {"kind": "task", "task_id": parts[1], "action": parts[2]}
        return {"kind": "unknown", "data": data}

    def _audit_telegram_access(
        self,
        allowed: bool,
        reason: str,
        user_id: int | None,
        chat_id: int | None,
        text: str | None,
        raw: dict[str, Any],
    ) -> None:
        if self.audit:
            self.audit.append(
                AuditEventType.TELEGRAM_ACCESS_DECISION,
                actor="telegram",
                payload={
                    "allowed": allowed,
                    "reason": reason,
                    "user_id": user_id,
                    "chat_id": chat_id,
                    "telegram_enabled": self.config.enabled,
                    "allowed_user_ids": self.config.allowed_user_ids,
                    "allowed_chat_ids": self.config.allowed_chat_ids,
                    "text_preview": _preview(text),
                    "message_id": raw.get("message_id") or raw.get("id"),
                },
            )


class TelegramIntakeService:
    def __init__(
        self,
        adapter: TelegramAdapter,
        repositories: Repositories,
        audit: AuditLogger,
        settings: AppSettings | None = None,
        bot_api: TelegramBotApi | None = None,
        stt: STTAdapter | None = None,
        screenshot_service: ScreenshotService | None = None,
        classifier: MessageClassifier | None = None,
        responder: ChatResponder | None = None,
        memory_service: ConversationMemoryService | None = None,
    ) -> None:
        self.adapter = adapter
        self.repositories = repositories
        self.audit = audit
        self.settings = settings
        self.bot_api = bot_api
        self.stt = stt
        self.screenshot_service = screenshot_service
        self.classifier = classifier
        self.responder = responder
        self.memory_service = memory_service or ConversationMemoryService(repositories)
        # Set per callback by _apply_callback, read once at the answer site.
        # Updates are handled one at a time by the polling loop, so this never
        # spans two presses.
        self._callback_ack: str | None = None

    def handle_update(self, update: dict[str, Any]) -> ChannelUpdateResult:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.handle_update_async(update))
        raise RuntimeError("handle_update cannot run inside an active event loop; use handle_update_async")

    async def handle_update_async(self, update: dict[str, Any]) -> ChannelUpdateResult:
        result = self.adapter.normalize_update(update)
        if not result.authorized:
            return result

        if result.inbound_message:
            conversation_id = self.repositories.conversations.get_or_create(
                result.inbound_message.channel,
                result.inbound_message.chat_id,
            )
            inbound = result.inbound_message
            if inbound.kind == MessageKind.VOICE and not inbound.text:
                try:
                    inbound = await self._transcribe_voice(inbound)
                except Exception as exc:
                    self.audit.append(
                        AuditEventType.ERROR,
                        actor=f"telegram:user:{inbound.sender_id}",
                        correlation_id=inbound.correlation_id,
                        payload={
                            "error": "voice_transcription_failed",
                            "message_id": inbound.id,
                            "reason": str(exc),
                        },
                    )
                    return ChannelUpdateResult(
                        authorized=True,
                        inbound_message=inbound,
                        # Was f"Voice transcription failed: {exc}", which sent
                        # "RuntimeError: STT adapter is disabled" to a person -
                        # and called a feature being switched off a failure.
                        outbound_message=self._out(inbound.chat_id, explain_voice_failure(exc)),
                    )
            if not self.repositories.messages.try_create(inbound, conversation_id):
                return ChannelUpdateResult(authorized=True, inbound_message=inbound)
            if inbound.text:
                await self._update_conversation_memory(conversation_id, inbound.text)

            if result.command is None:
                plain_response = self._plain_text_command_response(inbound)
                if plain_response is not None:
                    return ChannelUpdateResult(
                        authorized=True,
                        inbound_message=inbound,
                        outbound_message=plain_response,
                    )
                clarify_response = resume_clarifying_reply(self.repositories, self.audit, inbound, conversation_id)
                if clarify_response is not None:
                    return ChannelUpdateResult(
                        authorized=True,
                        inbound_message=inbound,
                        outbound_message=clarify_response,
                    )
                return await classify_and_spawn_task(
                    inbound, conversation_id,
                    repositories=self.repositories, audit=self.audit,
                    classifier=self.classifier, responder=self.responder, settings=self.settings,
                    send_progress=self._send_progress,
                )

        if result.command:
            self._callback_ack = None
            signal = self._apply_command(result.command)
            outbound = self._command_response(result.command, signal)
            if result.command.type == "telegram.callback" and self.bot_api:
                callback_query_id = result.command.payload.get("callback_query_id")
                if callback_query_id:
                    try:
                        # Pass the note through. Telegram shows this as a toast
                        # on the button press - the only channel that can tell
                        # the operator whether the tap did anything, since
                        # _command_response() never builds a reply for callbacks.
                        await self.bot_api.answer_callback_query(
                            callback_query_id, text=self._callback_ack
                        )
                    except Exception:
                        # The decision was already recorded (_apply_callback already
                        # wrote it); this only stops the button's loading spinner,
                        # so a failure here shouldn't surface as a handling error.
                        logger.warning("failed to answer Telegram callback query", exc_info=True)
            return ChannelUpdateResult(
                authorized=True,
                inbound_message=result.inbound_message,
                command=result.command,
                signal=signal,
                outbound_message=outbound,
            )

        return result

    async def _update_conversation_memory(self, conversation_id: str, text: str) -> None:
        await self.memory_service.update_from_user_message(conversation_id, text)

    async def _transcribe_voice(self, inbound: InboundMessage) -> InboundMessage:
        if self.bot_api is None or self.stt is None:
            raise RuntimeError("voice transcription is not configured")
        voice = next((attachment for attachment in inbound.attachments if isinstance(attachment, VoiceAttachment)), None)
        if voice is None or not voice.file_id:
            raise RuntimeError("voice file id is missing")

        file_info = await self.bot_api.get_file(voice.file_id)
        file_path = file_info.get("file_path")
        if not file_path:
            raise RuntimeError("Telegram did not return a voice file path")

        audio = await self.bot_api.download_file(file_path)
        transcript = await self.stt.transcribe(
            audio,
            file_name=file_path.rsplit("/", 1)[-1],
            mime_type=voice.mime_type,
        )
        text = transcript.text.strip()
        if not text:
            raise RuntimeError("voice transcript was empty")

        updated_attachments = [
            attachment.model_copy(update={"transcript": text}) if isinstance(attachment, VoiceAttachment) else attachment
            for attachment in inbound.attachments
        ]
        self.audit.append(
            AuditEventType.MESSAGE_RECEIVED,
            actor=f"telegram:user:{inbound.sender_id}",
            correlation_id=inbound.correlation_id,
            payload={
                "message_id": inbound.id,
                "kind": MessageKind.VOICE.value,
                "sender_id": inbound.sender_id,
                "chat_id": inbound.chat_id,
                "text": text,
                "text_preview": _preview(text),
                "voice_file_id": voice.file_id,
                "voice_transcribed": True,
                "transcription": transcript.model_dump(mode="json"),
            },
        )
        return inbound.model_copy(update={"text": text, "attachments": updated_attachments})

    async def _send_progress(self, chat_id: str, text: str) -> None:
        if self.bot_api is None:
            return
        try:
            await self.bot_api.send_message(chat_id, text)
        except Exception as exc:
            self.audit.append(
                AuditEventType.ERROR,
                actor="telegram_progress",
                payload={"error": "progress_send_failed", "reason": str(exc), "chat_id": chat_id},
            )

    def _apply_command(self, command: CommandEnvelope) -> TaskSignal | None:
        payload = command.payload
        if command.type == "telegram.callback":
            return self._apply_callback(payload, command.source)

        name = payload.get("command")
        args = payload.get("args") or []
        if name in {"pause", "resume", "cancel"} and args:
            return self._create_task_signal(args[0], name, command.source, payload)
        return None

    def _command_response(self, command: CommandEnvelope, signal: TaskSignal | None) -> OutboundMessage | None:
        if command.type != "telegram.command":
            return None
        payload = command.payload
        name = payload.get("command")
        chat_id = str(payload.get("chat_id"))
        args = payload.get("args") or []

        if name == "status":
            return self._out(chat_id, status_summary(self.repositories))
        if name == "agents":
            return self._out(chat_id, self._agents_summary())
        if name == "tasks":
            tasks = self.repositories.tasks.list_recent(10)
            if not tasks:
                return self._out(chat_id, "No tasks found.")
            lines = [f"{task.id} | {task.status.value} | {task.objective[:80]}" for task in tasks]
            return self._out(chat_id, "\n".join(lines))
        if name == "task" and args:
            task = self.repositories.tasks.get(args[0])
            if not task:
                return self._out(chat_id, f"Task not found: {args[0]}")
            return self._out(chat_id, f"{task.id}\nstatus: {task.status.value}\nobjective: {task.objective}")
        if name == "logs" and args:
            events = self.repositories.audit.list_for_task(args[0])[-10:]
            if not events:
                return self._out(chat_id, f"No logs found for {args[0]}.")
            lines = [f"{event.created_at.isoformat()} | {event.type.value} | {event.actor}" for event in events]
            return self._out(chat_id, "\n".join(lines))
        if name == "screenshot":
            policy = self.settings.capabilities.get(Capability.DESKTOP_SCREENSHOT) if self.settings else None
            enabled = bool(policy and policy.enabled)
            if not enabled:
                return self._out(
                    chat_id,
                    "Screenshots are turned off. Enable them in the admin console under "
                    "Access > desktop.screenshot, and set adapters.desktop.screenshot_enabled: "
                    "true in config/config.yaml - both are required.",
                )
            if not self.screenshot_service:
                return self._out(
                    chat_id,
                    "Screenshots are allowed by policy, but capture is not set up on this "
                    "machine. Set adapters.desktop.screenshot_enabled: true in "
                    "config/config.yaml, then restart the stack.",
                )
            try:
                artifact = self.screenshot_service.capture()
            except Exception as exc:
                # Same class of leak as the voice reply above.
                return self._out(chat_id, f"I couldn't take the screenshot. {explain_for_user(exc)}")
            return OutboundMessage(
                channel=ChannelType.TELEGRAM,
                chat_id=chat_id,
                text="Screenshot captured.",
                artifact_ids=[artifact.id],
            )
        if name in {"pause", "resume", "cancel"}:
            if signal:
                return self._out(chat_id, f"{name} signal recorded for {signal.task_id}.")
            return self._out(chat_id, f"Usage: /{name} <task_id>")
        return None

    def _remember_from_message(self, inbound: InboundMessage) -> OutboundMessage | None:
        content = detect_remember_request(inbound.text or "")
        if content is None:
            return None
        self.repositories.memory_facts.create(
            MemoryFact(category="user_note", content=content, source=MemorySource.USER_STATED)
        )
        return self._out(inbound.chat_id, f"Got it, I'll remember: {content}")

    def _plain_text_command_response(self, inbound: InboundMessage) -> OutboundMessage | None:
        # "Remember that ..." (docs/archive/UI_UX_AUDIT.md Phase 15) is checked here,
        # at the runtime level, before the LLM classifier ever sees the
        # message - provenance is decided by the runtime, never selectable
        # by the model, same guarantee task_derived facts already have via
        # memory.manage. Same precedence as every other plain-text command:
        # it wins even while a clarification is pending (see the caller).
        remember_response = self._remember_from_message(inbound)
        if remember_response is not None:
            return remember_response
        text = (inbound.text or "").strip().lower()
        if text in {"approve", "approved", "approve it", "yes approve", "yes, approve"}:
            return approve_latest_pending(self.repositories, self.audit, inbound)
        if text in {"status", "task status", "tasks status", "what is the status"}:
            return self._out(inbound.chat_id, status_summary(self.repositories))
        if text in {"tasks", "list tasks", "show tasks"}:
            tasks = self.repositories.tasks.list_recent(10)
            if not tasks:
                return self._out(inbound.chat_id, "No tasks found.")
            lines = [f"{task.id} | {task.status.value} | {task.objective[:80]}" for task in tasks]
            return self._out(inbound.chat_id, "\n".join(lines))
        agent_reply = self._coding_agent_fast_reply(text)
        if agent_reply is not None:
            return self._out(inbound.chat_id, agent_reply)
        return None

    # Deterministic coding-agent queries answered straight from session files,
    # so "what is codex doing" works instantly even when every LLM is down.
    _CODING_ALIASES = {"codex": "codex", "claude": "claude_code", "copilot": "github_copilot"}
    _CODING_STATUS_MARKERS = (
        "status", "doing", "done", "finished", "finish", "progress", "update",
        "up to", "how is", "how's", "going", "say", "output", "working",
    )
    _CODING_STOP_MARKERS = ("stop", "kill", "abort", "terminate")

    def _coding_agent_fast_reply(self, text: str) -> str | None:
        if not text or len(text) > 120 or self.settings is None:
            return None
        provider = next(
            (canonical for alias, canonical in self._CODING_ALIASES.items() if alias in text),
            None,
        )
        if provider is None:
            return None
        session_root = self.settings.adapters.coding_agent.session_root
        if any(marker in text for marker in self._CODING_STOP_MARKERS):
            session = latest_session(session_root, provider=provider)
            if session is None or session.get("status") != "running":
                return f"No running {provider} session to stop."
            stop_session_process(session)
            return f"Stop requested for {provider} session {session.get('session_id')}. A final report follows once it exits."
        if any(marker in text for marker in self._CODING_STATUS_MARKERS) or text.strip() == provider:
            session = latest_session(session_root, provider=provider)
            if session is None:
                return f"No {provider} sessions have run yet."
            log_tail = read_log_tail(str(session.get("log_path") or ""))
            return format_session_status(session, log_tail=log_tail)[:3900]
        return None

    def _agents_summary(self) -> str:
        if self.settings is None:
            return "Coding agent sessions are not configured."
        sessions = load_sessions(self.settings.adapters.coding_agent.session_root, limit=8)
        if not sessions:
            return "No coding agent sessions yet. Providers: " + ", ".join(CODING_PROVIDERS)
        lines = ["Recent coding sessions:"]
        for session in sessions:
            lines.append(
                f"- {session.get('provider')} | {session.get('status')} | {session.get('session_id')} | "
                f"{str(session.get('prompt') or '')[:60]}"
            )
        return "\n".join(lines)

    @staticmethod
    def _out(chat_id: str, text: str) -> OutboundMessage:
        return OutboundMessage(channel=ChannelType.TELEGRAM, chat_id=chat_id, text=text)

    def _stale_approval_note(self, approval: ApprovalRequest | None) -> str:
        """Say why a button press did nothing, in the operator's terms."""
        if approval is None:
            return "That approval is no longer available."
        if approval.status in (ApprovalStatus.APPROVED, ApprovalStatus.CONSUMED):
            return "Already approved - this button was from an earlier step."
        if approval.status == ApprovalStatus.REJECTED:
            return "Already rejected - this button was from an earlier step."
        if approval.status == ApprovalStatus.EXPIRED:
            return "That approval expired. Send the request again."
        return f"No longer actionable (status: {approval.status.value})."

    def _apply_callback(self, payload: dict[str, Any], actor: str) -> TaskSignal | None:
        if payload.get("kind") == "approval":
            approval_id = str(payload.get("approval_id") or "")
            approval = self.repositories.approvals.get(approval_id) if approval_id else None
            decision = payload.get("decision")
            decided = False
            if decision == "approve":
                decided = self.repositories.approvals.decide_pending(approval_id, ApprovalStatus.APPROVED)
            elif decision == "reject":
                decided = self.repositories.approvals.decide_pending(approval_id, ApprovalStatus.REJECTED)
            if decided:
                if approval is not None:
                    requeue_after_approval_decision(self.repositories, approval.task_id)
                self.audit.append(
                    AuditEventType.APPROVAL_DECIDED,
                    actor=actor,
                    task_id=approval.task_id if approval is not None else None,
                    payload={"approval_id": approval_id, "decision": decision},
                )
                self._callback_ack = "Approved - continuing." if decision == "approve" else "Rejected."
            else:
                # Every non-decision used to be silent. decide_pending() returns
                # False for an approval that is already decided, expired, or
                # unknown, and the only acknowledgement sent was an empty
                # answerCallbackQuery - which just stops the button spinner. A
                # successful approve looked exactly like a dead button, so a
                # stale message from an earlier step read as "I click approve
                # and nothing happens." Each of these now says which one it was.
                self._callback_ack = self._stale_approval_note(approval)
            return None

        if payload.get("kind") == "task" and payload.get("action") in {"pause", "resume", "cancel"}:
            return self._create_task_signal(payload["task_id"], payload["action"], actor, payload)
        return None

    def _create_task_signal(
        self,
        task_id: str,
        action: str,
        actor: str,
        payload: dict[str, Any],
    ) -> TaskSignal:
        signal, _, _ = apply_task_signal(
            self.repositories, self.audit, task_id, action, actor, payload, settings=self.settings
        )
        return signal


class TelegramVoiceIntakeService:
    def __init__(
        self,
        adapter: TelegramAdapter,
        bot_api: TelegramBotApi,
        stt: STTAdapter,
        repositories: Repositories,
        audit: AuditLogger,
    ) -> None:
        self.adapter = adapter
        self.bot_api = bot_api
        self.stt = stt
        self.repositories = repositories
        self.audit = audit

    async def _tell_user(self, result: "ChannelUpdateResult", text: str) -> None:
        """Say something back when a voice note cannot be handled.

        Every failure path here used to write an audit event and return, so a
        voice message that could not be transcribed produced complete silence -
        no reply, no error, nothing. That is indistinguishable from the bot
        being offline, and it is what "I sent a voice message and it didn't
        work" looks like from the outside.
        """
        message = result.inbound_message
        if message is None:
            return
        try:
            await self.bot_api.send_message(message.chat_id, text)
        except Exception:  # noqa: BLE001 - never let the reply mask the cause
            logger.warning("failed to tell the user why a voice note failed", exc_info=True)

    async def handle_update(self, update: dict[str, Any]) -> ChannelUpdateResult:
        result = self.adapter.normalize_update(update)
        if not result.authorized or not result.inbound_message:
            return result
        if result.inbound_message.kind != MessageKind.VOICE:
            return result

        conversation_id = self.repositories.conversations.get_or_create(
            result.inbound_message.channel,
            result.inbound_message.chat_id,
        )
        if not self.repositories.messages.try_create(result.inbound_message, conversation_id):
            return result

        voice = next(
            (attachment for attachment in result.inbound_message.attachments if isinstance(attachment, VoiceAttachment)),
            None,
        )
        if voice is None or not voice.file_id:
            self.audit.append(
                AuditEventType.ERROR,
                actor=f"telegram:{result.inbound_message.sender_id}",
                correlation_id=result.inbound_message.correlation_id,
                payload={"error": "voice_file_id_missing", "message_id": result.inbound_message.id},
            )
            await self._tell_user(
                result, "That voice message arrived without any audio I could download. Could you send it again?"
            )
            return result

        file_info = await self.bot_api.get_file(voice.file_id)
        file_path = file_info.get("file_path")
        if not file_path:
            self.audit.append(
                AuditEventType.ERROR,
                actor=f"telegram:{result.inbound_message.sender_id}",
                correlation_id=result.inbound_message.correlation_id,
                payload={"error": "telegram_file_path_missing", "file_id": voice.file_id},
            )
            await self._tell_user(
                result, "Telegram would not give me that audio file. Could you send the voice message again?"
            )
            return result

        audio = await self.bot_api.download_file(file_path)
        transcript = await self.stt.transcribe(
            audio,
            file_name=file_path.rsplit("/", 1)[-1],
            mime_type=voice.mime_type,
        )
        if not transcript.text.strip():
            self.audit.append(
                AuditEventType.ERROR,
                actor=f"telegram:{result.inbound_message.sender_id}",
                correlation_id=result.inbound_message.correlation_id,
                payload={"error": "empty_transcript", "file_id": voice.file_id},
            )
            await self._tell_user(
                result, "I could not make out any words in that recording. Could you try again, or send it as text?"
            )
            return result

        artifact = Artifact(
            type=ArtifactType.TRANSCRIPT,
            content_preview=transcript.text,
            metadata=transcript.model_dump(mode="json"),
        )
        task = self.repositories.tasks.create(
            transcript.text,
            conversation_id=conversation_id,
            metadata={
                "source_message_id": result.inbound_message.id,
                "source_channel": result.inbound_message.channel.value,
                "source_chat_id": result.inbound_message.chat_id,
                "source_sender_id": result.inbound_message.sender_id,
                "transcript_artifact_id": artifact.id,
                "voice_file_id": voice.file_id,
            },
        )
        artifact = self.repositories.artifacts.create(artifact.model_copy(update={"task_id": task.id}))

        self.audit.append(
            AuditEventType.ARTIFACT_CREATED,
            actor=f"telegram:{result.inbound_message.sender_id}",
            task_id=task.id,
            correlation_id=result.inbound_message.correlation_id,
            payload={"artifact_id": artifact.id, "type": ArtifactType.TRANSCRIPT.value},
        )
        self.audit.append(
            AuditEventType.TASK_CREATED,
            actor=f"telegram:user:{result.inbound_message.sender_id}",
            task_id=task.id,
            correlation_id=result.inbound_message.correlation_id,
            payload={
                "objective": task.objective,
                "conversation_id": conversation_id,
                "task_type": "voice",
                "source_message_id": result.inbound_message.id,
            },
        )

        return ChannelUpdateResult(authorized=True, inbound_message=result.inbound_message, task=task)
