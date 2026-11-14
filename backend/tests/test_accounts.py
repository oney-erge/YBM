"""Logins YBM makes for websites (accounts.py): generated here, kept in the vault, typed by the browser tool, never shown to the model."""

from __future__ import annotations

import json
import re

import pytest

from agent_control.accounts import SERVICE, AccountVault, host_of, new_password, uses_placeholder
from agent_control.config import SecretVaultConfig
from agent_control.storage.secrets import SecretVault


@pytest.fixture()
def accounts(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SECRET_VAULT_KEY", SecretVault.generate_key())
    return AccountVault(SecretVaultConfig(path=str(tmp_path / "vault.json")))


def test_a_generated_password_is_long_mixed_and_has_no_look_alike_characters():
    seen = {new_password() for _ in range(200)}
    assert len(seen) == 200
    for password in seen:
        assert len(password) == 20
        assert re.search(r"[a-z]", password) and re.search(r"[A-Z]", password) and re.search(r"\d", password) and re.search(r"[^A-Za-z0-9]", password)
        assert not set(password) & set("lIO01")


def test_the_site_is_the_host_without_www():
    assert host_of("https://www.Pinecone.io/signup?x=1") == "pinecone.io"
    assert host_of("http://127.0.0.1:8899/signup.html") == "127.0.0.1"
    assert host_of("about:blank") == "" and host_of(None) == ""


def test_both_password_fields_of_one_task_get_the_same_new_password(accounts):
    fields = {"Email address": "me@example.com", "Password": "{{new_password}}", "Confirm password": "{{ new_password }}"}
    resolved = accounts.resolve_fields(fields, host="pinecone.io", task_id="task_1", url="https://pinecone.io/signup")
    assert resolved["Password"] == resolved["Confirm password"] and resolved["Password"] != "{{new_password}}"
    assert resolved["Email address"] == "me@example.com"
    entry = accounts.get("pinecone.io")
    assert entry["password"] == resolved["Password"] and entry["username"] == "me@example.com" and entry["task_id"] == "task_1"


def test_the_password_and_the_email_may_be_typed_in_separate_calls_in_either_order(accounts):
    first = accounts.resolve_fields({"Password": "{{new_password}}"}, host="site.test", task_id="task_2")
    accounts.resolve_fields({"Email": "me@example.com"}, host="site.test", task_id="task_2")
    second = accounts.resolve_fields({"Confirm password": "{{new_password}}"}, host="site.test", task_id="task_2")
    assert first["Password"] == second["Confirm password"]
    assert accounts.get("site.test")["username"] == "me@example.com"


def test_another_task_makes_its_own_password_and_replaces_the_entry(accounts):
    one = accounts.resolve_fields({"Password": "{{new_password}}"}, host="site.test", task_id="task_a")["Password"]
    two = accounts.resolve_fields({"Password": "{{new_password}}"}, host="site.test", task_id="task_b")["Password"]
    assert one != two and accounts.get("site.test")["password"] == two


def test_a_saved_login_is_typed_back_to_log_in(accounts):
    accounts.resolve_fields({"Email": "me@example.com", "Password": "{{new_password}}"}, host="site.test", task_id="task_a")
    login = accounts.resolve_fields({"Email": "{{saved_username}}", "Password": "{{saved_password}}"}, host="site.test", task_id="task_login")
    assert login["Email"] == "me@example.com" and login["Password"] == accounts.get("site.test")["password"]


def test_a_saved_login_that_does_not_exist_is_an_error_the_model_can_act_on(accounts):
    with pytest.raises(ValueError, match="no login is saved for nowhere.test"):
        accounts.resolve_fields({"Password": "{{saved_password}}"}, host="nowhere.test", task_id="task_x")


def test_a_placeholder_with_no_page_open_says_to_open_the_page_first(accounts):
    with pytest.raises(ValueError, match="open the site's page first"):
        accounts.resolve_fields({"Password": "{{new_password}}"}, host="", task_id="task_x")


def test_fields_without_placeholders_pass_through_and_touch_no_vault(accounts, monkeypatch):
    assert not uses_placeholder({"Name": "Oney"})
    fields = {"Name": "Oney", "Message": "hello"}
    assert accounts.resolve_fields(fields, host="site.test", task_id="task_x") is fields
    monkeypatch.delenv("AGENT_SECRET_VAULT_KEY")
    assert accounts.resolve_fields({"Email": "me@example.com"}, host="site.test", task_id="task_x") == {"Email": "me@example.com"}


def test_the_listing_never_carries_a_password(accounts):
    accounts.resolve_fields({"Email": "me@example.com", "Password": "{{new_password}}"}, host="a.test", task_id="t1", url="https://a.test/up")
    accounts.resolve_fields({"Password": "{{new_password}}"}, host="b.test", task_id="t2")
    entries = accounts.list_entries()
    assert [e["host"] for e in entries] == ["a.test", "b.test"]
    assert "password" not in json.dumps(entries)


def test_a_person_finds_a_site_by_part_of_its_name(accounts):
    accounts.resolve_fields({"Password": "{{new_password}}"}, host="notes.pinecone.io", task_id="t1")
    accounts.resolve_fields({"Password": "{{new_password}}"}, host="example.com", task_id="t2")
    assert accounts.find("pinecone") == ["notes.pinecone.io"]
    assert accounts.find("my example.com login") == ["example.com"] or accounts.find("example") == ["example.com"]
    assert accounts.find("") == [] and accounts.find("nothing") == []


def test_forgetting_a_login_removes_it(accounts):
    accounts.resolve_fields({"Password": "{{new_password}}"}, host="a.test", task_id="t1")
    assert accounts.forget("a.test") is True and accounts.get("a.test") is None and accounts.forget("a.test") is False


def test_the_vault_file_holds_nothing_readable(accounts, tmp_path):
    password = accounts.resolve_fields({"Password": "{{new_password}}"}, host="a.test", task_id="t1")["Password"]
    raw = (tmp_path / "vault.json").read_bytes()
    assert password.encode() not in raw and b"a.test" not in raw and SERVICE.encode() not in raw
