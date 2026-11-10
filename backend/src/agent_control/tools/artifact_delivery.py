from __future__ import annotations

from mimetypes import guess_type
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import unquote, urlparse

from agent_control.config import AppSettings
from agent_control.schemas import (
    Artifact,
    ArtifactType,
    Capability,
    ChannelType,
    TaskRecord,
    ToolCallRequest,
    ToolCallResult,
    ToolResultStatus,
    channel_chat_id,
)
from agent_control.text import trim_text as _trim
from agent_control.storage.repositories import ArtifactRepository, TaskRepository
from agent_control.tools.contracts import ArtifactDeliverInput, ArtifactDeliveryOutput
from agent_control.tools.spec import (
    Adapters,
    Definitions,
    RegistryDeps,
    ToolDefinition,
    capability_enabled,
    failed_result,
    same_output_schema,
)


class TelegramFileClient(Protocol):
    async def send_photo_file(self, chat_id: str | int, path: str, caption: str | None = None) -> dict[str, Any]:
        ...

    async def send_document_file(self, chat_id: str | int, path: str, caption: str | None = None) -> dict[str, Any]:
        ...


class ArtifactDeliveryAdapter:
    def __init__(
        self,
        artifacts: ArtifactRepository,
        tasks: TaskRepository,
        *,
        telegram_client: TelegramFileClient | None = None,
        allowed_roots: list[str] | None = None,
        recent_fallback_enabled: bool = False,
    ) -> None:
        self.artifacts = artifacts
        self.tasks = tasks
        self.telegram_client = telegram_client
        self.allowed_roots = [Path(root).expanduser().resolve() for root in (allowed_roots or [])]
        self.recent_fallback_enabled = recent_fallback_enabled

    async def execute(self, request: ToolCallRequest) -> ToolCallResult:
        operation = str(request.input.get("operation") or "send_latest")
        try:
            if operation == "list_artifacts":
                output = self._list_artifacts(request)
            elif operation in {"send_file", "send_latest", "send_screenshot"}:
                output = await self._send(request, operation)
            else:
                return failed_result(request, f"unsupported artifact delivery operation: {operation}")
        except Exception as exc:
            return failed_result(request, str(exc))

        output["operation"] = operation
        output["terminal_output"] = [
            {
                "content": output.get("summary") or f"artifact.deliver {operation} completed.",
                "is_final": True,
                "exit_code": 0,
            }
        ]
        return ToolCallResult(request_id=request.id, status=ToolResultStatus.SUCCEEDED, output=output)

    def _list_artifacts(self, request: ToolCallRequest) -> dict[str, Any]:
        artifacts = self.artifacts.list_for_task(request.task_id)
        return {
            "delivered": False,
            "artifact_ids": [artifact.id for artifact in artifacts],
            "artifacts": [artifact.model_dump(mode="json") for artifact in artifacts],
            "summary": f"Found {len(artifacts)} artifact(s) for task {request.task_id}."
            + "".join(f"\n- {artifact.type.value}: {artifact.uri or artifact.id}" for artifact in artifacts[:25]),
        }

    async def _send(self, request: ToolCallRequest, operation: str) -> dict[str, Any]:
        task = self.tasks.get(request.task_id)
        if task is None:
            raise ValueError(f"task not found: {request.task_id}")

        artifact, path = self._resolve_artifact_path(request, task, operation)
        if path is None and operation == "send_latest" and self.recent_fallback_enabled:
            artifact, path = self._resolve_recent_artifact_path(request)
        if path is None and operation == "send_latest":
            artifact, path = self._materialize_latest_text(request, task)
        if path is None:
            raise ValueError("no deliverable artifact or file path was found")
        if not path.exists() or not path.is_file():
            raise ValueError(f"deliverable file does not exist: {path}")

        if artifact is None:
            artifact = self.artifacts.create(
                Artifact(
                    task_id=request.task_id,
                    type=_artifact_type_for_path(path, operation),
                    uri=str(path),
                    content_preview=str(request.input.get("caption") or path.name),
                    metadata={"source": "artifact.deliver", "mime_type": request.input.get("mime_type") or guess_type(path.name)[0]},
                )
            )

        chat_id = str(request.input.get("chat_id") or channel_chat_id(task, ChannelType.TELEGRAM) or "")
        if not chat_id:
            source_channel = task.metadata.get("source_channel") or ChannelType.TELEGRAM.value
            if source_channel != ChannelType.TELEGRAM.value:
                raise ValueError(
                    f"artifact.deliver only supports Telegram in this version; this task's "
                    f"channel is '{source_channel}', which has no Telegram chat_id to deliver to"
                )
            raise ValueError("telegram chat_id is required and could not be inferred from the task")
        if self.telegram_client is None:
            raise ValueError("Telegram delivery client is not configured")

        caption = _trim(str(request.input.get("caption") or artifact.content_preview or path.name), 900)
        if operation == "send_screenshot" or artifact.type == ArtifactType.SCREENSHOT:
            telegram_result = await self.telegram_client.send_photo_file(chat_id, str(path), caption)
            method = "telegram.sendPhoto"
        else:
            telegram_result = await self.telegram_client.send_document_file(chat_id, str(path), caption)
            method = "telegram.sendDocument"

        return {
            "delivered": True,
            "delivery_method": method,
            "artifact_id": artifact.id,
            "artifact_ids": [artifact.id],
            "path": str(path),
            "chat_id": chat_id,
            "summary": f"Delivered {path.name} to Telegram chat {chat_id}.",
            "telegram_result": telegram_result,
        }

    def _resolve_artifact_path(
        self,
        request: ToolCallRequest,
        task: TaskRecord,
        operation: str,
    ) -> tuple[Artifact | None, Path | None]:
        artifact_id = request.input.get("artifact_id")
        if artifact_id:
            artifact = self.artifacts.get(str(artifact_id))
            if artifact is None:
                raise ValueError(f"artifact not found: {artifact_id}")
            if artifact.task_id not in {None, request.task_id}:
                raise ValueError(f"artifact {artifact_id} is not linked to task {request.task_id}")
            return artifact, path_from_uri(artifact.uri)

        raw_path = request.input.get("path")
        if raw_path:
            # When the planner passes a bare filename (no separators), prefer
            # a task artifact with the same basename. Tools that produce files
            # register them as artifacts (e.g. code.interpreter), so a basename
            # passed across steps maps directly to the file the prior step made.
            match = self._artifact_by_basename(request.task_id, str(raw_path))
            if match is not None:
                artifact, path = match
                return artifact, path
            return None, self._safe_path(str(raw_path))

        if operation == "send_screenshot":
            for value in _task_screenshot_values(task):
                path = path_from_uri(value)
                if path and path.exists() and path.is_file():
                    return _artifact_for_path(self.artifacts.list_for_task(request.task_id), path), path

        artifact_type = str(request.input.get("artifact_type") or "").strip()
        artifacts = self.artifacts.list_for_task(request.task_id)
        if artifact_type:
            artifacts = [item for item in artifacts if item.type.value == artifact_type]
        if operation == "send_screenshot":
            artifacts = [item for item in artifacts if item.type == ArtifactType.SCREENSHOT]
        for artifact in reversed(artifacts):
            path = path_from_uri(artifact.uri)
            if path and path.exists() and path.is_file():
                return artifact, path
        return None, None

    def _artifact_by_basename(
        self, task_id: str, raw: str
    ) -> tuple[Artifact, Path] | None:
        """If ``raw`` is a bare filename, return the matching task artifact + path.

        Returns ``None`` when ``raw`` contains a path separator (treat as real path)
        or no artifact with that basename exists.
        """
        name = raw.strip().strip("\"'")
        if "/" in name or "\\" in name or not name:
            return None
        for artifact in reversed(self.artifacts.list_for_task(task_id)):
            path = path_from_uri(artifact.uri)
            if path is None or not path.exists() or not path.is_file():
                continue
            if path.name == name:
                return artifact, path
        return None

    def _resolve_recent_artifact_path(self, request: ToolCallRequest) -> tuple[Artifact | None, Path | None]:
        list_recent = getattr(self.artifacts, "list_recent", None)
        if not callable(list_recent):
            return None, None
        artifact_type = str(request.input.get("artifact_type") or "").strip() or None
        for artifact in list_recent(limit=25, artifact_type=artifact_type):
            path = path_from_uri(artifact.uri)
            if path and path.exists() and path.is_file():
                return artifact, path
        if artifact_type:
            for artifact in list_recent(limit=25):
                path = path_from_uri(artifact.uri)
                if path and path.exists() and path.is_file():
                    return artifact, path
        return None, None

    def _materialize_latest_text(self, request: ToolCallRequest, task: TaskRecord) -> tuple[Artifact | None, Path | None]:
        text = _latest_task_text(task)
        if not text:
            return None, None
        root = self.allowed_roots[0] if self.allowed_roots else Path(".agent_control/artifacts").resolve()
        root.mkdir(parents=True, exist_ok=True)
        path = root / f"{request.task_id}_latest_output.txt"
        path.write_text(text, encoding="utf-8")
        artifact = self.artifacts.create(
            Artifact(
                task_id=request.task_id,
                type=ArtifactType.TEXT_LOG,
                uri=str(path),
                content_preview=_trim(text, 500),
                metadata={"source": "artifact.deliver", "operation": "send_latest", "materialized": True},
            )
        )
        return artifact, path

    def _safe_path(self, value: str) -> Path:
        path = path_from_uri(value)
        if path is None:
            raise ValueError(f"invalid file path: {value}")
        path = path.expanduser().resolve()

        # If a bare filename was given (or a path that does not exist where it resolved),
        # try common locations: Desktop, Documents, Downloads, and recursive search of
        # the allowed_roots. This makes "send me resume.pdf" work after the user has
        # previously read it from their Desktop in a separate task.
        if not path.exists():
            resolved = self._search_for_filename(value)
            if resolved is not None:
                path = resolved

        if self.allowed_roots and not any(root == path or root in path.parents for root in self.allowed_roots):
            raise ValueError(f"path is outside configured delivery roots: {path}")
        return path

    def _search_for_filename(self, value: str) -> Path | None:
        """Locate a file by name across common user folders + configured roots.

        Used as a fallback when the planner gives a bare filename like
        ``resume.pdf`` instead of a full path. Order matters - search
        recently-modified locations first (code interpreter workspace,
        screenshots), then user folders, then bounded recursive scan of
        allowed roots (depth-limited to keep latency sane on large trees).
        """
        raw = value.strip().strip("\"'")
        if "\\" in raw or "/" in raw:
            return None
        filename = raw
        if not filename:
            return None
        home = Path.home()
        # 1. Direct hits in high-signal locations (recent tool outputs, then user folders).
        priority_roots = [
            *self.allowed_roots,                       # includes code interpreter + workspaces
            home / "Desktop",
            home / "Documents",
            home / "Downloads",
        ]
        seen: set[Path] = set()
        for root in priority_roots:
            if not root or root in seen or not root.exists():
                continue
            seen.add(root)
            direct = root / filename
            if direct.exists() and direct.is_file():
                return direct.resolve()
        # 2. Bounded recursive scan (depth ≤ 4) - avoids walking all of C:\for fun.
        #    Code interpreter writes one task-dir deep; workspaces nest one level deeper;
        #    screenshots are flat. Depth 4 covers all real cases without blowing up latency.
        for root in seen:
            for path in _walk_with_max_depth(root, max_depth=4):
                if path.name == filename and path.is_file():
                    return path.resolve()
        return None




def _task_screenshot_values(task: TaskRecord) -> list[object]:
    result = task.metadata.get("last_tool_result")
    output = result.get("output") if isinstance(result, dict) else {}
    if not isinstance(output, dict):
        output = {}
    return [
        task.metadata.get("screenshot_path"),
        output.get("screenshot_path"),
        task.metadata.get("screenshot_uri"),
        output.get("screenshot_uri"),
    ]


def path_from_uri(value: object) -> Path | None:
    """Not module-private: admin.py's artifact download endpoint reuses this
    (and artifact_delivery_roots below) so path resolution has exactly one
    implementation, not a second one re-derived at the API boundary."""
    if not value:
        return None
    text = str(value)
    alias_path = _alias_path(text)
    if alias_path is not None:
        return alias_path.expanduser().resolve()
    if text.startswith("file:///"):
        parsed = urlparse(text)
        raw_path = unquote(parsed.path)
        if raw_path.startswith("/") and len(raw_path) > 3 and raw_path[2] == ":":
            raw_path = raw_path[1:]
        return Path(raw_path).expanduser().resolve()
    return Path(text).expanduser().resolve()


def _walk_with_max_depth(root: Path, *, max_depth: int):
    """Yield files/dirs under ``root`` up to ``max_depth`` levels deep.

    Cheaper than ``rglob("*")`` on huge trees - stops descending once we hit
    the depth limit instead of walking everything.
    """
    root_depth = len(root.parts)
    try:
        stack = [root]
        while stack:
            current = stack.pop()
            try:
                for entry in current.iterdir():
                    depth = len(entry.parts) - root_depth
                    yield entry
                    if entry.is_dir() and depth < max_depth:
                        stack.append(entry)
            except (PermissionError, OSError):
                continue
    except (PermissionError, OSError):
        return


def _alias_path(value: str) -> Path | None:
    normalized = value.strip().strip("\"'").replace("/", "\\")
    lowered = normalized.lower()
    home = Path.home()
    aliases = {
        "desktop": home / "Desktop",
        "%desktop%": home / "Desktop",
        "documents": home / "Documents",
        "my documents": home / "Documents",
        "%documents%": home / "Documents",
        "downloads": home / "Downloads",
        "%downloads%": home / "Downloads",
        "home": home,
        "user": home,
        "my directory": home,
        "my folder": home,
    }
    if lowered in aliases:
        return aliases[lowered]
    for prefix, root in (
        ("desktop\\", home / "Desktop"),
        ("documents\\", home / "Documents"),
        ("my documents\\", home / "Documents"),
        ("downloads\\", home / "Downloads"),
        ("home\\", home),
        ("my directory\\", home),
    ):
        if lowered.startswith(prefix):
            return root / normalized[len(prefix) :]
    return None


def _latest_task_text(task: TaskRecord) -> str | None:
    result = task.metadata.get("last_tool_result")
    output = result.get("output") if isinstance(result, dict) else {}
    if isinstance(output, dict):
        terminal_output = output.get("terminal_output")
        if isinstance(terminal_output, list):
            for item in reversed(terminal_output):
                if isinstance(item, dict) and item.get("content"):
                    return str(item["content"])
        for key in ("summary", "final_summary", "response", "text", "stdout"):
            if output.get(key):
                return str(output[key])
    if task.metadata.get("last_tool_output_text"):
        return str(task.metadata["last_tool_output_text"])
    return None


def _artifact_for_path(artifacts: list[Artifact], path: Path) -> Artifact | None:
    resolved = path.resolve()
    for artifact in artifacts:
        artifact_path = path_from_uri(artifact.uri)
        if artifact_path and artifact_path.resolve() == resolved:
            return artifact
    return None


def _artifact_type_for_path(path: Path, operation: str) -> ArtifactType:
    if operation == "send_screenshot" or path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}:
        return ArtifactType.SCREENSHOT
    return ArtifactType.DOCUMENT


def register(deps: RegistryDeps, definitions: Definitions, adapters: Adapters) -> None:
    settings = deps.settings
    enabled = capability_enabled(settings, Capability.TELEGRAM_SEND)
    definitions.append(
        ToolDefinition(
            name="artifact.deliver",
            capability=Capability.TELEGRAM_SEND,
            enabled=enabled,
            description="list task artifacts and deliver screenshots or files to the source Telegram chat",
            operations=("send_file", "send_latest", "send_screenshot", "list_artifacts"),
            input_schema=ArtifactDeliverInput,
            output_schema=ArtifactDeliveryOutput,
            operation_output_schemas=same_output_schema(
                ("send_file", "send_latest", "send_screenshot", "list_artifacts"),
                ArtifactDeliveryOutput,
            ),
            default_operation="send_latest",
            examples=(
                # Deliver a file by basename - finds files produced by a prior
                # code.interpreter step automatically (registered as artifacts).
                {"operation": "send_file", "path": "sales_data.xlsx"},
                {"operation": "send_screenshot"},
                {"operation": "send_latest"},
            ),
        )
    )
    if deps.artifact_repository is not None and deps.task_repository is not None:
        adapters["artifact.deliver"] = ArtifactDeliveryAdapter(
            deps.artifact_repository,  # type: ignore[arg-type]
            deps.task_repository,  # type: ignore[arg-type]
            telegram_client=deps.telegram_client,  # type: ignore[arg-type]
            allowed_roots=artifact_delivery_roots(settings),
            recent_fallback_enabled=settings.adapters.artifact_delivery.recent_artifact_fallback_enabled,
        )


def artifact_delivery_roots(settings: AppSettings) -> list[str]:
    return [
        settings.storage.artifact_dir,
        settings.adapters.workspace.root_dir,
        settings.adapters.browser.screenshot_dir,
        settings.adapters.computer_use.screenshot_dir,
        # Files produced by code.interpreter live here - without this entry,
        # "generate a file and send it" requests can't deliver the result.
        settings.adapters.code_interpreter.workspace_root,
        *settings.adapters.computer_use.allowed_roots,
    ]
