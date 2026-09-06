from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
from typing import Any
from urllib import error, parse, request as urlrequest

try:
    import websocket
except ImportError:  # pragma: no cover - exercised only when optional dependency is missing.
    websocket = None

from agent_control.config import BrowserAdapterConfig
from agent_control.schemas import Capability, RiskLevel, ToolCallRequest, ToolCallResult, ToolResultStatus
from agent_control.tools.contracts import (
    BrowserCheckPageUpdateInput,
    BrowserClickInput,
    BrowserCloseTabInput,
    BrowserExtractPageStateInput,
    BrowserFillFormInput,
    BrowserFillFormStepInput,
    BrowserInspectTabsInput,
    BrowserNavigateInput,
    BrowserOpenInput,
    BrowserResearchInput,
    BrowserResearchPagesInput,
    BrowserScreenshotInput,
    BrowserSearchInput,
    BrowserSummarizePageInput,
    BrowserToolOutput,
)
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


logger = logging.getLogger(__name__)


class BrowserAdapter:
    """Chrome DevTools Protocol adapter for local browser open, search, inspect, and control."""

    def __init__(self, config: BrowserAdapterConfig) -> None:
        self.config = config

    async def execute(self, request: ToolCallRequest) -> ToolCallResult:
        if not self.config.enabled:
            return failed_result(request, "browser adapter is disabled")
        if websocket is None:
            return failed_result(request, "browser adapter requires the websocket-client package")

        try:
            output = await asyncio.to_thread(self._execute_sync, request)
        except Exception as exc:
            return failed_result(request, f"browser operation failed: {exc}")
        return ToolCallResult(request_id=request.id, status=ToolResultStatus.SUCCEEDED, output=output)

    def _execute_sync(self, request: ToolCallRequest) -> dict[str, Any]:
        operation = str(request.input.get("operation") or _default_operation(request.tool_name))
        client = ChromeDevToolsClient(self.config)
        client.ensure_available()

        if operation == "open":
            output = self._open(client, request)
        elif operation == "search":
            output = self._search(client, request)
        elif operation == "research":
            output = self._research(client, request)
        elif operation == "inspect_tabs":
            output = self._inspect_tabs(client, request)
        elif operation == "screenshot":
            output = self._screenshot(client, request)
        elif operation == "summarize_page":
            output = self._summarize_page(client, request)
        elif operation == "research_pages":
            output = self._research_pages(client, request)
        elif operation == "navigate":
            output = self._navigate(client, request)
        elif operation == "close_tab":
            output = self._close_tab(client, request)
        elif operation == "click":
            output = self._click(client, request)
        elif operation == "fill_form":
            output = self._fill_form(client, request)
        elif operation == "check_page_update":
            output = self._check_page_update(client, request)
        elif operation == "extract_page_state":
            output = self._extract_page_state(client, request)
        elif operation == "fill_form_step":
            output = self._fill_form(client, request)
        elif operation == "chain":
            output = self._chain(client, request)
        else:
            raise ValueError(f"unsupported browser operation: {operation}")

        output["operation"] = operation
        output.setdefault("terminal_output", [_terminal_output(operation, output)])
        return output

    def _open(self, client: "ChromeDevToolsClient", request: ToolCallRequest) -> dict[str, Any]:
        input_payload = request.input
        url = _target_url(
            input_payload.get("url"),
            input_payload.get("query"),
            input_payload.get("objective"),
            search_template=self.config.search_url_template,
            force_search=False,
        )
        target = client.open_url(url, new_tab=bool(input_payload.get("new_tab", True)))
        client.wait(float(input_payload.get("wait_seconds") or self.config.default_wait_seconds))
        return self._summarize_target(client, target)

    def _search(self, client: "ChromeDevToolsClient", request: ToolCallRequest) -> dict[str, Any]:
        input_payload = request.input
        query = str(input_payload.get("query") or input_payload.get("objective") or "").strip()
        if not query:
            raise ValueError("query or objective is required for browser search")
        target = client.open_url(_search_url(self.config.search_url_template, query), new_tab=True)
        client.wait(float(input_payload.get("wait_seconds") or self.config.default_wait_seconds))
        output = self._summarize_target(client, target)
        if bool(input_payload.get("open_first_result", False)):
            output = self._open_first_result(client, target, output)
        return output

    def _research(self, client: "ChromeDevToolsClient", request: ToolCallRequest) -> dict[str, Any]:
        input_payload = request.input
        objective = str(input_payload.get("objective") or "").strip()
        url = str(input_payload.get("url") or _first_url(objective) or "").strip()
        query = str(input_payload.get("query") or "").strip()
        if url:
            target = client.open_url(_normalize_url(url), new_tab=True)
        else:
            query = query or _query_from_objective(objective)
            if not query:
                raise ValueError("objective, url, or query is required for browser research")
            target = client.open_url(_search_url(self.config.search_url_template, query), new_tab=True)
        client.wait(float(input_payload.get("wait_seconds") or self.config.default_wait_seconds))
        output = self._summarize_target(client, target)

        open_first = input_payload.get("open_first_result")
        if open_first is None:
            open_first = _objective_wants_first_result(objective)
        if open_first and not url:
            output = self._open_first_result(client, target, output)

        if bool(input_payload.get("screenshot", False)) or _objective_wants_screenshot(objective):
            screenshot = client.screenshot(target, self._screenshot_path(request.task_id, None), full_page=True)
            output["screenshot_path"] = screenshot
            output["screenshot_uri"] = Path(screenshot).resolve().as_uri()
        return output

    def _inspect_tabs(self, client: "ChromeDevToolsClient", request: ToolCallRequest) -> dict[str, Any]:
        include_text = bool(request.input.get("include_text", False))
        max_tabs = int(request.input.get("max_tabs") or 8)
        tabs = []
        for target in client.page_targets()[:max_tabs]:
            tab = _tab_summary(target)
            if include_text and target.web_socket_debugger_url:
                tab["page"] = client.page_summary(target, max_chars=min(self.config.max_summary_chars, 2500))
            tabs.append(tab)
        return {
            "browser_state": {"tab_count": len(tabs), "remote_debugging": client.base_url},
            "tabs": tabs,
            "summary": _tabs_summary(tabs),
        }

    def _screenshot(self, client: "ChromeDevToolsClient", request: ToolCallRequest) -> dict[str, Any]:
        input_payload = request.input
        target = self._target_from_input(client, input_payload)
        if input_payload.get("url"):
            target = client.open_url(_normalize_url(str(input_payload["url"])), new_tab=True)
        client.wait(float(input_payload.get("wait_seconds") or self.config.default_wait_seconds))
        summary = self._summarize_target(client, target)
        screenshot = client.screenshot(
            target,
            self._screenshot_path(request.task_id, input_payload.get("filename")),
            full_page=bool(input_payload.get("full_page", True)),
        )
        return {
            **summary,
            "screenshot_path": screenshot,
            "screenshot_uri": Path(screenshot).resolve().as_uri(),
        }

    def _summarize_page(self, client: "ChromeDevToolsClient", request: ToolCallRequest) -> dict[str, Any]:
        input_payload = request.input
        target = self._target_from_input(client, input_payload)
        if input_payload.get("url"):
            target = client.open_url(_normalize_url(str(input_payload["url"])), new_tab=True)
        client.wait(float(input_payload.get("wait_seconds") or self.config.default_wait_seconds))
        return self._summarize_target(client, target)

    def _research_pages(self, client: "ChromeDevToolsClient", request: ToolCallRequest) -> dict[str, Any]:
        input_payload = request.input
        query = str(input_payload.get("query") or input_payload.get("objective") or "").strip()
        if not query:
            raise ValueError("query or objective is required for research_pages")
        page_limit = int(input_payload.get("page_limit") or 10)
        target = client.open_url(_search_url(self.config.search_url_template, query), new_tab=True)
        client.wait(float(input_payload.get("wait_seconds") or self.config.default_wait_seconds))
        search_output = self._summarize_target(client, target)
        links = search_output.get("links") or []
        visited_urls: list[str] = []
        page_summaries: list[dict[str, Any]] = []
        for link in _external_links(links, search_output.get("url") or "")[:page_limit]:
            client.navigate(target, link["href"])
            client.wait(float(input_payload.get("wait_seconds") or self.config.default_wait_seconds))
            refreshed = next((item for item in client.page_targets() if item.id == target.id), None)
            if refreshed is not None:
                target = refreshed
            page = self._summarize_target(client, target)
            visited_urls.append(str(page.get("url") or link["href"]))
            page_summaries.append(
                {
                    "url": page.get("url") or link["href"],
                    "title": page.get("page_title"),
                    "summary": page.get("summary"),
                }
            )
        summary = f"Visited {len(visited_urls)} page(s) for {query!r}."
        return {
            "browser_state": {"remote_debugging": client.base_url, "page_limit": page_limit},
            "browser_url": visited_urls[-1] if visited_urls else search_output.get("browser_url"),
            "url": visited_urls[-1] if visited_urls else search_output.get("url"),
            "page_title": page_summaries[-1].get("title") if page_summaries else search_output.get("page_title"),
            "summary": summary,
            "links": links,
            "visited_urls": visited_urls,
            "page_summaries": page_summaries,
        }

    def _navigate(self, client: "ChromeDevToolsClient", request: ToolCallRequest) -> dict[str, Any]:
        input_payload = request.input
        target = self._target_from_input(client, input_payload)
        client.navigate(target, _normalize_url(str(input_payload["url"])))
        client.wait(float(input_payload.get("wait_seconds") or self.config.default_wait_seconds))
        return self._summarize_target(client, target)

    def _close_tab(self, client: "ChromeDevToolsClient", request: ToolCallRequest) -> dict[str, Any]:
        target = self._target_from_input(client, request.input, required=False)
        if target is None:
            return {
                "browser_state": {"closed": False, "reason": "no matching tab"},
                "summary": "No matching Chrome tab was found to close.",
            }
        client.close_tab(target)
        remaining = [_tab_summary(item) for item in client.page_targets()]
        return {
            "browser_state": {"closed": True, "closed_tab": _tab_summary(target), "tab_count": len(remaining)},
            "tabs": remaining,
            "summary": f"Closed tab: {target.title or target.url or target.id}",
        }

    def _click(self, client: "ChromeDevToolsClient", request: ToolCallRequest) -> dict[str, Any]:
        input_payload = request.input
        target = self._target_from_input(client, input_payload)
        selector = str(input_payload.get("selector") or "").strip()
        text = str(input_payload.get("text") or "").strip()
        if not selector and not text:
            raise ValueError("selector or text is required for browser click")
        clicked = client.click(target, selector=selector or None, text=text or None)
        client.wait(float(input_payload.get("wait_seconds") or self.config.default_wait_seconds))
        output = self._summarize_target(client, target)
        output["browser_state"] = {**(output.get("browser_state") or {}), "clicked": clicked}
        return output

    def _fill_form(self, client: "ChromeDevToolsClient", request: ToolCallRequest) -> dict[str, Any]:
        input_payload = request.input
        target = self._target_from_input(client, input_payload)
        fields = input_payload.get("fields") if isinstance(input_payload.get("fields"), dict) else {}
        if not fields:
            raise ValueError("fields are required for browser fill_form")
        filled = client.fill_form(
            target,
            {str(key): str(value) for key, value in fields.items()},
            submit=bool(input_payload.get("submit", False)),
            submit_selector=input_payload.get("submit_selector"),
        )
        client.wait(float(input_payload.get("wait_seconds") or self.config.default_wait_seconds))
        output = self._summarize_target(client, target)
        output["browser_state"] = {**(output.get("browser_state") or {}), "filled_fields": filled}
        filled_items = filled.get("filled") if isinstance(filled, dict) else None
        if isinstance(filled_items, list) and not filled_items:
            summary = str(output.get("summary") or "")
            url = str(output.get("url") or output.get("browser_url") or "")
            if _looks_like_login_or_auth_page(summary, url):
                output["browser_state"] = {
                    **(output.get("browser_state") or {}),
                    "blocked_reason": "login_required",
                }
                output["summary"] = (
                    "The page appears to require login or account access before the requested form/prompt can be filled. "
                    "No fields were changed."
                )
        return output

    def _chain(self, client: "ChromeDevToolsClient", request: ToolCallRequest) -> dict[str, Any]:
        """Execute a sequence of browser sub-operations, passing page state between steps."""
        steps = request.input.get("steps") if isinstance(request.input.get("steps"), list) else []
        if not steps:
            raise ValueError("steps list is required for browser chain operation")

        last_output: dict[str, Any] = {}
        all_summaries: list[str] = []

        for i, step in enumerate(steps):
            if not isinstance(step, dict):
                continue
            operation = str(step.get("operation") or "open")
            step_input = {**request.input, **step, "operation": operation}
            # Remove the steps list so sub-operations don't recurse
            step_input.pop("steps", None)
            sub_request = request.model_copy(update={"input": step_input})

            if operation == "open":
                step_output = self._open(client, sub_request)
            elif operation == "navigate":
                step_output = self._navigate(client, sub_request)
            elif operation == "click":
                step_output = self._click(client, sub_request)
            elif operation in {"fill_form", "fill_form_step"}:
                step_output = self._fill_form(client, sub_request)
            elif operation == "extract_page_state":
                step_output = self._extract_page_state(client, sub_request)
            elif operation == "summarize_page":
                step_output = self._summarize_page(client, sub_request)
            elif operation == "screenshot":
                step_output = self._screenshot(client, sub_request)
            else:
                step_output = {"summary": f"Skipped unsupported chain sub-operation: {operation}"}

            last_output = step_output
            step_summary = str(step_output.get("summary") or "")
            if step_summary:
                all_summaries.append(f"Step {i + 1} ({operation}): {step_summary}")

            # Stop chain early if login/auth wall detected
            blocked = (step_output.get("browser_state") or {}).get("blocked_reason")
            if blocked == "login_required":
                url = str(step_output.get("url") or "")
                last_output["summary"] = (
                    f"I opened {url or 'the page'} but it requires login before I can continue. "
                    "Let me know how to proceed - you can log in manually and ask me to retry, "
                    "or I can try a different approach."
                )
                last_output["chain_stopped_at"] = i
                break

        last_output["chain_steps_count"] = len(steps)
        if all_summaries:
            last_output["chain_summary"] = "\n".join(all_summaries)
        return last_output

    def _check_page_update(self, client: "ChromeDevToolsClient", request: ToolCallRequest) -> dict[str, Any]:
        input_payload = request.input
        target = self._target_from_input(client, input_payload, required=False)
        if input_payload.get("url"):
            target = client.open_url(_normalize_url(str(input_payload["url"])), new_tab=True)
        if target is None:
            raise ValueError("url or matching tab is required for check_page_update")
        client.wait(float(input_payload.get("wait_seconds") or self.config.default_wait_seconds))
        output = self._summarize_target(client, target)
        previous = str(input_payload.get("previous_observation") or "").strip()
        current = str(output.get("summary") or "")
        markers = _update_markers(current)
        changed = bool(previous and previous not in current)
        output["browser_state"] = {
            **(output.get("browser_state") or {}),
            "update_check": {"changed_from_previous": changed, "markers": markers},
        }
        output["summary"] = (
            f"Checked page for updates. Markers: {', '.join(markers[:8]) or 'none found'}. "
            f"Changed from previous observation: {changed}."
        )
        return output

    def _extract_page_state(self, client: "ChromeDevToolsClient", request: ToolCallRequest) -> dict[str, Any]:
        output = self._summarize_page(client, request)
        page = client.page_summary(
            self._target_from_input(client, request.input),
            max_chars=self.config.max_summary_chars,
        )
        forms = page.get("forms") if isinstance(page.get("forms"), list) else []
        output["forms"] = forms
        output["browser_state"] = {**(output.get("browser_state") or {}), "forms_detected": len(forms)}
        output["summary"] = f"Detected {len(forms)} form(s) on {output.get('url') or 'the page'}."
        return output

    def _summarize_target(self, client: "ChromeDevToolsClient", target: "BrowserTarget") -> dict[str, Any]:
        page = client.page_summary(target, max_chars=self.config.max_summary_chars)
        summary = _page_summary_text(page)
        return {
            "browser_state": {
                "tab_id": target.id,
                "remote_debugging": client.base_url,
                "note": _remote_debugging_note(),
            },
            "browser_url": page.get("url") or target.url,
            "url": page.get("url") or target.url,
            "page_title": page.get("title") or target.title,
            "summary": summary,
            "links": page.get("links") or [],
        }

    def _open_first_result(
        self,
        client: "ChromeDevToolsClient",
        target: "BrowserTarget",
        output: dict[str, Any],
    ) -> dict[str, Any]:
        first = _first_external_link(output.get("links") or [], output.get("url") or "")
        if not first:
            output["summary"] = f"{output.get('summary') or ''}\n\nNo external search result link was found."
            return output
        client.navigate(target, first["href"])
        client.wait(self.config.default_wait_seconds)
        opened = self._summarize_target(client, target)
        opened["browser_state"] = {
            **(opened.get("browser_state") or {}),
            "opened_first_result": first,
        }
        return opened

    def _target_from_input(
        self,
        client: "ChromeDevToolsClient",
        input_payload: dict[str, Any],
        *,
        required: bool = True,
    ) -> "BrowserTarget | None":
        tab_id = str(input_payload.get("tab_id") or "").strip()
        url_contains = str(input_payload.get("url_contains") or "").strip().lower()
        title_contains = str(input_payload.get("title_contains") or "").strip().lower()
        targets = client.page_targets()
        for target in targets:
            if tab_id and target.id == tab_id:
                return target
            if url_contains and url_contains in target.url.lower():
                return target
            if title_contains and title_contains in target.title.lower():
                return target
        if not tab_id and not url_contains and not title_contains and targets:
            return targets[0]
        if required:
            raise ValueError("no matching browser tab found")
        return None

    def _screenshot_path(self, task_id: str, filename: Any) -> Path:
        root = Path(self.config.screenshot_dir).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        if filename:
            safe = _safe_png_filename(str(filename))
        else:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            safe = f"{_safe_stem(task_id)}_{stamp}.png"
        path = (root / safe).resolve()
        if root != path and root not in path.parents:
            raise ValueError("screenshot path escaped configured directory")
        return path


@dataclass(frozen=True)
class BrowserTarget:
    id: str
    type: str
    title: str
    url: str
    web_socket_debugger_url: str | None = None


class ChromeDevToolsClient:
    def __init__(self, config: BrowserAdapterConfig) -> None:
        self.config = config
        self.base_url = f"http://{config.host}:{config.remote_debugging_port}"
        self._message_id = 0

    def ensure_available(self) -> None:
        if self._version() is not None:
            return
        if not self.config.launch_if_missing:
            raise RuntimeError(f"Chrome DevTools is not available at {self.base_url}")
        chrome_path = _chrome_path(self.config.chrome_path)
        user_data_dir = Path(self.config.user_data_dir).expanduser().resolve()
        user_data_dir.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(
            [
                chrome_path,
                f"--remote-debugging-port={self.config.remote_debugging_port}",
                "--remote-allow-origins=*",
                f"--user-data-dir={user_data_dir}",
                "--no-first-run",
                "--no-default-browser-check",
                "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.time() + self.config.startup_timeout_seconds
        while time.time() < deadline:
            if self._version() is not None:
                return
            time.sleep(0.25)
        raise RuntimeError(f"Chrome launched but DevTools did not become ready at {self.base_url}")

    def page_targets(self) -> list[BrowserTarget]:
        payload = self._json("/json/list")
        targets = []
        for item in payload if isinstance(payload, list) else []:
            if item.get("type") != "page":
                continue
            targets.append(
                BrowserTarget(
                    id=str(item.get("id") or ""),
                    type=str(item.get("type") or ""),
                    title=str(item.get("title") or ""),
                    url=str(item.get("url") or ""),
                    web_socket_debugger_url=item.get("webSocketDebuggerUrl"),
                )
            )
        return targets

    def open_url(self, url: str, *, new_tab: bool = True) -> BrowserTarget:
        normalized = _normalize_url(url)
        if not new_tab:
            targets = self.page_targets()
            if targets:
                self.navigate(targets[0], normalized)
                return targets[0]
        encoded = parse.quote(normalized, safe="")
        payload = self._json(f"/json/new?{encoded}", method="PUT")
        return BrowserTarget(
            id=str(payload.get("id") or ""),
            type=str(payload.get("type") or "page"),
            title=str(payload.get("title") or ""),
            url=str(payload.get("url") or normalized),
            web_socket_debugger_url=payload.get("webSocketDebuggerUrl"),
        )

    def navigate(self, target: BrowserTarget, url: str) -> None:
        ws = self._socket(target)
        try:
            self._call(ws, "Page.enable")
            self._call(ws, "Page.navigate", {"url": _normalize_url(url)})
        finally:
            ws.close()

    def close_tab(self, target: BrowserTarget) -> None:
        self._json(f"/json/close/{parse.quote(target.id, safe='')}")

    def page_summary(self, target: BrowserTarget, *, max_chars: int) -> dict[str, Any]:
        script = _summary_script(max_chars)
        ws = self._socket(target)
        try:
            self._call(ws, "Runtime.enable")
            response = self._call(
                ws,
                "Runtime.evaluate",
                {"expression": script, "returnByValue": True, "awaitPromise": True},
            )
        finally:
            ws.close()
        result = response.get("result") if isinstance(response, dict) else {}
        inner = result.get("result") if isinstance(result, dict) else {}
        value = inner.get("value") if isinstance(inner, dict) else None
        if isinstance(value, dict):
            return value
        return {"url": target.url, "title": target.title, "text": ""}

    def screenshot(self, target: BrowserTarget, path: Path, *, full_page: bool) -> str:
        ws = self._socket(target)
        try:
            self._call(ws, "Page.enable")
            params: dict[str, Any] = {"format": "png", "captureBeyondViewport": bool(full_page)}
            response = self._call(ws, "Page.captureScreenshot", params)
        finally:
            ws.close()
        result = response.get("result") if isinstance(response, dict) else {}
        data = result.get("data") if isinstance(result, dict) else None
        if not data:
            raise RuntimeError("Chrome did not return screenshot data")
        path.write_bytes(base64.b64decode(data))
        return str(path)

    def click(self, target: BrowserTarget, *, selector: str | None, text: str | None) -> dict[str, Any]:
        script = _click_script(selector, text)
        value = self._evaluate_value(target, script)
        return value if isinstance(value, dict) else {"clicked": False}

    def fill_form(
        self,
        target: BrowserTarget,
        fields: dict[str, str],
        *,
        submit: bool,
        submit_selector: str | None,
    ) -> dict[str, Any]:
        script = _fill_form_script(fields, submit=submit, submit_selector=submit_selector)
        value = self._evaluate_value(target, script)
        return value if isinstance(value, dict) else {"filled": []}

    def wait(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)

    def _evaluate_value(self, target: BrowserTarget, script: str) -> Any:
        ws = self._socket(target)
        try:
            self._call(ws, "Runtime.enable")
            response = self._call(
                ws,
                "Runtime.evaluate",
                {"expression": script, "returnByValue": True, "awaitPromise": True},
            )
        finally:
            ws.close()
        result = response.get("result") if isinstance(response, dict) else {}
        inner = result.get("result") if isinstance(result, dict) else {}
        return inner.get("value") if isinstance(inner, dict) else None

    def _version(self) -> dict[str, Any] | None:
        try:
            payload = self._json("/json/version", timeout=1.0)
        except Exception:
            logger.debug("DevTools /json/version probe failed", exc_info=True)
            return None
        return payload if isinstance(payload, dict) else None

    def _json(self, path: str, *, method: str = "GET", timeout: float | None = None) -> Any:
        req = urlrequest.Request(f"{self.base_url}{path}", method=method)
        try:
            with urlrequest.urlopen(req, timeout=timeout or 5.0) as response:
                return json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            if method == "PUT":
                req = urlrequest.Request(f"{self.base_url}{path}", method="GET")
                with urlrequest.urlopen(req, timeout=timeout or 5.0) as response:
                    return json.loads(response.read().decode("utf-8"))
            raise exc

    def _socket(self, target: BrowserTarget):
        if not target.web_socket_debugger_url:
            refreshed = next((item for item in self.page_targets() if item.id == target.id), None)
            if refreshed is not None:
                target = refreshed
        if not target.web_socket_debugger_url:
            raise RuntimeError(f"tab has no debugger websocket: {target.id}")
        return websocket.create_connection(target.web_socket_debugger_url, timeout=10)

    def _call(self, ws, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._message_id += 1
        message_id = self._message_id
        ws.send(json.dumps({"id": message_id, "method": method, "params": params or {}}))
        while True:
            payload = json.loads(ws.recv())
            if payload.get("id") != message_id:
                continue
            if payload.get("error"):
                raise RuntimeError(f"CDP {method} failed: {payload['error']}")
            return payload


def _default_operation(tool_name: str) -> str:
    return "navigate" if tool_name == "browser.control" else "open"


def _target_url(url: Any, query: Any, objective: Any, *, search_template: str, force_search: bool) -> str:
    explicit_url = str(url or "").strip()
    if explicit_url:
        return _normalize_url(explicit_url)
    objective_text = str(objective or "").strip()
    found_url = _first_url(objective_text)
    if found_url and not force_search:
        return _normalize_url(found_url)
    query_text = str(query or "").strip() or _query_from_objective(objective_text)
    if query_text:
        return _search_url(search_template, query_text)
    return "about:blank"


def _first_url(value: str) -> str | None:
    match = re.search(r"https?://[^\s<>()]+|www\.[^\s<>()]+|\b[A-Za-z0-9.-]+\.(?:com|org|net|io|ai|dev|edu|gov|co)\b", value)
    return match.group(0).rstrip(".,") if match else None


def _query_from_objective(value: str) -> str:
    cleaned = value.strip()
    cleaned = re.sub(r"\b(open|launch|go to|browse|search|look up|find|summarize|summary|tell me about)\b", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned


def _search_url(template: str, query: str) -> str:
    return template.format(query=parse.quote_plus(query))


def _normalize_url(url: str) -> str:
    stripped = url.strip()
    if stripped.startswith(("about:", "http://", "https://", "chrome://", "edge://")):
        return stripped
    if stripped.startswith("www."):
        return f"https://{stripped}"
    return f"https://{stripped}"


def _objective_wants_first_result(objective: str) -> bool:
    lowered = objective.lower()
    return any(phrase in lowered for phrase in ("first result", "first website", "first site", "go to the first", "open the first"))


def _objective_wants_screenshot(objective: str) -> bool:
    lowered = objective.lower()
    return "screenshot" in lowered or "screen shot" in lowered


def _first_external_link(links: list[dict[str, Any]], current_url: str) -> dict[str, str] | None:
    links = _external_links(links, current_url)
    return links[0] if links else None


def _external_links(links: list[dict[str, Any]], current_url: str) -> list[dict[str, str]]:
    current_host = parse.urlparse(current_url).netloc.lower()
    results = []
    for item in links:
        href = str(item.get("href") or "").strip()
        text = str(item.get("text") or "").strip()
        if not href.startswith(("http://", "https://")):
            continue
        host = parse.urlparse(href).netloc.lower()
        if not host or host == current_host:
            continue
        if any(blocked in host for blocked in ("bing.com", "google.com", "microsoft.com")) and "search" in href:
            continue
        results.append({"href": href, "text": text})
    return results


def _update_markers(text: str) -> list[str]:
    patterns = [
        r"\b(?:season|episode)\s+\d+\b",
        r"\b(?:new|latest|released|premiere)\b[^.!?]{0,80}",
        r"\b\d{4}-\d{2}-\d{2}\b",
        r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2},?\s+\d{4}\b",
    ]
    markers = []
    for pattern in patterns:
        markers.extend(match.group(0).strip() for match in re.finditer(pattern, text, flags=re.IGNORECASE))
    return markers[:20]


def _looks_like_login_or_auth_page(summary: str, url: str) -> bool:
    lowered = f"{summary}\n{url}".lower()
    return any(marker in lowered for marker in ("log in", "login", "sign in", "sign up", "authentication", "create account"))


def _tab_summary(target: BrowserTarget) -> dict[str, str]:
    return {"id": target.id, "title": target.title, "url": target.url}


def _tabs_summary(tabs: list[dict[str, Any]]) -> str:
    if not tabs:
        return "No Chrome tabs are exposed through the configured remote debugging session."
    lines = [f"{len(tabs)} Chrome tab(s) exposed through remote debugging:"]
    for index, tab in enumerate(tabs, 1):
        title = tab.get("title") or "Untitled"
        url = tab.get("url") or ""
        lines.append(f"{index}. {title} - {url}")
    return "\n".join(lines)


def _page_summary_text(page: dict[str, Any]) -> str:
    lines = [
        f"Title: {page.get('title') or 'Untitled'}",
        f"URL: {page.get('url') or ''}",
    ]
    if page.get("description"):
        lines.append(f"Description: {page['description']}")
    headings = page.get("headings") if isinstance(page.get("headings"), list) else []
    if headings:
        lines.append("Headings: " + "; ".join(str(item) for item in headings[:8]))
    forms = page.get("forms") if isinstance(page.get("forms"), list) else []
    if forms:
        lines.append(f"Forms detected: {len(forms)}")
    text = str(page.get("text") or "").strip()
    if text:
        lines.append("")
        lines.append(_clip(text, 5000))
    return "\n".join(lines)


def _terminal_output(operation: str, output: dict[str, Any]) -> dict[str, Any]:
    summary = output.get("summary") or ""
    url = output.get("url") or output.get("browser_url")
    screenshot = output.get("screenshot_path")
    parts = [f"Browser operation `{operation}` completed."]
    if url:
        parts.append(f"URL: {url}")
    if screenshot:
        parts.append(f"Screenshot: {screenshot}")
    if summary:
        parts.append("")
        parts.append(str(summary))
    return {"content": "\n".join(parts), "is_final": True, "exit_code": 0}


def _summary_script(max_chars: int) -> str:
    return f"""(() => {{
  const normalize = (value) => (value || '').replace(/\\s+/g, ' ').trim();
  const meta = document.querySelector('meta[name="description"], meta[property="og:description"]');
  const headings = Array.from(document.querySelectorAll('h1,h2,h3'))
    .map((node) => normalize(node.innerText || node.textContent))
    .filter(Boolean)
    .slice(0, 12);
  const links = Array.from(document.querySelectorAll('a[href]'))
    .map((node) => {{
      try {{
        return {{ text: normalize(node.innerText || node.textContent).slice(0, 180), href: new URL(node.href, location.href).href }};
      }} catch (error) {{
        return null;
      }}
    }})
    .filter(Boolean)
    .slice(0, 40);
  const forms = Array.from(document.querySelectorAll('form')).map((form) => {{
    const inputs = Array.from(form.querySelectorAll('input, textarea, select')).map((node) => {{
      return {{
        name: node.getAttribute('name') || '',
        id: node.id || '',
        placeholder: node.getAttribute('placeholder') || '',
        ariaLabel: node.getAttribute('aria-label') || '',
        type: node.getAttribute('type') || node.tagName.toLowerCase()
      }};
    }});
    return {{ action: form.action || '', method: form.method || '', inputs }};
  }}).slice(0, 8);
  return {{
    url: location.href,
    title: document.title || '',
    description: meta ? normalize(meta.getAttribute('content')) : '',
    headings,
    links,
    forms,
    text: normalize(document.body ? document.body.innerText : '').slice(0, {int(max_chars)})
  }};
}})()"""


def _click_script(selector: str | None, text: str | None) -> str:
    selector_json = json.dumps(selector)
    text_json = json.dumps(text.lower() if text else None)
    return f"""(() => {{
  const selector = {selector_json};
  const wantedText = {text_json};
  const normalize = (value) => (value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
  let node = selector ? document.querySelector(selector) : null;
  if (!node && wantedText) {{
    node = Array.from(document.querySelectorAll('button,a,input,[role="button"]'))
      .find((item) => normalize(item.innerText || item.value || item.getAttribute('aria-label')).includes(wantedText));
  }}
  if (!node) return {{ clicked: false, reason: 'element_not_found' }};
  node.click();
  return {{ clicked: true, tag: node.tagName, text: (node.innerText || node.value || '').slice(0, 160) }};
}})()"""


def _fill_form_script(fields: dict[str, str], *, submit: bool, submit_selector: str | None) -> str:
    fields_json = json.dumps(fields)
    submit_selector_json = json.dumps(submit_selector)
    return f"""(() => {{
  const fields = {fields_json};
  const submit = {json.dumps(submit)};
  const submitSelector = {submit_selector_json};
  const normalize = (value) => (value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
  const matches = (node, key) => {{
    const wanted = normalize(key);
    const label = node.id ? document.querySelector(`label[for="${{CSS.escape(node.id)}}"]`) : null;
    const wrappingLabel = node.closest ? node.closest('label') : null;
    const candidates = [
      node.name,
      node.id,
      node.placeholder,
      node.getAttribute('aria-label'),
      label ? label.innerText : '',
      wrappingLabel ? wrappingLabel.innerText : ''
    ].map(normalize);
    return candidates.some((value) => value === wanted || value.includes(wanted));
  }};
  const setNativeValue = (node, value) => {{
    if (node.isContentEditable || node.getAttribute('contenteditable') === 'true') {{
      node.textContent = value;
      return;
    }}
    const prototype = node.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype :
      node.tagName === 'SELECT' ? HTMLSelectElement.prototype : HTMLInputElement.prototype;
    const descriptor = Object.getOwnPropertyDescriptor(prototype, 'value');
    if (descriptor && descriptor.set) descriptor.set.call(node, value);
    else node.value = value;
  }};
  const filled = [];
  const nodes = Array.from(document.querySelectorAll('input, textarea, select, [contenteditable="true"], [role="textbox"]'));
  for (const [key, value] of Object.entries(fields)) {{
    const node = nodes.find((item) => matches(item, key));
    if (!node) continue;
    node.focus();
    setNativeValue(node, value);
    node.dispatchEvent(new InputEvent('input', {{ bubbles: true, inputType: 'insertText', data: value }}));
    node.dispatchEvent(new Event('input', {{ bubbles: true }}));
    node.dispatchEvent(new Event('change', {{ bubbles: true }}));
    node.blur();
    filled.push(key);
  }}
  if (submit) {{
    const submitNode = submitSelector ? document.querySelector(submitSelector) : document.querySelector('button[type="submit"], input[type="submit"]');
    if (submitNode) submitNode.click();
    else if (document.forms[0]) document.forms[0].requestSubmit();
  }}
  return {{ filled, submitted: submit }};
}})()"""


def _chrome_path(configured: str | None) -> str:
    if configured:
        configured_path = Path(configured).expanduser()
        if configured_path.exists():
            return str(configured_path)
    candidates = [
        shutil.which("chrome"),
        shutil.which("chrome.exe"),
        shutil.which("google-chrome"),
        shutil.which("chromium"),
        shutil.which("chromium-browser"),
    ]
    for env_name in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        env_value = os.environ.get(env_name)
        value = Path(env_value) if env_value else None
        if value is None and env_name == "LOCALAPPDATA":
            value = Path.home() / "AppData" / "Local"
        if value:
            candidates.append(str(value / "Google" / "Chrome" / "Application" / "chrome.exe"))
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return str(candidate)
    raise RuntimeError("Chrome executable was not found; set adapters.browser.chrome_path")


def _remote_debugging_note() -> str:
    return (
        "Only Chrome tabs exposed through the configured remote debugging port can be inspected. "
        "Normal Chrome windows launched without remote debugging are not visible to this adapter."
    )


def _safe_png_filename(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._")
    if not cleaned.lower().endswith(".png"):
        cleaned = f"{cleaned or 'screenshot'}.png"
    return cleaned


def _safe_stem(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._") or "screenshot"


def _clip(value: str, limit: int) -> str:
    return value if len(value) <= limit else f"{value[: limit - 3]}..."




def register(deps: RegistryDeps, definitions: Definitions, adapters: Adapters) -> None:
    settings = deps.settings
    open_enabled = settings.adapters.browser.enabled and capability_enabled(settings, Capability.BROWSER_OPEN)
    control_enabled = settings.adapters.browser.enabled and capability_enabled(settings, Capability.BROWSER_CONTROL)
    definitions.append(
        ToolDefinition(
            name="browser.open",
            capability=Capability.BROWSER_OPEN,
            enabled=open_enabled,
            description=(
                "open Chrome, search the web, summarize exposed tabs/pages, and capture browser screenshots "
                f"through DevTools at {settings.adapters.browser.host}:{settings.adapters.browser.remote_debugging_port}"
            ),
            operations=("open", "search", "research", "inspect_tabs", "screenshot", "summarize_page", "research_pages"),
            operation_schemas={
                "open": BrowserOpenInput,
                "search": BrowserSearchInput,
                "research": BrowserResearchInput,
                "inspect_tabs": BrowserInspectTabsInput,
                "screenshot": BrowserScreenshotInput,
                "summarize_page": BrowserSummarizePageInput,
                "research_pages": BrowserResearchPagesInput,
            },
            output_schema=BrowserToolOutput,
            operation_output_schemas=same_output_schema(
                ("open", "search", "research", "inspect_tabs", "screenshot", "summarize_page", "research_pages"),
                BrowserToolOutput,
            ),
            default_operation="open",
            minimum_risk=RiskLevel.LOW,
            # "search"/"objective"-driven operations don't know their
            # destination host until the adapter actually picks one, so this
            # reads from the result's own browser_url/url/visited_urls, not
            # from input (egress.extract_egress_hosts checks both).
            operation_egress=("open", "search", "research", "research_pages"),
            # Page text a person did not author and this machine does not
            # control (docs/THREAT_MODEL.md) - not inspect_tabs (tab
            # metadata) or screenshot (an image, not text the model reads).
            operation_content_trust={
                "open": UNTRUSTED_EXTERNAL,
                "search": UNTRUSTED_EXTERNAL,
                "research": UNTRUSTED_EXTERNAL,
                "research_pages": UNTRUSTED_EXTERNAL,
                "summarize_page": UNTRUSTED_EXTERNAL,
            },
            examples=(
                {"operation": "open", "url": "https://dizibox.com"},
                {"operation": "summarize_page", "objective": "list the first 5 new episodes"},
                {"operation": "search", "query": "python official docs"},
            ),
        )
    )
    definitions.append(
        ToolDefinition(
            name="browser.control",
            capability=Capability.BROWSER_CONTROL,
            enabled=control_enabled,
            description="navigate, close tabs, click elements, and fill simple forms in Chrome through DevTools",
            operations=(
                "navigate",
                "close_tab",
                "click",
                "fill_form",
                "check_page_update",
                "extract_page_state",
                "fill_form_step",
            ),
            operation_schemas={
                "navigate": BrowserNavigateInput,
                "close_tab": BrowserCloseTabInput,
                "click": BrowserClickInput,
                "fill_form": BrowserFillFormInput,
                "check_page_update": BrowserCheckPageUpdateInput,
                "extract_page_state": BrowserExtractPageStateInput,
                "fill_form_step": BrowserFillFormStepInput,
            },
            output_schema=BrowserToolOutput,
            operation_output_schemas=same_output_schema(
                (
                    "navigate",
                    "close_tab",
                    "click",
                    "fill_form",
                    "check_page_update",
                    "extract_page_state",
                    "fill_form_step",
                ),
                BrowserToolOutput,
            ),
            default_operation="navigate",
            operation_risks={
                "check_page_update": RiskLevel.LOW,
                "extract_page_state": RiskLevel.LOW,
                "navigate": RiskLevel.CRITICAL,
                "close_tab": RiskLevel.CRITICAL,
                "click": RiskLevel.CRITICAL,
                "fill_form": RiskLevel.CRITICAL,
                "fill_form_step": RiskLevel.CRITICAL,
            },
            operation_egress=("navigate",),
            operation_content_trust={
                "extract_page_state": UNTRUSTED_EXTERNAL,
                "check_page_update": UNTRUSTED_EXTERNAL,
            },
            examples=(
                {"operation": "navigate", "url": "https://example.com/contact"},
                {"operation": "fill_form",
                 "fields": {"name": "Oney", "email": "oney@example.com", "message": "Hello"},
                 "submit": True},
                {"operation": "click", "selector": "button.submit"},
            ),
        )
    )
    if settings.adapters.browser.enabled:
        browser = BrowserAdapter(settings.adapters.browser)
        adapters["browser.open"] = browser
        adapters["browser.control"] = browser
