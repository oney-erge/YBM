"""What the page in front of the model asks for, said once, in the task's history (orchestration/ladder.py form_hint)."""

from __future__ import annotations

from agent_control.orchestration import ladder
from agent_control.orchestration.request_facts import RequestFacts
from agent_control.orchestration.workflows import instantiate_plan
from agent_control.storage.redaction import redact_payload

SIGN_UP = "Go to https://pinecone.example/signup and create an account there for me with my email me@example.com."
LOG_IN = "Log me in to https://pinecone.example/login"
FORM_PAGE = "Title: Create your account | Forms detected: 1 | Create your account Full name Email address Password Confirm password I agree to the terms of service Create account"


def _page(summary: str, tool: str = "browser.open", status: str = "succeeded") -> dict:
    return {"tool_name": tool, "input": {"operation": "open"}, "status": status, "output_summary": summary}


def _hint(request: str, steps: list[dict], given: set[str] | None = None):
    return ladder.form_hint(request, steps, given or set())


def test_what_counts_as_asking_for_an_account_or_a_login():
    assert RequestFacts.from_text(SIGN_UP).wants_a_new_account
    for text in ("sign me up for the newsletter at x.com", "register on that site", "Make a new account at example.com", "create a login for me"):
        assert RequestFacts.from_text(text).wants_a_new_account, text
    for text in ("what is on that page", "log in to my bank", "write a script"):
        assert not RequestFacts.from_text(text).wants_a_new_account, text
    assert RequestFacts.from_text(LOG_IN).wants_to_log_in and RequestFacts.from_text("sign in to github").wants_to_log_in


def test_a_password_field_on_a_sign_up_page_gets_the_placeholder_advice_once():
    first = _hint(SIGN_UP, [_page(FORM_PAGE)])
    assert first is not None and first.id == "password"
    assert "{{new_password}}" in first.text and "never shows it to you" in first.text
    assert _hint(SIGN_UP, [_page(FORM_PAGE)], {"password"}).id == "terms"  # next thing the page asks for
    assert _hint(SIGN_UP, [_page(FORM_PAGE)], {"password", "terms"}) is None


def test_the_advice_is_not_given_when_the_model_already_used_the_placeholder():
    filled = {"tool_name": "browser.control", "input": {"operation": "fill_form", "fields": {"Password": "{{new_password}}"}},
              "status": "succeeded", "output_summary": FORM_PAGE}
    hint = _hint(SIGN_UP, [_page(FORM_PAGE), filled])
    assert hint is None or hint.id != "password"


def test_a_password_field_on_a_page_for_something_else_gets_no_advice():
    assert _hint("What is on https://pinecone.example/signup ?", [_page(FORM_PAGE)]) is None
    assert _hint(SIGN_UP, [_page(FORM_PAGE, tool="filesystem.manage")]) is None
    assert _hint(SIGN_UP, [_page(FORM_PAGE, status="failed")]) is None


def test_a_login_request_is_pointed_to_the_saved_login():
    hint = _hint(LOG_IN, [_page("Sign in Email Password Remember me Sign in")])
    assert hint is not None and hint.id == "login" and "{{saved_password}}" in hint.text


def test_a_captcha_is_handed_to_the_user_with_a_screenshot():
    hint = _hint(SIGN_UP, [_page("Please verify you are human. Complete the CAPTCHA to continue.")])
    assert hint is not None and hint.id == "captcha" and "send_screenshot" in hint.text and "get around" in hint.text
    assert _hint(SIGN_UP, [_page("Complete the CAPTCHA")], {"captcha"}) is None


def test_a_confirmation_email_ends_the_task_honestly_after_the_account_was_made():
    typed = {"tool_name": "browser.control", "input": {"operation": "fill_form", "fields": {"Password": "{{new_password}}"}, "submit": True},
             "status": "succeeded", "output_summary": "Your account is ready. We sent a confirmation link to your email address."}
    hint = _hint(SIGN_UP, [_page(FORM_PAGE), typed])
    assert hint is not None and hint.id == "email_confirmation" and "cannot read the user's mail" in hint.text
    # before any password was typed it is just a page that mentions email
    assert (_hint(SIGN_UP, [_page("We sent you a confirmation link by email")]) or ladder.Hint("none", "")).id != "email_confirmation"


def test_the_hints_come_through_next_hint_ahead_of_the_search_rungs():
    hint = ladder.next_hint(SIGN_UP, [_page(FORM_PAGE)], set(), code_enabled=True)
    assert hint is not None and hint.id == "password"


def test_a_vault_placeholder_survives_redaction_but_a_real_password_does_not():
    payload = {"fields": {"Password": "{{new_password}}", "Confirm password": "{{ new_password }}", "Pin": "hunter2", "Email": "me@example.com"}}
    redacted = redact_payload(payload)
    assert redacted["fields"]["Password"] == "{{new_password}}" and redacted["fields"]["Confirm password"] == "{{ new_password }}"
    assert redacted["fields"]["Email"] == "me@example.com"
    assert redact_payload({"password": "hunter2", "Password": "x{{new_password}}"}) == {"password": "***", "Password": "***"}


def test_a_saved_workflow_keeps_vault_placeholders_and_does_not_ask_for_them_as_parameters():
    plan = [{"tool_name": "browser.control", "tool_input": {"operation": "fill_form", "fields": {"Password": "{{new_password}}", "Email": "{{email}}"}}}]
    filled = instantiate_plan(plan, {"email": "me@example.com"})
    assert filled[0]["tool_input"]["fields"] == {"Password": "{{new_password}}", "Email": "me@example.com"}
