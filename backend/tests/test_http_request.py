from __future__ import annotations

import httpx
import pytest

from agent_control.config import AppSettings, CapabilityPolicy, HttpRequestAdapterConfig, SecretVaultConfig
from agent_control.orchestration.executor import ToolExecutor
from agent_control.policy import PolicyEngine
from agent_control.schemas import AuditEventType, Capability, RiskLevel, ToolCallRequest, ToolResultStatus
from agent_control.storage.secrets import SecretVault
from agent_control.tools.http_request import HttpRequestAdapter, _require_allowed_url
from agent_control.tools.registry import build_tool_registry
from agent_control.tools.spec import ToolDefinition
from helpers import make_repos


def test_http_request_declares_its_response_as_untrusted_content() -> None:
    """docs/THREAT_MODEL.md: an HTTP response body is external, uncontrolled
    content, same boundary as browser/document/MCP content."""
    settings = AppSettings(
        _env_file=None,
        capabilities={Capability.NETWORK_HTTP: CapabilityPolicy(enabled=True, requires_approval=False, max_risk_level=RiskLevel.HIGH)},
        adapters={"http_request": {"enabled": True, "allowed_hosts": ["api.example.com"]}},
    )
    registry = build_tool_registry(settings, "http://127.0.0.1:8765")
    definition = next(d for d in registry.definitions if d.name == "http.request")

    assert definition.operation_content_trust == {"request": "untrusted_external"}


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
    """docs/archive/UI_UX_AUDIT.md Phase 2: a real, non-loopback call must show up
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


# --- reading any public web page, read only ------------------------------------------------------------------------------------------


from agent_control.tools import http_request as http_module  # noqa: E402


@pytest.mark.parametrize("address, public", [
    ("93.184.216.34", True), ("8.8.8.8", True), ("2606:4700:4700::1111", True),
    ("127.0.0.1", False), ("10.1.2.3", False), ("192.168.0.5", False), ("172.16.0.1", False), ("169.254.169.254", False),
    ("0.0.0.0", False), ("::1", False), ("fe80::1", False), ("fd00::1", False), ("224.0.0.1", False), ("not-an-address", False),
])
def test_only_public_addresses_count_as_public(address, public):
    assert http_module._is_public_address(address) is public


def _resolver(*addresses):
    return lambda host, port: [(2, 1, 6, "", (address, 0)) for address in addresses]


def test_a_name_that_resolves_to_a_private_address_is_not_a_public_site_even_if_another_answer_is_public():
    assert http_module._resolves_only_to_public_addresses("x.example", resolver=_resolver("93.184.216.34")) is True
    assert http_module._resolves_only_to_public_addresses("x.example", resolver=_resolver("93.184.216.34", "10.0.0.7")) is False

    def broken(host, port):
        raise OSError("no such host")

    assert http_module._resolves_only_to_public_addresses("nope.invalid", resolver=broken) is False


def test_public_reads_are_off_by_default_and_a_get_to_a_public_host_is_then_refused():
    config = HttpRequestAdapterConfig(allowed_hosts=["api.example.com"])
    with pytest.raises(ValueError, match="not allowlisted"):
        _require_allowed_url("https://news.example.org/", config, method="GET")


def test_with_public_reads_on_a_get_or_head_to_a_public_host_is_allowed_and_anything_else_is_not(monkeypatch):
    monkeypatch.setattr(http_module.socket, "getaddrinfo", _resolver("93.184.216.34"))
    config = HttpRequestAdapterConfig(allow_public_reads=True)
    _require_allowed_url("https://news.example.org/front", config, method="GET")
    _require_allowed_url("https://news.example.org/front", config, method="head")
    for method in ("POST", "PUT", "DELETE"):
        with pytest.raises(ValueError, match="not allowlisted"):
            _require_allowed_url("https://news.example.org/front", config, method=method)


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8765/admin", "http://localhost:8000/", "http://192.168.1.1/", "http://169.254.169.254/latest/meta-data/",
])
def test_public_reads_never_reach_this_computer_or_its_network(url):
    with pytest.raises(ValueError, match="not a public website"):
        _require_allowed_url(url, HttpRequestAdapterConfig(allow_public_reads=True), method="GET")


def test_public_reads_still_honour_the_block_list_and_the_userinfo_rule(monkeypatch):
    monkeypatch.setattr(http_module.socket, "getaddrinfo", _resolver("93.184.216.34"))
    config = HttpRequestAdapterConfig(allow_public_reads=True, blocked_hosts=["bad.example.org"])
    with pytest.raises(ValueError, match="blocked"):
        _require_allowed_url("https://bad.example.org/", config, method="GET")
    with pytest.raises(ValueError, match="userinfo"):
        _require_allowed_url("https://user:pw@news.example.org/", config, method="GET")


def test_the_tool_is_on_with_public_reads_even_with_no_allowlist_and_says_so():
    settings = AppSettings(
        _env_file=None,
        capabilities={Capability.NETWORK_HTTP: CapabilityPolicy(enabled=True, requires_approval=False, max_risk_level=RiskLevel.HIGH)},
        adapters={"http_request": {"enabled": True, "allow_public_reads": True}},
    )
    definition = build_tool_registry(settings, "http://127.0.0.1:8765").definition_index["http.request"]
    assert definition.enabled is True and "any public website" in definition.description
    off = AppSettings(
        _env_file=None,
        capabilities={Capability.NETWORK_HTTP: CapabilityPolicy(enabled=True, requires_approval=False, max_risk_level=RiskLevel.HIGH)},
        adapters={"http_request": {"enabled": True}},
    )
    assert build_tool_registry(off, "http://127.0.0.1:8765").definition_index["http.request"].enabled is False


# --- the exact title of a page, read by code ---------------------------------------------------------------------------------------


def test_the_title_and_visible_text_of_a_page_are_read_without_scripts_and_markup():
    page = (
        "<!doctype html><html><head><title>\n  Welcome to  Python.org </title><style>p{color:red}</style></head>"
        "<body><script>var x = 'not text';</script><h1>Hello</h1><p>First <b>bold</b> paragraph.</p><noscript>no js</noscript></body></html>"
    )
    title, text = http_module.html_digest(page)
    assert title == "Welcome to Python.org"
    assert text == "Hello First bold paragraph."


def test_a_page_with_no_title_gives_none_and_text_is_capped():
    title, text = http_module.html_digest("<html><body>" + "word " * 1000 + "</body></html>")
    assert title is None and len(text) == 1500


@pytest.mark.asyncio
async def test_an_html_response_carries_its_exact_title_in_the_summary_and_a_json_one_does_not():
    html = "<html><head><title>Mozilla - Internet for people</title></head><body>Hello world</body></html>"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api":
            return httpx.Response(200, json={"a": 1})
        return httpx.Response(200, text=html, headers={"content-type": "text/html; charset=utf-8"})

    adapter = HttpRequestAdapter(
        HttpRequestAdapterConfig(allowed_hosts=["site.example"]), SecretVaultConfig(), transport=httpx.MockTransport(handler),
    )
    page = await adapter.execute(ToolCallRequest(
        task_id="t", tool_name="http.request", capability=Capability.NETWORK_HTTP, input={"url": "https://site.example/", "method": "GET"},
    ))
    assert page.output["page_title"] == "Mozilla - Internet for people"
    assert 'The page title is exactly: "Mozilla - Internet for people".' in page.output["summary"]
    assert "The page text starts: Hello world" in page.output["summary"]
    data = await adapter.execute(ToolCallRequest(
        task_id="t", tool_name="http.request", capability=Capability.NETWORK_HTTP, input={"url": "https://site.example/api", "method": "GET"},
    ))
    assert data.output["page_title"] is None and "page title" not in data.output["summary"]
