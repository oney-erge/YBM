"""What a research job uses from the outside, on the laptop: the local model, a way to search the web, and a way to read a page.

The page read is the assistant's own `http.request` adapter, so a page is fetched with the same checks as everywhere else (a public website only,
every redirect checked, a size limit, no credentials), and the model is the one provider the Operator uses. A page is reduced to its visible text
here, because the adapter returns the markup and a model reading a page needs the words.

Searching is harder than it looks. Found 11/14, the first research job found no pages at all: the default engine (DuckDuckGo's HTML page) answers
this connection with a human check, and Bing's plain HTML answers with results for the first word of the query only ("heat pump installation
cost Chicago" returned films). A real browser gets real results, so the search order is: the person's Chrome, when it is running with DevTools
(its Bing page is read as a person would read it), then the `web.search` adapter as configured (Brave or SearXNG with a key or an instance, or
DuckDuckGo where it is not refused). When none answers, the job says why instead of saying it found nothing.
"""

from __future__ import annotations

import asyncio
import base64
import html.parser
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import parse_qs, quote_plus, urlparse

from pydantic import BaseModel

from agent_control.config import BrowserAdapterConfig
from agent_control.jobs.research import Page, ResearchIO
from agent_control.schemas import Capability, ToolCallRequest, ToolResultStatus
from agent_control.tools.http_request import _SKIPPED_TAGS

#: The body of a page the read tool is asked for. The model sees PAGE_CHARS of the visible text; the rest of a long page is not needed for a note.
BODY_CHARS = 400_000
_JOB_TASK = "job"  # the adapters ask for a task id to put in their audit lines; a job is not a task, so this names what it is

BrowserSearch = Callable[[str, int], Awaitable[list[dict[str, str]]]]


class _Visible(html.parser.HTMLParser):
    """The title and the visible text of a page, one paragraph-ish run per line, without scripts, styles and navigation chrome."""

    _BLOCK = {"p", "div", "li", "br", "h1", "h2", "h3", "h4", "tr", "section", "article"}
    _CHROME = frozenset({"nav", "footer", "header", "aside", "form"})

    def __init__(self, *, chrome: bool = True) -> None:
        super().__init__(convert_charrefs=True)
        self._chrome = self._CHROME if chrome else frozenset()
        self.title = ""
        self.parts: list[str] = []
        self._in_title = False
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title":
            self._in_title = True
        elif tag in _SKIPPED_TAGS or tag in self._chrome:
            self._skip += tag != "head"
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        elif (tag in _SKIPPED_TAGS or tag in self._chrome) and tag != "head" and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        elif not self._skip and data.strip():
            self.parts.append(data.strip() + " ")


#: A page whose reduced text is shorter than this is read again with only scripts and styles taken out, and that reading is used when it is much fuller (see `visible_text`).
MIN_PAGE_TEXT = 500


def visible_text(markup: str) -> Page:
    """The title and the words of a page. Navigation, footers and forms are left out, unless that leaves almost nothing.

    Found live on 11/14: 16 of 36 pages of one research job read as empty, though each had its article in the HTML. A site that wraps its whole
    page in a `<form>` (ASP.NET), or whose `<header>` or `<aside>` is never closed, put the article "inside" a part the reducer drops. The page is
    then read again without dropping any part, and the fuller reading is used.
    """
    page = _reduce(markup, chrome=True)
    if len(page.text) >= MIN_PAGE_TEXT:
        return page
    fuller = _reduce(markup, chrome=False)
    # Three times as much, and enough to be a page: a short page with a menu stays without its menu.
    return fuller if len(fuller.text) >= max(MIN_PAGE_TEXT, 3 * len(page.text)) else page


def _reduce(markup: str, *, chrome: bool) -> Page:
    parser = _Visible(chrome=chrome)
    try:
        parser.feed(markup)
        parser.close()
    except Exception:  # noqa: BLE001 - a page that will not parse is read as far as it did
        pass
    lines = [" ".join(line.split()) for line in "".join(parser.parts).split("\n")]
    return Page(title=" ".join(parser.title.split()), text="\n".join(line for line in lines if len(line) > 25))


# --- searching through the person's own Chrome ---------------------------------------------------------------------------------------------------

#: Read the organic results from a Bing results page the way a person sees it (sponsored results are a different list item, so they are left out).
_RESULTS_SCRIPT = (
    "(() => [...document.querySelectorAll('li.b_algo')].map(li => {"
    " const a = li.querySelector('h2 a'); const p = li.querySelector('.b_caption p, p');"
    " return a ? {title: a.innerText.trim(), href: a.href, snippet: p ? p.innerText.trim() : ''} : null;"
    "}).filter(Boolean))()"
)


def real_url(href: str) -> str:
    """Bing wraps a result in a redirect (`bing.com/ck/a?...&u=a1<base64 of the address>`); the address is what a page read needs."""
    host = urlparse(href).netloc.lower()
    if not host.endswith("bing.com"):
        return href
    encoded = parse_qs(urlparse(href).query).get("u", [""])[0]
    if encoded.startswith("a1"):
        payload = encoded[2:]
        try:
            return base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return ""
    return ""  # a Bing address that is not a result (a vertical, a feedback link) is not a page to read


def parse_results(raw: Any, count: int) -> list[dict[str, str]]:
    """The results a results page gave, as title/url/snippet, with the redirects removed and no address twice."""
    results, seen = [], set()
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        url = real_url(str(item.get("href") or ""))
        if not url.startswith(("http://", "https://")) or url in seen:
            continue
        seen.add(url)
        results.append({"title": " ".join(str(item.get("title") or url).split())[:160], "url": url, "snippet": " ".join(str(item.get("snippet") or "").split())[:300]})
    return results[:count]


def make_browser_search(config: BrowserAdapterConfig) -> BrowserSearch | None:
    """Search through Chrome with DevTools. When Chrome is not running, the assistant's own profile is started, minimized (`launch_if_missing`, on by default).

    A job that has to search cannot wait for a person to open a browser: after a restart, or the night after they approved it, nobody has. Starting the
    browser the web tool already uses, out of the way, is within what they agreed to when they approved a job that reads the web.
    """
    if not config.enabled:
        return None
    from agent_control.tools.browser import ChromeDevToolsClient

    def search_sync(query: str, count: int) -> list[dict[str, str]]:
        client = ChromeDevToolsClient(config)
        client.ensure_available(minimized=True)  # raises when Chrome cannot be reached or started
        target = client.open_url(f"https://www.bing.com/search?q={quote_plus(query)}&setlang=en-US", new_tab=True)
        try:
            for _ in range(3):  # the results may not be in the page yet
                client.wait(2.0)
                results = parse_results(client._evaluate_value(target, _RESULTS_SCRIPT), count)
                if results:
                    return results
            return []
        finally:
            try:
                client.close_tab(target)
            except Exception:  # noqa: BLE001 - a tab that will not close is not a failed search
                pass

    async def search(query: str, count: int) -> list[dict[str, str]]:
        return await asyncio.to_thread(search_sync, query, count)

    return search


# --- the whole of it ---------------------------------------------------------------------------------------------------------------------------


def build_io(
    provider: Any, search_adapter: Any, http_adapter: Any, *, browser_search: BrowserSearch | None = None, list_folder: Any = None, run_monitor: Any = None,
) -> ResearchIO:
    async def think(system: str, user: str, schema: type[BaseModel]) -> BaseModel:
        return await provider.generate_structured(system, user, schema, temperature=0.1)

    async def write(system: str, user: str) -> str:
        return str(await provider.generate_text(system, user))

    async def adapter_search(query: str, count: int) -> list[dict[str, str]]:
        result = await search_adapter.execute(ToolCallRequest(
            task_id=_JOB_TASK, tool_name="web.search", capability=Capability.NETWORK_HTTP,
            input={"operation": "search", "query": query, "max_results": count},
        ))
        if result.status != ToolResultStatus.SUCCEEDED:
            raise RuntimeError(result.error_message or "the web search tool failed")
        return [dict(item) for item in (result.output or {}).get("results", []) if isinstance(item, dict)]

    async def search(query: str, count: int) -> list[dict[str, str]]:
        reasons = []
        for name, engine in (("Chrome", browser_search), ("the web search tool", adapter_search)):
            if engine is None:
                continue
            try:
                results = await engine(query, count)
            except Exception as exc:  # noqa: BLE001 - the next engine is tried, and the reasons are kept for the person
                reasons.append(f"{name}: {str(exc)[:200]}")
                continue
            if results:
                return results
            reasons.append(f"{name}: no results")
        raise RuntimeError("no search engine gave results (" + "; ".join(reasons) + ")")

    async def fetch(url: str) -> Page | None:
        result = await http_adapter.execute(ToolCallRequest(
            task_id=_JOB_TASK, tool_name="http.request", capability=Capability.NETWORK_HTTP, timeout_seconds=30,
            input={"operation": "request", "url": url, "method": "GET", "parse_json": False, "max_response_chars": BODY_CHARS, "timeout_seconds": 20},
        ))
        output = result.output or {}
        if result.status != ToolResultStatus.SUCCEEDED or not output.get("ok"):
            return None  # a page that would not open, was blocked or is not a public website is skipped, not an error of the job
        content_type = str((output.get("headers") or {}).get("content-type", "")).lower()
        if content_type and "html" not in content_type and "text" not in content_type:
            return None
        return visible_text(str(output.get("text") or ""))

    async def probe(op: str, args: dict[str, Any]) -> Any:
        from agent_control.diagnose import run_probe

        return await run_probe(op, **args)

    return ResearchIO(think=think, write=write, search=search, fetch=fetch, probe=probe, list_folder=list_folder, run_monitor=run_monitor)

