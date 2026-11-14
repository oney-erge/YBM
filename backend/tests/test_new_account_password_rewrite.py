"""A password the model made up for a new account is replaced by the vault's placeholder (orchestration/call_rewrites.py)."""

from __future__ import annotations

from agent_control.orchestration import call_policy
from agent_control.orchestration.call_rewrites import _new_account_password_from_the_vault
from agent_control.schemas import TaskRecord


def _task(text: str) -> TaskRecord:
    return TaskRecord(objective=text, metadata={"original_message_text": text})


SIGN_UP = "Go to https://pinecone.example/signup and create an account for me with me@example.com"
FILL = {"operation": "fill_form", "fields": {"Email": "me@example.com", "Password": "OneyErge#Secure2026!", "Confirm password": "OneyErge#Secure2026!"}}


def test_the_password_the_model_made_up_becomes_the_placeholder_and_the_rest_is_kept():
    rewritten = _new_account_password_from_the_vault(_task(SIGN_UP), "browser.control", FILL)
    assert rewritten["fields"] == {"Email": "me@example.com", "Password": "{{new_password}}", "Confirm password": "{{new_password}}"}
    assert FILL["fields"]["Password"] == "OneyErge#Secure2026!"  # the original input is not changed in place


def test_a_password_the_user_wrote_is_left_alone_and_so_are_requests_that_are_not_new_accounts():
    said = SIGN_UP + " and use the password Tr0ub4dor&3"
    mine = {"operation": "fill_form", "fields": {"Password": "Tr0ub4dor&3"}}
    assert _new_account_password_from_the_vault(_task(said), "browser.control", mine) == mine
    assert _new_account_password_from_the_vault(_task("Log me in to https://x.example"), "browser.control", FILL) == FILL
    assert _new_account_password_from_the_vault(_task(SIGN_UP), "filesystem.manage", FILL) == FILL
    click = {"operation": "click", "selector": "button"}
    assert _new_account_password_from_the_vault(_task(SIGN_UP), "browser.control", click) == click


def test_placeholders_empty_values_and_other_fields_are_not_touched():
    already = {"operation": "fill_form", "fields": {"Password": "{{ new_password }}", "Pin": "1234", "Name": "Oney"}}
    assert _new_account_password_from_the_vault(_task(SIGN_UP), "browser.control", already) == already
    blank = {"operation": "fill_form", "fields": {"Password": "  "}}
    assert _new_account_password_from_the_vault(_task(SIGN_UP), "browser.control", blank) == blank


def test_it_runs_with_the_other_rewrites_in_the_call_policy():
    labels = [label for label, _ in call_policy.REWRITES]
    assert "a made-up password for a new account becomes the vault's" in labels
    vetted = call_policy.vet_call(_task(SIGN_UP), "browser.control", FILL, [], {})
    assert vetted.refusal is None and vetted.tool_input["fields"]["Password"] == "{{new_password}}"
