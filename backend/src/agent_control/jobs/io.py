"""What a research job uses from the outside, on the laptop: the local model, the web search tool and the web read tool.

The search and the read are the assistant's own `web.search` and `http.request` adapters, so a page is fetched with the same checks as everywhere
else (a public website only, every redirect checked, a size limit, no credentials), and the model is the one provider the Operator uses. A page is
reduced to its visible text here, because the adapter returns the markup and a model reading a page needs the words.
"""

from __future__ import annotations

import html.parser
from typing import Any

from pydantic import BaseModel

from agent_control.jobs.research import Page, ResearchIO
from agent_control.schemas import Capability, ToolCallRequest, ToolResultStatus
from agent_control.tools.http_request import _SKIPPED_TAGS

#: The body of a page the read tool is asked for. The model sees PAGE_CHARS of the visible text; the rest of a long page is not needed for a note.
BODY_CHARS = 400_000
_JOB_TASK = "job"  # the adapters ask for a task id to put in their audit lines; a job is not a task, so this names what it is


class _Visible(html.parser.HTMLParser):
    """The title and the visible text of a page, one paragraph-ish run per line, without scripts, styles and navigation chrome."""

    _BLOCK = {"p", "div", "li", "br", "h1", "h2", "h3", "h4", "tr", "section", "article"}
    _CHROME = {"nav", "footer", "header", "aside", "form"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.parts: list[str] = []
        self._in_title = False
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title":
            self._in_title = True
        elif tag in _SKIPPED_TAGS or tag in self._CHROME:
            self._skip += tag != "head"
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        elif (tag in _SKIPPED_TAGS or tag in self._CHROME) and tag != "head" and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        elif not self._skip and data.strip():
            self.parts.append(data.strip() + " ")


def visible_text(markup: str) -> Page:
    parser = _Visible()
    try:
        parser.feed(markup)
        parser.close()
    except Exception:  # noqa: BLE001 - a page that will not parse is read as far as it did
        pass
    lines = [" ".join(line.split()) for line in "".join(parser.parts).split("\n")]
    return Page(title=" ".join(parser.title.split()), text="\n".join(line for line in lines if len(line) > 25))


def build_io(provider: Any, search_adapter: Any, http_adapter: Any) -> ResearchIO:
    async def think(system: str, user: str, schema: type[BaseModel]) -> BaseModel:
        return await provider.generate_structured(system, user, schema, temperature=0.1)

    async def write(system: str, user: str) -> str:
        return str(await provider.generate_text(system, user))

    async def search(query: str, count: int) -> list[dict[str, str]]:
        result = await search_adapter.execute(ToolCallRequest(
            task_id=_JOB_TASK, tool_name="web.search", capability=Capability.NETWORK_HTTP,
            input={"operation": "search", "query": query, "max_results": count},
        ))
        if result.status != ToolResultStatus.SUCCEEDED:
            raise RuntimeError(f"web search failed: {result.error_message}")
        return [dict(item) for item in (result.output or {}).get("results", []) if isinstance(item, dict)]

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

    return ResearchIO(think=think, write=write, search=search, fetch=fetch)
