"""Reading back the logins YBM made: "show my <site> password" (channels/account_commands.py)."""

from __future__ import annotations

import pytest

from agent_control.accounts import AccountVault
from agent_control.channels.account_commands import account_command_reply
from agent_control.config import SecretVaultConfig
from agent_control.schemas import ChannelType, InboundMessage, MessageKind
from agent_control.storage import AuditLogger, Database, Repositories
from agent_control.storage.secrets import SecretVault


@pytest.fixture()
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SECRET_VAULT_KEY", SecretVault.generate_key())
    config = SecretVaultConfig(path=str(tmp_path / "vault.json"))
    database = Database(f"sqlite:///{tmp_path / 'a.db'}")
    database.initialize()
    repos = Repositories.for_database(database)
    accounts = AccountVault(config)
    password = accounts.resolve_fields({"Email": "me@example.com", "Password": "{{new_password}}"}, host="pinecone.example", task_id="t1")["Password"]
    return config, AuditLogger(repos.audit), password, repos


def _say(text: str, channel: ChannelType = ChannelType.TELEGRAM) -> InboundMessage:
    return InboundMessage(id="m1", channel=channel, kind=MessageKind.TEXT, sender_id="42", chat_id="100", text=text)


def test_asking_for_a_password_shows_it_and_says_so_in_the_audit_trail(setup):
    config, audit, password, repos = setup
    for text in ("show my pinecone password", "What is my pinecone password?", "send me the pinecone login"):
        reply = account_command_reply(config, audit, _say(text))
        assert reply is not None and password in reply.text and "me@example.com" in reply.text, text
    events = [e for e in repos.audit.list_recent(20) if "account_password_shown" in str(e.payload)]
    assert events and password not in str(events[0].payload)


def test_asking_what_it_made_lists_sites_and_usernames_without_passwords(setup):
    config, audit, password, _ = setup
    reply = account_command_reply(config, audit, _say("which accounts did you make?"))
    assert reply is not None and "pinecone.example" in reply.text and "me@example.com" in reply.text and password not in reply.text


def test_forgetting_a_login_removes_it_from_the_vault(setup):
    config, audit, password, _ = setup
    reply = account_command_reply(config, audit, _say("forget my pinecone login"))
    assert reply is not None and "Forgot" in reply.text
    assert AccountVault(config).get("pinecone.example") is None
    assert "not made any logins" in account_command_reply(config, audit, _say("list my logins")).text


def test_a_site_with_no_saved_login_and_other_sentences_go_on_to_ordinary_intake(setup):
    config, audit, _, _ = setup
    for text in ("show my bank password", "what is a good password", "my password is hunter2", "remind me to change my password", "what is the weather"):
        assert account_command_reply(config, audit, _say(text)) is None, text


def test_only_telegram_and_only_with_a_vault_configured(setup):
    config, audit, _, _ = setup
    assert account_command_reply(config, audit, _say("show my pinecone password", ChannelType.WEB)) is None
    assert account_command_reply(None, audit, _say("show my pinecone password")) is None
