from __future__ import annotations

import httpx
import pytest

from agent_control.config import AppSettings, CapabilityPolicy, HttpRequestAdapterConfig, SecretVaultConfig
from agent_control.orchestration.executor import ToolExecutor
from agent_control.policy import PolicyEngine
from agent_control.schemas import AuditEventType, Capability, RiskLevel, ToolCallRequest, ToolResultStatus
from agent_control.storage.secrets import SecretVault
from agent_control.tools.http_request import HttpRequestAdapter, _require_allowed_url
from agent_control.tools.spec import ToolDefinition
from helpers import make_repos


@pytest.mark.asyncio
async def test_http_request_injects_and_redacts_secret(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AGENT_SECRET_VAULT_KEY", SecretVault.generate_key())
    secrets_config = SecretVaultConfig(path=str(tmp_path / "vault.json"))
    SecretVault(secrets_config).set_secret("demo", "token", "super-token")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer super-token"
        return httpx.Response(
            200,
            json={"echo": "super-token"},
            headers={"set-cookie": "session=super-token"},
        )

    adapter = HttpRequestAdapter(
        HttpRequestAdapterConfig(allowed_hosts=["api.example.com"]),
        secrets_config,
        transport=httpx.MockTransport(handler),
    )

    result = await adapter.execute(
        ToolCallRequest(
            task_id="task_http",
            tool_name="http.request",
            capability=Capability.NETWORK_HTTP,
            input={
                "operation": "request",
                "method": "GET",
                "url": "https://api.example.com/me",
                "secret_refs": {
                    "headers.Authorization": {"ref": "demo.token", "template": "Bearer {secret}"},
                },
            },
        )
    )

    assert result.status == ToolResultStatus.SUCCEEDED
    assert result.output["json"]["echo"] == "***"
    assert result.output["headers"]["set-cookie"] == "***"


@pytest.mark.asyncio
async def test_http_request_records_egress_for_the_receipt(tmp_path) -> None:
    """docs/UI_UX_AUDIT.md Phase 2: a real, non-loopback call must show up
    as an EGRESS_CONTACTED audit event so Task Receipts can say what left
    the machine.

    The adapter itself no longer calls egress.record_egress() - that
    call site moved to ToolExecutor, driven by the ToolDefinition's
    operation_egress declaration (spec.py), so this is now an executor-level
    test through the real adapter rather than a direct adapter test.
    """
    secrets_config = SecretVaultConfig(path=str(tmp_path / "vault.json"))
    adapter = HttpRequestAdapter(
        HttpRequestAdapterConfig(allowed_hosts=["api.example.com"]),
        secrets_config,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True})),
    )
    definition = ToolDefinition(
        name="http.request",
        capability=Capability.NETWORK_HTTP,
        enabled=True,
        description="test",
        operations=("request",),
        default_operation="request",
        # The real registration's risk_resolver drops a plain GET to LOW;
        # matched here with minimum_risk so this test's request doesn't need
        # an approval round-trip just to reach the adapter.
        minimum_risk=RiskLevel.LOW,
        operation_egress=("request",),
    )
    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("t")
    settings = AppSettings()
    settings.capabilities[Capability.NETWORK_HTTP] = CapabilityPolicy(
        enabled=True, requires_approval=False, max_risk_level=RiskLevel.LOW,
    )
    executor = ToolExecutor(
        PolicyEngine(settings, audit),
        repos,
        audit,
        adapters={"http.request": adapter},
        tool_definitions=[definition],
    )

    await executor.execute(
        ToolCallRequest(
            task_id=task.id,
            tool_name="http.request",
            capability=Capability.NETWORK_HTTP,
            risk_level=RiskLevel.LOW,
            input={"operation": "request", "method": "GET", "url": "https://api.example.com/status"},
        )
    )

    events = [e for e in repos.audit.list_for_task(task.id) if e.type == AuditEventType.EGRESS_CONTACTED]
    assert len(events) == 1
    assert events[0].task_id == task.id
    assert events[0].payload["host"] == "api.example.com"


@pytest.mark.asyncio
async def test_http_request_rejects_non_allowlisted_host(tmp_path) -> None:
    adapter = HttpRequestAdapter(
        HttpRequestAdapterConfig(allowed_hosts=["api.example.com"]),
        SecretVaultConfig(path=str(tmp_path / "vault.json")),
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text="ok")),
    )

    result = await adapter.execute(
        ToolCallRequest(
            task_id="task_http",
            tool_name="http.request",
            capability=Capability.NETWORK_HTTP,
            input={"operation": "request", "url": "https://not.example.com/me"},
        )
    )

    assert result.status == ToolResultStatus.FAILED
    assert "not allowlisted" in (result.error_message or "")


@pytest.mark.parametrize(
    "url",
    [
        # Look-alike subdomain: character sequence matches the prefix string
        # but the real, attacker-controlled host is "api.example.com.attacker.com".
        "https://api.example.com.attacker.com/steal",
        # Userinfo trick: everything before "@" looks like the allowed host
        # to a naive string-prefix check, but the real host is "attacker.com".
        "https://api.example.com@attacker.com/steal",
    ],
)
def test_require_allowed_url_rejects_prefix_lookalikes(url: str) -> None:
    config = HttpRequestAdapterConfig(allowed_url_prefixes=["https://api.example.com"])
    with pytest.raises(ValueError):
        _require_allowed_url(url, config)


def test_require_allowed_url_accepts_genuine_prefix_match() -> None:
    config = HttpRequestAdapterConfig(allowed_url_prefixes=["https://api.example.com"])
    _require_allowed_url("https://api.example.com/v1/users", config)  # must not raise
