from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import webbrowser
from typing import Any

from agent_control.config import WorkspaceAdapterConfig
from agent_control.schemas import Capability, ToolCallRequest, ToolCallResult, ToolResultStatus, ToolVerification
from agent_control.tools.contracts import (
    WorkspaceLaunchStaticInput,
    WorkspaceLaunchStaticOutput,
    WorkspaceMaterializeStaticAppInput,
    WorkspaceMaterializeStaticAppOutput,
    WorkspacePrepareInput,
    WorkspacePrepareOutput,
    WorkspaceWebAppPreviewInput,
    WorkspaceWebAppPreviewOutput,
    WorkspaceWriteFilesInput,
    WorkspaceWriteFilesOutput,
)
from agent_control.tools.path_utils import safe_path_segment
from agent_control.tools.spec import Adapters, Definitions, RegistryDeps, ToolDefinition, capability_enabled, failed_result


class LocalWorkspaceAdapter:
    """General local workspace tool for task files, generated code, and previews."""

    def __init__(self, config: WorkspaceAdapterConfig) -> None:
        self.config = config

    async def execute(self, request: ToolCallRequest) -> ToolCallResult:
        if not self.config.enabled:
            return failed_result(request, "workspace adapter is disabled")

        operation = str(request.input.get("operation") or _default_operation(request.tool_name))
        try:
            if operation == "prepare":
                output = self._prepare(request)
            elif operation == "write_files":
                output = self._write_files(request)
            elif operation == "materialize_static_app":
                output = self._materialize_static_app(request)
            elif operation == "launch_static":
                output = self._launch_static(request)
            elif operation == "web_app_preview":
                output = self._web_app_preview(request)
            else:
                return failed_result(request, f"unsupported workspace operation: {operation}")
        except Exception as exc:
            return failed_result(request, f"workspace operation failed: {exc}")

        output["operation"] = operation
        output["terminal_output"] = [_terminal_output(operation, output)]
        return ToolCallResult(request_id=request.id, status=ToolResultStatus.SUCCEEDED, output=output)

    def _prepare(self, request: ToolCallRequest) -> dict[str, Any]:
        workspace_dir = workspace_dir_for_task(self.config.root_dir, request.task_id)
        requested_workspace = str(request.input.get("workspace_path") or "").strip()
        if requested_workspace and Path(requested_workspace).expanduser().resolve() != workspace_dir:
            raise ValueError(
                "workspace_path does not match this task's managed workspace; "
                f"expected {workspace_dir}"
            )
        workspace_dir.mkdir(parents=True, exist_ok=True)
        objective = str(request.input.get("objective") or "").strip()
        task_file = workspace_dir / "TASK.md"
        if not task_file.exists() or bool(request.input.get("refresh_task_file", True)):
            task_file.write_text(_task_markdown(request.task_id, objective), encoding="utf-8")
        return {
            "workspace_dir": str(workspace_dir),
            "files": [str(task_file)],
        }

    def _write_files(self, request: ToolCallRequest) -> dict[str, Any]:
        prepared = self._prepare(request)
        workspace_dir = Path(prepared["workspace_dir"])
        files = []
        for item in request.input.get("files") or []:
            if not isinstance(item, dict):
                continue
            relative_path = str(item.get("path") or "").strip()
            content = str(item.get("content") or "")
            if not relative_path:
                continue
            target = _safe_child_path(workspace_dir, relative_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            files.append(str(target))
        return {
            "workspace_dir": str(workspace_dir),
            "files": [*prepared.get("files", []), *files],
            "changed_paths": files,
        }

    def _launch_static(self, request: ToolCallRequest) -> dict[str, Any]:
        prepared = self._prepare(request)
        workspace_dir = Path(prepared["workspace_dir"])
        objective = str(request.input.get("objective") or request.input.get("prompt") or "").strip()
        index_file = workspace_dir / "index.html"
        fallback_files: list[str] = []
        if bool(request.input.get("ensure_index", True)) and not index_file.exists():
            title = _title_from_objective(objective)
            index_file.write_text(_web_app_html(request.task_id, title, objective), encoding="utf-8")
            fallback_files.append(str(index_file))
        fallback_files.extend(_ensure_referenced_local_assets(workspace_dir, objective))
        port = _free_port(self.config.web_host, int(request.input.get("web_port_start") or self.config.web_port_start))
        url = f"http://{self.config.web_host}:{port}/"
        process = _start_static_server(workspace_dir, self.config.web_host, port)
        if bool(request.input.get("open_browser", self.config.open_browser)):
            webbrowser.open(url)
        return {
            "workspace_dir": str(workspace_dir),
            "url": url,
            "server_pid": process.pid,
            "files": sorted(set([*prepared.get("files", []), *_workspace_preview_files(workspace_dir), *fallback_files])),
            "changed_paths": fallback_files,
        }

    def _materialize_static_app(self, request: ToolCallRequest) -> dict[str, Any]:
        prepared = self._prepare(request)
        workspace_dir = Path(prepared["workspace_dir"])
        overwrite = bool(request.input.get("overwrite", False))
        allow_fallback_template = bool(request.input.get("allow_fallback_template", True))
        require_index = bool(request.input.get("require_index", False))
        source_text = str(request.input.get("source_text") or request.input.get("assistant_output") or "").strip()
        objective = str(request.input.get("objective") or request.input.get("prompt") or "").strip()

        parsed_files = _extract_files_from_text(source_text)
        existing_index = workspace_dir / "index.html"
        if existing_index.exists() and not overwrite:
            fallback_files = _ensure_referenced_local_assets(workspace_dir, objective)
            return {
                "workspace_dir": str(workspace_dir),
                "files": sorted(set([*prepared.get("files", []), str(existing_index), *fallback_files])),
                "changed_paths": fallback_files,
                "materialized_from": "existing_files",
            }

        files = []
        if parsed_files:
            if require_index and not _has_index_html(parsed_files):
                raise ValueError("assistant output did not include an index.html file")
            for relative_path, content in parsed_files.items():
                target = _safe_child_path(workspace_dir, relative_path)
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists() and not overwrite:
                    files.append(str(target))
                    continue
                target.write_text(content, encoding="utf-8")
                files.append(str(target))
            materialized_from = "assistant_output"
            files.extend(_ensure_referenced_local_assets(workspace_dir, objective))
        else:
            if not allow_fallback_template:
                raise ValueError("assistant output did not include materializable static app files")
            title = _title_from_objective(objective)
            target = workspace_dir / "index.html"
            target.write_text(_web_app_html(request.task_id, title, objective), encoding="utf-8")
            files.append(str(target))
            materialized_from = "fallback_template"
            files.extend(_ensure_referenced_local_assets(workspace_dir, objective))

        readme = workspace_dir / "README.md"
        if not readme.exists() or overwrite:
            readme.write_text(_web_app_readme(_title_from_objective(objective), request.task_id, objective), encoding="utf-8")
            files.append(str(readme))

        return {
            "workspace_dir": str(workspace_dir),
            "files": sorted(set([*prepared.get("files", []), *files])),
            "changed_paths": sorted(set(files)),
            "materialized_from": materialized_from,
        }

    def _web_app_preview(self, request: ToolCallRequest) -> dict[str, Any]:
        objective = str(request.input.get("objective") or request.input.get("prompt") or "").strip()
        title = _title_from_objective(objective)
        write_output = self._write_files(
            request.model_copy(
                update={
                    "input": {
                        **request.input,
                        "objective": objective,
                        "files": [
                            {"path": "index.html", "content": _web_app_html(request.task_id, title, objective)},
                            {"path": "README.md", "content": _web_app_readme(title, request.task_id, objective)},
                        ],
                    }
                }
            )
        )
        launch_output = self._launch_static(request)
        return {
            **launch_output,
            "workspace_dir": write_output["workspace_dir"],
            "files": sorted(set([*write_output.get("files", []), *launch_output.get("files", [])])),
            "changed_paths": sorted(
                set([*write_output.get("changed_paths", []), *launch_output.get("changed_paths", [])])
            ),
        }


LocalWorkspaceWebAppAdapter = LocalWorkspaceAdapter


def workspace_dir_for_task(root_dir: str, task_id: str) -> Path:
    root = Path(root_dir).expanduser().resolve()
    workspace = (root / safe_path_segment(task_id, fallback="task")).resolve()
    if root != workspace and root not in workspace.parents:
        raise ValueError("workspace path escaped configured root")
    return workspace


def _default_operation(tool_name: str) -> str:
    if tool_name == "workspace.web_app":
        return "web_app_preview"
    return "prepare"


def _safe_child_path(workspace_dir: Path, relative_path: str) -> Path:
    # Accept either a relative path or an absolute path that points INTO the
    # workspace. The LLM often gives us an absolute path after substituting
    # {{workspace_dir}} into a `path` field - that path is safe as long as it
    # actually resolves under the workspace, even if it was passed as absolute.
    raw = Path(relative_path)
    if raw.is_absolute():
        target = raw.resolve()
    else:
        target = (workspace_dir / raw).resolve()
    if workspace_dir != target and workspace_dir not in target.parents:
        raise ValueError(f"file path escaped workspace: {relative_path}")
    return target


def _extract_files_from_text(source_text: str) -> dict[str, str]:
    files: dict[str, str] = {}
    if not source_text:
        return files

    fence_pattern = re.compile(
        r"```(?P<lang>[A-Za-z0-9_+.-]*)[ \t]*(?P<meta>[^\n`]*)\n(?P<code>.*?)```",
        re.DOTALL,
    )
    for match in fence_pattern.finditer(source_text):
        language = (match.group("lang") or "").strip().lower()
        metadata = match.group("meta") or ""
        code = match.group("code")
        filename = _filename_from_fence(metadata) or _filename_before_fence(source_text[: match.start()])
        if filename is None and language in {"html", "htm"}:
            filename = "index.html"
        elif filename is None and language == "css":
            filename = "styles.css"
        elif filename is None and language in {"js", "javascript"}:
            filename = "script.js"
        if not filename:
            continue
        safe = _safe_relative_file(filename)
        if safe:
            files[safe] = code.rstrip() + "\n"
    return files


def _filename_from_fence(metadata: str) -> str | None:
    match = re.search(r"(?:filename|file|path)\s*=\s*['\"]?([^'\"\s`]+)", metadata, re.IGNORECASE)
    if match:
        return match.group(1)
    stripped = metadata.strip()
    if _looks_like_filename(stripped):
        return stripped
    return None


def _filename_before_fence(prefix: str) -> str | None:
    for line in reversed(prefix.splitlines()[-4:]):
        cleaned = line.strip().strip("`*: ")
        cleaned = re.sub(r"^(?:file|filename|path)\s*[:=-]\s*", "", cleaned, flags=re.IGNORECASE)
        if _looks_like_filename(cleaned):
            return cleaned
    return None


def _looks_like_filename(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_.\-/ ]+\.(?:html|css|js|mjs|json|md|txt)", value.strip()))


def _safe_relative_file(value: str) -> str | None:
    normalized = value.replace("\\", "/").strip().lstrip("/")
    if not normalized or ".." in Path(normalized).parts:
        return None
    return normalized


def _has_index_html(files: dict[str, str]) -> bool:
    return any(path.replace("\\", "/").lower().endswith("index.html") for path in files)


def _ensure_referenced_local_assets(workspace_dir: Path, objective: str) -> list[str]:
    index_file = workspace_dir / "index.html"
    if not index_file.exists():
        return []
    html = index_file.read_text(encoding="utf-8", errors="replace")
    created: list[str] = []
    for reference in sorted(_local_asset_references(html)):
        try:
            target = _safe_child_path(workspace_dir, reference)
        except ValueError:
            continue
        if target.exists():
            continue
        if target.suffix.lower() == ".js":
            content = _default_script(objective)
        elif target.suffix.lower() == ".css":
            content = _default_stylesheet(objective)
        else:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        created.append(str(target))
    return created


def _local_asset_references(html: str) -> set[str]:
    references: set[str] = set()
    for match in re.finditer(r"""(?:src|href)\s*=\s*["']([^"']+)["']""", html, re.IGNORECASE):
        raw = match.group(1).strip()
        if not raw:
            continue
        lowered = raw.lower()
        if lowered.startswith(("http://", "https://", "data:", "mailto:", "javascript:", "#", "/")):
            continue
        path = raw.split("#", 1)[0].split("?", 1)[0].strip()
        if Path(path).suffix.lower() in {".css", ".js"}:
            safe = _safe_relative_file(path)
            if safe:
                references.add(safe)
    return references


def _workspace_preview_files(workspace_dir: Path) -> list[str]:
    extensions = {".html", ".css", ".js", ".mjs", ".json", ".md", ".txt", ".png", ".jpg", ".jpeg", ".webp", ".svg"}
    files = []
    for path in workspace_dir.rglob("*"):
        if path.is_file() and path.suffix.lower() in extensions:
            files.append(str(path))
    return files


def _default_script(objective: str) -> str:
    return f"""document.addEventListener("DOMContentLoaded", () => {{
  const themeToggle = document.getElementById("themeToggle");
  if (themeToggle) {{
    themeToggle.addEventListener("click", () => {{
      document.documentElement.classList.toggle("light");
    }});
  }}

  const factText = document.getElementById("factText");
  const factButton = document.getElementById("factBtn");
  const facts = [
    "Primates use social learning to solve problems and adapt to new environments.",
    "Many monkey species live in complex groups with distinct calls and roles.",
    "Conservation work protects habitats that support entire ecosystems."
  ];
  if (factText && factButton) {{
    factButton.addEventListener("click", () => {{
      factText.textContent = facts[Math.floor(Math.random() * facts.length)];
    }});
  }}

  const modal = document.getElementById("modal");
  const modalImg = document.getElementById("modalImg");
  const modalClose = document.getElementById("modalClose");
  document.querySelectorAll("[data-full]").forEach((item) => {{
    item.addEventListener("click", (event) => {{
      event.preventDefault();
      if (!modal || !modalImg) return;
      modalImg.src = item.getAttribute("data-full") || "";
      modal.classList.add("open");
      modal.setAttribute("aria-hidden", "false");
    }});
  }});
  if (modalClose && modal) {{
    modalClose.addEventListener("click", () => {{
      modal.classList.remove("open");
      modal.setAttribute("aria-hidden", "true");
    }});
  }}

  console.info("Local preview ready", {{"objective": {_js_string(objective)}}});
}});
"""


def _default_stylesheet(objective: str) -> str:
    title = _title_from_objective(objective)
    return f"""body {{
  margin: 0;
  font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  background: #f7f8fb;
  color: #1f2328;
}}
main {{
  width: min(960px, calc(100vw - 32px));
  margin: 48px auto;
}}
h1::after {{
  content: " - {title}";
  color: #667085;
  font-size: 0.45em;
}}
a {{
  color: #0a7f5a;
}}
"""


def _js_string(value: str) -> str:
    return json.dumps(value)


def _title_from_objective(objective: str) -> str:
    lowered = objective.lower()
    if "hello" in lowered and "world" in lowered:
        return "Hello World"
    if "web app" in lowered:
        return "Local Web App"
    return "Generated Web App"


def _task_markdown(task_id: str, objective: str) -> str:
    return f"""# Task Workspace

Task: `{task_id}`

Objective:

```text
{objective or "No objective provided."}
```

Generated at: {datetime.now().isoformat(timespec="seconds")}
"""


def _web_app_html(task_id: str, title: str, objective: str) -> str:
    created_at = datetime.now().isoformat(timespec="seconds")
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{_html(title)}</title>
  <style>
    :root {{
      color-scheme: light dark;
      --bg: #f7f8fb;
      --panel: #ffffff;
      --text: #1f2328;
      --muted: #57606a;
      --accent: #0a7f5a;
      --border: #d8dee4;
    }}
    @media (prefers-color-scheme: dark) {{
      :root {{
        --bg: #0d1117;
        --panel: #161b22;
        --text: #e6edf3;
        --muted: #8b949e;
        --accent: #3fb950;
        --border: #30363d;
      }}
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      display: grid;
      place-items: center;
      background: var(--bg);
      color: var(--text);
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    main {{
      width: min(760px, calc(100vw - 32px));
      border: 1px solid var(--border);
      border-radius: 8px;
      background: var(--panel);
      padding: 32px;
    }}
    h1 {{ margin: 0 0 10px; font-size: clamp(32px, 6vw, 56px); line-height: 1.05; }}
    p {{ margin: 0 0 18px; color: var(--muted); font-size: 18px; }}
    dl {{ display: grid; grid-template-columns: max-content 1fr; gap: 8px 14px; margin: 24px 0 0; }}
    dt {{ color: var(--muted); }}
    dd {{ margin: 0; overflow-wrap: anywhere; }}
  </style>
</head>
<body>
  <main>
    <h1>{_html(title)}</h1>
    <p>This preview was generated from your Telegram task and is running locally.</p>
    <dl>
      <dt>Task</dt><dd>{_html(task_id)}</dd>
      <dt>Objective</dt><dd>{_html(objective or "Create a local web app preview.")}</dd>
      <dt>Created</dt><dd>{_html(created_at)}</dd>
    </dl>
  </main>
</body>
</html>
"""


def _web_app_readme(title: str, task_id: str, objective: str) -> str:
    return f"""# {title}

Task: `{task_id}`

Objective:

```text
{objective or "Create a local web app preview."}
```

Run locally from this directory:

```powershell
python -m http.server
```
"""


def _html(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def _free_port(host: str, start: int) -> int:
    for port in range(start, min(start + 200, 65536)):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.2)
            if sock.connect_ex((host, port)) != 0:
                return port
    raise RuntimeError(f"no free port found from {start}")


def _start_static_server(workspace_dir: Path, host: str, port: int) -> subprocess.Popen:
    log = (workspace_dir / "server.log").open("ab")
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        return subprocess.Popen(
            [sys.executable, "-m", "http.server", str(port), "--bind", host],
            cwd=str(workspace_dir),
            stdout=log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            creationflags=creationflags,
        )
    finally:
        log.close()


def _terminal_output(operation: str, output: dict[str, Any]) -> dict[str, Any]:
    lines = [f"Workspace operation completed: {operation}"]
    if output.get("url"):
        lines.append(f"URL: {output['url']}")
    if output.get("workspace_dir"):
        lines.append(f"Workspace: {output['workspace_dir']}")
    if output.get("server_pid"):
        lines.append(f"Server PID: {output['server_pid']}")
    if output.get("files"):
        lines.append("Files:")
        lines.extend(f"- {path}" for path in output["files"])
    return {
        "instance_id": "local-worker",
        "terminal_id": "workspace",
        "content": "\n".join(lines),
        "command_id": None,
        "is_final": True,
        "exit_code": 0,
        "source": "local_workspace",
    }




def _verify_local_workspace(request: ToolCallRequest, result: ToolCallResult) -> ToolVerification | None:
    """Re-reads the workspace after a SUCCEEDED call to confirm every path
    it claimed to have written actually exists (docs/ROADMAP.md "Finish
    the Proof") - `files`/`changed_paths` are already absolute paths this
    adapter itself resolved, not user input.

    Prefers `changed_paths` (what this specific call wrote) when present;
    falls back to `files` (the workspace's full accumulated listing) for
    `prepare`, which has no `changed_paths` key, and for a call whose
    `changed_paths` came back empty (e.g. materialize_static_app finding an
    existing index.html and writing nothing new) - re-checking the full
    claimed listing is a strictly stronger check in that case, not a
    weaker one.
    """
    paths = result.output.get("changed_paths")
    if not isinstance(paths, list) or not paths:
        paths = result.output.get("files")
    if not isinstance(paths, list) or not paths:
        return None
    checked = 0
    missing: list[str] = []
    for item in paths:
        if not isinstance(item, str) or not item:
            continue
        checked += 1
        if not Path(item).exists():
            missing.append(f"file not found: {item}")
    if checked == 0:
        return None
    return ToolVerification(checked=checked, verified=checked - len(missing), missing=missing)


def register(deps: RegistryDeps, definitions: Definitions, adapters: Adapters) -> None:
    settings = deps.settings
    enabled = capability_enabled(settings, Capability.FILESYSTEM_WRITE) and settings.adapters.workspace.enabled
    definitions.append(
        ToolDefinition(
            name="workspace.manage",
            capability=Capability.FILESYSTEM_WRITE,
            enabled=enabled,
            description=f"manage task workspaces under {settings.adapters.workspace.root_dir}",
            operations=("prepare", "write_files", "materialize_static_app", "launch_static", "web_app_preview"),
            operation_schemas={
                "prepare": WorkspacePrepareInput,
                "write_files": WorkspaceWriteFilesInput,
                "materialize_static_app": WorkspaceMaterializeStaticAppInput,
                "launch_static": WorkspaceLaunchStaticInput,
                "web_app_preview": WorkspaceWebAppPreviewInput,
            },
            operation_output_schemas={
                "prepare": WorkspacePrepareOutput,
                "write_files": WorkspaceWriteFilesOutput,
                "materialize_static_app": WorkspaceMaterializeStaticAppOutput,
                "launch_static": WorkspaceLaunchStaticOutput,
                "web_app_preview": WorkspaceWebAppPreviewOutput,
            },
            default_operation="prepare",
            verify=_verify_local_workspace,
        )
    )
    if settings.adapters.workspace.enabled:
        workspace = LocalWorkspaceAdapter(settings.adapters.workspace)
        adapters["workspace.manage"] = workspace
        adapters["workspace.web_app"] = workspace
