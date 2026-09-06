from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI
from fastapi.testclient import TestClient
import yaml

import agent_control.admin as admin_module
from agent_control.admin import create_admin_router
from agent_control.config import AppSettings, default_capability_policies
from agent_control.main import app
from datetime import timedelta

from agent_control.schemas import (
    ApprovalRequest,
    ApprovalStatus,
    Artifact,
    ArtifactType,
    AuditEventType,
    Capability,
    CapabilityAccessMode,
    LLMCallRecord,
    MemoryFact,
    RiskLevel,
    ScheduleRecord,
    TaskStatus,
    ToolCallRequest,
    ToolCallResult,
    ToolResultStatus,
    ToolVerification,
    utc_now,
)
from agent_control.storage import AuditLogger, Database, Repositories
from agent_control.tools.vscode_bridge import VSCodeBridgeStore


def _repositories(database_url: str) -> Repositories:
    database = Database(database_url)
    database.initialize()
    return Repositories.for_database(database)


def _admin_client(
    repositories: Repositories,
    settings: AppSettings | None = None,
    vscode_store: VSCodeBridgeStore | None = None,
) -> TestClient:
    # The one piece of setup every test in this file needs - was hand-copied
    # 30 times (two slightly different formattings) before being pulled out
    # here. `lambda: settings or AppSettings(...)` preserves the original
    # per-call laziness (several tests re-read config.yaml after a POST
    # writes it, which depends on the settings loader re-constructing
    # AppSettings, not returning a cached instance).
    app = FastAPI()
    app.include_router(
        create_admin_router(
            lambda: settings or AppSettings(_env_file=None),
            lambda: repositories,
            vscode_store or VSCodeBridgeStore(),
        )
    )
    return TestClient(app)


def test_admin_page_points_to_build_instructions_before_a_react_build_exists(monkeypatch, tmp_path) -> None:
    # chdir, not just delenv: read_env_value() reads .env from the current
    # working directory, so a bare delenv here is not real isolation.
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AGENT_ADMIN_TOKEN", raising=False)
    monkeypatch.setenv("AGENT_STORAGE__DATABASE_URL", f"sqlite:///{tmp_path / 'admin.db'}")
    # Explicitly force the "no build yet" precondition (docs/UI_REWRITE_PLAN.md
    # §9 Phase 0.2's case 3) rather than relying on whatever
    # backend/src/agent_control/static/admin/ happens to contain on this
    # machine - that directory is a gitignored, locally-generated build
    # artifact (frontend/'s `npm run build` output), so this test would
    # otherwise pass or fail depending on whether someone had run the
    # frontend build locally, which a fresh clone or CI never has.
    monkeypatch.setattr(admin_module, "_STATIC_ADMIN_DIR", tmp_path / "no_build_here")
    client = TestClient(app)

    page = client.get("/admin")

    # The React console is the only admin UI now (Streamlit was removed at
    # cutover, docs/UI_REWRITE_PLAN.md §19) - /admin falls back to a small
    # pointer telling the operator to build it, rather than 404ing or
    # crashing when no build is present at this checkout.
    assert page.status_code == 200
    assert "YBM" in page.text
    assert "ui-build" in page.text
    assert "8501" not in page.text
    assert "streamlit" not in page.text.lower()
    assert len(page.text) < 2000
    assert "onclick=" not in page.text
    assert "task-card" not in page.text


def test_admin_summary_api_unaffected_by_html_page_removal(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)  # see test_admin_page_points_to_build_instructions for why chdir, not just delenv
    monkeypatch.delenv("AGENT_ADMIN_TOKEN", raising=False)
    monkeypatch.setenv("AGENT_STORAGE__DATABASE_URL", f"sqlite:///{tmp_path / 'admin.db'}")
    client = TestClient(app)

    summary = client.get("/admin/api/summary")

    assert summary.status_code == 200
    assert summary.json()["config"]["identity"]["instance_name"] == "ybm-control"
    assert "services" in summary.json()
    assert "schedules" in summary.json()
    assert "tool_registry" in summary.json()


def test_admin_fails_closed_on_non_loopback_host_without_token(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)  # see test_admin_page_and_summary for why chdir, not just delenv
    monkeypatch.delenv("AGENT_ADMIN_TOKEN", raising=False)
    monkeypatch.setenv("AGENT_SERVER__HOST", "0.0.0.0")
    monkeypatch.setenv("AGENT_STORAGE__DATABASE_URL", f"sqlite:///{tmp_path / 'admin.db'}")
    client = TestClient(app)

    summary = client.get("/admin/api/summary")

    assert summary.status_code == 503


def test_admin_rejects_cross_origin_request_even_without_token(monkeypatch, tmp_path) -> None:
    # The exploitable case: no token configured (the common local, convenient
    # setup), host is loopback (the default) - require_admin's token/host
    # checks alone would let this through. A malicious site the admin's
    # browser visits could otherwise trigger this exact request against
    # 127.0.0.1 without ever needing to read the response.
    monkeypatch.chdir(tmp_path)  # see test_admin_page_and_summary for why chdir, not just delenv
    monkeypatch.delenv("AGENT_ADMIN_TOKEN", raising=False)
    monkeypatch.setenv("AGENT_STORAGE__DATABASE_URL", f"sqlite:///{tmp_path / 'admin.db'}")
    client = TestClient(app)

    response = client.get("/admin/api/summary", headers={"origin": "http://evil.example"})

    assert response.status_code == 403


def test_admin_allows_same_origin_request_without_token(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)  # see test_admin_page_and_summary for why chdir, not just delenv
    monkeypatch.delenv("AGENT_ADMIN_TOKEN", raising=False)
    monkeypatch.setenv("AGENT_STORAGE__DATABASE_URL", f"sqlite:///{tmp_path / 'admin.db'}")
    client = TestClient(app)

    # TestClient's default base_url makes same-origin requests carry
    # Origin: http://testserver against Host: testserver.
    response = client.get("/admin/api/summary", headers={"origin": "http://testserver"})

    assert response.status_code == 200


def test_admin_bootstrap_reports_onboarding_incomplete_without_config(monkeypatch, tmp_path) -> None:
    # docs/UI_REWRITE_PLAN.md §9 Phase 0.3: the SPA shell's very first call,
    # before it knows whether a token is even needed - deliberately NOT
    # behind require_admin (a token-required client can't learn it needs a
    # token from an endpoint that itself demands one).
    monkeypatch.chdir(tmp_path)  # empty tmp_path -> no config/config.yaml
    monkeypatch.delenv("AGENT_ADMIN_TOKEN", raising=False)
    monkeypatch.setenv("AGENT_STORAGE__DATABASE_URL", f"sqlite:///{tmp_path / 'admin.db'}")
    client = TestClient(app)

    response = client.get("/admin/api/bootstrap")

    assert response.status_code == 200
    body = response.json()
    assert body["token_required"] is False
    assert body["onboarding_complete"] is False
    assert isinstance(body["llm_reachable"], bool)
    assert isinstance(body["version"], str) and body["version"]


def test_admin_bootstrap_reports_onboarding_complete_and_token_required(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    # onboarding_complete now means "a real LLM is configured", not just
    # "config.yaml exists" (ybm setup always creates one) - so this config
    # needs a default profile with a present api key, not an empty file.
    (tmp_path / "config" / "config.yaml").write_text(
        "llm:\n"
        "  default_profile: cloud\n"
        "  profiles:\n"
        "    cloud:\n"
        "      provider: openai_compatible\n"
        "      model: gpt-4.1\n"
        "      base_url: https://api.openai.com/v1\n"
        "      api_key_env: TEST_OPENAI_KEY\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENT_ADMIN_TOKEN", "secret-token")
    monkeypatch.setenv("TEST_OPENAI_KEY", "sk-test")
    monkeypatch.setenv("AGENT_STORAGE__DATABASE_URL", f"sqlite:///{tmp_path / 'admin.db'}")
    client = TestClient(app)

    response = client.get("/admin/api/bootstrap")

    assert response.status_code == 200
    body = response.json()
    assert body["token_required"] is True
    assert body["onboarding_complete"] is True
    assert body["llm_reachable"] is True


def test_admin_bootstrap_reports_onboarding_incomplete_when_config_exists_but_no_llm_works(
    monkeypatch, tmp_path
) -> None:
    # The regression this guards: `ybm setup` always creates config.yaml
    # now (bootstrap.run_setup builds the admin console + generates
    # tokens automatically), so "config.yaml exists" alone can never be
    # the signal that first-run choices were actually made - every fresh
    # install would satisfy it immediately and the wizard would never show.
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "config.yaml").write_text("{}", encoding="utf-8")
    monkeypatch.delenv("AGENT_ADMIN_TOKEN", raising=False)
    monkeypatch.setenv("AGENT_STORAGE__DATABASE_URL", f"sqlite:///{tmp_path / 'admin.db'}")
    client = TestClient(app)

    response = client.get("/admin/api/bootstrap")

    assert response.status_code == 200
    body = response.json()
    assert body["onboarding_complete"] is False
    assert body["llm_reachable"] is False


def test_admin_bootstrap_rejects_cross_origin_request_even_without_token(monkeypatch, tmp_path) -> None:
    # Same CSRF-style protection as every other admin route - bootstrap not
    # requiring a *token* does not mean it skips the same-origin check.
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AGENT_ADMIN_TOKEN", raising=False)
    monkeypatch.setenv("AGENT_STORAGE__DATABASE_URL", f"sqlite:///{tmp_path / 'admin.db'}")
    client = TestClient(app)

    response = client.get("/admin/api/bootstrap", headers={"origin": "http://evil.example"})

    assert response.status_code == 403


def test_admin_setup_detect_reports_real_ollama_models_and_credential_presence(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AGENT_ADMIN_TOKEN", raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("YBM_LOCALDEPLOY_ROOT", raising=False)
    monkeypatch.setattr(
        admin_module, "_http_json",
        lambda url, timeout=2.0: {"models": [{"name": "qwen3:8b"}, {"name": "mistral:7b"}]},
    )
    repositories = _repositories(f"sqlite:///{tmp_path / 'admin.db'}")
    client = _admin_client(repositories)

    response = client.get("/admin/api/setup/detect")

    assert response.status_code == 200
    body = response.json()
    assert body["ollama"] == {
        "available": True,
        "reachable": True,
        "models": ["qwen3:8b", "mistral:7b"],
        # Preferred over mistral so the wizard can offer a default instead of
        # an undifferentiated list.
        "recommended": "qwen3:8b",
    }
    assert body["localdeploy_root_present"] is False
    assert body["openai_key_present"] is False
    assert body["telegram_token_present"] is True


def test_admin_setup_detect_reports_no_ollama_when_unreachable(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AGENT_ADMIN_TOKEN", raising=False)
    monkeypatch.setattr(admin_module, "_http_json", lambda url, timeout=2.0: None)
    repositories = _repositories(f"sqlite:///{tmp_path / 'admin.db'}")
    client = _admin_client(repositories)

    response = client.get("/admin/api/setup/detect")

    assert response.status_code == 200
    assert response.json()["ollama"] == {
        "available": False,
        "reachable": False,
        "models": [],
        "recommended": None,
    }


def test_admin_setup_detect_separates_an_empty_ollama_from_a_missing_one(monkeypatch, tmp_path) -> None:
    """A running server with nothing pulled used to look identical to no server
    at all, which is why onboarding could only say "no local model" and send
    the user off to find a model name."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AGENT_ADMIN_TOKEN", raising=False)
    monkeypatch.setattr(admin_module, "_http_json", lambda url, timeout=2.0: {"models": []})
    repositories = _repositories(f"sqlite:///{tmp_path / 'admin.db'}")
    client = _admin_client(repositories)

    body = client.get("/admin/api/setup/detect").json()

    assert body["ollama"]["reachable"] is True
    assert body["ollama"]["available"] is False
    assert body["ollama"]["recommended"] is None


def test_recommended_ollama_model_prefers_a_known_good_tag() -> None:
    recommend = admin_module._recommended_ollama_model

    # One installed model is the answer whatever it is - pulling it was itself
    # the choice.
    assert recommend(["something-obscure:latest"]) == "something-obscure:latest"
    assert recommend(["mistral:7b", "qwen3-vl:8b-instruct"]) == "qwen3-vl:8b-instruct"
    assert recommend([]) is None
    # Nothing recognised among several: do not guess on the user's behalf.
    assert recommend(["obscure-a:latest", "obscure-b:latest"]) is None


def test_admin_serves_built_index_html_when_a_build_exists(monkeypatch, tmp_path) -> None:
    # docs/UI_REWRITE_PLAN.md §9 Phase 0.2: once frontend/ is actually built,
    # /admin should serve the real SPA shell instead of the Streamlit pointer.
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AGENT_ADMIN_TOKEN", raising=False)
    monkeypatch.setenv("AGENT_STORAGE__DATABASE_URL", f"sqlite:///{tmp_path / 'admin.db'}")
    static_dir = tmp_path / "static_admin"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("<html><body>console shell</body></html>", encoding="utf-8")
    monkeypatch.setattr(admin_module, "_STATIC_ADMIN_DIR", static_dir)
    client = TestClient(app)

    page = client.get("/admin")

    assert page.status_code == 200
    assert "console shell" in page.text


def test_admin_serves_a_real_static_asset_by_path(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AGENT_ADMIN_TOKEN", raising=False)
    monkeypatch.setenv("AGENT_STORAGE__DATABASE_URL", f"sqlite:///{tmp_path / 'admin.db'}")
    static_dir = tmp_path / "static_admin"
    (static_dir / "assets").mkdir(parents=True)
    (static_dir / "index.html").write_text("<html>shell</html>", encoding="utf-8")
    (static_dir / "assets" / "index-abc123.js").write_text("console.log('hi')", encoding="utf-8")
    monkeypatch.setattr(admin_module, "_STATIC_ADMIN_DIR", static_dir)
    client = TestClient(app)

    asset = client.get("/admin/assets/index-abc123.js")

    assert asset.status_code == 200
    assert "console.log" in asset.text


def test_admin_falls_back_to_index_html_for_client_side_routes(monkeypatch, tmp_path) -> None:
    # React Router routes like /admin/tasks have no matching file on disk -
    # a hard refresh there must still serve the SPA shell, not 404.
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AGENT_ADMIN_TOKEN", raising=False)
    monkeypatch.setenv("AGENT_STORAGE__DATABASE_URL", f"sqlite:///{tmp_path / 'admin.db'}")
    static_dir = tmp_path / "static_admin"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("<html>shell</html>", encoding="utf-8")
    monkeypatch.setattr(admin_module, "_STATIC_ADMIN_DIR", static_dir)
    client = TestClient(app)

    page = client.get("/admin/tasks")

    assert page.status_code == 200
    assert "shell" in page.text


def test_admin_catch_all_404s_on_unmatched_api_path_instead_of_serving_html(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AGENT_ADMIN_TOKEN", raising=False)
    monkeypatch.setenv("AGENT_STORAGE__DATABASE_URL", f"sqlite:///{tmp_path / 'admin.db'}")
    static_dir = tmp_path / "static_admin"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("<html>shell</html>", encoding="utf-8")
    monkeypatch.setattr(admin_module, "_STATIC_ADMIN_DIR", static_dir)
    client = TestClient(app)

    response = client.get("/admin/api/this-route-does-not-exist")

    assert response.status_code == 404
    assert "shell" not in response.text


def test_admin_rejects_path_traversal_outside_static_dir(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AGENT_ADMIN_TOKEN", raising=False)
    monkeypatch.setenv("AGENT_STORAGE__DATABASE_URL", f"sqlite:///{tmp_path / 'admin.db'}")
    static_dir = tmp_path / "static_admin"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("<html>shell</html>", encoding="utf-8")
    secret = tmp_path / "secret.txt"
    secret.write_text("should never be served", encoding="utf-8")
    monkeypatch.setattr(admin_module, "_STATIC_ADMIN_DIR", static_dir)
    client = TestClient(app)

    # A literal "/admin/../secret.txt" gets normalized away by the HTTP
    # client itself before the request is even sent (confirmed empirically -
    # it never reaches the server, let alone this route) - not a real test
    # of the guard. URL-encoded dot segments survive that client-side
    # normalization and arrive at the route with a literal ".." in
    # sub_path, which is what actually exercises _serve_admin_app's
    # relative_to() check.
    response = client.get("/admin/%2e%2e/secret.txt")

    assert response.status_code == 404
    assert "should never be served" not in response.text
    # Distinguishes "the guard fired" from "no route matched at all" (which
    # would also 404, but via Starlette's own generic handler, not proof
    # the traversal attempt was actually blocked).
    assert response.json()["detail"] == "not found"


def test_admin_lists_tasks_and_audit(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)  # see test_admin_page_and_summary for why chdir, not just delenv
    monkeypatch.delenv("AGENT_ADMIN_TOKEN", raising=False)
    database_url = f"sqlite:///{tmp_path / 'admin.db'}"
    monkeypatch.setenv("AGENT_STORAGE__DATABASE_URL", database_url)
    repositories = _repositories(database_url)
    task = repositories.tasks.create("review admin dashboard")
    AuditLogger(repositories.audit).append(AuditEventType.TASK_CREATED, actor="test", task_id=task.id)
    client = TestClient(app)

    tasks = client.get("/admin/api/tasks").json()["tasks"]
    audit = client.get("/admin/api/audit").json()["events"]
    filtered = client.get("/admin/api/audit?category=spawned_task").json()["events"]

    assert tasks[0]["objective"] == "review admin dashboard"
    assert audit[0]["type"] == AuditEventType.TASK_CREATED.value
    assert audit[0]["category"] == "spawned_task"
    assert audit[0]["formatted_time"].endswith("UTC")
    assert filtered[0]["category"] == "spawned_task"


def test_admin_task_signal_updates_task(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)  # see test_admin_page_and_summary for why chdir, not just delenv
    database_url = f"sqlite:///{tmp_path / 'admin.db'}"
    repositories = _repositories(database_url)
    task = repositories.tasks.create("pause me")
    client = _admin_client(repositories)

    response = client.post(f"/admin/api/tasks/{task.id}/signals", json={"signal": "pause"})
    updated = repositories.tasks.get(task.id)

    assert response.status_code == 200
    assert updated is not None
    assert updated.status == TaskStatus.PAUSED


def _pending_approval(repositories, task_id: str) -> ApprovalRequest:
    return repositories.approvals.create(
        ApprovalRequest(
            task_id=task_id,
            capability=Capability.FILESYSTEM_WRITE,
            risk_level=RiskLevel.HIGH,
            summary="write a file",
            expires_at=utc_now() + timedelta(minutes=15),
        )
    )


def test_admin_pending_approvals_lists_only_pending(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    database_url = f"sqlite:///{tmp_path / 'admin.db'}"
    repositories = _repositories(database_url)
    task = repositories.tasks.create("write a report", metadata={"source_chat_id": "1"})
    pending = _pending_approval(repositories, task.id)
    decided = _pending_approval(repositories, task.id)
    repositories.approvals.set_status(decided.id, ApprovalStatus.APPROVED)
    client = _admin_client(repositories)

    response = client.get("/admin/api/approvals")

    assert response.status_code == 200
    items = response.json()["approvals"]
    assert [item["approval"]["id"] for item in items] == [pending.id]
    assert items[0]["task_objective"] == "write a report"
    assert items[0]["task_status"] == TaskStatus.RECEIVED.value


def test_admin_pending_approvals_includes_capability_ceiling_and_blast_radius(monkeypatch, tmp_path) -> None:
    # docs/UI_REWRITE_PLAN.md §11.2 - the Evidence Pack's "Authority" (risk
    # vs. the configured ceiling) and "Blast radius" (what this would
    # actually touch) fields, both derived from data the existing
    # ApprovalRequest doesn't expose on its own.
    monkeypatch.chdir(tmp_path)
    database_url = f"sqlite:///{tmp_path / 'admin.db'}"
    repositories = _repositories(database_url)
    task = repositories.tasks.create("write a report", metadata={"source_chat_id": "1"})
    repositories.approvals.create(
        ApprovalRequest(
            task_id=task.id,
            capability=Capability.FILESYSTEM_WRITE,
            risk_level=RiskLevel.HIGH,
            summary="write a file",
            action_payload={
                "tool_name": "filesystem.manage",
                "input": {"path": "/tmp/secret.txt", "command": "rm -rf /tmp"},
            },
            expires_at=utc_now() + timedelta(minutes=15),
        )
    )
    client = _admin_client(repositories)

    response = client.get("/admin/api/approvals")

    assert response.status_code == 200
    item = response.json()["approvals"][0]
    # FILESYSTEM_WRITE gets no override in default_capability_policies(), so
    # it carries the bare CapabilityPolicy() default: max_risk_level=LOW.
    # This approval's own risk_level (HIGH) exceeding that ceiling is
    # exactly the "why a human should look closely" signal Authority exists
    # to surface.
    assert item["capability_max_risk_level"] == "low"
    assert item["blast_radius"]["files"] == ["/tmp/secret.txt"]
    assert item["blast_radius"]["commands"] == ["rm -rf /tmp"]
    assert item["blast_radius"]["urls"] == []


def test_admin_pending_approvals_blast_radius_is_empty_for_unrelated_input(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    database_url = f"sqlite:///{tmp_path / 'admin.db'}"
    repositories = _repositories(database_url)
    task = repositories.tasks.create("do something")
    repositories.approvals.create(
        ApprovalRequest(
            task_id=task.id,
            capability=Capability.FILESYSTEM_WRITE,
            risk_level=RiskLevel.HIGH,
            summary="write a file",
            action_payload={"tool_name": "some.tool", "input": {"objective": "no file/url/command keys here"}},
            expires_at=utc_now() + timedelta(minutes=15),
        )
    )
    client = _admin_client(repositories)

    response = client.get("/admin/api/approvals")

    item = response.json()["approvals"][0]
    assert item["blast_radius"] == {"files": [], "urls": [], "commands": []}


def test_admin_pending_approvals_excludes_expired_and_orders_by_soonest_expiry(monkeypatch, tmp_path) -> None:
    """docs/UI_UX_AUDIT.md Phase 12: an approval whose expiry already
    passed must not appear in the list at all (it used to stay PENDING
    forever and, being the oldest by created_at, permanently led the
    list) - and among what remains, the most urgent (soonest to expire)
    should lead, not the oldest."""
    monkeypatch.chdir(tmp_path)
    database_url = f"sqlite:///{tmp_path / 'admin.db'}"
    repositories = _repositories(database_url)
    task = repositories.tasks.create("write a report", metadata={"source_chat_id": "1"})

    def _approval(minutes: int) -> ApprovalRequest:
        return repositories.approvals.create(
            ApprovalRequest(
                task_id=task.id, capability=Capability.FILESYSTEM_WRITE, risk_level=RiskLevel.HIGH,
                summary="write a file", expires_at=utc_now() + timedelta(minutes=minutes),
            )
        )

    already_expired = _approval(-5)
    expires_soon = _approval(5)
    expires_later = _approval(30)
    client = _admin_client(repositories)

    response = client.get("/admin/api/approvals")

    ids = [item["approval"]["id"] for item in response.json()["approvals"]]
    assert already_expired.id not in ids
    assert ids == [expires_soon.id, expires_later.id]
    assert repositories.approvals.get(already_expired.id).status == ApprovalStatus.EXPIRED


def test_admin_decide_approval_approve_for_task_creates_a_grant(monkeypatch, tmp_path) -> None:
    """docs/UI_UX_AUDIT.md Phase 1's "Allow for this task" - approves the
    current call exactly like "approve" and additionally creates an
    ApprovalGrant (task_id, tool_name, capability) so the executor won't
    ask again for the same tool+capability within this task."""
    monkeypatch.chdir(tmp_path)
    database_url = f"sqlite:///{tmp_path / 'admin.db'}"
    repositories = _repositories(database_url)
    task = repositories.tasks.create("write a report")
    approval = repositories.approvals.create(
        ApprovalRequest(
            task_id=task.id,
            capability=Capability.FILESYSTEM_WRITE,
            risk_level=RiskLevel.HIGH,
            summary="write a file",
            action_payload={"tool_name": "filesystem.manage", "input": {"path": "/tmp/report.txt"}},
            expires_at=utc_now() + timedelta(minutes=15),
        )
    )
    client = _admin_client(repositories)

    response = client.post(f"/admin/api/approvals/{approval.id}/decide", json={"decision": "approve_for_task"})

    assert response.status_code == 200
    body = response.json()
    assert body["approval"]["status"] == ApprovalStatus.APPROVED.value
    assert body["grant"]["task_id"] == task.id
    assert body["grant"]["tool_name"] == "filesystem.manage"
    assert body["grant"]["capability"] == Capability.FILESYSTEM_WRITE.value
    grants = repositories.approval_grants.list_for_task(task.id)
    assert len(grants) == 1
    assert repositories.approval_grants.find_matching(task.id, "filesystem.manage", Capability.FILESYSTEM_WRITE) is not None


def test_admin_decide_approval_approve_updates_status_and_audits(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    database_url = f"sqlite:///{tmp_path / 'admin.db'}"
    repositories = _repositories(database_url)
    task = repositories.tasks.create("write a report")
    approval = _pending_approval(repositories, task.id)
    client = _admin_client(repositories)

    response = client.post(f"/admin/api/approvals/{approval.id}/decide", json={"decision": "approve"})

    assert response.status_code == 200
    assert response.json()["approval"]["status"] == ApprovalStatus.APPROVED.value
    assert repositories.approvals.get(approval.id).status == ApprovalStatus.APPROVED
    events = repositories.audit.list_for_task(task.id)
    assert any(event.type == AuditEventType.APPROVAL_DECIDED for event in events)


def test_admin_decide_approval_reject_updates_status(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    database_url = f"sqlite:///{tmp_path / 'admin.db'}"
    repositories = _repositories(database_url)
    task = repositories.tasks.create("write a report")
    approval = _pending_approval(repositories, task.id)
    client = _admin_client(repositories)

    response = client.post(f"/admin/api/approvals/{approval.id}/decide", json={"decision": "reject"})

    assert response.status_code == 200
    assert response.json()["approval"]["status"] == ApprovalStatus.REJECTED.value
    assert repositories.approvals.get(approval.id).status == ApprovalStatus.REJECTED


def test_admin_decide_approval_requeues_a_task_stuck_awaiting_approval(monkeypatch, tmp_path) -> None:
    """docs/UI_UX_AUDIT.md Phase 8, second review: deciding an approval must
    make the task claimable again (AWAITING_APPROVAL isn't in
    WORKABLE_STATUSES), not just flip the ApprovalRequest's own status -
    otherwise a decided approval is never revisited and the task hangs
    forever even though the human already answered."""
    monkeypatch.chdir(tmp_path)
    database_url = f"sqlite:///{tmp_path / 'admin.db'}"
    repositories = _repositories(database_url)
    task = repositories.tasks.create("write a report")
    repositories.tasks.update_metadata(task.id, task.metadata, TaskStatus.AWAITING_APPROVAL)
    approval = _pending_approval(repositories, task.id)
    client = _admin_client(repositories)

    response = client.post(f"/admin/api/approvals/{approval.id}/decide", json={"decision": "approve"})

    assert response.status_code == 200
    assert repositories.tasks.get(task.id).status == TaskStatus.RUNNING


def test_admin_decide_approval_404_for_unknown_id(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    database_url = f"sqlite:///{tmp_path / 'admin.db'}"
    repositories = _repositories(database_url)
    client = _admin_client(repositories)

    response = client.post("/admin/api/approvals/approval_does_not_exist/decide", json={"decision": "approve"})

    assert response.status_code == 404


def test_admin_decide_approval_409_when_already_decided(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    database_url = f"sqlite:///{tmp_path / 'admin.db'}"
    repositories = _repositories(database_url)
    task = repositories.tasks.create("write a report")
    approval = _pending_approval(repositories, task.id)
    repositories.approvals.set_status(approval.id, ApprovalStatus.APPROVED)
    client = _admin_client(repositories)

    response = client.post(f"/admin/api/approvals/{approval.id}/decide", json={"decision": "reject"})

    assert response.status_code == 409


def test_admin_task_trace_includes_operator_history_tool_calls_and_audit(monkeypatch, tmp_path) -> None:
    """The trace endpoint's execution record is operator_history + real
    tool_invocations now - PlanModel is dead (docs/HISTORY.md \u00a71.1), nothing
    creates one anymore."""
    monkeypatch.chdir(tmp_path)  # see test_admin_page_and_summary for why chdir, not just delenv
    database_url = f"sqlite:///{tmp_path / 'admin.db'}"
    repositories = _repositories(database_url)
    audit = AuditLogger(repositories.audit)
    task = repositories.tasks.create("create a test app")
    request = ToolCallRequest(
        task_id=task.id,
        tool_name="vscode.copilot_terminal",
        capability=Capability.VSCODE_WRITE_FILES,
        input={"prompt": "build the app", "cwd": "workspace"},
    )
    repositories.tasks.update_metadata(
        task.id,
        {
            "operator_history": [
                {
                    "tool_name": "vscode.copilot_terminal",
                    "input": {"prompt": "build the app", "cwd": "workspace"},
                    "status": "succeeded",
                    "output_summary": "created files",
                    "error": None,
                    "request_id": request.id,
                }
            ]
        },
    )
    repositories.tool_invocations.create(request)
    repositories.tool_invocations.complete(
        ToolCallResult(
            request_id=request.id,
            status=ToolResultStatus.SUCCEEDED,
            output={"terminal_output": [{"content": "created files"}]},
        )
    )
    audit.append(
        AuditEventType.TASK_STATE_CHANGED,
        actor="operator",
        task_id=task.id,
        payload={"action": "operator_decision", "decision": {"action": "call_tool", "tool_name": "vscode.copilot_terminal"}},
    )
    client = _admin_client(repositories)

    response = client.get(f"/admin/api/tasks/{task.id}/trace")
    body = response.json()

    assert response.status_code == 200
    assert body["task"]["id"] == task.id
    assert "plan" not in body
    assert body["operator_history"][0]["tool_name"] == "vscode.copilot_terminal"
    assert body["operator_history"][0]["output_summary"] == "created files"
    assert body["operator_history"][0]["duration_ms"] is not None
    assert body["operator_history"][0]["duration_ms"] >= 0
    assert body["tool_invocations"][0]["request"]["input"]["prompt"] == "build the app"
    assert body["tool_invocations"][0]["result"]["output"]["terminal_output"][0]["content"] == "created files"
    assert body["audit"][0]["details"]["action"] == "operator_decision"


def test_admin_task_trace_operator_history_entry_without_request_id_has_no_duration(monkeypatch, tmp_path) -> None:
    """docs/UI_UX_AUDIT.md Phase 14: pseudo-check entries (audit/fulfillment
    gap) and other non-tool-call steps carry no request_id, so they must not
    be given a fabricated duration by matching against an unrelated
    tool_invocations row."""
    monkeypatch.chdir(tmp_path)
    database_url = f"sqlite:///{tmp_path / 'admin.db'}"
    repositories = _repositories(database_url)
    task = repositories.tasks.create("do something")
    repositories.tasks.update_metadata(
        task.id,
        {
            "operator_history": [
                {"tool_name": "_fulfillment_check", "input": None, "status": "fulfillment_gap", "error": "..."},
            ]
        },
    )
    client = _admin_client(repositories)

    response = client.get(f"/admin/api/tasks/{task.id}/trace")

    assert response.json()["operator_history"][0]["duration_ms"] is None


def test_admin_task_trace_timeline_includes_category_and_duration(monkeypatch, tmp_path) -> None:
    """docs/UI_UX_AUDIT.md Phase 14: the timeline used to render every row
    as either "tool" or "audit" with no further distinction. category
    reuses format_audit_event's own CATEGORY_BY_TYPE (already computed,
    just not included here before); duration_ms is exact for a completed
    tool call and None for an audit event, which is an instantaneous log
    point, not a span."""
    monkeypatch.chdir(tmp_path)
    database_url = f"sqlite:///{tmp_path / 'admin.db'}"
    repositories = _repositories(database_url)
    audit = AuditLogger(repositories.audit)
    task = repositories.tasks.create("do something")

    request = ToolCallRequest(
        task_id=task.id, tool_name="filesystem.manage", capability=Capability.FILESYSTEM_READ,
        input={"operation": "read_file", "path": "C:/tmp/notes.txt"},
    )
    repositories.tool_invocations.create(request)
    repositories.tool_invocations.complete(
        ToolCallResult(request_id=request.id, status=ToolResultStatus.SUCCEEDED, output={})
    )
    audit.append(AuditEventType.APPROVAL_REQUESTED, actor="worker", task_id=task.id, payload={})

    client = _admin_client(repositories)

    response = client.get(f"/admin/api/tasks/{task.id}/trace")
    timeline = response.json()["timeline"]

    tool_item = next(item for item in timeline if item["kind"] == "tool")
    audit_item = next(item for item in timeline if item["kind"] == "audit")
    assert tool_item["category"] == "tool"
    assert tool_item["duration_ms"] is not None
    assert tool_item["duration_ms"] >= 0
    assert audit_item["category"] == "approval"
    assert audit_item["duration_ms"] is None


def test_admin_task_trace_evidence_aggregates_files_urls_and_commands(monkeypatch, tmp_path) -> None:
    """The evidence view (docs/HISTORY.md N5): what a completed task actually
    touched, pulled from real tool_invocations rather than a new repository -
    files/urls/commands are found by known field name across every tool's
    input/output, deduplicated, so a task that wrote two files and visited
    one URL surfaces exactly that, not raw per-tool payload dumps."""
    monkeypatch.chdir(tmp_path)
    database_url = f"sqlite:///{tmp_path / 'admin.db'}"
    repositories = _repositories(database_url)
    task = repositories.tasks.create("write a report and check a page")

    write_request = ToolCallRequest(
        task_id=task.id,
        tool_name="filesystem.manage",
        capability=Capability.FILESYSTEM_WRITE,
        input={"operation": "write_text_file", "path": "C:/tmp/report.txt"},
    )
    repositories.tool_invocations.create(write_request)
    repositories.tool_invocations.complete(
        ToolCallResult(
            request_id=write_request.id,
            status=ToolResultStatus.SUCCEEDED,
            output={"changed_paths": ["C:/tmp/report.txt", "C:/tmp/report-backup.txt"]},
        )
    )

    browser_request = ToolCallRequest(
        task_id=task.id,
        tool_name="browser.open",
        capability=Capability.BROWSER_OPEN,
        input={"url": "https://example.com"},
    )
    repositories.tool_invocations.create(browser_request)
    repositories.tool_invocations.complete(
        ToolCallResult(
            request_id=browser_request.id,
            status=ToolResultStatus.SUCCEEDED,
            output={"url": "https://example.com", "visited_urls": ["https://example.com"]},
        )
    )

    client = _admin_client(repositories)

    response = client.get(f"/admin/api/tasks/{task.id}/trace")
    evidence = response.json()["evidence"]

    assert response.status_code == 200
    assert [item["value"] for item in evidence["files"]] == ["C:/tmp/report.txt", "C:/tmp/report-backup.txt"]
    assert [item["value"] for item in evidence["urls"]] == ["https://example.com"]
    assert evidence["commands"] == []
    assert evidence["files"][0]["tool_name"] == "filesystem.manage"


def test_admin_task_trace_evidence_includes_a_real_effect_label_per_item(monkeypatch, tmp_path) -> None:
    """docs/UI_UX_AUDIT.md Phase 14: evidence items say what actually
    happened (read/modified/moved/...), not just that something was
    touched - Phase 8's "Touched during this task" wording fix was
    explicitly a stopgap for this."""
    monkeypatch.chdir(tmp_path)
    database_url = f"sqlite:///{tmp_path / 'admin.db'}"
    repositories = _repositories(database_url)
    task = repositories.tasks.create("read one file and write another")

    read_request = ToolCallRequest(
        task_id=task.id, tool_name="filesystem.manage", capability=Capability.FILESYSTEM_READ,
        input={"operation": "read_file", "path": "C:/tmp/notes.txt"},
    )
    repositories.tool_invocations.create(read_request)
    repositories.tool_invocations.complete(
        ToolCallResult(request_id=read_request.id, status=ToolResultStatus.SUCCEEDED, output={})
    )

    write_request = ToolCallRequest(
        task_id=task.id, tool_name="filesystem.manage", capability=Capability.FILESYSTEM_WRITE,
        input={"operation": "write_text_file", "path": "C:/tmp/report.txt"},
    )
    repositories.tool_invocations.create(write_request)
    repositories.tool_invocations.complete(
        ToolCallResult(request_id=write_request.id, status=ToolResultStatus.SUCCEEDED, output={})
    )

    client = _admin_client(repositories)

    response = client.get(f"/admin/api/tasks/{task.id}/trace")
    files = {item["value"]: item["effect"] for item in response.json()["evidence"]["files"]}

    assert files["C:/tmp/notes.txt"] == "read"
    assert files["C:/tmp/report.txt"] == "modified"


def test_admin_task_trace_includes_llm_calls(monkeypatch, tmp_path) -> None:
    """docs/UI_UX_AUDIT.md Phase 14d: the trace endpoint surfaces the
    persisted LLM-call receipts (worker.py's _record_llm_call), not just
    tool_invocations - this is what the Duration view uses for its real,
    measured (non-inferred) segments."""
    monkeypatch.chdir(tmp_path)
    database_url = f"sqlite:///{tmp_path / 'admin.db'}"
    repositories = _repositories(database_url)
    task = repositories.tasks.create("do something")
    repositories.llm_calls.create(
        LLMCallRecord(
            task_id=task.id,
            source="operator",
            model="test-model",
            step_index=0,
            messages=[{"role": "user", "content": "hello"}],
            response_text="hi",
            prompt_tokens=10,
            completion_tokens=5,
            total_tokens=15,
            latency_ms=42.0,
        )
    )
    client = _admin_client(repositories)

    response = client.get(f"/admin/api/tasks/{task.id}/trace")
    calls = response.json()["llm_calls"]

    assert len(calls) == 1
    assert calls[0]["source"] == "operator"
    assert calls[0]["model"] == "test-model"
    assert calls[0]["latency_ms"] == 42.0


def test_admin_skills_catalog_lists_the_real_bundled_starters(monkeypatch, tmp_path) -> None:
    """docs/UI_UX_AUDIT.md Phase 11: skills/starter/ is committed (unlike
    adapters.skills.root_dir, which is generated), so there's something to
    browse and install on a fresh checkout with zero skills installed yet.
    Reads the real directory - this is the actual bundled catalog, not a
    fixture standing in for it.
    """
    client, _repositories = _chat_client(monkeypatch, tmp_path)

    response = client.get("/admin/api/skills/catalog")
    body = response.json()

    assert response.status_code == 200
    names = {s["name"] for s in body["skills"]}
    assert "File Organization" in names
    assert "Document Summary" in names
    assert len(body["skills"]) >= 6
    # Every starter declares its own tools explicitly - the catalog
    # shouldn't be relying on the substring-scan fallback for its own
    # bundled content.
    assert all(s["tools_declared"] for s in body["skills"])


def test_admin_skill_install_list_and_uninstall(monkeypatch, tmp_path) -> None:
    """docs/UI_UX_AUDIT.md Phase 5: install a skill (writing its manifest
    file), see it in the catalog with inferred permission labels, remove it
    - entirely through the console, no manual filesystem access."""
    client, repositories = _chat_client(monkeypatch, tmp_path)

    installed = client.post(
        "/admin/api/skills",
        json={
            "name": "Invoice Extraction",
            "description": "Pulls totals from invoice PDFs.",
            "body": "Use filesystem.manage to read the PDF, then report the total.",
            "version": "1",
        },
    )
    assert installed.status_code == 200
    skill = installed.json()["skill"]
    assert skill["name"] == "Invoice Extraction"
    assert skill["tools_declared"] is False
    assert "filesystem.manage" in skill["tools"]

    listed = client.get("/admin/api/skills").json()["skills"]
    assert [s["name"] for s in listed] == ["Invoice Extraction"]

    uninstalled = client.delete(f"/admin/api/skills/{quote('Invoice Extraction')}")
    assert uninstalled.status_code == 200
    assert uninstalled.json() == {"name": "Invoice Extraction", "deleted": True}
    assert client.get("/admin/api/skills").json()["skills"] == []

    events = repositories.audit.list_recent(limit=20)
    actions = {e.payload.get("action") for e in events if e.payload.get("section") == "skills"}
    assert actions == {"install", "uninstall"}


def test_admin_skill_install_overwrites_an_existing_skill_with_the_same_name(monkeypatch, tmp_path) -> None:
    client, _repositories = _chat_client(monkeypatch, tmp_path)
    client.post("/admin/api/skills", json={"name": "My Skill", "description": "v1", "body": "v1 body"})

    updated = client.post("/admin/api/skills", json={"name": "My Skill", "description": "v2", "body": "v2 body"})

    assert updated.status_code == 200
    listed = client.get("/admin/api/skills").json()["skills"]
    assert len(listed) == 1
    assert listed[0]["description"] == "v2"


def test_admin_skill_uninstall_404s_for_an_unknown_skill(monkeypatch, tmp_path) -> None:
    client, _repositories = _chat_client(monkeypatch, tmp_path)

    assert client.delete("/admin/api/skills/does-not-exist").status_code == 404


def test_admin_memory_create_list_update_and_forget(monkeypatch, tmp_path) -> None:
    """docs/UI_UX_AUDIT.md Phase 4: remember/edit/forget over a real,
    inspectable table - not a black box deciding on its own what to keep."""
    client, repositories = _chat_client(monkeypatch, tmp_path)

    created = client.post("/admin/api/memory", json={"category": "preference", "content": "Prefers concise answers"})
    assert created.status_code == 200
    fact = created.json()["fact"]
    assert fact["source"] == "operator_admin"

    listed = client.get("/admin/api/memory").json()["facts"]
    assert len(listed) == 1
    assert listed[0]["id"] == fact["id"]

    updated = client.patch(f"/admin/api/memory/{fact['id']}", json={"category": "preference", "content": "Prefers very concise answers"})
    assert updated.status_code == 200
    assert updated.json()["fact"]["content"] == "Prefers very concise answers"

    deleted = client.delete(f"/admin/api/memory/{fact['id']}")
    assert deleted.status_code == 200
    assert deleted.json() == {"fact_id": fact["id"], "deleted": True}
    assert client.get("/admin/api/memory").json()["facts"] == []

    events = repositories.audit.list_recent(limit=20)
    actions = {e.payload.get("action") for e in events if e.payload.get("section") == "memory"}
    assert actions == {"create", "edit", "forget"}


def test_admin_memory_update_and_delete_404_for_an_unknown_fact(monkeypatch, tmp_path) -> None:
    client, _repositories = _chat_client(monkeypatch, tmp_path)

    assert client.patch("/admin/api/memory/mem_missing", json={"category": "x", "content": "y"}).status_code == 404
    assert client.delete("/admin/api/memory/mem_missing").status_code == 404


def test_admin_memory_search_filters_by_category_and_query(monkeypatch, tmp_path) -> None:
    client, repositories = _chat_client(monkeypatch, tmp_path)
    repositories.memory_facts.create(MemoryFact(category="preference", content="Likes dark mode"))
    repositories.memory_facts.create(MemoryFact(category="project", content="Works in Python"))

    by_category = client.get("/admin/api/memory?category=project").json()["facts"]
    assert [f["content"] for f in by_category] == ["Works in Python"]

    by_query = client.get("/admin/api/memory?q=dark").json()["facts"]
    assert [f["content"] for f in by_query] == ["Likes dark mode"]


def test_admin_task_receipt_404s_for_an_unknown_task(monkeypatch, tmp_path) -> None:
    client, _repositories = _chat_client(monkeypatch, tmp_path)

    response = client.get("/admin/api/tasks/task_does_not_exist/receipt")

    assert response.status_code == 404


def test_admin_task_receipt_summarizes_a_completed_task(monkeypatch, tmp_path) -> None:
    """docs/UI_UX_AUDIT.md Phase 2: what was asked, what changed, what was
    contacted outside this machine, what was approved, how long it took."""
    monkeypatch.chdir(tmp_path)
    database_url = f"sqlite:///{tmp_path / 'admin.db'}"
    repositories = _repositories(database_url)
    audit = AuditLogger(repositories.audit)
    task = repositories.tasks.create("organize downloads and check a status page")
    repositories.tasks.update_metadata(
        task.id,
        {"synthesized_answer": "Moved 3 files and checked the page.", "token_usage": {"last_model": "gpt-4.1", "total_tokens": 500}},
        TaskStatus.COMPLETED,
    )

    write_request = ToolCallRequest(
        task_id=task.id, tool_name="filesystem.manage", capability=Capability.FILESYSTEM_WRITE,
        input={"operation": "move", "path": "C:/dl/a.pdf"},
    )
    repositories.tool_invocations.create(write_request)
    repositories.tool_invocations.complete(
        ToolCallResult(request_id=write_request.id, status=ToolResultStatus.SUCCEEDED, output={"changed_paths": ["C:/dl/a.pdf"]})
    )
    http_request = ToolCallRequest(
        task_id=task.id, tool_name="http.request", capability=Capability.NETWORK_HTTP,
        input={"url": "https://example.com/status"},
    )
    repositories.tool_invocations.create(http_request)
    repositories.tool_invocations.complete(
        ToolCallResult(request_id=http_request.id, status=ToolResultStatus.SUCCEEDED, output={"url": "https://example.com/status"})
    )
    audit.append(
        AuditEventType.EGRESS_CONTACTED, actor="http.request", task_id=task.id,
        payload={"host": "example.com", "tool_name": "http.request"},
    )
    approval = repositories.approvals.create(
        ApprovalRequest(
            task_id=task.id, capability=Capability.NETWORK_HTTP, risk_level=RiskLevel.MEDIUM,
            summary="Approve http.request", expires_at=utc_now() + timedelta(minutes=15),
        )
    )
    repositories.approvals.decide_pending(approval.id, ApprovalStatus.APPROVED)
    client = _admin_client(repositories)

    response = client.get(f"/admin/api/tasks/{task.id}/receipt")
    body = response.json()

    assert response.status_code == 200
    assert body["task_id"] == task.id
    assert body["status"] == "completed"
    assert body["result_summary"] == "Moved 3 files and checked the page."
    assert body["changes"]["files"][0]["value"] == "C:/dl/a.pdf"
    assert body["services_contacted"] == [{"host": "example.com", "tool_name": "http.request", "at": body["services_contacted"][0]["at"]}]
    assert body["data_left_machine"] is True
    assert body["approvals"][0]["status"] == "approved"
    assert body["token_usage"]["last_model"] == "gpt-4.1"
    assert body["duration_seconds"] >= 0
    assert {entry["tool_name"] for entry in body["tools_used"]} == {"filesystem.manage", "http.request"}


def test_admin_task_receipt_reports_no_egress_for_a_fully_local_task(monkeypatch, tmp_path) -> None:
    client, repositories = _chat_client(monkeypatch, tmp_path)
    task = repositories.tasks.create("summarize a local file")
    repositories.tasks.update_metadata(task.id, {"synthesized_answer": "Done."}, TaskStatus.COMPLETED)

    response = client.get(f"/admin/api/tasks/{task.id}/receipt")
    body = response.json()

    assert body["services_contacted"] == []
    assert body["data_left_machine"] is False
    assert body["uncertainties"] == []


def test_admin_task_receipt_aggregates_verification_across_calls(monkeypatch, tmp_path) -> None:
    """docs/ROADMAP.md "Proof": a receipt should say how many things were
    actually re-checked on disk and how many came up missing, aggregated
    across every call's own ToolVerification (schemas.py) - not just
    whether the adapter reported "succeeded".
    """
    client, repositories = _chat_client(monkeypatch, tmp_path)
    task = repositories.tasks.create("sort receipts by vendor")
    repositories.tasks.update_metadata(task.id, {"synthesized_answer": "Sorted."}, TaskStatus.COMPLETED)

    verified_request = ToolCallRequest(
        task_id=task.id, tool_name="filesystem.manage", capability=Capability.FILESYSTEM_WRITE,
        input={"operation": "apply_manifest"},
    )
    repositories.tool_invocations.create(verified_request)
    repositories.tool_invocations.complete(
        ToolCallResult(
            request_id=verified_request.id,
            status=ToolResultStatus.SUCCEEDED,
            output={"changed_paths": ["a", "b"]},
            verification=ToolVerification(checked=2, verified=2, missing=[]),
        )
    )
    partial_request = ToolCallRequest(
        task_id=task.id, tool_name="filesystem.manage", capability=Capability.FILESYSTEM_WRITE,
        input={"operation": "apply_manifest"},
    )
    repositories.tool_invocations.create(partial_request)
    repositories.tool_invocations.complete(
        ToolCallResult(
            request_id=partial_request.id,
            status=ToolResultStatus.SUCCEEDED,
            output={"changed_paths": ["c"]},
            verification=ToolVerification(checked=1, verified=0, missing=["destination not found: c"]),
        )
    )
    unverified_request = ToolCallRequest(
        task_id=task.id, tool_name="web.search", capability=Capability.LLM_GENERATE, input={"query": "vendor names"},
    )
    repositories.tool_invocations.create(unverified_request)
    repositories.tool_invocations.complete(
        ToolCallResult(request_id=unverified_request.id, status=ToolResultStatus.SUCCEEDED, output={})
    )
    failing_request = ToolCallRequest(
        task_id=task.id, tool_name="filesystem.manage", capability=Capability.FILESYSTEM_WRITE, input={"operation": "apply_manifest"},
    )
    repositories.tool_invocations.create(failing_request)
    repositories.tool_invocations.complete(
        ToolCallResult(request_id=failing_request.id, status=ToolResultStatus.FAILED, error_message="disk full")
    )

    response = client.get(f"/admin/api/tasks/{task.id}/receipt")
    body = response.json()

    assert body["verification"] == {"checked": 3, "verified": 2, "missing": ["destination not found: c"]}
    # 4 calls total; none were needs_approval/denied/cancelled, so all 4
    # were attempted - 3 succeeded (including the unverified web.search
    # call, whose tool has no verify() hook), 1 failed.
    assert body["execution"] == {"calls_attempted": 4, "calls_succeeded": 3, "calls_failed": 1}
    assert any("1 verification issue" in item and "destination not found: c" in item for item in body["uncertainties"])


def _settings_with_artifact_root(root: Path) -> AppSettings:
    return AppSettings(_env_file=None, storage={"artifact_dir": str(root)})


def test_admin_artifact_download_serves_a_registered_file(tmp_path) -> None:
    """docs/UI_UX_AUDIT.md Phase 8: a generated file previously showed only
    its path in Chat - not actionable from a browser."""
    database_url = f"sqlite:///{tmp_path / 'admin.db'}"
    repositories = _repositories(database_url)
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    file_path = artifact_root / "report.csv"
    file_path.write_bytes(b"a,b,c\n1,2,3\n")
    artifact = repositories.artifacts.create(
        Artifact(type=ArtifactType.GENERATED_FILE, uri=str(file_path)),
    )
    client = _admin_client(repositories, settings=_settings_with_artifact_root(artifact_root))

    response = client.get(f"/admin/api/artifacts/{artifact.id}/download")

    assert response.status_code == 200
    assert response.content == b"a,b,c\n1,2,3\n"
    assert "attachment" in response.headers["content-disposition"]
    assert "report.csv" in response.headers["content-disposition"]


def test_admin_artifact_download_inline_uses_inline_disposition(tmp_path) -> None:
    database_url = f"sqlite:///{tmp_path / 'admin.db'}"
    repositories = _repositories(database_url)
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    file_path = artifact_root / "preview.pdf"
    file_path.write_bytes(b"%PDF-1.4 fake")
    artifact = repositories.artifacts.create(Artifact(type=ArtifactType.DOCUMENT, uri=str(file_path)))
    client = _admin_client(repositories, settings=_settings_with_artifact_root(artifact_root))

    response = client.get(f"/admin/api/artifacts/{artifact.id}/download?inline=true")

    assert response.status_code == 200
    assert "inline" in response.headers["content-disposition"]
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in response.headers["content-security-policy"]


def test_admin_artifact_download_uses_scoped_grant_not_admin_token_query(monkeypatch, tmp_path) -> None:
    database_url = f"sqlite:///{tmp_path / 'admin.db'}"
    repositories = _repositories(database_url)
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    file_path = artifact_root / "report.txt"
    file_path.write_text("safe report", encoding="utf-8")
    artifact = repositories.artifacts.create(Artifact(type=ArtifactType.DOCUMENT, uri=str(file_path)))
    monkeypatch.setenv("AGENT_ADMIN_TOKEN", "secret-token")
    client = _admin_client(repositories, settings=_settings_with_artifact_root(artifact_root))

    leaked_token = client.get(f"/admin/api/artifacts/{artifact.id}/download?token=secret-token")
    grant_response = client.post(
        f"/admin/api/artifacts/{artifact.id}/download-grant",
        headers={"X-Agent-Control-Admin-Token": "secret-token"},
    )
    granted_download = client.get(grant_response.json()["url"])

    assert leaked_token.status_code == 401
    assert grant_response.status_code == 200
    assert "secret-token" not in grant_response.json()["url"]
    assert granted_download.status_code == 200
    assert granted_download.content == b"safe report"


def test_admin_artifact_download_forces_active_content_to_attachment(tmp_path) -> None:
    database_url = f"sqlite:///{tmp_path / 'admin.db'}"
    repositories = _repositories(database_url)
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    file_path = artifact_root / "generated.html"
    file_path.write_text("<script>document.body.textContent = localStorage.length</script>", encoding="utf-8")
    artifact = repositories.artifacts.create(Artifact(type=ArtifactType.GENERATED_FILE, uri=str(file_path)))
    client = _admin_client(repositories, settings=_settings_with_artifact_root(artifact_root))

    response = client.get(f"/admin/api/artifacts/{artifact.id}/download?inline=true")

    assert response.status_code == 200
    assert "attachment" in response.headers["content-disposition"]
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-security-policy"] == "sandbox; default-src 'none'"


def test_admin_artifact_download_404s_for_an_unknown_artifact(tmp_path) -> None:
    database_url = f"sqlite:///{tmp_path / 'admin.db'}"
    repositories = _repositories(database_url)
    client = _admin_client(repositories)

    assert client.get("/admin/api/artifacts/artifact_missing/download").status_code == 404


def test_admin_artifact_download_404s_for_an_artifact_with_no_file(tmp_path) -> None:
    database_url = f"sqlite:///{tmp_path / 'admin.db'}"
    repositories = _repositories(database_url)
    artifact = repositories.artifacts.create(Artifact(type=ArtifactType.EXTERNAL_LINK, uri=None))
    client = _admin_client(repositories)

    assert client.get(f"/admin/api/artifacts/{artifact.id}/download").status_code == 404


def test_admin_artifact_download_403s_for_a_path_outside_allowed_roots(tmp_path) -> None:
    """The same allowed-roots check artifact.deliver itself enforces - an
    artifact row pointing somewhere it shouldn't must not become a way to
    read arbitrary files off the host through the admin API."""
    database_url = f"sqlite:///{tmp_path / 'admin.db'}"
    repositories = _repositories(database_url)
    outside_dir = tmp_path / "not_an_artifact_root"
    outside_dir.mkdir()
    secret_file = outside_dir / "secret.txt"
    secret_file.write_text("should never be servable", encoding="utf-8")
    artifact = repositories.artifacts.create(Artifact(type=ArtifactType.GENERATED_FILE, uri=str(secret_file)))
    client = _admin_client(repositories, settings=_settings_with_artifact_root(tmp_path / "artifacts"))

    assert client.get(f"/admin/api/artifacts/{artifact.id}/download").status_code == 403


def test_admin_artifact_download_404s_when_the_file_was_deleted_after_registration(tmp_path) -> None:
    database_url = f"sqlite:///{tmp_path / 'admin.db'}"
    repositories = _repositories(database_url)
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    file_path = artifact_root / "gone.csv"
    file_path.write_text("x", encoding="utf-8")
    artifact = repositories.artifacts.create(Artifact(type=ArtifactType.GENERATED_FILE, uri=str(file_path)))
    file_path.unlink()
    client = _admin_client(repositories, settings=_settings_with_artifact_root(artifact_root))

    assert client.get(f"/admin/api/artifacts/{artifact.id}/download").status_code == 404


def test_admin_doctor_reuses_collect_checks_and_summarizes_ok(monkeypatch, tmp_path) -> None:
    """docs/UI_UX_AUDIT.md Phase 9: same checks `ybm doctor` runs, from the
    console - never a second implementation, just collect_checks() itself."""
    from agent_control.bootstrap import Check

    database_url = f"sqlite:///{tmp_path / 'admin.db'}"
    repositories = _repositories(database_url)
    client = _admin_client(repositories)
    monkeypatch.setattr(
        admin_module, "collect_checks",
        lambda: [Check("Python version", "ok", "3.12.3"), Check("Telegram token", "warn", "not configured")],
    )

    response = client.get("/admin/api/doctor")
    body = response.json()

    assert response.status_code == 200
    assert body["ok"] is True  # no "fail" status present
    assert body["checks"] == [
        {"name": "Python version", "status": "ok", "detail": "3.12.3"},
        {"name": "Telegram token", "status": "warn", "detail": "not configured"},
    ]


def test_admin_doctor_reports_not_ok_when_any_check_fails(monkeypatch, tmp_path) -> None:
    from agent_control.bootstrap import Check

    database_url = f"sqlite:///{tmp_path / 'admin.db'}"
    repositories = _repositories(database_url)
    client = _admin_client(repositories)
    monkeypatch.setattr(admin_module, "collect_checks", lambda: [Check("Ports", "fail", "8765 already in use")])

    assert client.get("/admin/api/doctor").json()["ok"] is False


def test_admin_service_log_returns_the_tail_of_a_real_log_file(monkeypatch, tmp_path) -> None:
    """docs/UI_UX_AUDIT.md Phase 9: a per-service log link from the console
    instead of needing `ybm logs <service>` in a terminal."""
    monkeypatch.setattr("agent_control.supervisor._repo_root", lambda: tmp_path)
    log_dir = tmp_path / ".agent_control" / "logs"
    log_dir.mkdir(parents=True)
    (log_dir / "worker.ybmpy.log").write_text("line 1\nline 2\nline 3\n", encoding="utf-8")
    database_url = f"sqlite:///{tmp_path / 'admin.db'}"
    repositories = _repositories(database_url)
    client = _admin_client(repositories)

    response = client.get("/admin/api/logs/worker?lines=2")
    body = response.json()

    assert response.status_code == 200
    assert body["service"] == "worker"
    assert body["lines"] == ["line 2", "line 3"]
    assert body["log_path"] is not None


def test_admin_service_log_returns_empty_when_no_log_exists_yet(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("agent_control.supervisor._repo_root", lambda: tmp_path)
    database_url = f"sqlite:///{tmp_path / 'admin.db'}"
    repositories = _repositories(database_url)
    client = _admin_client(repositories)

    response = client.get("/admin/api/logs/worker")

    assert response.status_code == 200
    assert response.json() == {"service": "worker", "log_path": None, "lines": []}


def test_admin_service_log_404s_for_an_unknown_service_name(tmp_path) -> None:
    """The service name becomes a file path (supervisor._log_path) - must
    be validated against the known set, not accepted as arbitrary input.
    The check is unconditional string equality against six literal names
    (KNOWN_SERVICE_NAMES), so any non-matching string is rejected the same
    way regardless of what characters it contains - one representative
    case here, not one assertion per possible payload shape."""
    database_url = f"sqlite:///{tmp_path / 'admin.db'}"
    repositories = _repositories(database_url)
    client = _admin_client(repositories)

    assert client.get("/admin/api/logs/not_a_real_service").status_code == 404


def test_admin_clears_task_history_and_audit(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)  # see test_admin_page_and_summary for why chdir, not just delenv
    database_url = f"sqlite:///{tmp_path / 'admin.db'}"
    repositories = _repositories(database_url)
    completed = repositories.tasks.create("old task")
    repositories.tasks.update_status(completed.id, TaskStatus.COMPLETED)
    active = repositories.tasks.create("active task")
    AuditLogger(repositories.audit).append(AuditEventType.TASK_CREATED, actor="test", task_id=completed.id)
    AuditLogger(repositories.audit).append(AuditEventType.TASK_CREATED, actor="test", task_id=active.id)
    client = _admin_client(repositories)

    clear_completed = client.delete("/admin/api/tasks?include_active=false")
    remaining_tasks = client.get("/admin/api/tasks?limit=10").json()["tasks"]
    clear_audit = client.delete("/admin/api/audit")

    assert clear_completed.status_code == 200
    assert clear_completed.json()["deleted_tasks"] == 1
    assert [task["id"] for task in remaining_tasks] == [active.id]
    assert clear_audit.status_code == 200
    assert repositories.audit.list_recent(10) == []


def test_admin_tasks_are_paginated(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)  # see test_admin_page_and_summary for why chdir, not just delenv
    database_url = f"sqlite:///{tmp_path / 'admin.db'}"
    repositories = _repositories(database_url)
    for index in range(7):
        repositories.tasks.create(f"task {index}")
    client = _admin_client(repositories)

    first_page = client.get("/admin/api/tasks?limit=5").json()
    summary = client.get("/admin/api/summary?task_limit=5").json()

    assert len(first_page["tasks"]) == 5
    assert first_page["pagination"]["total"] == 7
    assert first_page["pagination"]["has_more"] is True
    assert summary["task_pagination"]["has_more"] is True


def test_admin_task_resume_restores_paused_status(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)  # see test_admin_page_and_summary for why chdir, not just delenv
    database_url = f"sqlite:///{tmp_path / 'admin.db'}"
    repositories = _repositories(database_url)
    task = repositories.tasks.create("resume me")
    task = repositories.tasks.update_status(task.id, TaskStatus.RUNNING)
    client = _admin_client(repositories)

    paused = client.post(f"/admin/api/tasks/{task.id}/signals", json={"signal": "pause"})
    resumed = client.post(f"/admin/api/tasks/{task.id}/signals", json={"signal": "resume"})
    updated = repositories.tasks.get(task.id)

    assert paused.status_code == 200
    assert resumed.status_code == 200
    assert updated is not None
    assert updated.status == TaskStatus.RUNNING


def test_admin_rejects_vscode_terminal_command_by_default(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)  # see test_admin_page_and_summary for why chdir, not just delenv
    monkeypatch.delenv("AGENT_ADMIN_TOKEN", raising=False)
    repositories = _repositories(f"sqlite:///{tmp_path / 'admin.db'}")
    store = VSCodeBridgeStore()
    settings = AppSettings(_env_file=None, capabilities=default_capability_policies())
    client = _admin_client(repositories, settings=settings, vscode_store=store)

    response = client.post("/admin/api/vscode/terminal-commands", json={"command": "echo blocked"})

    assert response.status_code == 403
    assert store.terminal_commands == []


def test_admin_can_queue_vscode_terminal_command_when_enabled(monkeypatch, tmp_path) -> None:
    # chdir before constructing AppSettings so the temp repo owns config.yaml
    # and any local .env file it reads.
    monkeypatch.chdir(tmp_path)
    database_url = f"sqlite:///{tmp_path / 'admin.db'}"
    repositories = _repositories(database_url)
    settings = AppSettings(
        _env_file=None,
        adapters={"vscode": {"enabled": True}},
        capabilities=default_capability_policies(),
    )
    settings.capabilities[Capability.TERMINAL_RUN].enabled = True
    settings.capabilities[Capability.TERMINAL_RUN].requires_approval = False
    store = VSCodeBridgeStore()
    client = _admin_client(repositories, settings=settings, vscode_store=store)

    response = client.post("/admin/api/vscode/terminal-commands", json={"command": "echo hi"})

    assert response.status_code == 200
    assert store.terminal_commands[0].command == "echo hi"


def test_admin_writes_llm_runtime_config(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    repositories = _repositories(f"sqlite:///{tmp_path / 'admin.db'}")
    client = _admin_client(repositories)

    response = client.post(
        "/admin/api/config/llm",
        json={
            "profile_name": "local",
            "default_profile": "local",
            "provider": "openai_compatible",
            "model": "local-coder",
            "base_url": "http://127.0.0.1:1234/v1",
            "api_key_env": "",
        },
    )
    saved = yaml.safe_load((tmp_path / "config" / "config.yaml").read_text(encoding="utf-8"))

    assert response.status_code == 200
    assert saved["llm"]["default_profile"] == "local"
    assert saved["llm"]["profiles"]["local"]["base_url"] == "http://127.0.0.1:1234/v1"
    assert saved["llm"]["profiles"]["local"]["api_key_env"] is None
    assert not (tmp_path / ".env").exists()


def test_admin_selects_llm_preset(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    repositories = _repositories(f"sqlite:///{tmp_path / 'admin.db'}")
    client = _admin_client(repositories)

    response = client.post("/admin/api/config/llm/preset", json={"preset": "localdeploy_gemma3_12b"})
    saved = yaml.safe_load((tmp_path / "config" / "config.yaml").read_text(encoding="utf-8"))

    assert response.status_code == 200
    assert saved["llm"]["default_profile"] == "localdeploy_gemma3_12b"
    assert saved["llm"]["profiles"]["localdeploy_gemma3_12b"]["model"] == "gemma3_12b_ollama_safe"
    assert saved["llm"]["profiles"]["localdeploy_gemma3_12b"]["base_url"] == "http://127.0.0.1:8000/v1"
    assert saved["llm"]["profiles"]["localdeploy_gemma3_12b"]["timeout_seconds"] == 360


def test_admin_writes_telegram_runtime_config(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    repositories = _repositories(f"sqlite:///{tmp_path / 'admin.db'}")
    client = _admin_client(repositories)

    response = client.post(
        "/admin/api/config/telegram",
        json={
            "enabled": True,
            "token_env": "TELEGRAM_BOT_TOKEN",
            "allowed_user_ids": [123],
            "allowed_chat_ids": [456],
            "polling": True,
        },
    )
    saved = yaml.safe_load((tmp_path / "config" / "config.yaml").read_text(encoding="utf-8"))

    assert response.status_code == 200
    assert saved["channels"]["telegram"]["enabled"] is True
    assert saved["channels"]["telegram"]["allowed_user_ids"] == [123]
    assert not (tmp_path / ".env").exists()


def test_admin_llm_test_requires_configured_profile(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)  # see test_admin_page_and_summary for why chdir, not just delenv
    repositories = _repositories(f"sqlite:///{tmp_path / 'admin.db'}")
    settings = AppSettings(_env_file=None, llm={"default_profile": "missing", "profiles": {}})
    client = _admin_client(repositories, settings=settings)

    response = client.post("/admin/api/llm/test", json={})

    assert response.status_code == 400


def test_admin_writes_vscode_runtime_config(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    repositories = _repositories(f"sqlite:///{tmp_path / 'admin.db'}")
    client = _admin_client(repositories)

    response = client.post(
        "/admin/api/config/vscode",
        json={
            "enabled": True,
            "bridge_host": "127.0.0.1",
            "bridge_port": 8766,
            "auth_token_env": "VSCODE_BRIDGE_TOKEN",
            "bridge_token": "secret",
        },
    )
    saved = yaml.safe_load((tmp_path / "config" / "config.yaml").read_text(encoding="utf-8"))
    env_text = (tmp_path / ".env").read_text(encoding="utf-8")

    assert response.status_code == 200
    assert saved["adapters"]["vscode"]["enabled"] is True
    assert "VSCODE_BRIDGE_TOKEN=secret" in env_text


def test_admin_writes_workspace_runtime_config(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    repositories = _repositories(f"sqlite:///{tmp_path / 'admin.db'}")
    client = _admin_client(repositories)

    response = client.post(
        "/admin/api/config/workspace",
        json={
            "enabled": True,
            "root_dir": ".agent_control/workspaces",
            "web_host": "127.0.0.1",
            "web_port_start": 8890,
            "open_browser": False,
        },
    )
    saved = yaml.safe_load((tmp_path / "config" / "config.yaml").read_text(encoding="utf-8"))

    assert response.status_code == 200
    assert saved["adapters"]["workspace"]["root_dir"] == ".agent_control/workspaces"
    assert saved["adapters"]["workspace"]["open_browser"] is False
    assert not (tmp_path / ".env").exists()


def test_admin_writes_computer_use_runtime_config(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    repositories = _repositories(f"sqlite:///{tmp_path / 'admin.db'}")
    client = _admin_client(repositories)

    response = client.post(
        "/admin/api/config/computer-use",
        json={
            "enabled": True,
            "max_steps": 12,
            "step_delay_seconds": 0.2,
            "screenshot_dir": ".agent_control/computer_use/screenshots",
            "allowed_roots": [str(tmp_path)],
            "allowed_apps": ["notepad.exe"],
            "require_session_approval": True,
            "max_ui_elements": 120,
        },
    )
    saved = yaml.safe_load((tmp_path / "config" / "config.yaml").read_text(encoding="utf-8"))

    assert response.status_code == 200
    assert saved["adapters"]["computer_use"]["enabled"] is True
    assert saved["adapters"]["computer_use"]["allowed_roots"] == [str(tmp_path)]
    assert saved["adapters"]["computer_use"]["allowed_apps"] == ["notepad.exe"]
    assert saved["adapters"]["computer_use"]["max_steps"] == 12
    assert not (tmp_path / ".env").exists()


def test_admin_writes_access_modes(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    repositories = _repositories(f"sqlite:///{tmp_path / 'admin.db'}")
    client = _admin_client(repositories)

    response = client.post(
        "/admin/api/config/access-modes",
        json={"modes": {"filesystem": CapabilityAccessMode.READ_ONLY.value}},
    )
    saved = yaml.safe_load((tmp_path / "config" / "config.yaml").read_text(encoding="utf-8"))

    assert response.status_code == 200
    assert saved["capabilities"][Capability.FILESYSTEM_READ.value]["enabled"] is True
    assert saved["capabilities"][Capability.FILESYSTEM_WRITE.value]["enabled"] is False


def test_admin_access_modes_sync_desktop_screenshot_adapter(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    repositories = _repositories(f"sqlite:///{tmp_path / 'admin.db'}")
    client = _admin_client(repositories)

    response = client.post(
        "/admin/api/config/access-modes",
        json={"modes": {"desktop_screenshot": CapabilityAccessMode.READ_ONLY.value}},
    )
    saved = yaml.safe_load((tmp_path / "config" / "config.yaml").read_text(encoding="utf-8"))

    assert response.status_code == 200
    assert saved["capabilities"][Capability.DESKTOP_SCREENSHOT.value]["enabled"] is True
    assert saved["adapters"]["desktop"]["screenshot_enabled"] is True


def test_admin_access_modes_sync_computer_use_adapter(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    repositories = _repositories(f"sqlite:///{tmp_path / 'admin.db'}")
    client = _admin_client(repositories)

    response = client.post(
        "/admin/api/config/access-modes",
        json={"modes": {"desktop_control": CapabilityAccessMode.WRITE_ACCESS.value}},
    )
    saved = yaml.safe_load((tmp_path / "config" / "config.yaml").read_text(encoding="utf-8"))

    assert response.status_code == 200
    assert saved["capabilities"][Capability.DESKTOP_CONTROL.value]["enabled"] is True
    assert saved["capabilities"][Capability.DESKTOP_CONTROL.value]["requires_approval"] is True
    assert saved["adapters"]["desktop"]["control_enabled"] is True
    assert saved["adapters"]["computer_use"]["enabled"] is True
    assert saved["adapters"]["computer_use"]["require_session_approval"] is True

    response = client.post(
        "/admin/api/config/access-modes",
        json={"modes": {"desktop_control": CapabilityAccessMode.FULL_ACCESS.value}},
    )
    saved = yaml.safe_load((tmp_path / "config" / "config.yaml").read_text(encoding="utf-8"))

    assert response.status_code == 200
    assert saved["capabilities"][Capability.DESKTOP_CONTROL.value]["enabled"] is True
    assert saved["capabilities"][Capability.DESKTOP_CONTROL.value]["requires_approval"] is False
    assert saved["adapters"]["desktop"]["control_enabled"] is True
    assert saved["adapters"]["computer_use"]["enabled"] is True
    assert saved["adapters"]["computer_use"]["require_session_approval"] is False


def test_admin_access_modes_sync_browser_adapter(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    repositories = _repositories(f"sqlite:///{tmp_path / 'admin.db'}")
    client = _admin_client(repositories)

    response = client.post(
        "/admin/api/config/access-modes",
        json={"modes": {"browser": CapabilityAccessMode.WRITE_ACCESS.value}},
    )
    saved = yaml.safe_load((tmp_path / "config" / "config.yaml").read_text(encoding="utf-8"))

    assert response.status_code == 200
    assert saved["capabilities"][Capability.BROWSER_OPEN.value]["enabled"] is True
    assert saved["capabilities"][Capability.BROWSER_CONTROL.value]["enabled"] is True
    assert saved["capabilities"][Capability.BROWSER_CONTROL.value]["requires_approval"] is True
    assert saved["adapters"]["browser"]["enabled"] is True


def test_admin_summary_includes_database_and_schedule_data(monkeypatch, tmp_path) -> None:
    """Database table counts and schedule listing are embedded in
    /api/summary, not a separate route - GET /api/database/summary and
    GET /api/schedules were exact duplicates of data /api/summary already
    returns, and the Streamlit UI never called either, so both were deleted
    rather than kept as dead routes (docs/HISTORY.md redundancy pass)."""
    monkeypatch.chdir(tmp_path)  # see test_admin_page_and_summary for why chdir, not just delenv
    database_url = f"sqlite:///{tmp_path / 'admin.db'}"
    repositories = _repositories(database_url)
    repositories.tasks.create("inspect db")
    repositories.schedules.create(
        ScheduleRecord(
            objective="check example.com daily",
            cadence="daily",
            next_run_at=utc_now(),
        )
    )
    settings = AppSettings(_env_file=None, storage={"database_url": database_url})
    client = _admin_client(repositories, settings=settings)

    response = client.get("/admin/api/summary")
    body = response.json()

    assert response.status_code == 200
    assert body["database"]["table_counts"]["tasks"] == 1
    assert "schedules" in body["database"]["table_counts"]
    assert body["schedules"]["total"] == 1
    assert body["schedules"]["items"][0]["objective"] == "check example.com daily"


def _secrets_client(monkeypatch, tmp_path) -> TestClient:
    monkeypatch.chdir(tmp_path)
    repositories = _repositories(f"sqlite:///{tmp_path / 'admin.db'}")
    settings = AppSettings(_env_file=None, secrets={"path": str(tmp_path / "vault.json")})
    return _admin_client(repositories, settings=settings)


def test_admin_secrets_unavailable_without_vault_key(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("AGENT_SECRET_VAULT_KEY", raising=False)
    client = _secrets_client(monkeypatch, tmp_path)

    listed = client.get("/admin/api/secrets")
    assert listed.status_code == 200
    assert listed.json() == {"available": False, "key_env": "AGENT_SECRET_VAULT_KEY", "services": {}}

    # Setting a secret must fail clearly, not with a raw SecretVaultError leak.
    set_response = client.post("/admin/api/secrets", json={"service": "openai", "key": "api_key", "value": "sk-1"})
    assert set_response.status_code == 400
    assert "ybm setup" in set_response.json()["detail"]


def test_admin_secrets_init_generates_a_key_and_the_vault_becomes_usable(monkeypatch, tmp_path) -> None:
    # The "Vault not initialized" dead end (docs/HISTORY.md's P2 UX pass) -
    # a real fix, not a link to a terminal command: generate the key, and
    # the vault must be immediately usable with no restart (read_env_value
    # re-reads .env live).
    monkeypatch.delenv("AGENT_SECRET_VAULT_KEY", raising=False)
    client = _secrets_client(monkeypatch, tmp_path)
    assert client.get("/admin/api/secrets").json()["available"] is False

    init_response = client.post("/admin/api/secrets/init")
    assert init_response.status_code == 200
    assert init_response.json() == {"key_env": "AGENT_SECRET_VAULT_KEY", "generated": True}

    listed = client.get("/admin/api/secrets")
    assert listed.json()["available"] is True
    created = client.post("/admin/api/secrets", json={"service": "openai", "key": "api_key", "value": "sk-1"})
    assert created.status_code == 200


def test_admin_secrets_init_is_idempotent_when_a_key_already_exists(monkeypatch, tmp_path) -> None:
    from agent_control.storage.secrets import SecretVault

    existing_key = SecretVault.generate_key()
    monkeypatch.setenv("AGENT_SECRET_VAULT_KEY", existing_key)
    client = _secrets_client(monkeypatch, tmp_path)

    response = client.post("/admin/api/secrets/init")

    assert response.status_code == 200
    assert response.json() == {"key_env": "AGENT_SECRET_VAULT_KEY", "generated": False}


def test_admin_secrets_set_list_and_delete_round_trip(monkeypatch, tmp_path) -> None:
    from agent_control.storage.secrets import SecretVault

    monkeypatch.setenv("AGENT_SECRET_VAULT_KEY", SecretVault.generate_key())
    client = _secrets_client(monkeypatch, tmp_path)

    created = client.post("/admin/api/secrets", json={"service": "openai", "key": "api_key", "value": "sk-secret-value"})
    assert created.status_code == 200
    assert created.json() == {"service": "openai", "key": "api_key", "set": True}

    listed = client.get("/admin/api/secrets")
    body = listed.json()
    assert body["available"] is True
    assert body["services"] == {"openai": ["api_key"]}
    # The listing endpoint must never leak the value.
    assert "sk-secret-value" not in listed.text

    deleted = client.delete("/admin/api/secrets/openai/api_key")
    assert deleted.status_code == 200
    assert deleted.json() == {"service": "openai", "key": "api_key", "deleted": True}

    after = client.get("/admin/api/secrets")
    assert after.json()["services"] == {}


def test_admin_secrets_delete_missing_returns_404(monkeypatch, tmp_path) -> None:
    from agent_control.storage.secrets import SecretVault

    monkeypatch.setenv("AGENT_SECRET_VAULT_KEY", SecretVault.generate_key())
    client = _secrets_client(monkeypatch, tmp_path)

    response = client.delete("/admin/api/secrets/nobody/nothing")

    assert response.status_code == 404


def test_admin_secrets_set_audits_service_and_key_but_not_value(monkeypatch, tmp_path) -> None:
    from agent_control.storage.secrets import SecretVault

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGENT_SECRET_VAULT_KEY", SecretVault.generate_key())
    repositories = _repositories(f"sqlite:///{tmp_path / 'admin.db'}")
    settings = AppSettings(_env_file=None, secrets={"path": str(tmp_path / "vault.json")})
    client = _admin_client(repositories, settings=settings)

    client.post("/admin/api/secrets", json={"service": "openai", "key": "api_key", "value": "sk-must-not-be-logged"})

    events = repositories.audit.list_recent(limit=20)
    matching = [e for e in events if e.type == AuditEventType.CONFIG_UPDATED and e.payload.get("section") == "secrets"]
    assert matching
    assert matching[0].payload["patch"] == {"action": "set", "service": "openai", "key": "api_key"}
    assert "sk-must-not-be-logged" not in str(matching[0].payload)


def _chat_client(monkeypatch, tmp_path) -> tuple[TestClient, Repositories]:
    monkeypatch.chdir(tmp_path)
    repositories = _repositories(f"sqlite:///{tmp_path / 'admin.db'}")
    return _admin_client(repositories), repositories


def test_admin_chat_attachment_upload_creates_a_real_artifact(monkeypatch, tmp_path) -> None:
    client, repositories = _chat_client(monkeypatch, tmp_path)

    response = client.post(
        "/admin/api/chat/attachments",
        files={"file": ("notes.txt", b"hello from a real upload", "text/plain")},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["file_name"] == "notes.txt"
    assert body["size_bytes"] == len(b"hello from a real upload")
    artifact = repositories.artifacts.get(body["artifact_id"])
    assert artifact is not None
    assert Path(artifact.uri).read_bytes() == b"hello from a real upload"


def test_admin_chat_attachment_upload_rejects_oversized_files(monkeypatch, tmp_path) -> None:
    client, _repositories = _chat_client(monkeypatch, tmp_path)
    oversized = b"x" * (admin_module.MAX_CHAT_ATTACHMENT_BYTES + 1)

    response = client.post(
        "/admin/api/chat/attachments",
        files={"file": ("big.bin", oversized, "application/octet-stream")},
    )

    assert response.status_code == 413


def test_admin_chat_send_folds_an_attached_artifact_into_the_objective(monkeypatch, tmp_path) -> None:
    client, repositories = _chat_client(monkeypatch, tmp_path)
    uploaded = client.post(
        "/admin/api/chat/attachments",
        files={"file": ("budget.csv", b"a,b\n1,2\n", "text/csv")},
    ).json()

    response = client.post(
        "/admin/api/chat/messages",
        json={"text": "summarize this file", "attachment_ids": [uploaded["artifact_id"]]},
    )

    assert response.status_code == 200
    task = response.json()["task"]
    assert "budget.csv" in task["objective"]
    assert task["metadata"]["attachment_ids"] == [uploaded["artifact_id"]]
    assert task["artifacts"][0]["id"] == uploaded["artifact_id"]


def test_admin_list_folders_with_no_path_returns_configured_roots(monkeypatch, tmp_path) -> None:
    """docs/UI_UX_AUDIT.md Phase 13: the folder picker's starting point is
    the same computer_use.allowed_roots filesystem.manage itself is scoped
    to - a folder this picker can reach is a folder the agent can actually
    act on, not a second boundary to keep in sync."""
    monkeypatch.chdir(tmp_path)
    root = tmp_path / "Downloads"
    root.mkdir()
    repositories = _repositories(f"sqlite:///{tmp_path / 'admin.db'}")
    settings = AppSettings(_env_file=None, adapters={"computer_use": {"allowed_roots": [str(root)]}})
    client = _admin_client(repositories, settings=settings)

    response = client.get("/admin/api/folders")

    assert response.status_code == 200
    body = response.json()
    assert body["current_path"] is None
    assert body["parent_path"] is None
    assert body["roots"] == [str(root)]
    assert body["entries"] == [{"name": "Downloads", "path": str(root)}]


def test_admin_list_folders_lists_subdirectories_sorted_and_skips_hidden(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    root = tmp_path / "Downloads"
    (root / "Zebra").mkdir(parents=True)
    (root / "apple").mkdir()
    (root / ".git").mkdir()
    (root / "not_a_dir.txt").write_text("x", encoding="utf-8")
    repositories = _repositories(f"sqlite:///{tmp_path / 'admin.db'}")
    settings = AppSettings(_env_file=None, adapters={"computer_use": {"allowed_roots": [str(root)]}})
    client = _admin_client(repositories, settings=settings)

    response = client.get("/admin/api/folders", params={"path": str(root)})

    assert response.status_code == 200
    body = response.json()
    assert body["current_path"] == str(root)
    assert [e["name"] for e in body["entries"]] == ["apple", "Zebra"]


def test_admin_list_folders_parent_path_is_none_at_a_root_boundary(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    root = tmp_path / "Downloads"
    child = root / "Invoices"
    child.mkdir(parents=True)
    repositories = _repositories(f"sqlite:///{tmp_path / 'admin.db'}")
    settings = AppSettings(_env_file=None, adapters={"computer_use": {"allowed_roots": [str(root)]}})
    client = _admin_client(repositories, settings=settings)

    at_root = client.get("/admin/api/folders", params={"path": str(root)}).json()
    at_child = client.get("/admin/api/folders", params={"path": str(child)}).json()

    assert at_root["parent_path"] is None
    assert at_child["parent_path"] == str(root)


def test_admin_list_folders_rejects_a_path_outside_allowed_roots(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    root = tmp_path / "Downloads"
    root.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    repositories = _repositories(f"sqlite:///{tmp_path / 'admin.db'}")
    settings = AppSettings(_env_file=None, adapters={"computer_use": {"allowed_roots": [str(root)]}})
    client = _admin_client(repositories, settings=settings)

    response = client.get("/admin/api/folders", params={"path": str(outside)})

    assert response.status_code == 400


def test_admin_list_folders_404s_for_a_path_that_does_not_exist(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    root = tmp_path / "Downloads"
    root.mkdir()
    repositories = _repositories(f"sqlite:///{tmp_path / 'admin.db'}")
    settings = AppSettings(_env_file=None, adapters={"computer_use": {"allowed_roots": [str(root)]}})
    client = _admin_client(repositories, settings=settings)

    response = client.get("/admin/api/folders", params={"path": str(root / "does_not_exist")})

    assert response.status_code == 404


def test_admin_list_folders_returns_empty_when_no_roots_configured(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    repositories = _repositories(f"sqlite:///{tmp_path / 'admin.db'}")
    settings = AppSettings(_env_file=None, adapters={"computer_use": {"allowed_roots": []}})
    client = _admin_client(repositories, settings=settings)

    response = client.get("/admin/api/folders")

    assert response.status_code == 200
    assert response.json() == {"current_path": None, "parent_path": None, "roots": [], "entries": []}


def test_admin_chat_send_creates_a_task_and_list_returns_it_oldest_first(monkeypatch, tmp_path) -> None:
    """docs/HISTORY.md Part 4 T2.8: the local web chat channel - a message
    becomes a normal task, going through the exact same worker/policy
    pipeline as any other channel, with no Telegram dependency."""
    client, repositories = _chat_client(monkeypatch, tmp_path)

    first = client.post("/admin/api/chat/messages", json={"text": "what is the status?"})
    second = client.post("/admin/api/chat/messages", json={"text": "thanks"})

    assert first.status_code == 200
    assert first.json()["task"]["objective"] == "what is the status?"
    assert first.json()["task"]["metadata"]["source_chat_id"] == "local"
    assert second.status_code == 200

    listed = client.get("/admin/api/chat/messages")
    body = listed.json()
    assert [t["objective"] for t in body["tasks"]] == ["what is the status?", "thanks"]

    # Both messages share the same conversation - a real conversation_id
    # from the same fixed local web chat thread, not a fresh one per message.
    assert first.json()["conversation_id"] == second.json()["conversation_id"] == body["conversation_id"]
    real_task = repositories.tasks.get(first.json()["task"]["id"])
    assert real_task.conversation_id == body["conversation_id"]


def test_admin_chat_list_is_empty_before_any_message_sent(monkeypatch, tmp_path) -> None:
    client, _repositories = _chat_client(monkeypatch, tmp_path)

    listed = client.get("/admin/api/chat/messages")

    assert listed.status_code == 200
    assert listed.json()["tasks"] == []


def test_admin_chat_redacts_secret_values_from_historical_errors(monkeypatch, tmp_path) -> None:
    token = "123456:super-secret-token-value-that-must-not-leak"
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", token)
    client, repositories = _chat_client(monkeypatch, tmp_path)
    created = client.post("/admin/api/chat/messages", json={"text": "hello"}).json()["task"]
    task = repositories.tasks.get(created["id"])
    repositories.tasks.update_metadata(
        task.id,
        {**task.metadata, "last_worker_error": f"https://api.telegram.org/bot{token}/sendMessage"},
        TaskStatus.FAILED,
    )

    response_text = client.get("/admin/api/chat/messages").text

    assert token not in response_text
    assert "/bot***/sendMessage" in response_text


def test_admin_chat_send_resumes_a_clarifying_task_instead_of_spawning_a_new_one(monkeypatch, tmp_path) -> None:
    """The web channel previously always created a new task, so a reply to
    a clarifying question just spawned an unrelated second task while the
    real one sat stuck forever - the exact gap Telegram's own
    _resume_clarifying_task (now shared via clarification.py) already
    closed for that channel."""
    client, repositories = _chat_client(monkeypatch, tmp_path)
    original = client.post("/admin/api/chat/messages", json={"text": "organize my files"}).json()["task"]
    repositories.tasks.update_metadata(
        original["id"],
        {**original["metadata"], "clarifying_question": "Which folder?"},
        TaskStatus.CLARIFYING,
    )

    response = client.post("/admin/api/chat/messages", json={"text": "the Downloads folder"})

    assert response.status_code == 200
    body = response.json()["task"]
    assert body["id"] == original["id"]
    assert body["status"] == "received"
    assert "[User clarification: the Downloads folder]" in body["objective"]

    listed = client.get("/admin/api/chat/messages").json()["tasks"]
    assert len(listed) == 1


def test_admin_chat_send_resume_also_attaches_a_file_sent_with_the_reply(monkeypatch, tmp_path) -> None:
    """An attachment sent alongside a clarification reply was silently
    dropped: the resume branch only ever looked at payload.text, never
    payload.attachment_ids."""
    client, repositories = _chat_client(monkeypatch, tmp_path)
    original = client.post("/admin/api/chat/messages", json={"text": "organize my files"}).json()["task"]
    repositories.tasks.update_metadata(
        original["id"],
        {**original["metadata"], "clarifying_question": "Which files?"},
        TaskStatus.CLARIFYING,
    )
    uploaded = client.post(
        "/admin/api/chat/attachments",
        files={"file": ("list.txt", b"a,b,c", "text/plain")},
    ).json()

    response = client.post(
        "/admin/api/chat/messages",
        json={"text": "these ones", "attachment_ids": [uploaded["artifact_id"]]},
    )

    assert response.status_code == 200
    task = response.json()["task"]
    assert task["id"] == original["id"]
    assert "list.txt" in task["objective"]
    assert task["artifacts"][0]["id"] == uploaded["artifact_id"]


def test_admin_chat_send_cancel_word_cancels_the_clarifying_task(monkeypatch, tmp_path) -> None:
    client, repositories = _chat_client(monkeypatch, tmp_path)
    original = client.post("/admin/api/chat/messages", json={"text": "organize my files"}).json()["task"]
    repositories.tasks.update_metadata(
        original["id"],
        {**original["metadata"], "clarifying_question": "Which folder?"},
        TaskStatus.CLARIFYING,
    )

    response = client.post("/admin/api/chat/messages", json={"text": "never mind"})

    assert response.json()["task"]["status"] == "cancelled"


def test_admin_chat_list_includes_a_task_s_artifacts(monkeypatch, tmp_path) -> None:
    """The trace endpoint already inlines artifacts (build_task_trace) - a
    file a task produced was otherwise invisible in Chat unless you opened
    the trace to find it."""
    client, repositories = _chat_client(monkeypatch, tmp_path)
    created = client.post("/admin/api/chat/messages", json={"text": "generate a report"}).json()["task"]
    assert created["artifacts"] == []
    repositories.artifacts.create(
        Artifact(task_id=created["id"], type=ArtifactType.GENERATED_FILE, uri="file:///tmp/report.csv"),
    )

    listed = client.get("/admin/api/chat/messages").json()["tasks"]

    assert len(listed) == 1
    assert listed[0]["artifacts"][0]["uri"] == "file:///tmp/report.csv"
    assert listed[0]["artifacts"][0]["type"] == "generated_file"


def test_admin_chat_send_rejects_empty_text(monkeypatch, tmp_path) -> None:
    client, _repositories = _chat_client(monkeypatch, tmp_path)

    response = client.post("/admin/api/chat/messages", json={"text": ""})

    assert response.status_code == 422


def test_admin_chat_send_audits_task_creation(monkeypatch, tmp_path) -> None:
    client, repositories = _chat_client(monkeypatch, tmp_path)

    response = client.post("/admin/api/chat/messages", json={"text": "hello"})

    task_id = response.json()["task"]["id"]
    events = repositories.audit.list_recent(limit=20)
    assert any(
        e.type == AuditEventType.TASK_CREATED and e.task_id == task_id and e.actor == "admin_chat"
        for e in events
    )


def test_telegram_verify_names_the_bot_back(monkeypatch, tmp_path) -> None:
    """Pasting an opaque string and being told nothing is the step people get
    wrong, and the failure is silent and much later. getMe turns it into a
    confirmation the user can recognise."""
    import respx
    from httpx import Response

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AGENT_ADMIN_TOKEN", raising=False)
    client = _admin_client(_repositories(f"sqlite:///{tmp_path / 'admin.db'}"))

    with respx.mock:
        respx.get(url__regex=r".*/getMe$").mock(
            return_value=Response(200, json={"ok": True, "result": {"username": "my_bot", "first_name": "My Bot"}})
        )
        body = client.post("/admin/api/setup/telegram/verify", json={"bot_token": "123:AA-token"}).json()

    assert body["ok"] is True
    assert body["username"] == "my_bot"
    assert body["link"] == "https://t.me/my_bot"


def test_telegram_verify_reports_a_rejected_token_plainly(monkeypatch, tmp_path) -> None:
    import respx
    from httpx import Response

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AGENT_ADMIN_TOKEN", raising=False)
    client = _admin_client(_repositories(f"sqlite:///{tmp_path / 'admin.db'}"))

    with respx.mock:
        respx.get(url__regex=r".*/getMe$").mock(return_value=Response(401, json={"ok": False}))
        response = client.post("/admin/api/setup/telegram/verify", json={"bot_token": "nope"})

    assert response.status_code == 400
    assert "copied" in response.json()["detail"]
    # The token must never appear in an error, since it lives in the request URL.
    assert "nope" not in response.text


def test_telegram_await_first_message_returns_the_sender_id(monkeypatch, tmp_path) -> None:
    """The allowlist is what makes the bot answer at all - _authorization_decision
    fails closed on an empty one - and asking someone to find their own numeric
    id is the worst way to fill it. This learns it from the message they were
    going to send anyway."""
    import respx
    from httpx import Response

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AGENT_ADMIN_TOKEN", raising=False)
    client = _admin_client(_repositories(f"sqlite:///{tmp_path / 'admin.db'}"))

    update = {
        "message": {
            "from": {"id": 4242, "username": "someone", "first_name": "Some"},
            "chat": {"id": 4242},
        }
    }
    with respx.mock:
        respx.get(url__regex=r".*/getUpdates.*").mock(return_value=Response(200, json={"ok": True, "result": [update]}))
        body = client.post(
            "/admin/api/setup/telegram/await-first-message",
            json={"bot_token": "123:AA-token", "wait_seconds": 5},
        ).json()

    assert body == {
        "found": True,
        "user_id": 4242,
        "chat_id": 4242,
        "username": "someone",
        "first_name": "Some",
    }


def test_telegram_await_first_message_reports_nothing_arrived(monkeypatch, tmp_path) -> None:
    import respx
    from httpx import Response

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AGENT_ADMIN_TOKEN", raising=False)
    client = _admin_client(_repositories(f"sqlite:///{tmp_path / 'admin.db'}"))

    with respx.mock:
        respx.get(url__regex=r".*/getUpdates.*").mock(return_value=Response(200, json={"ok": True, "result": []}))
        body = client.post(
            "/admin/api/setup/telegram/await-first-message",
            json={"bot_token": "123:AA-token", "wait_seconds": 5},
        ).json()

    assert body == {"found": False}


def test_llm_provider_catalog_is_served_to_the_console(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AGENT_ADMIN_TOKEN", raising=False)
    client = _admin_client(_repositories(f"sqlite:///{tmp_path / 'admin.db'}"))

    body = client.get("/admin/api/llm/providers").json()
    keys = {p["key"] for p in body["providers"]}

    assert {"anthropic", "openai", "ollama", "custom"} <= keys
    anthropic = next(p for p in body["providers"] if p["key"] == "anthropic")
    assert anthropic["kind"] == "anthropic"
    assert anthropic["needs_key"] is True
    assert anthropic["keys_url"]
    ollama = next(p for p in body["providers"] if p["key"] == "ollama")
    assert ollama["local"] is True and ollama["needs_key"] is False


def test_llm_verify_lists_models_for_an_openai_compatible_provider(monkeypatch, tmp_path) -> None:
    """Verifying proves the key and fills the model picker in one call, so the
    list is never a hardcoded one that rots."""
    import respx
    from httpx import Response

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AGENT_ADMIN_TOKEN", raising=False)
    client = _admin_client(_repositories(f"sqlite:///{tmp_path / 'admin.db'}"))

    with respx.mock:
        respx.get("https://api.openai.com/v1/models").mock(
            return_value=Response(200, json={"data": [{"id": "gpt-4.1"}, {"id": "gpt-4o"}]})
        )
        body = client.post(
            "/admin/api/setup/llm/verify", json={"provider": "openai", "api_key": "sk-live"}
        ).json()

    assert body["ok"] is True
    assert [m["id"] for m in body["models"]] == ["gpt-4.1", "gpt-4o"]
    assert body["listed"] is True


def test_llm_verify_reports_a_rejected_key_without_echoing_it(monkeypatch, tmp_path) -> None:
    import respx
    from httpx import Response

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AGENT_ADMIN_TOKEN", raising=False)
    client = _admin_client(_repositories(f"sqlite:///{tmp_path / 'admin.db'}"))

    with respx.mock:
        respx.get("https://api.groq.com/openai/v1/models").mock(return_value=Response(401))
        response = client.post(
            "/admin/api/setup/llm/verify", json={"provider": "groq", "api_key": "sk-wrong"}
        )

    assert response.status_code == 400
    assert "rejected" in response.json()["detail"]
    assert "sk-wrong" not in response.text


def test_llm_verify_rejects_an_unknown_provider(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AGENT_ADMIN_TOKEN", raising=False)
    client = _admin_client(_repositories(f"sqlite:///{tmp_path / 'admin.db'}"))

    response = client.post(
        "/admin/api/setup/llm/verify", json={"provider": "nope", "api_key": "x"}
    )
    assert response.status_code == 400
    assert "unknown provider" in response.json()["detail"]


def test_llm_verify_requires_a_key_for_a_remote_provider(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AGENT_ADMIN_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    client = _admin_client(_repositories(f"sqlite:///{tmp_path / 'admin.db'}"))

    response = client.post("/admin/api/setup/llm/verify", json={"provider": "anthropic"})
    assert response.status_code == 400
    assert "needs an API key" in response.json()["detail"]


def test_llm_verify_says_a_local_runtime_is_not_running(monkeypatch, tmp_path) -> None:
    """The useful message for a local provider is 'is it running', not a
    connection-error class name."""
    import httpx
    import respx

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AGENT_ADMIN_TOKEN", raising=False)
    client = _admin_client(_repositories(f"sqlite:///{tmp_path / 'admin.db'}"))

    with respx.mock:
        respx.get("http://127.0.0.1:11434/v1/models").mock(
            side_effect=httpx.ConnectError("refused")
        )
        response = client.post("/admin/api/setup/llm/verify", json={"provider": "ollama"})

    assert response.status_code == 502
    assert "running" in response.json()["detail"]


def test_channel_catalog_reports_live_connection_state(monkeypatch, tmp_path) -> None:
    """Adding a way to reach YBM is a catalog row, but `connected` must come
    from real config - the console must never claim a channel is live when it
    is not."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AGENT_ADMIN_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    client = _admin_client(_repositories(f"sqlite:///{tmp_path / 'admin.db'}"))

    body = client.get("/admin/api/channels").json()
    by_key = {c["key"]: c for c in body["channels"]}

    # Web chat is the one thing that always works.
    assert by_key["web"]["connected"] is True
    assert by_key["web"]["zero_setup"] is True
    # Telegram is unconfigured on a fresh install, so it must not claim to be.
    assert by_key["telegram"]["connected"] is False
    assert by_key["telegram"]["guided"] is True
    # Planned channels are listed, so the shape of the product is visible.
    assert by_key["discord"]["status"] == "planned"
    assert by_key["discord"]["note"]


def test_channel_catalog_does_not_call_telegram_connected_on_a_token_alone(
    monkeypatch, tmp_path
) -> None:
    """A token with an empty allowlist produces a bot that ignores every
    message, so that state is not 'connected'."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AGENT_ADMIN_TOKEN", raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:AA-token")
    settings = AppSettings(_env_file=None)
    settings.channels.telegram.enabled = True
    settings.channels.telegram.allowed_user_ids = []
    settings.channels.telegram.allowed_chat_ids = []
    client = _admin_client(_repositories(f"sqlite:///{tmp_path / 'admin.db'}"), settings=settings)

    body = client.get("/admin/api/channels").json()
    telegram = next(c for c in body["channels"] if c["key"] == "telegram")
    assert telegram["connected"] is False


def test_llm_test_makes_a_real_completion_before_setup_finishes(monkeypatch, tmp_path) -> None:
    """Listing models proves the key. Only a real round trip proves the chosen
    model answers, the routing is right, and the response parses."""
    import respx
    from httpx import Response

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AGENT_ADMIN_TOKEN", raising=False)
    client = _admin_client(_repositories(f"sqlite:///{tmp_path / 'admin.db'}"))

    with respx.mock:
        route = respx.post("https://api.openai.com/v1/chat/completions").mock(
            return_value=Response(
                200,
                json={"choices": [{"message": {"content": "ready"}}], "usage": {}},
            )
        )
        body = client.post(
            "/admin/api/setup/llm/test",
            json={"provider": "openai", "model": "gpt-4.1", "api_key": "sk-live"},
        ).json()

    assert route.called, "no completion was actually attempted"
    assert body["ok"] is True
    assert body["reply"] == "ready"
    assert body["model"] == "gpt-4.1"
    assert isinstance(body["latency_ms"], int)


def test_llm_test_surfaces_a_model_that_does_not_answer(monkeypatch, tmp_path) -> None:
    """A valid key with a bad model name is exactly the case model-listing
    alone would have let through."""
    import respx
    from httpx import Response

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AGENT_ADMIN_TOKEN", raising=False)
    client = _admin_client(_repositories(f"sqlite:///{tmp_path / 'admin.db'}"))

    with respx.mock:
        respx.post("https://api.openai.com/v1/chat/completions").mock(
            return_value=Response(404, json={"error": {"message": "no such model"}})
        )
        response = client.post(
            "/admin/api/setup/llm/test",
            json={"provider": "openai", "model": "gpt-nonexistent", "api_key": "sk-live"},
        )

    assert response.status_code == 502
    assert "sk-live" not in response.text


def test_llm_test_routes_anthropic_to_the_native_provider(monkeypatch, tmp_path) -> None:
    """The test call must go through the same routing a real request uses, or
    it proves nothing about the provider that will actually serve traffic."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AGENT_ADMIN_TOKEN", raising=False)
    client = _admin_client(_repositories(f"sqlite:///{tmp_path / 'admin.db'}"))

    captured: dict = {}

    class _Client:
        def __init__(self, **kwargs):
            async def _create(**kw):
                captured.update(kw)
                return type(
                    "_M",
                    (),
                    {
                        "content": [type("_B", (), {"type": "text", "text": "hello"})()],
                        "stop_reason": "end_turn",
                        "usage": None,
                    },
                )()

            self.messages = type("_Messages", (), {"create": staticmethod(_create)})()

        async def close(self):
            return None

    import anthropic

    monkeypatch.setattr(anthropic, "AsyncAnthropic", _Client)
    body = client.post(
        "/admin/api/setup/llm/test",
        json={"provider": "anthropic", "model": "claude-sonnet-5", "api_key": "sk-ant"},
    ).json()

    assert body["reply"] == "hello"
    # Proof it went through AnthropicProvider: temperature is omitted, which is
    # the whole reason that provider exists.
    assert "temperature" not in captured
    assert captured["model"] == "claude-sonnet-5"


def test_presets_offered_are_only_the_ones_that_could_work(monkeypatch, tmp_path) -> None:
    """Inside a container 127.0.0.1 is the container, so every loopback preset
    is unreachable - offering them makes the recommended choice the one that
    cannot work. Outside one, the host-gateway preset is just a confusing
    duplicate. Which it is, is checkable."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AGENT_ADMIN_TOKEN", raising=False)
    client = _admin_client(_repositories(f"sqlite:///{tmp_path / 'admin.db'}"))

    monkeypatch.delenv("YBM_HEADLESS", raising=False)
    keys = {p["key"] for p in client.get("/admin/api/summary").json()["integrations"]["llm"]["presets"]}
    assert not any(k.endswith("_container") for k in keys), keys
    assert keys, "a normal install must still be offered local presets"

    monkeypatch.setenv("YBM_HEADLESS", "1")
    container_keys = {
        p["key"] for p in client.get("/admin/api/summary").json()["integrations"]["llm"]["presets"]
    }
    assert all(k.endswith("_container") for k in container_keys), container_keys
    assert container_keys, "a containerised install must still get a workable preset"


def test_no_cloud_preset_bypasses_the_verified_provider_path(monkeypatch, tmp_path) -> None:
    """A cloud preset saved without verifying a key, while the picker verifies
    and requires a real completion - two routes to the same place with
    different quality."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AGENT_ADMIN_TOKEN", raising=False)
    client = _admin_client(_repositories(f"sqlite:///{tmp_path / 'admin.db'}"))

    presets = client.get("/admin/api/summary").json()["integrations"]["llm"]["presets"]
    assert all(p.get("api_key_env") is None for p in presets), (
        "presets must be local-only; anything needing a key goes through the picker"
    )
