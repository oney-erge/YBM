"""A fill that typed nothing is a failure; a partial fill says what it missed."""

from __future__ import annotations

import pytest

from agent_control.config import BrowserAdapterConfig
from agent_control.schemas import Capability, ToolCallRequest
from agent_control.tools import browser as browser_module
from agent_control.tools.browser import BrowserAdapter, BrowserTarget


class _Client:
    result: dict = {"filled": []}
    url = "http://127.0.0.1:8899/"

    def __init__(self, config) -> None:
        self.base_url = "http://127.0.0.1:9222"
        self.target = BrowserTarget("tab_1", "page", "Form", type(self).url, "ws://example")

    def ensure_available(self) -> None:
        return None

    def page_targets(self):
        return [self.target]

    def fill_form(self, target, fields, *, submit, submit_selector):
        return type(self).result

    def wait(self, seconds: float) -> None:
        return None

    def page_summary(self, target, *, max_chars: int) -> dict:
        return {"url": target.url, "title": "Form", "text": "A form", "links": [], "forms": []}


async def _fill(monkeypatch, result, url="http://127.0.0.1:8899/", **inputs):
    _Client.result, _Client.url = result, url
    monkeypatch.setattr(browser_module, "ChromeDevToolsClient", _Client)
    adapter = BrowserAdapter(BrowserAdapterConfig(enabled=True, default_wait_seconds=0))
    return await adapter.execute(ToolCallRequest(
        task_id="task_fill", tool_name="browser.control", capability=Capability.BROWSER_CONTROL,
        input={"operation": "fill_form_step", "fields": {"Full name": "Oney", "T-shirt size": "M"}, **inputs},
    ))


@pytest.mark.asyncio
async def test_a_fill_that_typed_nothing_fails_and_says_what_to_do(monkeypatch):
    result = await _fill(monkeypatch, {"filled": []})
    assert result.status.value == "failed"
    assert "None of the fields (Full name, T-shirt size) matched anything on http://127.0.0.1:8899/" in result.error_message
    assert "browser.open" in result.error_message


@pytest.mark.asyncio
async def test_a_fill_on_a_blank_tab_says_so(monkeypatch):
    result = await _fill(monkeypatch, {"filled": []}, url="about:blank")
    assert result.status.value == "failed"
    assert "matched anything on a blank tab" in result.error_message


@pytest.mark.asyncio
async def test_a_complete_fill_succeeds_unchanged(monkeypatch):
    result = await _fill(monkeypatch, {"filled": ["Full name", "T-shirt size"]})
    assert result.status.value == "succeeded"
    assert "unmatched_fields" not in result.output["browser_state"]


@pytest.mark.asyncio
async def test_a_partial_fill_succeeds_and_names_what_it_missed(monkeypatch):
    result = await _fill(monkeypatch, {"filled": ["Full name"]})
    assert result.status.value == "succeeded"
    assert result.output["browser_state"]["unmatched_fields"] == ["T-shirt size"]
    assert "Not found on the page, so not filled: T-shirt size." in result.output["summary"]
