"""A click that matched nothing is a failure, not a success."""

from __future__ import annotations

import pytest

from agent_control.config import BrowserAdapterConfig
from agent_control.schemas import Capability, ToolCallRequest
from agent_control.tools import browser as browser_module
from agent_control.tools.browser import BrowserAdapter, BrowserTarget


class _Client:
    clicked = {"clicked": True, "tag": "BUTTON", "text": "Sign me up"}

    def __init__(self, config) -> None:
        self.base_url = "http://127.0.0.1:9222"
        self.target = BrowserTarget("tab_1", "page", "Form", "http://127.0.0.1:8899/", "ws://example")
        self.last_click: dict | None = None

    def ensure_available(self) -> None:
        return None

    def page_targets(self):
        return [self.target]

    def click(self, target, *, selector, text):
        self.last_click = {"selector": selector, "text": text}
        return type(self).clicked

    def wait(self, seconds: float) -> None:
        return None

    def page_summary(self, target, *, max_chars: int) -> dict:
        return {"url": target.url, "title": "Form", "text": "Thanks", "links": [], "forms": []}


async def _click(monkeypatch, result, **inputs):
    _Client.clicked = result
    monkeypatch.setattr(browser_module, "ChromeDevToolsClient", _Client)
    adapter = BrowserAdapter(BrowserAdapterConfig(enabled=True, default_wait_seconds=0))
    return await adapter.execute(ToolCallRequest(
        task_id="task_click", tool_name="browser.control", capability=Capability.BROWSER_CONTROL,
        input={"operation": "click", **inputs},
    ))


@pytest.mark.asyncio
async def test_a_click_that_matched_nothing_fails_and_says_what_to_do(monkeypatch):
    result = await _click(monkeypatch, {"clicked": False, "reason": "element_not_found"}, selector="Sign me up")
    assert result.status.value == "failed"
    assert "Nothing on the page matched 'Sign me up'" in result.error_message
    assert "'text'" in result.error_message


@pytest.mark.asyncio
async def test_a_click_that_landed_succeeds(monkeypatch):
    result = await _click(monkeypatch, {"clicked": True, "tag": "BUTTON", "text": "Sign me up"}, text="Sign me up")
    assert result.status.value == "succeeded"
    assert result.output["browser_state"]["clicked"]["clicked"] is True


def test_words_in_the_selector_slot_are_tried_as_the_buttons_text():
    script = browser_module._click_script("Sign me up", None)
    # Falls back to visible text only when the CSS lookup found nothing, and only for plain words.
    assert "const spoken" in script and "const textToFind = wantedText || spoken" in script
    assert "catch (error)" in script  # an invalid CSS selector is not an exception that ends the step
