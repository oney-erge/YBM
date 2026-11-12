"""Filling in a form is not signing someone up: a request to submit it is only done once it was submitted."""

from __future__ import annotations

import pytest

from agent_control.orchestration.fulfillment import expected_postconditions, fulfillment_guidance, validate_fulfillment
from agent_control.schemas import PostconditionType, TaskRecord, TaskStatus


def _task(message: str, history: list[dict]) -> TaskRecord:
    return TaskRecord(
        objective=message, status=TaskStatus.RUNNING,
        metadata={"original_message_text": message, "operator_history": history},
    )


def _step(operation: str, status: str = "succeeded", **inp) -> dict:
    return {"tool_name": "browser.control", "input": {"operation": operation, **inp}, "status": status}


OPEN = {"tool_name": "browser.open", "input": {"operation": "open", "url": "http://127.0.0.1:8899/"}, "status": "succeeded"}
SIGN_UP = "Open http://127.0.0.1:8899/ and sign me up for the meetup with what you know about me."


def _missing(task):
    return validate_fulfillment(task).missing


@pytest.mark.parametrize("message", [
    SIGN_UP,
    "Go to the website and submit the contact form with my details.",
    "Register me on this site: https://example.test/join",
])
def test_a_request_to_submit_a_form_expects_it_to_be_submitted(message):
    kinds = {item.type for item in expected_postconditions(_task(message, []))}
    assert PostconditionType.FORM_SUBMITTED in kinds


@pytest.mark.parametrize("message", [
    "Open http://127.0.0.1:8899/ and tell me which fields the form has.",
    "Summarize the page at https://example.test/news.",
    "Submit my report to the team.",
    "Apply the changes to notes.txt",
    "What is the capital of France?",
])
def test_other_requests_do_not_expect_a_submission(message):
    kinds = {item.type for item in expected_postconditions(_task(message, []))}
    assert PostconditionType.FORM_SUBMITTED not in kinds


def test_filling_without_submitting_is_a_gap():
    task = _task(SIGN_UP, [OPEN, _step("fill_form_step", fields={"Name": "Oney"})])
    assert PostconditionType.FORM_SUBMITTED in _missing(task)


def test_a_click_after_the_fill_counts():
    task = _task(SIGN_UP, [OPEN, _step("fill_form_step", fields={"Name": "Oney"}), _step("click", text="Sign me up")])
    assert PostconditionType.FORM_SUBMITTED not in _missing(task)


def test_a_fill_that_submits_counts():
    task = _task(SIGN_UP, [OPEN, _step("fill_form", fields={"Name": "Oney"}, submit=True)])
    assert PostconditionType.FORM_SUBMITTED not in _missing(task)


def test_a_click_before_anything_was_filled_does_not_count():
    task = _task(SIGN_UP, [OPEN, _step("click", text="Sign me up"), _step("fill_form_step", fields={"Name": "Oney"})])
    assert PostconditionType.FORM_SUBMITTED in _missing(task)


def test_a_failed_click_does_not_count():
    task = _task(SIGN_UP, [OPEN, _step("fill_form_step", fields={"Name": "Oney"}), _step("click", "failed", text="Sign me up")])
    assert PostconditionType.FORM_SUBMITTED in _missing(task)


def test_the_gap_tells_the_model_what_to_do():
    guidance = fulfillment_guidance("expected_form_submitted_missing")
    assert "never submitted" in guidance and "operation=click" in guidance and "'text'" in guidance


# ---- a repeated step is told what is still missing ----------------------------------------

from agent_control.orchestration.worker import _repeat_stop_message, _with_what_is_missing  # noqa: E402


def test_a_repeated_fill_is_told_the_form_still_has_to_be_submitted():
    task = _task(SIGN_UP, [OPEN, _step("fill_form", fields={"Name": "Oney"})])
    text = _with_what_is_missing(task, task.metadata["operator_history"], "Not run again: this exact call already succeeded at step 2.")
    assert text.startswith("Not run again:")
    assert "Still missing for the request:" in text and "operation=click" in text


def test_a_repeat_with_nothing_missing_is_unchanged():
    task = _task("What is the capital of France?", [])
    summary = "Not run again: this exact call already succeeded at step 1."
    assert _with_what_is_missing(task, [], summary) == summary


def test_the_stop_message_is_in_words_and_says_what_was_left():
    task = _task(SIGN_UP, [OPEN, _step("fill_form", fields={"Name": "Oney"})])
    text = _repeat_stop_message(task, task.metadata["operator_history"], "browser.control", {"operation": "fill_form"})
    assert text.startswith("I got stuck repeating a step that had already worked (filling in a form)")
    assert text.endswith("The form was filled in but never submitted, so the sign-up is not done.")
    assert "browser.control" not in text and "operation=" not in text


def test_a_declared_postcondition_does_not_drop_the_submission():
    """The classifier declared only browser_state for this request in a live run; the run navigated and said it was done."""
    task = _task(SIGN_UP, [OPEN])
    task.metadata["expected_postconditions"] = ["browser_state"]
    kinds = {item.type for item in expected_postconditions(task)}
    assert kinds == {PostconditionType.BROWSER_STATE, PostconditionType.FORM_SUBMITTED}
    assert PostconditionType.FORM_SUBMITTED in _missing(task)


def test_a_declaration_for_another_kind_of_request_is_untouched():
    task = _task("Open https://example.test/news and tell me the headline.", [OPEN])
    task.metadata["expected_postconditions"] = ["browser_state"]
    assert {item.type for item in expected_postconditions(task)} == {PostconditionType.BROWSER_STATE}
