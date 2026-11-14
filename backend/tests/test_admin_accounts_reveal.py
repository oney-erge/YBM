"""The console can show a login YBM made for a site, when the person presses the button (admin.py /api/accounts/reveal)."""

from __future__ import annotations

from agent_control.accounts import AccountVault
from agent_control.storage.secrets import SecretVault
from test_admin_session import HEADER, _client


def _client_with_vault(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENT_SECRET_VAULT_KEY", SecretVault.generate_key())
    from agent_control.config import AppSettings

    settings = AppSettings(_env_file=None)
    password = AccountVault(settings.secrets).resolve_fields(
        {"Email": "me@example.com", "Password": "{{new_password}}"}, host="pinecone.example", task_id="t1", url="https://pinecone.example/up"
    )["Password"]
    return client, password


def test_pressing_show_returns_the_login_and_is_never_cached(monkeypatch, tmp_path):
    client, password = _client_with_vault(monkeypatch, tmp_path)
    response = client.post("/admin/api/accounts/reveal", json={"host": "Pinecone.example"}, headers=HEADER)
    assert response.status_code == 200
    assert response.json()["password"] == password and response.json()["username"] == "me@example.com"
    assert response.headers["cache-control"] == "private, no-store"


def test_the_audit_trail_says_which_site_was_shown_and_never_the_password(monkeypatch, tmp_path):
    client, password = _client_with_vault(monkeypatch, tmp_path)
    client.post("/admin/api/accounts/reveal", json={"host": "pinecone.example"}, headers=HEADER)
    events = client.get("/admin/api/audit", headers=HEADER)
    text = events.text
    assert "account_password_shown" in text and "pinecone.example" in text and password not in text


def test_a_site_with_no_login_is_not_found_and_a_missing_token_is_refused(monkeypatch, tmp_path):
    client, _ = _client_with_vault(monkeypatch, tmp_path)
    assert client.post("/admin/api/accounts/reveal", json={"host": "nowhere.example"}, headers=HEADER).status_code == 404
    assert client.post("/admin/api/accounts/reveal", json={"host": "pinecone.example"}).status_code in (401, 403)
