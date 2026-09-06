"""web.search - results as data, instead of driving Chrome at a results page.

The only prior route was browser.open's search: a real browser launch needing
browser.control (critical) to perform what is a read, breaking on consent
walls and layout changes. These tests cover the parsing, because that is
where a search tool quietly goes wrong - returning plausible nonsense is
worse than returning nothing.
"""

from __future__ import annotations

import pytest

from agent_control.config import AppSettings, CapabilityPolicy, WebSearchAdapterConfig
from agent_control.schemas import Capability, RiskLevel, ToolCallRequest, ToolResultStatus
from agent_control.tools.registry import build_tool_registry
from agent_control.tools.web_search import WebSearchAdapter, _DuckDuckGoParser, _clean_ddg_url


DDG_HTML = """
<div class="result results_links">
  <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fguide&amp;rut=x">
    Example &amp; Guide
  </a>
  <a class="result__snippet">A short description of the guide.</a>
</div>
<div class="result results_links">
  <a class="result__a" href="https://plain.example.org/page">Plain Result</a>
  <a class="result__snippet">Second snippet.</a>
</div>
"""


def _request(**payload) -> ToolCallRequest:
    return ToolCallRequest(
        task_id="task_1", tool_name="web.search",
        capability=Capability.NETWORK_HTTP, risk_level=RiskLevel.LOW, input=payload,
    )


def test_parser_extracts_title_url_and_snippet() -> None:
    parser = _DuckDuckGoParser()
    parser.feed(DDG_HTML)

    assert len(parser.results) == 2
    first = parser.results[0]
    assert first["title"] == "Example & Guide"
    assert first["snippet"] == "A short description of the guide."
    # Unwrapped from DuckDuckGo's redirector, so the Operator can fetch it.
    assert first["url"] == "https://example.com/guide"
    assert parser.results[1]["url"] == "https://plain.example.org/page"


def test_changed_markup_yields_nothing_rather_than_garbage() -> None:
    """The failure mode that matters: a layout change must produce zero
    results the caller reports honestly, not confident nonsense."""
    parser = _DuckDuckGoParser()
    parser.feed("<div class='something-else'><a href='https://x.example'>x</a></div>")

    assert parser.results == []


@pytest.mark.parametrize(
    ("href", "expected"),
    [
        ("//duckduckgo.com/l/?uddg=https%3A%2F%2Fa.example%2Fb", "https://a.example/b"),
        ("https://direct.example/page", "https://direct.example/page"),
        ("/relative/path", ""),
        ("", ""),
    ],
)
def test_clean_ddg_url(href, expected) -> None:
    assert _clean_ddg_url(href) == expected


@pytest.mark.asyncio
async def test_empty_query_is_refused(tmp_path) -> None:
    adapter = WebSearchAdapter(WebSearchAdapterConfig(enabled=True))

    result = await adapter.execute(_request(operation="search", query="   "))

    assert result.status == ToolResultStatus.FAILED


@pytest.mark.asyncio
async def test_brave_without_a_key_says_which_variable_is_missing() -> None:
    """A missing key must name the variable to set - "search failed" alone
    leaves no next step."""
    adapter = WebSearchAdapter(
        WebSearchAdapterConfig(enabled=True, provider="brave", api_key_env="NOT_SET_ANYWHERE_XYZ")
    )

    result = await adapter.execute(_request(operation="search", query="anything"))

    assert result.status == ToolResultStatus.FAILED
    assert "NOT_SET_ANYWHERE_XYZ" in (result.error_message or "")


@pytest.mark.asyncio
async def test_searxng_without_a_base_url_says_so() -> None:
    adapter = WebSearchAdapter(WebSearchAdapterConfig(enabled=True, provider="searxng", base_url=None))

    result = await adapter.execute(_request(operation="search", query="anything"))

    assert "base_url" in (result.error_message or "")


def test_tool_follows_the_network_capability() -> None:
    """Shares NETWORK_HTTP rather than inventing a near-duplicate switch."""
    off = build_tool_registry(AppSettings(_env_file=None), backend_base_url="http://127.0.0.1:8765")
    assert next(d for d in off.definitions if d.name == "web.search").enabled is False

    settings = AppSettings(
        _env_file=None,
        capabilities={
            Capability.NETWORK_HTTP: CapabilityPolicy(
                enabled=True, requires_approval=True, max_risk_level=RiskLevel.HIGH
            )
        },
    )
    on = build_tool_registry(settings, backend_base_url="http://127.0.0.1:8765")
    definition = next(d for d in on.definitions if d.name == "web.search")
    assert definition.enabled is True
    # docs/THREAT_MODEL.md: search results are external, uncontrolled content.
    assert definition.operation_content_trust == {"search": "untrusted_external"}
