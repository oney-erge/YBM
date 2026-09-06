from __future__ import annotations

import pytest

from agent_control.config import AppSettings, BrowserAdapterConfig, CapabilityPolicy
from agent_control.schemas import Capability, RiskLevel, TaskRecord
from agent_control.tools.browser import BrowserAdapter, BrowserTarget
from agent_control.tools.registry import build_tool_registry

def test_registry_exposes_browser_tools_when_enabled() -> None:
    settings = AppSettings(
        _env_file=None,
        adapters={"browser": {"enabled": True}},
        capabilities={
            Capability.TELEGRAM_SEND: CapabilityPolicy(
                enabled=True,
                requires_approval=False,
                max_risk_level=RiskLevel.LOW,
            ),
            Capability.BROWSER_OPEN: CapabilityPolicy(
                enabled=True,
                requires_approval=False,
                max_risk_level=RiskLevel.LOW,
            ),
            Capability.BROWSER_CONTROL: CapabilityPolicy(
                enabled=True,
                requires_approval=True,
                max_risk_level=RiskLevel.CRITICAL,
            ),
        },
    )

    registry = build_tool_registry(settings, "http://127.0.0.1:8765")
    definitions = {definition.name: definition for definition in registry.definitions}

    assert definitions["browser.open"].enabled is True
    assert definitions["browser.open"].operations == (
        "open",
        "search",
        "research",
        "inspect_tabs",
        "screenshot",
        "summarize_page",
        "research_pages",
    )
    assert definitions["browser.control"].enabled is True
    assert "browser.open" in registry.adapters
    assert "browser.control" in registry.adapters


def test_browser_tools_declare_which_operations_can_leave_the_machine() -> None:
    """docs/GAPS.md: browser traffic was invisible to receipts because only
    http.request called record_egress. ToolExecutor now drives that off
    each ToolDefinition's operation_egress instead of a manual call site -
    this pins exactly which operations these two tools declare, so a future
    change here is a deliberate edit, not a silent regression back to the
    old gap.
    """
    settings = AppSettings(
        _env_file=None,
        adapters={"browser": {"enabled": True}},
        capabilities={
            Capability.BROWSER_OPEN: CapabilityPolicy(enabled=True, requires_approval=False, max_risk_level=RiskLevel.LOW),
            Capability.BROWSER_CONTROL: CapabilityPolicy(enabled=True, requires_approval=True, max_risk_level=RiskLevel.CRITICAL),
        },
    )

    registry = build_tool_registry(settings, "http://127.0.0.1:8765")
    definitions = {definition.name: definition for definition in registry.definitions}

    assert set(definitions["browser.open"].operation_egress) == {"open", "search", "research", "research_pages"}
    # Read-only operations against an already-open page contact no new host.
    assert "inspect_tabs" not in definitions["browser.open"].operation_egress
    assert "screenshot" not in definitions["browser.open"].operation_egress
    assert definitions["browser.control"].operation_egress == ("navigate",)

@pytest.mark.asyncio
async def test_browser_adapter_research_uses_chrome_client(monkeypatch) -> None:
    class FakeClient:
        def __init__(self, config: BrowserAdapterConfig) -> None:
            self.config = config
            self.base_url = "http://127.0.0.1:9222"

        def ensure_available(self) -> None:
            return None

        def open_url(self, url: str, *, new_tab: bool = True) -> BrowserTarget:
            assert "bing.com/search" in url
            return BrowserTarget(
                id="tab_1",
                type="page",
                title="Search",
                url=url,
                web_socket_debugger_url="ws://example.test",
            )

        def wait(self, seconds: float) -> None:
            return None

        def page_summary(self, target: BrowserTarget, *, max_chars: int) -> dict:
            return {
                "url": "https://example.test",
                "title": "Example",
                "description": "Example page",
                "headings": ["Example"],
                "text": "Example visible text",
                "links": [],
            }

    monkeypatch.setattr("agent_control.tools.browser.ChromeDevToolsClient", FakeClient)
    adapter = BrowserAdapter(BrowserAdapterConfig(enabled=True))
    request = TaskRecord(objective="unused")

    from agent_control.schemas import ToolCallRequest

    result = await adapter.execute(
        ToolCallRequest(
            task_id=request.id,
            tool_name="browser.open",
            capability=Capability.BROWSER_OPEN,
            input={"operation": "research", "objective": "search example"},
        )
    )

    assert result.status.value == "succeeded"
    assert result.output["browser_url"] == "https://example.test"
    assert "Example visible text" in result.output["summary"]
