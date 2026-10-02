from __future__ import annotations

from typing import Protocol

from agent_control.channels.memory import memory_context
from agent_control.config import AppSettings
from agent_control.llm.providers import LLMProvider
from agent_control.prompts import render_prompt
from agent_control.schemas import Capability, InboundMessage, TaskStatus
from agent_control.storage.repositories import Repositories


class ChatResponder(Protocol):
    """Channel-agnostic despite the pre-Phase-16 Telegram-flavored name this
    replaced (docs/archive/UI_UX_AUDIT.md Phase 16) - nothing in this Protocol or
    its implementations below ever referenced Telegram; only the name did.
    """

    async def answer(self, message: InboundMessage, conversation_id: str | None = None) -> str:
        ...


class LLMChatResponder:
    def __init__(self, provider: LLMProvider, settings: AppSettings, repositories: Repositories) -> None:
        self.provider = provider
        self.settings = settings
        self.repositories = repositories

    async def answer(self, message: InboundMessage, conversation_id: str | None = None) -> str:
        return await self.provider.generate_text(
            _system_prompt(),
            _user_prompt(message, gateway_context(self.settings, self.repositories, conversation_id, query_text=message.text or "")),
        )


class StaticChatResponder:
    def __init__(self, response: str = "ok") -> None:
        self.response = response
        self.messages: list[InboundMessage] = []

    async def answer(self, message: InboundMessage, conversation_id: str | None = None) -> str:
        self.messages.append(message)
        return self.response


def _system_prompt() -> str:
    return render_prompt("base/telegram_gateway_system.md")


def _user_prompt(message: InboundMessage, context: str) -> str:
    return render_prompt("tasks/telegram_gateway_user.md", context=context, message_text=message.text or "")


def gateway_context(
    settings: AppSettings, repositories: Repositories, conversation_id: str | None = None, *, query_text: str = ""
) -> str:
    tasks = repositories.tasks.list_recent(5)
    active_statuses = {TaskStatus.RECEIVED, TaskStatus.INTERPRETING, TaskStatus.PLANNED, TaskStatus.RUNNING, TaskStatus.RETRYING, TaskStatus.AWAITING_APPROVAL}
    active_tasks = [task for task in tasks if task.status in active_statuses]
    active_count = len(active_tasks)
    # Listed separately from "recent": deciding whether a message steers work
    # in flight needs to know what is in flight, and a mixed list of finished
    # and running tasks does not answer that.
    active_list = "\n".join(
        f"- {task.id}: {task.status.value} - {task.objective[:120]}" for task in active_tasks
    ) or "- none"
    # Schedules were absent from this context entirely, so "change the news job
    # to every 6 hours" had nothing to resolve against and became a fresh task
    # groping for which job was meant.
    try:
        schedules = repositories.schedules.list_recent(10)
    except Exception:  # noqa: BLE001 - intake context must never break intake
        schedules = []
    schedule_list = "\n".join(
        f"- {item.id}: {str(getattr(item, 'objective', ''))[:100]}" for item in schedules
    ) or "- none"
    recent = "\n".join(f"- {task.id}: {task.status.value} - {task.objective[:120]}" for task in tasks) or "- none"

    vscode_policy = settings.capabilities.get(Capability.VSCODE_WRITE_FILES)
    vscode_enabled = bool(settings.adapters.vscode.enabled and vscode_policy and vscode_policy.enabled)
    vscode_approval = "approval-free" if vscode_policy and not vscode_policy.requires_approval else "approval-gated"

    terminal_policy = settings.capabilities.get(Capability.TERMINAL_RUN)
    terminal_enabled = bool(terminal_policy and terminal_policy.enabled)
    terminal_approval = "approval-free" if terminal_policy and not terminal_policy.requires_approval else "approval-gated"

    screenshot_policy = settings.capabilities.get(Capability.DESKTOP_SCREENSHOT)
    screenshot_enabled = bool(screenshot_policy and screenshot_policy.enabled)
    workspace_policy = settings.capabilities.get(Capability.FILESYSTEM_WRITE)
    workspace_enabled = bool(settings.adapters.workspace.enabled and workspace_policy and workspace_policy.enabled)
    workspace_approval = "approval-free" if workspace_policy and not workspace_policy.requires_approval else "approval-gated"
    adapter_factory_enabled = bool(settings.adapters.adapter_factory.enabled and workspace_policy and workspace_policy.enabled)
    memory_record = repositories.conversation_memory.get(conversation_id) if conversation_id else None
    memory = memory_context(memory_record, remembered_facts=repositories.memory_facts.list_all(), objective=query_text)

    return f"""LLM profile: {settings.llm.default_profile}
Chat receive/send: enabled
VS Code/GitHub Copilot terminal route: {'enabled' if vscode_enabled else 'disabled'} ({vscode_approval})
Local workspaces and localhost previews: {'enabled' if workspace_enabled else 'disabled'} ({workspace_approval}); root={settings.adapters.workspace.root_dir}
Generated adapter proposal cache: {'enabled' if adapter_factory_enabled else 'disabled'}; root={settings.adapters.adapter_factory.root_dir}
Terminal command route: {'enabled' if terminal_enabled else 'disabled'} ({terminal_approval})
Desktop screenshots: {'enabled' if screenshot_enabled else 'disabled'}
Conversation memory:
{memory}
Recent tasks: {len(tasks)}
Active tasks: {active_count}
Recent task list:
{recent}
Tasks running right now (a message may be steering one of these):
{active_list}
Existing schedules (name one of these when the user changes or stops a job):
{schedule_list}"""
