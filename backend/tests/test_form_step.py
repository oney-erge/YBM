"""A form step with nothing to fill is refused before anyone is asked to approve it."""

from __future__ import annotations

import pytest

from agent_control.channels.task_notify import format_task_message
from agent_control.schemas import TaskRecord, TaskStatus
from agent_control.tools.contracts import BrowserFillFormStepInput


def test_a_step_with_no_fields_is_invalid():
    with pytest.raises(ValueError, match="fields"):
        BrowserFillFormStepInput.model_validate({"operation": "fill_form_step", "url_contains": "127.0.0.1"})
    with pytest.raises(ValueError, match="fields"):
        BrowserFillFormStepInput.model_validate({"operation": "fill_form_step", "fields": {}})


def test_one_field_as_a_pair_becomes_a_one_entry_fields():
    step = BrowserFillFormStepInput.model_validate({"operation": "fill_form_step", "field": "Full name", "value": "Oney Erge"})
    assert step.fields == {"Full name": "Oney Erge"}
    dumped = step.model_dump(exclude_none=True)
    assert "field" not in dumped and "value" not in dumped


def test_a_step_with_fields_is_unchanged():
    step = BrowserFillFormStepInput.model_validate({"operation": "fill_form_step", "fields": {"A": "1", "B": "2"}, "submit": True})
    assert step.fields == {"A": "1", "B": "2"} and step.submit is True


def _blocked(error):
    return TaskRecord(objective="sign me up", status=TaskStatus.BLOCKED, metadata={"last_tool_result": {"error_message": error, "output": {}}})


def test_an_ordinary_browser_error_is_not_reported_as_chrome_not_running():
    text = format_task_message(_blocked("browser operation failed: the Sign me up button was not found"))
    assert "Chrome is not running" not in text
    assert text.startswith("I could not complete this request.")


def test_a_real_chrome_connection_failure_still_says_so():
    text = format_task_message(_blocked("Chrome DevTools is not available at http://127.0.0.1:9222"))
    assert "Chrome is not running with remote debugging enabled" in text
