from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
import re
import subprocess
import time
from collections.abc import Callable
from typing import Any, Protocol

from agent_control.config import ComputerUseAdapterConfig
from agent_control.llm.providers import LLMProvider
from agent_control.prompts import prompt_text, render_prompt
from agent_control.schemas import Capability, RiskLevel, ToolCallRequest, ToolCallResult, ToolResultStatus
from agent_control.tools.contracts import (
    ComputerActInput,
    ComputerObserveInput,
    ComputerRunGoalInput,
    ComputerUseOutput,
)
from agent_control.tools.spec import (
    Adapters,
    Definitions,
    RegistryDeps,
    ToolDefinition,
    capability_enabled,
    failed_result,
    same_output_schema,
)


logger = logging.getLogger(__name__)


class ComputerBackend(Protocol):
    def observe(self, screenshot_path: Path, *, include_ui_tree: bool, max_ui_elements: int) -> dict[str, Any]:
        ...

    def execute_action(self, action: dict[str, Any], config: ComputerUseAdapterConfig) -> dict[str, Any]:
        ...


class ComputerUseAdapter:
    """Windows-first local computer-use adapter with bounded observe/action loops."""

    def __init__(
        self,
        config: ComputerUseAdapterConfig,
        provider: LLMProvider | None = None,
        backend: ComputerBackend | None = None,
        should_continue: Callable[[str], bool] | None = None,
    ) -> None:
        self.config = config
        self.provider = provider
        self.backend = backend or WindowsComputerBackend()
        self.should_continue = should_continue or (lambda task_id: True)

    async def execute(self, request: ToolCallRequest) -> ToolCallResult:
        if not self.config.enabled:
            return failed_result(request, "computer-use adapter is disabled")
        operation = str(request.input.get("operation") or "observe")
        try:
            if operation == "observe":
                output = await self._observe(request)
            elif operation == "act":
                output = await self._act(request)
            elif operation == "run_goal":
                output = await self._run_goal(request)
            else:
                return failed_result(request, f"unsupported computer-use operation: {operation}")
        except Exception as exc:
            return failed_result(request, f"computer-use operation failed: {exc}")

        output["operation"] = operation
        output["terminal_output"] = [_terminal_output(operation, output)]
        return ToolCallResult(request_id=request.id, status=ToolResultStatus.SUCCEEDED, output=output)

    async def _observe(self, request: ToolCallRequest) -> dict[str, Any]:
        observation = await asyncio.to_thread(
            self._observe_sync,
            request.task_id,
            bool(request.input.get("include_ui_tree", True)),
        )
        summary = _metadata_summary(observation)
        if request.input.get("summarize", True):
            summary = await self._summarize_observation(
                str(request.input.get("objective") or request.input.get("prompt") or ""),
                observation,
                fallback=summary,
            )
        screenshots = [observation["screenshot_path"]] if observation.get("screenshot_path") else []
        return {
            "observation": observation,
            "screenshots": screenshots,
            "screenshot_path": observation.get("screenshot_path"),
            "screenshot_uri": observation.get("screenshot_uri"),
            "final_summary": summary,
            "completed": True,
        }

    async def _act(self, request: ToolCallRequest) -> dict[str, Any]:
        action = request.input.get("action") if isinstance(request.input.get("action"), dict) else {}
        if not action:
            raise ValueError("action is required for computer-use act")
        result = await asyncio.to_thread(self.backend.execute_action, action, self.config)
        observation = await asyncio.to_thread(self._observe_sync, request.task_id, True)
        return {
            "observation": observation,
            "actions_taken": [{**action, "result": result}],
            "screenshots": [observation["screenshot_path"]] if observation.get("screenshot_path") else [],
            "screenshot_path": observation.get("screenshot_path"),
            "screenshot_uri": observation.get("screenshot_uri"),
            "final_summary": result.get("summary") or "Action completed.",
            "completed": True,
        }

    async def _run_goal(self, request: ToolCallRequest) -> dict[str, Any]:
        objective = str(request.input["objective"])
        max_steps = int(request.input.get("max_steps") or self.config.max_steps)
        include_ui_tree = bool(request.input.get("include_ui_tree", True))
        if request.input.get("require_vision", True) and not _has_multimodal_provider(self.provider):
            raise RuntimeError("local multimodal LLM is not available for computer-use run_goal")

        actions_taken: list[dict[str, Any]] = []
        screenshots: list[str] = []
        final_observation: dict[str, Any] = {}
        final_summary = ""
        for step_number in range(1, max_steps + 1):
            observation = await asyncio.to_thread(self._observe_sync, request.task_id, include_ui_tree)
            final_observation = observation
            if observation.get("screenshot_path"):
                screenshots.append(str(observation["screenshot_path"]))
            if not self.should_continue(request.task_id):
                return _stopped_output(final_observation, actions_taken, screenshots, "Stopped before the next computer-use action.")
            decision = await self._next_action(objective, observation, step_number)
            final_summary = str(decision.get("summary") or "")
            if bool(decision.get("completed")):
                return {
                    "observation": observation,
                    "actions_taken": actions_taken,
                    "screenshots": screenshots,
                    "screenshot_path": observation.get("screenshot_path"),
                    "screenshot_uri": observation.get("screenshot_uri"),
                    "final_summary": final_summary or "Objective completed.",
                    "completed": True,
                }
            action = decision.get("action") if isinstance(decision.get("action"), dict) else {"type": "wait", "seconds": 1}
            if not self.should_continue(request.task_id):
                return _stopped_output(final_observation, actions_taken, screenshots, "Stopped before the next computer-use action.")
            result = await asyncio.to_thread(self.backend.execute_action, action, self.config)
            actions_taken.append({**action, "result": result})
            await asyncio.sleep(self.config.step_delay_seconds)

        return {
            "observation": final_observation,
            "actions_taken": actions_taken,
            "screenshots": screenshots,
            "screenshot_path": final_observation.get("screenshot_path"),
            "screenshot_uri": final_observation.get("screenshot_uri"),
            "final_summary": final_summary or f"Stopped after {max_steps} step(s) without a completion signal.",
            "completed": False,
        }

    def _observe_sync(self, task_id: str, include_ui_tree: bool) -> dict[str, Any]:
        path = _screenshot_path(self.config.screenshot_dir, task_id)
        return self.backend.observe(path, include_ui_tree=include_ui_tree, max_ui_elements=self.config.max_ui_elements)

    async def _summarize_observation(self, objective: str, observation: dict[str, Any], *, fallback: str) -> str:
        if not _has_multimodal_provider(self.provider) or not observation.get("screenshot_path"):
            return fallback
        try:
            text = await self.provider.generate_multimodal_text(
                prompt_text("base/computer_use_system.md"),
                render_prompt(
                    "tasks/computer_use_observe.md",
                    objective=objective or "Observe the desktop.",
                    observation=_json_compact(observation),
                ),
                [str(observation["screenshot_path"])],
            )
            return _clean_summary_text(text)
        except Exception as exc:
            return f"{fallback}\nVision summary unavailable: {exc}"

    async def _next_action(self, objective: str, observation: dict[str, Any], step_number: int) -> dict[str, Any]:
        if not _has_multimodal_provider(self.provider):
            raise RuntimeError("local multimodal LLM is not available")
        text = await self.provider.generate_multimodal_text(
            prompt_text("base/computer_use_system.md"),
            render_prompt(
                "tasks/computer_use_decision.md",
                objective=objective,
                step_number=step_number,
                observation=_json_compact(observation),
            ),
            [str(observation["screenshot_path"])],
        )
        return _parse_json_object(text)


class WindowsComputerBackend:
    def observe(self, screenshot_path: Path, *, include_ui_tree: bool, max_ui_elements: int) -> dict[str, Any]:
        screenshot_path.parent.mkdir(parents=True, exist_ok=True)
        monitors = _capture_screenshot(screenshot_path)
        cursor = _cursor_position()
        windows, active_window, ui_tree = _uia_snapshot(include_ui_tree=include_ui_tree, max_ui_elements=max_ui_elements)
        return {
            "platform": "windows",
            "screenshot_path": str(screenshot_path),
            "screenshot_uri": screenshot_path.resolve().as_uri(),
            "monitors": monitors,
            "cursor_position": cursor,
            "active_window": active_window,
            "visible_windows": windows,
            "ui_tree": ui_tree,
        }

    def execute_action(self, action: dict[str, Any], config: ComputerUseAdapterConfig) -> dict[str, Any]:
        action_type = str(action.get("type") or action.get("action") or "wait")
        if action_type == "wait":
            seconds = float(action.get("seconds") or config.step_delay_seconds or 1)
            time.sleep(max(0.0, min(seconds, 30.0)))
            return {"ok": True, "summary": f"Waited {seconds:g} second(s)."}
        if action_type == "open_path":
            path = _safe_root_path(str(action["path"]), config.allowed_roots)
            os.startfile(str(path))  # type: ignore[attr-defined]
            return {"ok": True, "summary": f"Opened path {path}."}
        if action_type == "launch_app":
            app = str(action["app"])
            _validate_app(app, config.allowed_apps)
            subprocess.Popen([app], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return {"ok": True, "summary": f"Launched {app}."}
        if action_type == "focus_window":
            return _focus_window(str(action["title_contains"]))

        pyautogui = _pyautogui()
        pyautogui.FAILSAFE = True
        pyautogui.PAUSE = config.step_delay_seconds
        if action_type == "click":
            pyautogui.click(int(action["x"]), int(action["y"]))
        elif action_type == "double_click":
            pyautogui.doubleClick(int(action["x"]), int(action["y"]))
        elif action_type == "type":
            pyautogui.write(str(action["text"]), interval=float(action.get("interval", 0.01)))
        elif action_type == "hotkey":
            keys = action.get("keys")
            if not isinstance(keys, list) or not keys:
                raise ValueError("hotkey action requires keys list")
            pyautogui.hotkey(*[str(key) for key in keys])
        elif action_type == "scroll":
            if action.get("x") is not None and action.get("y") is not None:
                pyautogui.moveTo(int(action["x"]), int(action["y"]))
            pyautogui.scroll(int(action.get("clicks") or 0))
        elif action_type == "drag":
            pyautogui.moveTo(int(action["x"]), int(action["y"]))
            pyautogui.dragTo(
                int(action["to_x"]),
                int(action["to_y"]),
                duration=float(action.get("duration_seconds") or 0.2),
                button=str(action.get("button") or "left"),
            )
        else:
            raise ValueError(f"unsupported computer action: {action_type}")
        return {"ok": True, "summary": f"Executed {action_type}."}


def _capture_screenshot(path: Path) -> list[dict[str, Any]]:
    try:
        from mss import mss
        from PIL import Image

        with mss() as sct:
            monitor = sct.monitors[0]
            image = sct.grab(monitor)
            Image.frombytes("RGB", image.size, image.rgb).save(path)
            return [
                {
                    "index": index,
                    "left": item["left"],
                    "top": item["top"],
                    "width": item["width"],
                    "height": item["height"],
                }
                for index, item in enumerate(sct.monitors)
            ]
    except Exception:
        from PIL import ImageGrab

        image = ImageGrab.grab()
        image.save(path, format="PNG")
        return [{"index": 0, "left": 0, "top": 0, "width": image.width, "height": image.height}]


def _cursor_position() -> dict[str, int] | None:
    try:
        pyautogui = _pyautogui()
        position = pyautogui.position()
        return {"x": int(position.x), "y": int(position.y)}
    except Exception:
        logger.debug("pyautogui cursor position lookup failed", exc_info=True)
        return None


def _uia_snapshot(*, include_ui_tree: bool, max_ui_elements: int) -> tuple[list[dict[str, Any]], dict[str, Any] | None, list[dict[str, Any]]]:
    try:
        from pywinauto import Desktop

        desktop = Desktop(backend="uia")
        windows = [_window_info(window) for window in desktop.windows(visible_only=True)[:20]]
        active = desktop.get_active()
        active_window = _window_info(active) if active else None
        ui_tree: list[dict[str, Any]] = []
        if include_ui_tree and active is not None and max_ui_elements > 0:
            for element in active.descendants()[:max_ui_elements]:
                ui_tree.append(_control_info(element))
        return windows, active_window, ui_tree
    except Exception as exc:
        windows, active_window = _window_snapshot_fallback()
        if active_window is None:
            active_window = {"title": "UI Automation unavailable", "error": str(exc)}
        else:
            active_window["uia_error"] = str(exc)
        return windows, active_window, []


def _window_info(window) -> dict[str, Any]:
    rectangle = window.rectangle()
    return {
        "title": window.window_text(),
        "class_name": window.class_name(),
        "process_id": window.process_id(),
        "rectangle": {
            "left": rectangle.left,
            "top": rectangle.top,
            "right": rectangle.right,
            "bottom": rectangle.bottom,
        },
    }


def _control_info(element) -> dict[str, Any]:
    info = element.element_info
    rectangle = element.rectangle()
    return {
        "name": info.name,
        "control_type": info.control_type,
        "automation_id": info.automation_id,
        "rectangle": {
            "left": rectangle.left,
            "top": rectangle.top,
            "right": rectangle.right,
            "bottom": rectangle.bottom,
        },
    }


def _window_snapshot_fallback() -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    try:
        import pygetwindow as gw

        windows = []
        for window in gw.getAllWindows()[:20]:
            title = str(getattr(window, "title", "") or "")
            visible = _window_visible(window)
            if not title and not visible:
                continue
            windows.append(_pygetwindow_info(window))
        active = gw.getActiveWindow()
        return windows, _pygetwindow_info(active) if active is not None else None
    except Exception:
        logger.debug("pygetwindow window snapshot fallback failed", exc_info=True)
        return [], None


def _pygetwindow_info(window) -> dict[str, Any]:
    return {
        "title": str(getattr(window, "title", "") or ""),
        "class_name": "pygetwindow",
        "process_id": None,
        "rectangle": {
            "left": int(getattr(window, "left", 0) or 0),
            "top": int(getattr(window, "top", 0) or 0),
            "right": int((getattr(window, "left", 0) or 0) + (getattr(window, "width", 0) or 0)),
            "bottom": int((getattr(window, "top", 0) or 0) + (getattr(window, "height", 0) or 0)),
        },
    }


def _window_visible(window) -> bool:
    for name in ("visible", "isVisible"):
        value = getattr(window, name, None)
        try:
            return bool(value() if callable(value) else value)
        except Exception:
            logger.debug("window visibility probe %r failed; trying next attribute", name, exc_info=True)
            continue
    return False


def _focus_window(title_contains: str) -> dict[str, Any]:
    from pywinauto import Desktop

    wanted = title_contains.lower()
    desktop = Desktop(backend="uia")
    for window in desktop.windows(visible_only=True):
        if wanted in window.window_text().lower():
            window.set_focus()
            return {"ok": True, "summary": f"Focused window {window.window_text()}."}
    raise ValueError(f"no visible window matches {title_contains!r}")


def _safe_root_path(value: str, allowed_roots: list[str]) -> Path:
    path = Path(value).expanduser().resolve()
    if not allowed_roots:
        return path
    roots = [Path(root).expanduser().resolve() for root in allowed_roots]
    if not any(root == path or root in path.parents for root in roots):
        raise ValueError(f"path is outside configured computer-use roots: {path}")
    return path


def _validate_app(app: str, allowed_apps: list[str]) -> None:
    if not allowed_apps:
        return
    normalized = Path(app).name.lower()
    allowed = {Path(item).name.lower() for item in allowed_apps}
    if normalized not in allowed:
        raise ValueError(f"app is not allowed: {app}")


def _pyautogui():
    try:
        import pyautogui
    except ImportError as exc:
        raise RuntimeError("pyautogui is required for desktop control actions") from exc
    return pyautogui


def _screenshot_path(root: str, task_id: str) -> Path:
    directory = Path(root).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    safe_task = re.sub(r"[^A-Za-z0-9_.-]+", "_", task_id).strip("._") or "task"
    return directory / f"{safe_task}_{stamp}.png"


def _metadata_summary(observation: dict[str, Any]) -> str:
    active = observation.get("active_window") if isinstance(observation.get("active_window"), dict) else {}
    windows = observation.get("visible_windows") if isinstance(observation.get("visible_windows"), list) else []
    title = active.get("title") or "unknown"
    if title == "UI Automation unavailable" and windows:
        title = str(windows[0].get("title") or title)
    return f"Observed desktop. Active window: {title}. Visible windows: {len(windows)}."


def _has_multimodal_provider(provider: LLMProvider | None) -> bool:
    return bool(provider and hasattr(provider, "generate_multimodal_text"))


def _parse_json_object(text: str) -> dict[str, Any]:
    stripped = text.strip()
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", stripped, re.DOTALL)
        if not match:
            raise ValueError(f"computer-use decision was not JSON: {text[:500]}")
        parsed = json.loads(match.group(0))
    if not isinstance(parsed, dict):
        raise ValueError("computer-use decision JSON must be an object")
    return parsed


def _clean_summary_text(text: str) -> str:
    stripped = text.strip()
    fenced = re.fullmatch(r"```(?:json|text)?\s*(.*?)\s*```", stripped, flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        stripped = fenced.group(1).strip()
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        return stripped
    if isinstance(payload, dict):
        for key in ("summary", "description", "text", "message"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return stripped


def _json_compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)[:16000]


def _terminal_output(operation: str, output: dict[str, Any]) -> dict[str, Any]:
    summary = output.get("final_summary") or f"computer.use {operation} completed."
    screenshot = output.get("screenshot_path")
    content = f"{summary}\nScreenshot: {screenshot}" if screenshot else str(summary)
    return {"content": content, "is_final": True, "exit_code": 0}


def _stopped_output(
    observation: dict[str, Any],
    actions_taken: list[dict[str, Any]],
    screenshots: list[str],
    summary: str,
) -> dict[str, Any]:
    return {
        "observation": observation,
        "actions_taken": actions_taken,
        "screenshots": screenshots,
        "screenshot_path": observation.get("screenshot_path"),
        "screenshot_uri": observation.get("screenshot_uri"),
        "final_summary": summary,
        "completed": False,
    }




def register(deps: RegistryDeps, definitions: Definitions, adapters: Adapters) -> None:
    settings = deps.settings
    enabled = (
        settings.adapters.computer_use.enabled
        and settings.adapters.desktop.control_enabled
        and capability_enabled(settings, Capability.DESKTOP_CONTROL)
    )
    definitions.append(
        ToolDefinition(
            name="computer.use",
            group="desktop",
            provides=('click', 'type', 'open', 'app', 'press', 'key', 'screen', 'window', 'mouse', 'keyboard', 'desktop', 'control',),
            capability=Capability.DESKTOP_CONTROL,
            enabled=enabled,
            description="observe and control the local Windows desktop with bounded screenshot/action loops",
            operations=("observe", "act", "run_goal"),
            operation_schemas={
                "observe": ComputerObserveInput,
                "act": ComputerActInput,
                "run_goal": ComputerRunGoalInput,
            },
            output_schema=ComputerUseOutput,
            operation_output_schemas=same_output_schema(("observe", "act", "run_goal"), ComputerUseOutput),
            default_operation="observe",
            operation_risks={
                "observe": RiskLevel.LOW,
                "act": RiskLevel.CRITICAL,
                "run_goal": RiskLevel.CRITICAL,
            },
        )
    )
    if settings.adapters.computer_use.enabled:
        adapters["computer.use"] = ComputerUseAdapter(
            settings.adapters.computer_use,
            provider=deps.provider,
            should_continue=deps.should_continue,
        )

    # NOTE: There used to be a `desktop.screenshot` ToolDefinition here, but no
    # adapter was ever registered for it - the planner happily picked it from
    # the catalog and execution then failed with "tool adapter not registered".
    # All real screenshot work is done by `computer.use observe` (captures +
    # returns the image) and `artifact.deliver send_screenshot` (delivers it).
    # The legacy `/screenshot` command in telegram.py is a separate code path
    # that uses Capability.DESKTOP_SCREENSHOT directly, unaffected by removing
    # this tool advertisement.
