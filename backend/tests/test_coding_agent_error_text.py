"""A coding CLI that rejects the user's configured model is explained, not pasted as JSON."""

from __future__ import annotations

from agent_control.channels.task_notify import format_task_message
from agent_control.error_text import explain_for_user
from agent_control.schemas import TaskRecord, TaskStatus

RAW = (
    "codex exited with code 1. Last output: Defaulting to fallback metadata; this can degrade performance.\"}}\n"
    '{"type":"turn.started"}\n'
    '{"type":"error","message":"{\\"type\\":\\"error\\",\\"status\\":400,\\"error\\":{\\"type\\":\\"invalid_request_error\\",'
    "\\\"message\\\":\\\"The 'gpt-6.1-sol' model is not supported when using Codex with a ChatGPT account.\\\","
    '\\"code\\":\\"model_not_found\\"}}"}'
)


def test_a_rejected_codex_model_is_explained_with_what_to_do():
    text = explain_for_user(RAW)
    assert text == (
        "Codex is set to use gpt-6.1-sol, which your account cannot use, so it stopped before doing anything. "
        "Change the model in ~/.codex/config.toml to one your plan includes, or tell me to use Claude Code instead."
    )
    assert "{" not in text and "turn.started" not in text


def test_the_failed_task_message_shows_the_explanation_not_the_json():
    task = TaskRecord(
        objective="Use Codex to write a script", status=TaskStatus.BLOCKED,
        metadata={"last_tool_result": {"error_message": RAW, "output": {}}},
    )
    message = format_task_message(task)
    assert "Codex is set to use gpt-6.1-sol" in message
    assert "invalid_request_error" not in message and "turn.started" not in message


def test_other_errors_are_unchanged():
    assert explain_for_user("rate limit hit, 429") == "The model provider is rate limiting us. Wait a minute and try again."
