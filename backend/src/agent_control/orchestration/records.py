"""Writing down what happened: model usage, model calls and tool results."""
from __future__ import annotations

from typing import Any

from agent_control.channels.undo_command import (
    merge_undo_moves,
    remove_empty_folders_the_move_made,
    undo_moves_from_output,
)
from agent_control.llm.call_log import cap_messages, merge_token_usage
from agent_control.orchestration.auditor import AuditorService
from agent_control.orchestration.task_record import (
    merge_unique_list,
    tool_output_text,
    trim_result,
)
from agent_control.schemas import (
    AuditEventType,
    LLMCallRecord,
    TaskRecord,
    ToolCallResult,
)
from agent_control.storage.redaction import redact_payload


class RecordsStage:
    def _record_llm_usage(
        self, task: TaskRecord, source: str, usage: dict[str, Any] | None, *, fallback_used: bool = False,
    ) -> TaskRecord:
        """Accumulate one LLM call's token usage into task.metadata["token_usage"].

        `source` is "operator" or "auditor" - the two LLM calls the worker
        itself makes per step (docs/archive/HISTORY.md Part 4 T1.4). The Concierge's
        call is added by llm/call_log.py once its task exists. Deliberately
        does NOT cover coding-agent-reported usage (already tracked
        separately as last_tool_usage/last_copilot_usage in
        _record_tool_result - a different cost source with different
        pricing, not merged with this one). `usage` is None whenever the
        provider didn't report it (replay in tests, or a server that omits
        the field) - a no-op, never a fabricated zero.

        `fallback_used` (docs/ROADMAP.md 4.4: "an unexplained fallback is a
        silent quality change") is sticky once set - a task where any call
        ever fell back to a secondary profile stays flagged for the rest of
        the task, even if a later call succeeds on the primary again.
        """
        merged = merge_token_usage(task.metadata.get("token_usage"), source, usage, fallback_used=fallback_used)
        if merged is None:
            return task
        return self.repositories.tasks.update_metadata(task.id, {**task.metadata, "token_usage": merged})

    def _record_llm_call(
        self, task_id: str, source: str, step_index: int, service: Any, *, step_id: str
    ) -> None:
        """Persist one LLM call's request/response/timing (docs/archive/UI_UX_AUDIT.md
        Phase 14d) - a sibling to _record_llm_usage's running token totals
        above, not a replacement: that stays the cheap, always-on counter;
        this is the full per-call record that turns the Duration view's
        inferred "operator thinking" gaps into measured latency.

        Reads last_request/last_response_text/last_model/last_started_at/
        last_latency_ms off `service` (the OperatorLoopService or
        AuditorService instance that just made the call) - the same
        provider-to-service proxying _record_llm_usage already reads
        last_usage from. A no-op if persistence is disabled, or if the call
        didn't actually complete (no last_request/last_started_at means the
        provider raised before setting them). Best-effort: a persistence
        failure is logged, not raised - it must never block the task itself.

        `step_id` (docs/archive/UI_UX_AUDIT.md Phase 14e) is the same id stamped onto
        this step's operator_history entry and any ToolCallRequest.parent_step_id
        it leads to - the real parent-child link Graph v2 is built on.
        """
        if not self.persist_llm_calls:
            return
        messages = getattr(service, "last_request", None)
        started_at = getattr(service, "last_started_at", None)
        if not isinstance(messages, list) or not messages or started_at is None:
            return
        usage = getattr(service, "last_usage", None) or {}
        response_text = getattr(service, "last_response_text", None)
        if isinstance(response_text, str) and len(response_text) > self.llm_call_max_chars:
            response_text = f"{response_text[: self.llm_call_max_chars]}...[truncated]"
        try:
            record = LLMCallRecord(
                task_id=task_id,
                source=source,
                model=getattr(service, "last_model", None),
                step_index=step_index,
                step_id=step_id,
                messages=cap_messages(redact_payload(messages, self.redact_patterns), self.llm_call_max_chars),
                response_text=response_text,
                prompt_tokens=usage.get("prompt_tokens"),
                completion_tokens=usage.get("completion_tokens"),
                total_tokens=usage.get("total_tokens"),
                latency_ms=getattr(service, "last_latency_ms", None),
                created_at=started_at,
            )
            self.repositories.llm_calls.create(record)
        except Exception as exc:
            self.audit.append(
                AuditEventType.ERROR, actor="worker", task_id=task_id,
                payload={"error": "llm_call_persist_failed", "reason": str(exc)},
            )

    def _record_tool_result(self, task_id: str, tool_name: str, result: ToolCallResult) -> TaskRecord:
        latest = self.repositories.tasks.get(task_id)
        if latest is None:
            raise KeyError(f"task not found: {task_id}")
        metadata = {
            **latest.metadata,
            "last_tool_name": tool_name,
            "last_tool_result": trim_result(result),
        }
        output_text = tool_output_text(result)
        if output_text:
            metadata["last_tool_output_text"] = output_text if len(output_text) <= 20000 else f"{output_text[:19997]}..."
            if AuditorService.is_content_tool(tool_name):
                metadata["last_content_tool_output_text"] = (
                    output_text if len(output_text) <= 20000 else f"{output_text[:19997]}..."
                )
                metadata["last_content_tool_request_id"] = result.request_id
        output = result.output if isinstance(result.output, dict) else {}
        usage = output.get("usage")
        if isinstance(usage, dict) and usage:
            metadata["last_tool_usage"] = usage
            if "copilot" in tool_name:
                metadata["last_copilot_usage"] = usage
        for metadata_key, output_key in (
            ("workspace_dir", "workspace_dir"),
            ("server_pid", "server_pid"),
            ("adapter_dir", "adapter_dir"),
            ("adapter_name", "adapter_name"),
            ("browser_state", "browser_state"),
            ("browser_url", "browser_url"),
            ("page_title", "page_title"),
            ("screenshot_path", "screenshot_path"),
            ("screenshot_uri", "screenshot_uri"),
            ("desktop_observation", "observation"),
            ("computer_use_actions", "actions_taken"),
            ("changed_paths", "changed_paths"),
            ("files_created", "files_created"),
            ("files_modified", "files_modified"),
            ("organized_paths", "changed_paths"),
            ("rename_manifest", "rename_manifest"),
            ("file_manifest", "manifest"),
            ("document_path", "path"),
            ("document_summary", "summary"),
            ("changed_files", "changed_files"),
            ("schedule_id", "schedule_id"),
            ("scheduled_task_id", "task_id"),
            ("schedule_next_run_at", "next_run_at"),
            ("mcp_catalog_path", "catalog_path"),
            ("mcp_catalog_updated_at", "catalog_updated_at"),
            ("mcp_selected_tool", "selected_tool"),
        ):
            if output.get(output_key):
                if metadata_key in {
                    "changed_paths",
                    "files_created",
                    "files_modified",
                    "organized_paths",
                    "changed_files",
                }:
                    metadata[metadata_key] = merge_unique_list(
                        metadata.get(metadata_key), output[output_key]
                    )
                else:
                    metadata[metadata_key] = output[output_key]
        if tool_name == "coding.agent":
            for metadata_key, output_key in (
                ("coding_agent_workspace", "workspace_dir"),
                ("coding_agent_session_id", "session_id"),
                ("coding_agent_limit_state", "limit_state"),
            ):
                if output.get(output_key):
                    metadata[metadata_key] = output[output_key]
        if tool_name == "mcp.client":
            metadata["mcp_catalog"] = {
                "catalog_path": output.get("catalog_path"),
                "catalog_updated_at": output.get("catalog_updated_at"),
                "tool_count": len(output.get("tools") or []),
                "healthy": output.get("healthy"),
            }
        if result.artifact_ids:
            metadata["last_artifact_ids"] = result.artifact_ids
        elif output.get("artifact_ids"):
            metadata["last_artifact_ids"] = output["artifact_ids"]
        if output.get("preview_url"):
            metadata["preview_url"] = output["preview_url"]
        elif tool_name == "workspace.manage" and output.get("url"):
            metadata["preview_url"] = output["url"]
        if tool_name == "filesystem.manage" and output.get("operation") == "apply_manifest":
            # Where every moved file came from, so "undo that" can put them back (channels/undo_command.py).
            undo = undo_moves_from_output(output)
            if undo is not None:
                metadata["undo_moves"] = merge_undo_moves(latest.metadata.get("undo_moves"), undo)
            undone_task_id = latest.metadata.get("undo_of_task_id")
            if undone_task_id and not output.get("dry_run"):
                # This task is the "undo that" of an earlier one: the files are back, so the folders that move made can go.
                undone = self.repositories.tasks.get(str(undone_task_id))
                removed = remove_empty_folders_the_move_made((undone.metadata.get("undo_moves") if undone else None))
                if removed:
                    metadata["removed_empty_folders"] = removed
        if tool_name == "workspace.manage" and output.get("server_pid") and output.get("url"):
            # So "what apps are running?" and "stop the recipe app" can find it again (channels/app_commands.py).
            metadata["preview_server"] = {
                "pid": int(output["server_pid"]), "url": str(output["url"]), "workspace_dir": str(output.get("workspace_dir") or ""),
            }
        if tool_name == "workspace.manage" and isinstance(output.get("page_controls"), dict):
            # What the launched page actually offers, read back from the page (tools/page_check.py); a relaunch replaces it.
            metadata["page_controls"] = output["page_controls"]
        if tool_name == "artifact.deliver":
            metadata["artifact_delivery"] = output
            if output.get("artifact_id"):
                metadata["last_delivered_artifact_id"] = output["artifact_id"]
        return self.repositories.tasks.update_metadata(task_id, metadata)
