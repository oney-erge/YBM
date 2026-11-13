"""The leak check reads what the user can receive, not every line the task recorded."""

from __future__ import annotations

from test_e2e_runner import _runner_module


CASE = {"assertions": {"final_status_in": ["completed"], "bot_reply_excludes_all": ["task_"]}}
SCRIPT_OUTPUT = r"Code interpreter operation completed: run_python\nWorkspace: C:\agent\code_interpreter\task_task_ab12\nStdout:\n15"


def test_a_scratch_folder_named_in_the_tool_output_is_not_a_leak_when_the_user_never_gets_it():
    runner = _runner_module()
    turn = runner.TurnResult(
        final_status="completed",
        bot_reply_text="There are 15 lines in total.",
        synth_answer="There are 15 lines in total.",
        telegram_sent_events=[{"payload": {"kind": "text", "text": "There are 15 lines in total."}}],
        telegram_confirmed_text="There are 15 lines in total.",
        last_tool_output=SCRIPT_OUTPUT,
        metadata={"last_tool_output_text": SCRIPT_OUTPUT},
    )
    passed, reason = runner._diagnose_turn(CASE, turn, is_followup=False)
    assert passed is True, reason


def test_the_same_text_in_what_the_user_receives_still_fails():
    runner = _runner_module()
    for field in ("bot_reply_text", "synth_answer", "telegram_confirmed_text"):
        values = {"bot_reply_text": "ok", "synth_answer": "ok", "telegram_confirmed_text": "ok"}
        values[field] = "There are 15 lines.\nWorkspace: task_task_ab12 (in my working folder)"
        turn = runner.TurnResult(
            final_status="completed", telegram_sent_events=[{"payload": {"kind": "text", "text": values["telegram_confirmed_text"]}}],
            **values,
        )
        passed, reason = runner._diagnose_turn(CASE, turn, is_followup=False)
        assert passed is False and "leaked forbidden content" in reason, field


def test_an_error_or_gap_the_task_ended_with_still_counts_as_what_the_user_gets():
    runner = _runner_module()
    turn = runner.TurnResult(
        final_status="completed", bot_reply_text="ok", telegram_confirmed_text="ok",
        telegram_sent_events=[{"payload": {"kind": "text", "text": "ok"}}],
        metadata={"last_worker_error": "Blocked: see task_abc"},
    )
    passed, _ = runner._diagnose_turn(CASE, turn, is_followup=False)
    assert passed is False
