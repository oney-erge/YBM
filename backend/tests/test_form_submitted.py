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
