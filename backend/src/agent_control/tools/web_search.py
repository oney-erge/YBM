"""Search the web and get results back as data.

The only route before this was `browser.open`'s search operation: drive real
Chrome to a Bing results page and read the rendered DOM. That works, but it
needs `browser.control` (critical risk) for what is a read, costs a browser
launch per query, and breaks on consent walls and layout changes. For "search
online and give me a concise analysis" it is the weakest link in the chain.

This returns title/url/snippet triples the Operator can reason over directly,
then follow up on the two or three worth reading with `http.request`. The
browser path stays for pages that genuinely need interaction.

Three backends, because the right one depends on what the operator has:

* ``duckduckgo`` (default) - no API key, no account. Parses the HTML endpoint
  with stdlib HTMLParser rather than a regex, so a markup change degrades to
  fewer results instead of silently wrong ones.
* ``brave`` - a real JSON API, needs a key in ``api_key_env``.
* ``searxng`` - point ``base_url`` at a self-hosted instance; nothing leaves
  the network the operator controls.
"""

from __future__ import annotations

from html import unescape
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, quote_plus, urlparse

import httpx

from agent_control.config import WebSearchAdapterConfig
from agent_control.config_sync import read_env_value
from agent_control.schemas import Capability, ToolCallRequest, ToolCallResult, ToolResultStatus
from agent_control.tools.contracts import WebSearchInput, WebSearchOutput
from agent_control.tools.spec import (
    UNTRUSTED_EXTERNAL,
    Adapters,
    Definitions,
    RegistryDeps,
    ToolDefinition,
    capability_enabled,
    failed_result,
    same_output_schema,
)


class _DuckDuckGoParser(HTMLParser):
    """Pulls result links and snippets out of DuckDuckGo's HTML endpoint.

    A parser rather than a regex on purpose: when the markup changes, this
    yields fewer (or zero) results, which the caller reports honestly. A
    regex over HTML tends to keep matching *something* and hand back
    plausible-looking nonsense.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[dict[str, str]] = []
        self._in_title = False
        self._in_snippet = False
        self._href: str | None = None
        self._title_parts: list[str] = []
        self._snippet_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        classes = (attributes.get("class") or "").split()
        if tag == "a" and "result__a" in classes:
            self._in_title = True
            self._href = attributes.get("href")
            self._title_parts = []
        elif tag == "a" and "result__snippet" in classes:
            self._in_snippet = True
            self._snippet_parts = []

    def handle_endtag(self, tag: str) -> None:
        if tag != "a":
            return
        if self._in_title:
            self._in_title = False
            url = _clean_ddg_url(self._href or "")
            title = unescape("".join(self._title_parts)).strip()
            if url and title:
                self.results.append({"title": title, "url": url, "snippet": ""})
        elif self._in_snippet:
            self._in_snippet = False
            snippet = unescape("".join(self._snippet_parts)).strip()
            if self.results and not self.results[-1]["snippet"]:
                self.results[-1]["snippet"] = snippet

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title_parts.append(data)
        elif self._in_snippet:
            self._snippet_parts.append(data)


def _clean_ddg_url(href: str) -> str:
    """DuckDuckGo wraps results in /l/?uddg=<encoded>. Hand back the real
    target so the Operator can fetch it, not a redirector."""
    if not href:
        return ""
    if href.startswith("//"):
        href = f"https:{href}"
    parsed = urlparse(href)
    if "duckduckgo.com" in parsed.netloc and parsed.path.startswith("/l"):
        target = parse_qs(parsed.query).get("uddg")
        if target:
            return target[0]
    return href if href.startswith("http") else ""


class WebSearchAdapter:
    def __init__(self, config: WebSearchAdapterConfig) -> None:
        self.config = config

    async def execute(self, request: ToolCallRequest) -> ToolCallResult:
        query = str(request.input.get("query") or "").strip()
        if not query:
            return failed_result(request, "web.search requires a non-empty 'query'")
        limit = min(int(request.input.get("max_results") or self.config.max_results), self.config.max_results)
        try:
            if self.config.provider == "brave":
                results = await self._brave(query, limit)
            elif self.config.provider == "searxng":
                results = await self._searxng(query, limit)
            else:
                results = await self._duckduckgo(query, limit)
        except Exception as exc:  # noqa: BLE001 - reported to the operator
            return failed_result(request, f"web search failed: {exc}")
        return ToolCallResult(
            request_id=request.id,
            status=ToolResultStatus.SUCCEEDED,
            output={
                "operation": "search",
                "query": query,
                "provider": self.config.provider,
                "results": results[:limit],
                "summary": (
                    f"{len(results[:limit])} result(s) for {query!r} via {self.config.provider}."
                    if results
                    else f"No results for {query!r} via {self.config.provider}."
                ),
            },
        )

    async def _get(self, url: str, **kwargs: Any) -> httpx.Response:
        async with httpx.AsyncClient(
            timeout=self.config.timeout_seconds,
            follow_redirects=True,
            headers={"User-Agent": self.config.user_agent},
        ) as client:
            response = await client.get(url, **kwargs)
            response.raise_for_status()
            return response

    async def _duckduckgo(self, query: str, limit: int) -> list[dict[str, str]]:
        response = await self._get(f"https://html.duckduckgo.com/html/?q={quote_plus(query)}")
        parser = _DuckDuckGoParser()
        parser.feed(response.text)
        return parser.results[:limit]

    async def _brave(self, query: str, limit: int) -> list[dict[str, str]]:
        key = read_env_value(self.config.api_key_env or "")
        if not key:
            raise ValueError(
                f"provider 'brave' needs an API key in {self.config.api_key_env or '<api_key_env unset>'}"
            )
        response = await self._get(
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": query, "count": limit},
            headers={"X-Subscription-Token": key, "Accept": "application/json"},
        )
        payload = response.json()
        return [
            {
                "title": item.get("title") or "",
                "url": item.get("url") or "",
                "snippet": item.get("description") or "",
            }
            for item in (payload.get("web") or {}).get("results", [])
        ]

    async def _searxng(self, query: str, limit: int) -> list[dict[str, str]]:
        base = (self.config.base_url or "").rstrip("/")
        if not base:
            raise ValueError("provider 'searxng' needs adapters.web_search.base_url")
        response = await self._get(f"{base}/search", params={"q": query, "format": "json"})
        payload = response.json()
        return [
            {
                "title": item.get("title") or "",
                "url": item.get("url") or "",
                "snippet": item.get("content") or "",
            }
            for item in payload.get("results", [])[:limit]
        ]


def register(deps: RegistryDeps, definitions: Definitions, adapters: Adapters) -> None:
    settings = deps.settings
    config = settings.adapters.web_search
    # Shares NETWORK_HTTP: this is an outbound read, governed by the same
    # capability an operator already reasons about for http.request, rather
    # than a new switch that means almost the same thing.
    enabled = config.enabled and capability_enabled(settings, Capability.NETWORK_HTTP)
    definitions.append(
        ToolDefinition(
            name="web.search",
            capability=Capability.NETWORK_HTTP,
            enabled=enabled,
            description=(
                "search the web and get back title/url/snippet results as data "
                f"(provider: {config.provider}); follow up with http.request to read a page"
            ),
            operations=("search",),
            operation_schemas={"search": WebSearchInput},
            output_schema=WebSearchOutput,
            operation_output_schemas=same_output_schema(("search",), WebSearchOutput),
            operation_content_trust={"search": UNTRUSTED_EXTERNAL},
            examples=[{"operation": "search", "query": "site reliability engineering postmortem template", "max_results": 5}],
        )
    )
    if enabled:
        adapters["web.search"] = WebSearchAdapter(config)
