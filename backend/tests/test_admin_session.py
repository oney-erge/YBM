"""The console signs in once and stays signed in, without anyone pasting a token.

The admin token is generated for the person and lives in .env. These pin the
mechanism that spares them from ever handling it: a one-time ?token= link is
exchanged for an HttpOnly session cookie, and the console only shows the
"paste your admin token" screen when that sign-in is genuinely missing.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_control.admin import create_admin_router
from agent_control.config import AppSettings
from agent_control.storage import Database, Repositories
from agent_control.tools.vscode_bridge import VSCodeBridgeStore

TOKEN = "launch-token-abc123"
HEADER = {"X-Agent-Control-Admin-Token": TOKEN}


def _client(
    monkeypatch,
    tmp_path,
    *,
    token: str | None = TOKEN,
    base_url: str = "http://testserver",
    settings: AppSettings | None = None,
) -> TestClient:
    monkeypatch.chdir(tmp_path)  # read_env_value() also reads ./.env, so isolate it
    if token is None:
        monkeypatch.delenv("AGENT_ADMIN_TOKEN", raising=False)
    else:
        monkeypatch.setenv("AGENT_ADMIN_TOKEN", token)
    database = Database(f"sqlite:///{tmp_path / 'admin.db'}")
    database.initialize()
    repositories = Repositories.for_database(database)
    app = FastAPI()
    app.include_router(
        create_admin_router(lambda: settings or AppSettings(_env_file=None), lambda: repositories, VSCodeBridgeStore())
    )
    return TestClient(app, base_url=base_url)


def _bootstrap(client: TestClient, **kwargs) -> dict:
    response = client.get("/admin/api/bootstrap", **kwargs)
    assert response.status_code == 200
    return response.json()


def test_a_browser_with_no_credentials_is_not_authenticated(monkeypatch, tmp_path) -> None:
    body = _bootstrap(_client(monkeypatch, tmp_path))

    assert body["token_required"] is True
    assert body["authenticated"] is False


def test_the_launch_link_token_counts_as_authenticated(monkeypatch, tmp_path) -> None:
    body = _bootstrap(_client(monkeypatch, tmp_path), headers=HEADER)

    assert body["authenticated"] is True


def test_a_wrong_token_is_not_authenticated(monkeypatch, tmp_path) -> None:
    body = _bootstrap(_client(monkeypatch, tmp_path), headers={"X-Agent-Control-Admin-Token": "nope"})

    assert body["authenticated"] is False


def test_with_no_token_configured_nobody_is_asked_for_one(monkeypatch, tmp_path) -> None:
    body = _bootstrap(_client(monkeypatch, tmp_path, token=None))

    assert body["token_required"] is False
    assert body["authenticated"] is True


def test_starting_a_session_sets_a_hardened_cookie(monkeypatch, tmp_path) -> None:
    client = _client(monkeypatch, tmp_path)

    response = client.post("/admin/api/session", headers=HEADER)

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "persistent": True}
    cookie = response.headers["set-cookie"]
    assert cookie.startswith("ybm_admin_8765=")
    lowered = cookie.lower()
    assert "httponly" in lowered
    assert "samesite=strict" in lowered
    assert "path=/admin" in lowered
    assert "max-age=" in lowered
    # Plain http on loopback: Secure would stop the browser storing it.
    assert "secure" not in lowered


def test_the_cookie_never_contains_the_token(monkeypatch, tmp_path) -> None:
    client = _client(monkeypatch, tmp_path)

    cookie = client.post("/admin/api/session", headers=HEADER).headers["set-cookie"]

    assert TOKEN not in cookie


def test_after_a_session_starts_the_header_is_no_longer_needed(monkeypatch, tmp_path) -> None:
    """The point of the whole feature: a later visit, with no token in hand."""
    client = _client(monkeypatch, tmp_path)
    assert client.post("/admin/api/session", headers=HEADER).status_code == 200

    assert _bootstrap(client)["authenticated"] is True
    assert client.get("/admin/api/summary").status_code == 200  # no header, cookie only


def test_a_fresh_browser_without_the_cookie_is_still_refused(monkeypatch, tmp_path) -> None:
    signed_in = _client(monkeypatch, tmp_path)
    signed_in.post("/admin/api/session", headers=HEADER)

    stranger = _client(monkeypatch, tmp_path)

    assert stranger.get("/admin/api/summary").status_code == 401


def test_a_session_cannot_be_started_from_nothing(monkeypatch, tmp_path) -> None:
    client = _client(monkeypatch, tmp_path)

    response = client.post("/admin/api/session")

    assert response.status_code == 401
    assert "set-cookie" not in response.headers


def test_a_forged_cookie_is_refused(monkeypatch, tmp_path) -> None:
    client = _client(monkeypatch, tmp_path)
    client.cookies.set("ybm_admin_8765", "not-the-real-value", path="/admin")

    assert client.get("/admin/api/summary").status_code == 401
    assert _bootstrap(client)["authenticated"] is False


def test_the_raw_token_is_not_accepted_as_the_cookie_value(monkeypatch, tmp_path) -> None:
    client = _client(monkeypatch, tmp_path)
    client.cookies.set("ybm_admin_8765", TOKEN, path="/admin")

    assert client.get("/admin/api/summary").status_code == 401


def test_rotating_the_token_signs_every_browser_out(monkeypatch, tmp_path) -> None:
    client = _client(monkeypatch, tmp_path)
    client.post("/admin/api/session", headers=HEADER)
    assert client.get("/admin/api/summary").status_code == 200

    monkeypatch.setenv("AGENT_ADMIN_TOKEN", "a-different-token")

    assert client.get("/admin/api/summary").status_code == 401


def test_the_token_is_still_never_accepted_from_a_query_string(monkeypatch, tmp_path) -> None:
    client = _client(monkeypatch, tmp_path)

    assert client.get(f"/admin/api/summary?token={TOKEN}").status_code == 401


def test_two_instances_on_one_machine_do_not_share_a_cookie_name(monkeypatch, tmp_path) -> None:
    other_port = _client(monkeypatch, tmp_path, base_url="http://127.0.0.1:9001")

    cookie = other_port.post("/admin/api/session", headers=HEADER).headers["set-cookie"]

    assert cookie.startswith("ybm_admin_9001=")


def test_the_cookie_is_marked_secure_when_served_over_https(monkeypatch, tmp_path) -> None:
    client = _client(monkeypatch, tmp_path, base_url="https://testserver")

    cookie = client.post("/admin/api/session", headers=HEADER).headers["set-cookie"]

    assert "secure" in cookie.lower()


def test_signing_out_forgets_the_cookie(monkeypatch, tmp_path) -> None:
    client = _client(monkeypatch, tmp_path)
    client.post("/admin/api/session", headers=HEADER)

    response = client.delete("/admin/api/session")

    assert response.status_code == 200
    assert response.json() == {"status": "signed_out"}
    assert _bootstrap(client)["authenticated"] is False


def test_a_cross_site_page_cannot_start_a_session(monkeypatch, tmp_path) -> None:
    """The existing same-origin check covers the new route too: a page on
    another origin cannot mint a cookie for the console."""
    client = _client(monkeypatch, tmp_path)

    response = client.post("/admin/api/session", headers={**HEADER, "Origin": "https://evil.example"})

    assert response.status_code == 403
    assert "set-cookie" not in response.headers


def test_starting_a_session_without_a_configured_token_sets_nothing(monkeypatch, tmp_path) -> None:
    client = _client(monkeypatch, tmp_path, token=None)

    response = client.post("/admin/api/session")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "persistent": False}
    assert "set-cookie" not in response.headers


# ---- the console says which model first run picked ---------------------------


def _with_model(profile: dict) -> AppSettings:
    return AppSettings(_env_file=None, llm={"default_profile": "onboard", "profiles": {"onboard": profile}})


def test_bootstrap_reports_no_model_when_none_is_configured(monkeypatch, tmp_path) -> None:
    model = _bootstrap(_client(monkeypatch, tmp_path), headers=HEADER)["model"]

    assert model == {"configured": False, "model": None, "provider": None, "local": False, "key_env": None}


def test_bootstrap_names_the_environment_variable_in_use_but_never_its_value(monkeypatch, tmp_path) -> None:
    secret = "sk-this-must-never-leave-the-server"
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    settings = _with_model(
        {"provider": "openai_compatible", "model": "gpt-4.1", "base_url": "https://api.openai.com/v1", "api_key_env": "OPENAI_API_KEY"}
    )
    client = _client(monkeypatch, tmp_path, settings=settings)

    response = client.get("/admin/api/bootstrap", headers=HEADER)

    model = response.json()["model"]
    assert model["configured"] is True
    assert model["model"] == "gpt-4.1"
    assert model["provider"] == "OpenAI"
    assert model["local"] is False
    assert model["key_env"] == "OPENAI_API_KEY"
    assert secret not in response.text


def test_bootstrap_does_not_claim_a_key_that_is_not_actually_set(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    settings = _with_model(
        {"provider": "openai_compatible", "model": "gpt-4.1", "base_url": "https://api.openai.com/v1", "api_key_env": "OPENAI_API_KEY"}
    )

    model = _bootstrap(_client(monkeypatch, tmp_path, settings=settings), headers=HEADER)["model"]

    assert model["key_env"] is None
    assert model["configured"] is False  # a cloud profile with no key is not usable


def test_bootstrap_describes_a_local_model_as_local(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("agent_control.admin.check_llm_configured", lambda settings: True)
    settings = _with_model(
        {"provider": "openai_compatible", "model": "qwen3:8b", "base_url": "http://127.0.0.1:11434/v1", "api_key_env": None}
    )

    model = _bootstrap(_client(monkeypatch, tmp_path, settings=settings), headers=HEADER)["model"]

    assert model["local"] is True
    assert model["provider"] == "Ollama (on this machine)"
    assert model["key_env"] is None


def test_bootstrap_labels_anthropic_by_its_native_provider(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    settings = _with_model({"provider": "anthropic", "model": "claude-sonnet-5", "base_url": None, "api_key_env": "ANTHROPIC_API_KEY"})

    model = _bootstrap(_client(monkeypatch, tmp_path, settings=settings), headers=HEADER)["model"]

    assert model["provider"] == "Anthropic (Claude)"
    assert model["key_env"] == "ANTHROPIC_API_KEY"


def test_bootstrap_falls_back_to_a_plain_label_for_a_custom_endpoint(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("agent_control.admin.check_llm_configured", lambda settings: True)
    settings = _with_model(
        {"provider": "openai_compatible", "model": "my-model", "base_url": "https://llm.example.internal/v1", "api_key_env": None}
    )

    model = _bootstrap(_client(monkeypatch, tmp_path, settings=settings), headers=HEADER)["model"]

    assert model["provider"] == "Custom endpoint"
