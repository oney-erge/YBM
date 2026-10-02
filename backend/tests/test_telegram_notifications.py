from __future__ import annotations

from pathlib import Path

import pytest

from datetime import datetime, timedelta, timezone

from agent_control.channels.task_notify import format_task_message
from agent_control.channels.telegram_notifications import TelegramTaskNotifier
from agent_control.schemas import ApprovalRequest, ApprovalStatus, Capability, ChannelType, RiskLevel, TaskRecord, TaskStatus


class FakeApprovalRepository:
    def __init__(self, approvals: list[ApprovalRequest]) -> None:
        self._approvals = approvals

    def list_for_task(self, task_id: str) -> list[ApprovalRequest]:
        return [a for a in self._approvals if a.task_id == task_id]


def _approval(task_id: str, status: ApprovalStatus = ApprovalStatus.PENDING) -> ApprovalRequest:
    return ApprovalRequest(
        task_id=task_id,
        capability=Capability.FILESYSTEM_WRITE,
        risk_level=RiskLevel.HIGH,
        summary="write a file",
        status=status,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=15),
    )


class FakeTelegramClient:
    def __init__(self, fail_photo: bool = False) -> None:
        self.fail_photo = fail_photo
        self.messages: list[tuple[str | int, str]] = []
        self.photos: list[tuple[str | int, str, str | None]] = []
        self.last_reply_markup: dict | None = None

    async def send_message(self, chat_id: str | int, text: str, reply_markup: dict | None = None) -> dict:
        self.messages.append((chat_id, text))
        self.last_reply_markup = reply_markup
        return {"ok": True}

    async def send_photo_file(self, chat_id: str | int, path: str, caption: str | None = None) -> dict:
        if self.fail_photo:
            raise RuntimeError("sendPhoto rejected")
        self.photos.append((chat_id, path, caption))
        return {"ok": True}


@pytest.mark.asyncio
async def test_notifier_ignores_web_chat_source_ids() -> None:
    client = FakeTelegramClient()
    notifier = TelegramTaskNotifier(client)  # type: ignore[arg-type]
    task = TaskRecord(
        objective="answer in local chat",
        status=TaskStatus.COMPLETED,
        metadata={"source_channel": ChannelType.WEB.value, "source_chat_id": "local"},
    )

    await notifier.notify(task)

    assert client.messages == []


def test_task_message_prioritizes_result_links() -> None:
    task = TaskRecord(
        objective="Create a duck app and launch it",
        status=TaskStatus.COMPLETED,
        metadata={
            "preview_url": "http://127.0.0.1:8890/",
            "workspace_dir": "C:/tmp/duck",
            "last_tool_name": "workspace.manage",
            "last_tool_result": {
                "output": {
                    "terminal_output": [
                        {
                            "content": "Workspace operation completed: launch_static",
                            "is_final": True,
                            "exit_code": 0,
                        }
                    ]
                }
            },
        },
    )

    message = format_task_message(task)

    # Regression guard: this used to reply "The local workspace is ready." and
    # nothing else - the address the user asked for lives in
    # metadata["preview_url"], which the per-tool answer builders never read.
    assert "The local workspace is ready." in message
    assert "http://127.0.0.1:8890/" in message
    assert "C:/tmp/duck" in message


def test_task_message_reports_gap_and_retry_on_blocked_task() -> None:
    task = TaskRecord(
        objective="Create an app and launch it",
        status=TaskStatus.BLOCKED,
        metadata={
            "fulfillment_gap": "expected_preview_url_missing",
            "fulfillment_retry_count": 2,
            "last_tool_result": {
                "error_message": "assistant output did not include materializable static app files",
                "output": {},
            },
        },
    )

    message = format_task_message(task)

    assert "I could not complete this request." in message
    assert "Gap: expected_preview_url_missing" in message
    assert "Retries: 2" in message
    assert "Error: assistant output did not include materializable static app files" in message


def test_task_message_keeps_photo_path_out_of_text(tmp_path) -> None:
    screenshot = tmp_path / "screen.png"
    screenshot.write_bytes(b"png")
    task = TaskRecord(
        objective="Take a screenshot",
        status=TaskStatus.COMPLETED,
        metadata={
            "source_chat_id": "100",
            "screenshot_path": str(screenshot),
            "last_tool_name": "computer.use",
        },
    )

    message = format_task_message(task)

    assert "Screenshot:" not in message
    assert str(screenshot) not in message
    assert "Tool:" not in message


def test_completed_filesystem_search_message_includes_file_contents() -> None:
    task = TaskRecord(
        objective="Find the resume notes file from my desktop and read it to me.",
        status=TaskStatus.COMPLETED,
        metadata={
            "last_tool_name": "filesystem.manage",
            "last_tool_result": {
                "output": {
                    "operation": "search",
                    "root": "C:/Users/alex/Desktop",
                    "entries": [
                        {
                            "relative_path": "sample-resume-notes.txt",
                            "is_dir": False,
                            "size_bytes": 100,
                            "content_preview": "Oney resume notes include Python automation and local LLM orchestration.",
                        }
                    ],
                }
            },
        },
    )

    message = format_task_message(task)

    assert "sample-resume-notes.txt" in message
    assert "Python automation" in message
    assert "Task:" not in message
    assert "Tool:" not in message


@pytest.mark.asyncio
async def test_notifier_sends_screenshot_as_photo(tmp_path) -> None:
    screenshot = tmp_path / "screen.png"
    screenshot.write_bytes(b"png")
    task = TaskRecord(
        objective="Take a screenshot of my desktop and send it to me now",
        status=TaskStatus.COMPLETED,
        metadata={
            "source_chat_id": "100",
            "last_tool_name": "computer.use",
            "last_tool_result": {"output": {"screenshot_path": str(screenshot), "final_summary": "Desktop is visible."}},
        },
    )
    client = FakeTelegramClient()

    await TelegramTaskNotifier(client).notify(task)  # type: ignore[arg-type]

    assert client.messages == [("100", format_task_message(task))]
    assert client.photos == [("100", str(screenshot), "Screenshot - Take a screenshot of my desktop and send it to me now")]


@pytest.mark.asyncio
async def test_notifier_reports_photo_delivery_failure(tmp_path) -> None:
    screenshot = tmp_path / "screen.png"
    screenshot.write_bytes(b"png")
    task = TaskRecord(
        objective="Take a screenshot",
        status=TaskStatus.COMPLETED,
        metadata={"source_chat_id": "100", "screenshot_path": Path(screenshot).resolve().as_uri()},
    )
    client = FakeTelegramClient(fail_photo=True)

    await TelegramTaskNotifier(client).notify(task)  # type: ignore[arg-type]

    assert len(client.messages) == 2
    assert "Telegram photo delivery failed" in client.messages[1][1]
    assert str(screenshot) in client.messages[1][1]


@pytest.mark.asyncio
async def test_notifier_sends_approve_reject_keyboard_when_approval_pending() -> None:
    task = TaskRecord(
        objective="write a file",
        status=TaskStatus.AWAITING_APPROVAL,
        metadata={"source_chat_id": "100"},
    )
    approval = _approval(task.id)
    client = FakeTelegramClient()

    await TelegramTaskNotifier(client, approvals=FakeApprovalRepository([approval])).notify(task)  # type: ignore[arg-type]

    assert client.last_reply_markup == {
        "inline_keyboard": [
            [
                {"text": "Approve", "callback_data": f"approval:{approval.id}:approve"},
                {"text": "Reject", "callback_data": f"approval:{approval.id}:reject"},
            ]
        ]
    }


@pytest.mark.asyncio
async def test_notifier_sends_no_keyboard_without_pending_approval() -> None:
    task = TaskRecord(
        objective="write a file",
        status=TaskStatus.AWAITING_APPROVAL,
        metadata={"source_chat_id": "100"},
    )
    approved = _approval(task.id, status=ApprovalStatus.APPROVED)
    client = FakeTelegramClient()

    await TelegramTaskNotifier(client, approvals=FakeApprovalRepository([approved])).notify(task)  # type: ignore[arg-type]

    assert client.last_reply_markup is None


@pytest.mark.asyncio
async def test_notifier_sends_no_keyboard_when_approvals_repo_not_wired() -> None:
    task = TaskRecord(
        objective="write a file",
        status=TaskStatus.AWAITING_APPROVAL,
        metadata={"source_chat_id": "100"},
    )
    client = FakeTelegramClient()

    await TelegramTaskNotifier(client).notify(task)  # type: ignore[arg-type]

    assert client.last_reply_markup is None


@pytest.mark.asyncio
async def test_notifier_sends_no_keyboard_for_non_approval_status() -> None:
    task = TaskRecord(
        objective="write a file",
        status=TaskStatus.COMPLETED,
        metadata={"source_chat_id": "100"},
    )
    approval = _approval(task.id)
    client = FakeTelegramClient()

    await TelegramTaskNotifier(client, approvals=FakeApprovalRepository([approval])).notify(task)  # type: ignore[arg-type]

    assert client.last_reply_markup is None


def test_code_interpreter_response_shows_stdout_as_primary_content() -> None:
    task = TaskRecord(
        objective="Run a data processing script",
        status=TaskStatus.COMPLETED,
        metadata={
            "last_tool_name": "code.interpreter",
            "last_tool_result": {
                "output": {
                    "operation": "run_python",
                    "stdout": "Total: 190\nFiles processed: 3",
                    "summary": "Script ran successfully.",
                    "files_created": ["expense-summary.json"],
                    "workspace_dir": "/tmp/code/task_abc",
                }
            },
        },
    )

    message = format_task_message(task)

    assert "Total: 190" in message
    assert "expense-summary.json" in message
    stdout_pos = message.index("Total: 190")
    files_pos = message.index("expense-summary.json")
    assert stdout_pos < files_pos
    assert "Task:" not in message
    assert "Tool:" not in message
    assert "Command:" not in message


def test_mcp_response_shows_tool_result_content() -> None:
    task = TaskRecord(
        objective="Echo hello",
        status=TaskStatus.COMPLETED,
        metadata={
            "last_tool_name": "mcp.client",
            "last_tool_result": {
                "output": {
                    "operation": "call_tool",
                    "summary": "Called MCP tool fake.echo.",
                    "result": {"content": [{"type": "text", "text": "hello from E2E"}]},
                    "selected_tool": {"server": "fake", "tool": "echo"},
                }
            },
        },
    )

    message = format_task_message(task)

    assert "hello from E2E" in message
    assert "Task:" not in message


def test_completed_task_messages_do_not_leak_internal_ids() -> None:
    for tool_name, output in [
        ("filesystem.manage", {"operation": "inspect_folder", "root": "C:/Desktop", "entries": []}),
        ("browser.open", {"operation": "open", "url": "https://example.com", "page_title": "Example", "summary": "A page."}),
        ("code.interpreter", {"operation": "run_python", "stdout": "done", "summary": "ran", "files_created": [], "workspace_dir": "/tmp"}),
    ]:
        task = TaskRecord(
            objective="Test task",
            status=TaskStatus.COMPLETED,
            metadata={"last_tool_name": tool_name, "last_tool_result": {"output": output}},
        )
        message = format_task_message(task)
        assert "Task:" not in message, f"Task ID leaked for {tool_name}"
        assert "Command:" not in message, f"Command ID leaked for {tool_name}"


def test_user_facing_message_received_status_is_friendly() -> None:
    task = TaskRecord(objective="Find my resume", status=TaskStatus.RECEIVED)
    message = format_task_message(task)
    lower = message.lower()
    assert "working on it" in lower or "got your message" in lower or "on it" in lower


def test_user_facing_message_retrying_status_explains_retry() -> None:
    task = TaskRecord(objective="Do something", status=TaskStatus.RETRYING)
    message = format_task_message(task)
    lower = message.lower()
    assert "different" in lower or "trying" in lower or "approach" in lower or "retry" in lower


def test_user_facing_message_awaiting_approval_shows_preview_and_real_resume_path() -> None:
    # "Approving blind is not approval" (docs/archive/HISTORY.md P5) - the message
    # must say what's being approved, and must not point at the admin UI,
    # which has no approve/reject capability (read-only task-trace listing
    # only). Replying "approve" in this chat is the only working resume path
    # (channels/telegram.py's _approve_latest_pending()).
    task = TaskRecord(
        objective="Run a risky terminal command",
        status=TaskStatus.AWAITING_APPROVAL,
        metadata={"pending_approval_preview": "- Run cleanup script (risk: high) via terminal: {'command': 'rm -rf build'}"},
    )

    message = format_task_message(task)

    assert "Run cleanup script" in message
    assert "rm -rf build" in message
    assert "approve" in message.lower()
    assert "admin ui" not in message.lower()
    assert "admin UI" not in message


def test_user_facing_message_awaiting_approval_without_preview_still_names_resume_path() -> None:
    task = TaskRecord(objective="Run a risky terminal command", status=TaskStatus.AWAITING_APPROVAL)

    message = format_task_message(task)

    assert "approve" in message.lower()
    assert "admin UI" not in message


def test_running_progress_message_names_the_last_real_step() -> None:
    """Regression guard (docs/archive/HISTORY.md §3.3): _latest_attempt_summary() read
    metadata["attempt_history"], a plan-era field with zero writers since P3,
    so per-step progress messages silently lost their detail. Now reads
    operator_history."""
    task = TaskRecord(
        objective="Read the quarterly report",
        status=TaskStatus.RUNNING,
        metadata={
            "operator_history": [
                {"tool_name": "filesystem.manage", "status": "succeeded", "output_summary": "found it"},
                {"tool_name": "document.manage", "status": "failed", "error": "PDF is password protected"},
            ]
        },
    )

    message = format_task_message(task)

    assert "document.manage" in message
    assert "PDF is password protected" in message


def test_progress_message_skips_check_pseudo_entries() -> None:
    """The fulfillment/audit check rows are bookkeeping, not steps the user
    took - reporting "_fulfillment_check ended with fulfillment_gap" as the
    latest attempt would be noise."""
    task = TaskRecord(
        objective="Build and launch the app",
        status=TaskStatus.RUNNING,
        metadata={
            "operator_history": [
                {"tool_name": "code.interpreter", "status": "failed", "error": "SyntaxError on line 3"},
                {"tool_name": "_fulfillment_check", "status": "fulfillment_gap", "error": "expected_preview_url_missing"},
            ]
        },
    )

    message = format_task_message(task)

    assert "code.interpreter" in message
    assert "_fulfillment_check" not in message


def test_failed_task_message_includes_the_last_real_step() -> None:
    task = TaskRecord(
        objective="Read the quarterly report",
        status=TaskStatus.FAILED,
        metadata={
            "operator_history": [
                {"tool_name": "document.manage", "status": "failed", "error": "PDF is password protected"},
            ]
        },
    )

    message = format_task_message(task)

    assert "document.manage" in message
    assert "PDF is password protected" in message
