"""How a research job searches and reads (jobs/io.py): Chrome first, the search tool second, and an honest reason when neither answers."""

from __future__ import annotations

import base64

import httpx
import pytest

from agent_control.config import WebSearchAdapterConfig
from agent_control.jobs.io import build_io, parse_results, real_url, visible_text
from agent_control.schemas import Capability, ToolCallRequest, ToolCallResult, ToolResultStatus
from agent_control.tools.web_search import WebSearchAdapter


def _bing(url: str) -> str:
    encoded = base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")
    return f"https://www.bing.com/ck/a?!&&p=abc123&ptn=3&ver=2&hsh=4&fclid=x&u=a1{encoded}&ntb=1"


def test_a_bing_redirect_is_unwrapped_to_the_address_it_points_at_and_other_bing_links_are_not_results() -> None:
    assert real_url(_bing("https://www.energy.gov/energysaver/heat-pump-systems")) == "https://www.energy.gov/energysaver/heat-pump-systems"
    assert real_url("https://example.org/page") == "https://example.org/page"  # not Bing: as it is
    assert real_url("https://www.bing.com/images/search?q=heat") == ""  # a Bing vertical is not a page to read
    assert real_url("https://www.bing.com/ck/a?u=a1%%%notbase64") == ""


def test_results_are_cleaned_deduplicated_and_cut_to_the_count() -> None:
    raw = [
        {"title": "  Heat   pumps  | Energy.gov ", "href": _bing("https://www.energy.gov/hp"), "snippet": "A heat pump moves heat."},
        {"title": "Same page again", "href": _bing("https://www.energy.gov/hp"), "snippet": ""},
        {"title": "Not a result", "href": "javascript:void(0)", "snippet": ""},
        {"title": "Directory", "href": "https://example.org/dir", "snippet": ""},
        "not a dict",
    ]
    results = parse_results(raw, 5)
    assert [item["url"] for item in results] == ["https://www.energy.gov/hp", "https://example.org/dir"]
    assert results[0]["title"] == "Heat pumps | Energy.gov" and parse_results(raw, 1) == results[:1] and parse_results("nope", 3) == []


def test_a_page_is_reduced_to_its_words_without_scripts_menus_or_short_fragments() -> None:
    page = visible_text(
        "<html><head><title> Costs  of heat pumps </title><script>var x = 'never shown';</script><style>p{}</style></head><body>"
        "<nav><a href='/'>Home page navigation link text goes here</a></nav>"
        "<article><h1>What a heat pump costs to install in a house</h1><p>An air-source unit costs between 4,000 and 8,000 dollars installed.</p>"
        "<p>Short.</p></article><footer>Copyright notice and other footer words appear here</footer></body></html>"
    )
    assert page.title == "Costs of heat pumps"
    assert "4,000 and 8,000 dollars" in page.text and "What a heat pump costs" in page.text
    assert "never shown" not in page.text and "navigation" not in page.text and "Copyright" not in page.text and "Short." not in page.text


class _Search:
    """A stand-in search engine: answers, or says it has nothing, or fails."""

    def __init__(self, *, results=None, error: str | None = None) -> None:
        self.results, self.error, self.calls = results or [], error, 0

    async def __call__(self, query, count):
        self.calls += 1
        if self.error:
            raise RuntimeError(self.error)
        return self.results


class _Adapter:
    def __init__(self, results=None, error: str | None = None) -> None:
        self.results, self.error, self.calls = results or [], error, 0

    async def execute(self, request: ToolCallRequest) -> ToolCallResult:
        self.calls += 1
        if self.error:
            return ToolCallResult(request_id=request.id, status=ToolResultStatus.FAILED, error_message=self.error)
        return ToolCallResult(request_id=request.id, status=ToolResultStatus.SUCCEEDED, output={"results": self.results})


@pytest.mark.asyncio
async def test_chrome_is_tried_first_and_the_search_tool_only_when_it_gives_nothing() -> None:
    chrome, tool = _Search(results=[{"title": "A", "url": "https://a.example/x", "snippet": ""}]), _Adapter([{"title": "B", "url": "https://b.example/y"}])
    io = build_io(object(), tool, object(), browser_search=chrome)
    assert (await io.search("q", 5))[0]["url"] == "https://a.example/x" and tool.calls == 0

    empty_chrome = _Search(results=[])
    io = build_io(object(), tool, object(), browser_search=empty_chrome)
    assert (await io.search("q", 5))[0]["url"] == "https://b.example/y" and tool.calls == 1
    no_chrome = build_io(object(), tool, object())  # Chrome is not running with DevTools: the tool alone
    assert (await no_chrome.search("q", 5))[0]["url"] == "https://b.example/y"


@pytest.mark.asyncio
async def test_when_no_engine_answers_the_reasons_are_said_not_just_no_pages() -> None:
    io = build_io(object(), _Adapter(error="DuckDuckGo asked for a human check"), object(), browser_search=_Search(error="Chrome DevTools is not available at http://127.0.0.1:9222"))
    with pytest.raises(RuntimeError) as raised:
        await io.search("q", 5)
    message = str(raised.value)
    assert "Chrome: Chrome DevTools is not available" in message and "the web search tool: DuckDuckGo asked for a human check" in message


@pytest.mark.asyncio
async def test_a_page_that_is_not_html_or_would_not_open_is_skipped() -> None:
    class _Http:
        def __init__(self, output, status=ToolResultStatus.SUCCEEDED) -> None:
            self.output, self.status = output, status

        async def execute(self, request):
            return ToolCallResult(request_id=request.id, status=self.status, output=self.output)

    body = "<html><title>T</title><body><p>" + "A sentence that is long enough to count as a line of text. " * 5 + "</p></body></html>"
    ok = build_io(object(), object(), _Http({"ok": True, "text": body, "headers": {"content-type": "text/html; charset=utf-8"}}))
    assert (await ok.fetch("https://a.example")).title == "T"
    pdf = build_io(object(), object(), _Http({"ok": True, "text": "%PDF", "headers": {"content-type": "application/pdf"}}))
    assert await pdf.fetch("https://a.example/x.pdf") is None
    refused = build_io(object(), object(), _Http({"ok": False, "text": ""}))
    assert await refused.fetch("https://a.example") is None
    failed = build_io(object(), object(), _Http({}, ToolResultStatus.FAILED))
    assert await failed.fetch("https://a.example") is None


@pytest.mark.asyncio
async def test_duckduckgo_that_asks_for_a_human_check_is_an_error_not_no_results(monkeypatch) -> None:
    """Found 11/14: every search on this connection was answered with a human check, parsed to nothing, and reported as "No results"."""
    challenge = httpx.Response(202, text="<html><body>Unfortunately, bots use DuckDuckGo too. anomaly-modal</body></html>")

    async def blocked(self, url, **kwargs):
        return challenge

    monkeypatch.setattr(WebSearchAdapter, "_get", blocked)
    adapter = WebSearchAdapter(WebSearchAdapterConfig())
    result = await adapter.execute(ToolCallRequest(task_id="t", tool_name="web.search", capability=Capability.NETWORK_HTTP, input={"operation": "search", "query": "heat pump"}))
    assert result.status == ToolResultStatus.FAILED and "human check" in (result.error_message or "") and "BRAVE_SEARCH_API_KEY" in (result.error_message or "")
