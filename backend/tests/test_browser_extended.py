from __future__ import annotations

import pytest

from agent_control.config import BrowserAdapterConfig
from agent_control.schemas import Capability, ToolCallRequest
from agent_control.tools.browser import BrowserAdapter, BrowserTarget

class FakeClient:
    def __init__(self, config: BrowserAdapterConfig) -> None:
        self.config = config
        self.base_url = "http://127.0.0.1:9222"
        self.target = BrowserTarget("tab_1", "page", "Search", "https://search.test", "ws://example")
        self.navigated: list[str] = []

    def ensure_available(self) -> None:
        return None

    def open_url(self, url: str, *, new_tab: bool = True) -> BrowserTarget:
        self.target = BrowserTarget("tab_1", "page", "Opened", url, "ws://example")
        return self.target

    def navigate(self, target: BrowserTarget, url: str) -> None:
        self.navigated.append(url)
        self.target = BrowserTarget(target.id, "page", "Navigated", url, target.web_socket_debugger_url)

    def wait(self, seconds: float) -> None:
        return None

    def page_targets(self) -> list[BrowserTarget]:
        return [self.target]

    def page_summary(self, target: BrowserTarget, *, max_chars: int) -> dict:
        if "search" in target.url:
            return {
                "url": target.url,
                "title": "Search",
                "text": "Search results",
                "links": [
                    {"href": "https://one.test/page", "text": "One"},
                    {"href": "https://two.test/page", "text": "Two"},
                ],
                "forms": [],
            }
        return {
            "url": target.url,
            "title": "Episode page",
            "text": "Latest episode 7 released 2026-05-20. Contact form available.",
            "links": [],
            "forms": [{"inputs": [{"name": "email", "type": "email"}]}],
        }

    def fill_form(self, target: BrowserTarget, fields: dict[str, str], *, submit: bool, submit_selector: str | None) -> dict:
        return {"filled": sorted(fields), "submitted": submit}

    searched_on_pages: list[tuple[str, str]] = []
    page_has_a_search_box = True

    def search_on_page(self, target: BrowserTarget, query: str) -> dict:
        FakeClient.searched_on_pages.append((target.url, query))
        return {"searched": True, "field": "q", "pressed": "search"} if FakeClient.page_has_a_search_box else {"searched": False, "reason": "no_search_box"}

@pytest.mark.asyncio
async def test_browser_research_pages_visits_limited_results(monkeypatch) -> None:
    monkeypatch.setattr("agent_control.tools.browser.ChromeDevToolsClient", FakeClient)
    adapter = BrowserAdapter(BrowserAdapterConfig(enabled=True, default_wait_seconds=0))

    result = await adapter.execute(
        ToolCallRequest(
            task_id="task_browser",
            tool_name="browser.open",
            capability=Capability.BROWSER_OPEN,
            input={"operation": "research_pages", "query": "ducks", "page_limit": 2},
        )
    )

    assert result.status.value == "succeeded"
    assert result.output["visited_urls"] == ["https://one.test/page", "https://two.test/page"]
    assert len(result.output["page_summaries"]) == 2

@pytest.mark.asyncio
async def test_browser_check_page_update_extracts_markers(monkeypatch) -> None:
    monkeypatch.setattr("agent_control.tools.browser.ChromeDevToolsClient", FakeClient)
    adapter = BrowserAdapter(BrowserAdapterConfig(enabled=True, default_wait_seconds=0))

    result = await adapter.execute(
        ToolCallRequest(
            task_id="task_browser",
            tool_name="browser.control",
            capability=Capability.BROWSER_CONTROL,
            input={"operation": "check_page_update", "url": "https://show.test"},
        )
    )

    assert result.status.value == "succeeded"
    markers = result.output["browser_state"]["update_check"]["markers"]
    assert any("episode 7" in marker.lower() for marker in markers)

@pytest.mark.asyncio
async def test_browser_extract_state_and_fill_form_step(monkeypatch) -> None:
    monkeypatch.setattr("agent_control.tools.browser.ChromeDevToolsClient", FakeClient)
    adapter = BrowserAdapter(BrowserAdapterConfig(enabled=True, default_wait_seconds=0))

    state = await adapter.execute(
        ToolCallRequest(
            task_id="task_browser",
            tool_name="browser.control",
            capability=Capability.BROWSER_CONTROL,
            input={"operation": "extract_page_state", "url": "https://form.test"},
        )
    )
    filled = await adapter.execute(
        ToolCallRequest(
            task_id="task_browser",
            tool_name="browser.control",
            capability=Capability.BROWSER_CONTROL,
            input={"operation": "fill_form_step", "fields": {"email": "a@example.com"}},
        )
    )

    assert state.output["forms"][0]["inputs"][0]["name"] == "email"
    assert "Fields: email." in state.output["summary"]
    assert "What the page says now:" in state.output["summary"] and "Latest episode 7 released" in state.output["summary"]  # the words on the page are kept, not replaced by a count of forms
    assert filled.output["browser_state"]["filled_fields"]["filled"] == ["email"]
    assert filled.output["browser_state"]["filled_fields"]["submitted"] is False



@pytest.mark.asyncio
async def test_a_login_made_for_the_person_is_said_to_be_sent_to_them_so_the_model_neither_asks_nor_refuses(monkeypatch) -> None:
    monkeypatch.setattr("agent_control.tools.browser.ChromeDevToolsClient", FakeClient)
    adapter = BrowserAdapter(BrowserAdapterConfig(enabled=True, default_wait_seconds=0))

    made = await adapter.execute(ToolCallRequest(
        task_id="task_browser", tool_name="browser.control", capability=Capability.BROWSER_CONTROL,
        input={"operation": "fill_form", "url": "https://form.test", "fields": {"email": "a@example.com", "password": "{{new_password}}"}},
    ))
    plain = await adapter.execute(ToolCallRequest(
        task_id="task_browser", tool_name="browser.control", capability=Capability.BROWSER_CONTROL,
        input={"operation": "fill_form", "url": "https://form.test", "fields": {"email": "a@example.com"}},
    ))

    assert "the system sends the person the username and the password" in made.output["summary"] and "Do not ask" in made.output["summary"]
    assert "password" not in plain.output["summary"].lower()


def _search_call(**fields) -> ToolCallRequest:
    return ToolCallRequest(task_id="task_browser", tool_name="browser.open", capability=Capability.BROWSER_OPEN, input={"operation": "search", **fields})


@pytest.mark.asyncio
async def test_a_search_on_a_named_page_uses_that_pages_own_search_box_not_the_web(monkeypatch) -> None:
    """11/19 hands suite: "On <page> search for lighthouse" went to Bing, eleven times, and the answer was Bing's page."""
    monkeypatch.setattr("agent_control.tools.browser.ChromeDevToolsClient", FakeClient)
    FakeClient.searched_on_pages, FakeClient.page_has_a_search_box = [], True
    adapter = BrowserAdapter(BrowserAdapterConfig(enabled=True, default_wait_seconds=0))

    result = await adapter.execute(_search_call(query="lighthouse", url="http://127.0.0.1:51904/hands/search.html"))

    assert result.status.value == "succeeded"
    assert FakeClient.searched_on_pages == [("http://127.0.0.1:51904/hands/search.html", "lighthouse")]
    assert "bing.com" not in result.output["browser_url"]
    assert result.output["browser_state"]["searched_on_page"]["searched"] is True


@pytest.mark.asyncio
async def test_a_search_with_no_page_or_with_a_search_engine_address_is_still_the_web_search(monkeypatch) -> None:
    monkeypatch.setattr("agent_control.tools.browser.ChromeDevToolsClient", FakeClient)
    FakeClient.searched_on_pages = []
    adapter = BrowserAdapter(BrowserAdapterConfig(enabled=True, default_wait_seconds=0))

    plain = await adapter.execute(_search_call(query="ducks"))
    engine = await adapter.execute(_search_call(query="ducks", url="https://www.bing.com/"))

    assert plain.status.value == "succeeded" and engine.status.value == "succeeded"
    assert FakeClient.searched_on_pages == []
    assert "ducks" in plain.output["browser_url"] and "ducks" in engine.output["browser_url"]


@pytest.mark.asyncio
async def test_a_search_on_a_page_without_a_search_box_says_so_and_names_the_next_step(monkeypatch) -> None:
    monkeypatch.setattr("agent_control.tools.browser.ChromeDevToolsClient", FakeClient)
    FakeClient.searched_on_pages, FakeClient.page_has_a_search_box = [], False
    adapter = BrowserAdapter(BrowserAdapterConfig(enabled=True, default_wait_seconds=0))

    result = await adapter.execute(_search_call(query="lighthouse", url="https://about.test/us"))

    FakeClient.page_has_a_search_box = True
    assert result.status.value == "failed"
    assert "no search box" in (result.error_message or "") and "fill_form" in (result.error_message or "")
