from __future__ import annotations

from typing import Any

from agent_control.config import AppSettings
from agent_control.schemas import Capability, TaskStatus, ToolCallRequest, ToolCallResult, ToolResultStatus
from agent_control.storage.repositories import Repositories
from agent_control.tools.coding_agent import load_sessions
from agent_control.tools.contracts import TaskStatusInput, TaskStatusOutput
from agent_control.tools.spec import Adapters, Definitions, RegistryDeps, ToolDefinition, capability_enabled


class TaskStatusAdapter:
    """Report current task and plan state for status questions."""

    def __init__(self, repositories: Repositories, settings: AppSettings | None = None) -> None:
        self.repositories = repositories
        self.settings = settings

    async def execute(self, request: ToolCallRequest) -> ToolCallResult:
        limit = int(request.input.get("limit") or 10)
        recent = self.repositories.tasks.list_recent(limit=limit)
        active_statuses = [
            TaskStatus.RECEIVED,
            TaskStatus.INTERPRETING,
            TaskStatus.PLANNED,
            TaskStatus.RUNNING,
            TaskStatus.AWAITING_EXTERNAL,
            TaskStatus.AWAITING_APPROVAL,
            TaskStatus.RETRYING,
        ]
        # Query active/awaiting/clarification/approval tasks by status directly
        # rather than filtering `recent` (which is capped at `limit` by created_at).
        # A task stuck waiting for approval/clarification for a while must not
        # silently drop out of status visibility once enough newer tasks exist.
        active = self.repositories.tasks.list_by_statuses(active_statuses, limit=50)
        awaiting_external = [task for task in active if task.status == TaskStatus.AWAITING_EXTERNAL]
        waiting_clarification = self.repositories.tasks.list_by_statuses([TaskStatus.CLARIFYING], limit=50)
        waiting_approval = [task for task in active if task.status == TaskStatus.AWAITING_APPROVAL]
        active_sessions = self._active_coding_sessions()
        combined_tasks: dict[str, Any] = {task.id: task for task in recent}
        for task in (*active, *waiting_clarification):
            combined_tasks.setdefault(task.id, task)
        task_rows = []
        for task in combined_tasks.values():
            # Progress is the Operator loop's own step count. This used to read
            # task.plan_id -> repositories.plans, which has had zero writers
            # since the plan-based path was deleted, so every row reported
            # "plan": null regardless of how much work the task had done.
            history = task.metadata.get("operator_history")
            steps_taken = len(history) if isinstance(history, list) else 0
            task_rows.append(
                {
                    "id": task.id,
                    "status": task.status.value,
                    "objective": task.objective,
                    "updated_at": task.updated_at.isoformat(),
                    "steps_taken": steps_taken,
                    "last_tool_name": task.metadata.get("last_tool_name"),
                    "last_error": task.metadata.get("last_error"),
                    "awaiting_external": task.metadata.get("awaiting_external"),
                    "last_failure_type": task.metadata.get("last_failure_type"),
                }
            )

        summary_lines = [f"Status: {len(recent)} recent task(s), {len(active)} active."]
        if active:
            summary_lines.append("Current active work:")
            summary_lines.extend(f"- {task.id}: {task.status.value} - {task.objective[:120]}" for task in active[:5])
        else:
            summary_lines.append("Current active work: none.")
        if active_sessions:
            summary_lines.append("Active coding sessions:")
            summary_lines.extend(
                f"- {session.get('provider')}: {session.get('status')} ({session.get('session_id')})"
                for session in active_sessions[:5]
            )
        if awaiting_external:
            summary_lines.append(f"Waiting on external sessions: {len(awaiting_external)}.")
        if waiting_clarification:
            summary_lines.append(f"Waiting for clarification: {len(waiting_clarification)}.")
        if waiting_approval:
            summary_lines.append(f"Waiting for approval: {len(waiting_approval)}.")
        if recent:
            completed = [task for task in recent if task.status == TaskStatus.COMPLETED]
            blocked = [task for task in recent if task.status in {TaskStatus.BLOCKED, TaskStatus.FAILED}]
            summary_lines.append(f"Completed recently: {len(completed)}.")
            summary_lines.append(f"Blocked or failed recently: {len(blocked)}.")
            summary_lines.append("Latest task:")
            summary_lines.append(f"- {recent[0].id}: {recent[0].status.value} - {recent[0].objective[:160]}")

        output = {
            "operation": "status",
            "summary": "\n".join(summary_lines),
            "task_status": {
                "recent_count": len(recent),
                "active_count": len(active),
                "awaiting_external_count": len(awaiting_external),
                "waiting_clarification_count": len(waiting_clarification),
                "waiting_approval_count": len(waiting_approval),
                "recent_tasks": task_rows,
                "active_coding_sessions": active_sessions,
                "localdeploy": self._localdeploy_state(),
                "mcp": self._mcp_state(),
            },
            "plan": next((row["plan"] for row in task_rows if row.get("plan")), None),
            "terminal_output": [{"content": "\n".join(summary_lines), "exit_code": 0, "is_final": True}],
        }
        return ToolCallResult(request_id=request.id, status=ToolResultStatus.SUCCEEDED, output=output)

    def _active_coding_sessions(self) -> list[dict[str, Any]]:
        if self.settings is None:
            return []
        sessions = load_sessions(self.settings.adapters.coding_agent.session_root, limit=20)
        return [
            {
                key: session.get(key)
                for key in ("session_id", "provider", "status", "task_id", "workspace_dir", "started_at", "summary")
            }
            for session in sessions
            if session.get("status") in {"starting", "running"}
        ]

    def _localdeploy_state(self) -> dict[str, Any]:
        if self.settings is None:
            return {}
        profile = self.settings.llm.profiles.get(self.settings.llm.default_profile)
        base_url = profile.base_url if profile else None
        expects_localdeploy = bool(base_url and ("127.0.0.1:8000" in base_url or "localhost:8000" in base_url))
        return {
            "default_profile": self.settings.llm.default_profile,
            "fallback_profile": self.settings.llm.fallback_profile,
            "expects_localdeploy": expects_localdeploy,
            "fallback_configured": bool(self.settings.llm.fallback_profile),
        }

    def _mcp_state(self) -> dict[str, Any]:
        if self.settings is None:
            return {}
        return {
            "enabled": self.settings.mcp.enabled,
            "server_count": len(self.settings.mcp.servers),
            "enabled_servers": [name for name, server in self.settings.mcp.servers.items() if server.enabled],
        }


def register(deps: RegistryDeps, definitions: Definitions, adapters: Adapters) -> None:
    settings = deps.settings
    enabled = deps.repositories is not None and capability_enabled(settings, Capability.AGENT_CORE)
    definitions.append(
        ToolDefinition(
            name="task.status",
            capability=Capability.AGENT_CORE,
            enabled=enabled,
            description="report current task, plan, active, completed, and blocked state for status questions",
            operations=("status",),
            input_schema=TaskStatusInput,
            output_schema=TaskStatusOutput,
            default_operation="status",
            examples=(
                {"operation": "status", "limit": 10},
            ),
        )
    )
    if deps.repositories is not None:
        adapters["task.status"] = TaskStatusAdapter(deps.repositories, settings)  # type: ignore[arg-type]
