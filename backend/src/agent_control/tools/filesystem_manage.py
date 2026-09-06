from __future__ import annotations

import fnmatch
import json
import logging
from pathlib import Path
import os
import re
import shutil
from typing import Any

from agent_control.llm.providers import LLMProvider
from agent_control.prompts import prompt_text, render_prompt
from agent_control.schemas import Capability, RiskLevel, ToolCallRequest, ToolCallResult, ToolResultStatus, ToolVerification
from agent_control.tools.contracts import (
    FilesystemApplyManifestInput,
    FilesystemCollectFolderSnapshotInput,
    FilesystemDescribeFolderInput,
    FilesystemFindByDescriptionInput,
    FilesystemInspectInput,
    FilesystemManageOutput,
    FilesystemOpenFileInput,
    FilesystemOrganizePlanInput,
    FilesystemReadFileInput,
    FilesystemRenamePlanInput,
    FilesystemResolveDesktopItemInput,
    FilesystemSearchInput,
    FilesystemWriteTextFileInput,
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


class FilesystemManageAdapter:
    """Scoped filesystem inspection, search, and organization tool."""

    def __init__(self, allowed_roots: list[str], provider: LLMProvider | None = None) -> None:
        self.allowed_roots = [_resolve_root(root) for root in allowed_roots]
        self.provider = provider

    async def execute(self, request: ToolCallRequest) -> ToolCallResult:
        operation = str(request.input.get("operation") or "inspect_folder")
        try:
            if operation == "inspect_folder":
                output = self._inspect_folder(request)
            elif operation == "search":
                output = self._search(request)
            elif operation == "resolve_desktop_item":
                output = self._resolve_desktop_item(request)
            elif operation == "find_by_description":
                output = self._find_by_description(request)
            elif operation == "open_file":
                output = self._open_file(request)
            elif operation == "read_file":
                output = self._read_file(request)
            elif operation == "write_text_file":
                output = self._write_text_file(request)
            elif operation == "collect_folder_snapshot":
                output = self._collect_folder_snapshot(request)
            elif operation == "describe_folder":
                output = await self._describe_folder(request)
            elif operation == "organize_plan":
                output = self._organize_plan(request)
            elif operation == "rename_plan":
                output = self._rename_plan(request)
            elif operation == "apply_manifest":
                output = self._apply_manifest(request)
            else:
                return failed_result(request, f"unsupported filesystem operation: {operation}")
        except Exception as exc:
            return failed_result(request, f"filesystem operation failed: {exc}")

        output["operation"] = operation
        output["terminal_output"] = [_terminal_output(operation, output)]
        return ToolCallResult(request_id=request.id, status=ToolResultStatus.SUCCEEDED, output=output)

    def _inspect_folder(self, request: ToolCallRequest) -> dict[str, Any]:
        root = self._safe_path(str(request.input["root"]))
        if not root.exists() or not root.is_dir():
            raise ValueError(f"folder does not exist: {root}")
        max_depth = int(request.input.get("max_depth") or 2)
        max_entries = int(request.input.get("max_entries") or 200)
        entries = []
        for path in _walk_limited(root, max_depth=max_depth):
            if path == root:
                continue
            entries.append(_entry(root, path))
            if len(entries) >= max_entries:
                break
        return {
            "root": str(root),
            "entries": entries,
            "summary": f"Found {len(entries)} item(s) under {root}.",
        }

    def _search(self, request: ToolCallRequest) -> dict[str, Any]:
        root = self._safe_path(str(request.input["root"]))
        query = str(request.input["query"]).lower()
        wildcard_query = any(char in query for char in "*?[")
        include_content = bool(request.input.get("include_content", False))
        max_results = int(request.input.get("max_results") or 100)
        results = []
        for path in _walk_limited(root, max_depth=20):
            if path == root:
                continue
            matched = (
                fnmatch.fnmatch(path.name.lower(), query)
                if wildcard_query
                else query in path.name.lower()
            )
            content_preview = None
            content_summary = None
            if include_content and path.is_file() and _is_text_file(path):
                text = path.read_text(encoding="utf-8", errors="ignore")
                index = text.lower().find(query)
                matched = matched or index >= 0
                if index >= 0:
                    start = max(0, index - 120)
                    content_preview = text[start : index + 240].strip()
                elif matched:
                    content_preview = _compact_text(text, limit=1800)
                if content_preview:
                    content_summary = _simple_summary(content_preview)
            elif include_content and matched and path.is_file():
                text = _extract_supported_text(path, max_chars=3000)
                if text:
                    content_preview = text
                    content_summary = _simple_summary(text)
            if matched:
                item = _entry(root, path)
                if content_preview:
                    item["content_preview"] = content_preview
                if content_summary:
                    item["content_summary"] = content_summary
                results.append(item)
                if len(results) >= max_results:
                    break
        return {
            "root": str(root),
            "entries": results,
            "summary": f"Found {len(results)} result(s) for {request.input['query']!r} under {root}.",
        }

    def _resolve_desktop_item(self, request: ToolCallRequest) -> dict[str, Any]:
        query = str(request.input.get("query") or request.input.get("name") or "").lower().strip()
        item_type = str(request.input.get("item_type") or "any")
        roots = [root for root in self._candidate_roots("desktop") if root.exists()]
        entries: list[dict[str, Any]] = []
        for root in roots:
            for path in root.iterdir():
                if item_type == "file" and not path.is_file():
                    continue
                if item_type == "folder" and not path.is_dir():
                    continue
                if query and query not in path.name.lower():
                    continue
                entries.append(_entry(root, path))
        return {
            "root": str(roots[0]) if roots else None,
            "entries": entries,
            "summary": f"Resolved {len(entries)} desktop item(s) matching {query or '*'}."
        }

    def _find_by_description(self, request: ToolCallRequest) -> dict[str, Any]:
        root_value = request.input.get("root")
        root = self._safe_path(str(root_value)) if root_value else self._first_existing_root()
        description = str(request.input["description"]).lower()
        max_results = int(request.input.get("max_results") or 20)
        terms = [term for term in description.replace(".", " ").replace("_", " ").split() if len(term) >= 3]
        entries = []
        for path in _walk_limited(root, max_depth=20):
            if path == root:
                continue
            name = path.name.lower()
            score = sum(1 for term in terms if term in name)
            if score <= 0 and any(term in description for term in (path.suffix.lower().lstrip("."), _type_bucket(path))):
                score = 1
            if score > 0:
                item = _entry(root, path)
                item["score"] = score
                entries.append(item)
        entries.sort(key=lambda item: (-int(item.get("score") or 0), str(item.get("relative_path") or "")))
        entries = entries[:max_results]
        return {
            "root": str(root),
            "entries": entries,
            "summary": f"Found {len(entries)} item(s) matching the description under {root}.",
        }

    def _open_file(self, request: ToolCallRequest) -> dict[str, Any]:
        path = self._safe_path(str(request.input["path"]))
        if not path.exists():
            raise ValueError(f"path does not exist: {path}")
        os.startfile(str(path))  # type: ignore[attr-defined]
        return {
            "root": str(path.parent),
            "entries": [_entry(path.parent, path)],
            "changed_paths": [str(path)],
            "summary": f"Opened {path}.",
        }

    def _read_file(self, request: ToolCallRequest) -> dict[str, Any]:
        path = self._safe_path(str(request.input["path"]))
        if not path.exists() or not path.is_file():
            raise ValueError(f"file does not exist: {path}")
        max_chars = int(request.input.get("max_chars") or 12000)
        text = _extract_supported_text(path, max_chars=max_chars)
        if not text:
            text = path.read_bytes()[:max_chars].decode("utf-8", errors="ignore")
        text = _compact_text(text, limit=max_chars)
        summary = _simple_summary(text) if text else "No readable text was extracted from this file."
        return {
            "root": str(path.parent),
            "path": str(path),
            "entries": [_entry(path.parent, path)],
            "text": text,
            "content_preview": text[:4000],
            "content_summary": summary,
            "summary": f"Read {path.name}. {summary}",
        }

    def _write_text_file(self, request: ToolCallRequest) -> dict[str, Any]:
        path = self._safe_path(str(request.input["path"]))
        overwrite = bool(request.input.get("overwrite", False))
        original_path = path
        renamed = False
        # If the destination already exists and the caller didn't ask to
        # overwrite, auto-rename to a non-colliding sibling (foo.txt → foo-2.txt
        # → foo-3.txt ...). Never silently destroy data. Subsequent steps can
        # see the actual path via `path` in the response.
        if path.exists() and not overwrite:
            path = _dedupe_destination(path)
            renamed = True
        path.parent.mkdir(parents=True, exist_ok=True)
        content = str(request.input.get("content") or "")
        path.write_text(content, encoding="utf-8")
        result: dict[str, Any] = {
            "root": str(path.parent),
            "path": str(path),
            "entries": [_entry(path.parent, path)],
            "changed_paths": [str(path)],
            "summary": (
                f"Wrote text file {path}."
                if not renamed
                else f"Wrote text file {path} (requested path {original_path.name} already existed; auto-renamed)."
            ),
        }
        if renamed:
            result["renamed_from"] = str(original_path)
        return result

    def _collect_folder_snapshot(self, request: ToolCallRequest) -> dict[str, Any]:
        return self._inspect_folder(request)

    async def _describe_folder(self, request: ToolCallRequest) -> dict[str, Any]:
        root = self._safe_path(str(request.input["root"]))
        if not root.exists() or not root.is_dir():
            raise ValueError(f"folder does not exist: {root}")
        recursive = bool(request.input.get("recursive", False))
        include_ocr = bool(request.input.get("include_ocr", True))
        max_files = int(request.input.get("max_files") or 50)
        max_chars = int(request.input.get("max_chars_per_file") or 4000)
        iterator = _ordered_children(root, recursive=recursive)
        files: list[dict[str, Any]] = []
        for path in iterator:
            if len(files) >= max_files:
                break
            if not path.is_file():
                continue
            item = _entry(root, path)
            item["kind"] = _type_bucket(path)
            item["extension"] = path.suffix.lower()
            text = _extract_supported_text(path, max_chars=max_chars)
            if text:
                item["content_preview"] = _compact_text(text, limit=1200)
                item["content_summary"] = _simple_summary(text)
                item["extracted_text_chars"] = len(text)
            elif _is_image_file(path):
                item.update(await self._image_description(path, include_ocr=include_ocr))
            else:
                item["content_summary"] = "No supported text extraction is available for this file type."
            files.append(item)
        readable = sum(1 for item in files if item.get("content_preview") or item.get("ocr_text"))
        images = sum(1 for item in files if item.get("kind") == "images")
        return {
            "root": str(root),
            "entries": files,
            "file_descriptions": files,
            "summary": (
                f"Described {len(files)} file(s) under {root}. "
                f"Extracted readable text from {readable} file(s); inspected {images} image file(s)."
            ),
        }

    async def _image_description(self, path: Path, *, include_ocr: bool) -> dict[str, Any]:
        output: dict[str, Any] = {"ocr_status": "not_requested" if not include_ocr else "unavailable"}
        try:
            from PIL import Image

            with Image.open(path) as image:
                output["image_size"] = {"width": image.width, "height": image.height}
        except Exception as exc:
            output["image_error"] = str(exc)
        if not include_ocr:
            output["content_summary"] = "Image OCR was not requested."
            return output
        if self.provider is None:
            output["content_summary"] = "Image OCR needs a local multimodal provider; none is configured for filesystem.manage."
            return output
        try:
            description = await self.provider.generate_multimodal_text(
                prompt_text("base/folder_image_ocr_system.md"),
                render_prompt("tasks/folder_image_ocr_user.md", file_name=path.name),
                [str(path)],
            )
        except Exception as exc:
            output["ocr_status"] = "failed"
            output["ocr_error"] = str(exc)
            output["content_summary"] = "Image OCR failed; no visual content was inferred."
            return output
        output["ocr_status"] = "completed"
        output["ocr_text"] = _compact_text(description, limit=1200)
        output["content_summary"] = _simple_summary(description)
        return output

    def _organize_plan(self, request: ToolCallRequest) -> dict[str, Any]:
        root = self._safe_path(str(request.input["root"]))
        if not root.exists() or not root.is_dir():
            raise ValueError(f"folder does not exist: {root}")
        strategy = str(request.input.get("strategy") or "by_type")
        recursive = bool(request.input.get("recursive", False))
        max_files = int(request.input.get("max_files") or 1000)
        manifest = []
        iterator = _ordered_children(root, recursive=recursive)
        for path in iterator:
            if len(manifest) >= max_files:
                break
            if not path.is_file():
                continue
            bucket = _extension_bucket(path) if strategy == "by_extension" else _type_bucket(path)
            destination = _dedupe_destination(root / bucket / path.name)
            if destination.resolve() == path.resolve():
                continue
            manifest.append(
                {
                    "operation": "move",
                    "source": str(path.resolve()),
                    "destination": str(destination.resolve()),
                    "reason": f"Group by {strategy}: {bucket}",
                }
            )
        return {
            "root": str(root),
            "manifest": manifest,
            "dry_run": True,
            "summary": f"Prepared {len(manifest)} file organization action(s) for {root}.",
        }

    def _rename_plan(self, request: ToolCallRequest) -> dict[str, Any]:
        root = self._safe_path(str(request.input["root"]))
        if not root.exists() or not root.is_dir():
            raise ValueError(f"folder does not exist: {root}")
        recursive = bool(request.input.get("recursive", False))
        max_files = int(request.input.get("max_files") or 1000)
        manifest = []
        rename_manifest = []
        iterator = _ordered_children(root, recursive=recursive)
        for path in iterator:
            if len(manifest) >= max_files:
                break
            if not path.is_file():
                continue
            stem = _rename_stem(path)
            destination = _dedupe_destination(path.with_name(f"{stem}{path.suffix.lower()}"))
            if destination.resolve() == path.resolve():
                continue
            item = {
                "operation": "rename",
                "source": str(path.resolve()),
                "destination": str(destination.resolve()),
                "before_name": path.name,
                "after_name": destination.name,
                "reason": "Rename based on readable file content and existing filename.",
            }
            manifest.append(item)
            rename_manifest.append(
                {
                    "before": path.name,
                    "after": destination.name,
                    "source": str(path.resolve()),
                    "destination": str(destination.resolve()),
                    "reason": item["reason"],
                }
            )
        return {
            "root": str(root),
            "manifest": manifest,
            "rename_manifest": rename_manifest,
            "dry_run": True,
            "summary": f"Prepared {len(rename_manifest)} rename action(s) with before/after names.",
        }

    def _apply_manifest(self, request: ToolCallRequest) -> dict[str, Any]:
        root = self._safe_path(str(request.input["root"]))
        manifest = request.input.get("manifest") or []
        dry_run = bool(request.input.get("dry_run", False))
        overwrite = bool(request.input.get("overwrite", False))
        changed_paths = []
        normalized_manifest = []
        for item in manifest:
            # A manifest is scoped by `root`, so its relative paths must be
            # relative to that root. Resolving them from the process cwd made
            # the most natural model output (`budget.csv` -> `data/budget.csv`)
            # point at the repository instead and then fail the containment
            # check as an apparent escape.
            source = self._manifest_path(root, str(item["source"]))
            destination = self._manifest_path(root, str(item["destination"]))
            if root not in source.parents and root != source:
                raise ValueError(f"source is outside requested root: {source}")
            if root not in destination.parents and root != destination:
                raise ValueError(f"destination is outside requested root: {destination}")
            operation = str(item.get("operation") or "move")
            normalized_manifest.append({**item, "source": str(source), "destination": str(destination)})
            if dry_run:
                continue
            if not source.exists() or not source.is_file():
                raise ValueError(f"source file does not exist: {source}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists() and not overwrite:
                destination = _dedupe_destination(destination)
            if operation == "copy":
                shutil.copy2(source, destination)
            elif operation in {"move", "rename"}:
                shutil.move(str(source), str(destination))
            else:
                raise ValueError(f"unsupported manifest operation: {operation}")
            changed_paths.append(str(destination))
        rename_items = [
            item
            for item in normalized_manifest
            if str(item.get("operation") or "") == "rename"
            or Path(str(item.get("source"))).parent == Path(str(item.get("destination"))).parent
        ]
        return {
            "root": str(root),
            "manifest": normalized_manifest,
            "rename_manifest": [
                {
                    "before": Path(str(item["source"])).name,
                    "after": Path(str(item["destination"])).name,
                    "source": item["source"],
                    "destination": item["destination"],
                    "reason": item.get("reason"),
                }
                for item in rename_items
            ],
            "changed_paths": changed_paths,
            "dry_run": dry_run,
            "summary": (
                f"Would change {len(normalized_manifest)} file organization action(s)."
                if dry_run
                else (
                    f"Renamed {len(changed_paths)} file(s); before/after table is recorded."
                    if rename_items
                    else f"Moved {len(changed_paths)} file(s); changed {len(changed_paths)} path(s)."
                )
            ),
        }

    def _manifest_path(self, root: Path, value: str) -> Path:
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        return self._safe_path(str(candidate))

    def _safe_path(self, value: str) -> Path:
        if not self.allowed_roots:
            raise ValueError("no allowed filesystem roots are configured")
        # Some LLMs hallucinate placeholder usernames like "C:\Users\me\Desktop\foo.pdf".
        # Recover by detecting that pattern and rewriting to the real user's home.
        rewritten = _rewrite_placeholder_user_path(value)
        path = self._alias_path(rewritten).expanduser().resolve()
        if not any(root == path or root in path.parents for root in self.allowed_roots):
            allowed = ", ".join(str(root) for root in self.allowed_roots)
            raise ValueError(f"path is outside allowed roots: {path}; allowed roots: {allowed}")
        return path

    def _candidate_roots(self, alias: str) -> list[Path]:
        """Allowed roots reachable from an alias like "desktop".

        The alias path used to be included unconditionally - ``root ==
        alias_path`` is trivially true for it - so the desktop was enumerated
        even when it was not an allowed root, making this the one operation
        that read outside the configured boundary. It is kept only when an
        allowed root actually covers it.
        """
        alias_path = self._alias_path(alias).resolve()
        covered = any(root == alias_path or root in alias_path.parents for root in self.allowed_roots)
        candidates = [alias_path] if covered else []
        return candidates + [
            root
            for root in self.allowed_roots
            if root != alias_path and (alias_path in root.parents or root in alias_path.parents)
        ]

    def _first_existing_root(self) -> Path:
        for root in self.allowed_roots:
            if root.exists():
                return root
        raise ValueError("none of the allowed roots exists")

    @staticmethod
    def _alias_path(value: str) -> Path:
        normalized = value.strip().strip("\"'").replace("/", "\\")
        lowered = normalized.lower()
        home = Path.home()
        if lowered in {"desktop", "%desktop%"}:
            return home / "Desktop"
        if lowered in {"documents", "my documents", "%documents%"}:
            return home / "Documents"
        if lowered in {"downloads", "%downloads%"}:
            return home / "Downloads"
        if lowered in {"home", "user", "my directory", "my folder"}:
            return home
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
        return Path(value)


_PLACEHOLDER_USERNAMES = frozenset({
    "me", "user", "username", "<user>", "<username>",
    "{user}", "{username}", "your_user", "your_username", "youruser",
})


def _rewrite_placeholder_user_path(value: str) -> str:
    """Replace `C:\\Users\\<placeholder>` (e.g. \\Users\\me) with the actual user home.

    The LLM occasionally synthesizes paths with a literal placeholder username
    because it doesn't actually know the system user. We detect that pattern
    and rewrite to the real ``Path.home()`` so the request can still succeed.
    """
    text = str(value).strip().strip("\"'")
    normalized = text.replace("/", "\\")
    lowered = normalized.lower()
    marker = "\\users\\"
    idx = lowered.find(marker)
    if idx < 0:
        return value
    rest_start = idx + len(marker)
    sep_idx = lowered.find("\\", rest_start)
    user_segment = lowered[rest_start:sep_idx] if sep_idx > 0 else lowered[rest_start:]
    if user_segment not in _PLACEHOLDER_USERNAMES:
        return value
    # Splice from the normalized (back-slash) version so the tail separators are consistent.
    tail = normalized[(sep_idx if sep_idx > 0 else len(normalized)):]
    home = Path.home()
    return str(home) + tail


def _resolve_root(value: str) -> Path:
    return Path(value).expanduser().resolve()


def _ordered_children(root: Path, *, recursive: bool):
    """Directory entries in a stable order, on every platform.

    `Path.iterdir()` and `rglob()` yield in whatever order the filesystem
    returns. NTFS keeps a sorted index so Windows looks alphabetical; ext4
    hashes names, so Linux order is arbitrary and can differ between two
    machines listing the same directory.

    That matters twice over. Every one of these callers truncates at
    `max_files`, so an unstable order changes *which* files the model is shown,
    not merely their sequence. And a recorded scenario replays the prompt built
    from that listing, so an unsorted listing cannot replay across platforms.

    `_walk_limited` (used by inspect_folder and search) already sorted with
    this key; these four callers did not.
    """
    iterator = root.rglob("*") if recursive else root.iterdir()
    return sorted(iterator, key=lambda path: (str(path).casefold(), str(path)))


def _walk_limited(root: Path, *, max_depth: int):
    yield root

    def walk(directory: Path, depth: int):
        if depth >= max_depth:
            return
        children = sorted(
            directory.iterdir(),
            key=lambda path: (path.name.casefold(), path.name),
        )
        for path in children:
            yield path
            if path.is_dir() and not path.is_symlink():
                yield from walk(path, depth + 1)

    yield from walk(root, 0)


def _entry(root: Path, path: Path) -> dict[str, Any]:
    stat = path.stat()
    resolved = path.resolve()
    try:
        relative = str(resolved.relative_to(root.resolve()))
    except ValueError:
        # Searches span several allowed roots (see _alias_roots), so an entry
        # found under one root can be paired with another here. relative_to
        # then raises, and the raw "'C:\\...' is not in the subpath of
        # 'C:\\...'" surfaced to the operator as if the tool had broken -
        # unreachable while only one root was configured, which is why it
        # went unnoticed. The absolute path is already in "path"; this field
        # is a display convenience, so degrade instead of failing the call.
        relative = resolved.name
    return {
        "path": str(resolved),
        "relative_path": relative,
        "is_dir": path.is_dir(),
        "size_bytes": stat.st_size if path.is_file() else None,
        "modified_at": stat.st_mtime,
    }


def _rename_stem(path: Path) -> str:
    text = _content_for_rename(path)
    tokens = [token.lower() for token in re.findall(r"[A-Za-z0-9]+", text)]
    stop_words = {
        "a",
        "an",
        "and",
        "are",
        "by",
        "file",
        "for",
        "in",
        "is",
        "of",
        "on",
        "or",
        "the",
        "this",
        "to",
        "with",
    }
    meaningful = [token for token in tokens if len(token) > 2 and token not in stop_words][:5]
    if not meaningful:
        meaningful = [token.lower() for token in re.findall(r"[A-Za-z0-9]+", path.stem) if token][:5]
    stem = "_".join(meaningful)[:80].strip("_")
    return stem or path.stem


def _content_for_rename(path: Path) -> str:
    if _is_text_file(path):
        return path.read_text(encoding="utf-8", errors="ignore")[:4000]
    try:
        return path.read_bytes()[:4000].decode("utf-8", errors="ignore")
    except Exception:
        logger.debug("failed to read bytes for rename hint at %s; falling back to stem", path, exc_info=True)
        return path.stem


def _type_bucket(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg"}:
        return "images"
    if ext in {".pdf", ".doc", ".docx", ".txt", ".md", ".rtf"}:
        return "documents"
    if ext in {".xls", ".xlsx", ".csv"}:
        return "spreadsheets"
    if ext in {".ppt", ".pptx"}:
        return "presentations"
    if ext in {".zip", ".rar", ".7z", ".tar", ".gz"}:
        return "archives"
    if ext in {".py", ".js", ".ts", ".html", ".css", ".json", ".yaml", ".yml"}:
        return "code"
    if ext in {".mp3", ".wav", ".flac", ".m4a"}:
        return "audio"
    if ext in {".mp4", ".mov", ".avi", ".mkv"}:
        return "video"
    return "other"


def _extension_bucket(path: Path) -> str:
    return path.suffix.lower().lstrip(".") or "no_extension"


def _dedupe_destination(path: Path) -> Path:
    candidate = path
    index = 2
    while candidate.exists():
        candidate = path.with_name(f"{path.stem}_{index}{path.suffix}")
        index += 1
    return candidate


def _is_text_file(path: Path) -> bool:
    return path.suffix.lower() in {".txt", ".md", ".py", ".js", ".ts", ".html", ".css", ".json", ".yaml", ".yml", ".csv"}


def _is_image_file(path: Path) -> bool:
    return path.suffix.lower() in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff"}


def _extract_supported_text(path: Path, *, max_chars: int) -> str:
    suffix = path.suffix.lower()
    if _is_text_file(path):
        text = path.read_text(encoding="utf-8", errors="ignore")
        if suffix in {".html", ".htm"}:
            text = re.sub(r"<script\b.*?</script>", " ", text, flags=re.IGNORECASE | re.DOTALL)
            text = re.sub(r"<style\b.*?</style>", " ", text, flags=re.IGNORECASE | re.DOTALL)
            text = re.sub(r"<[^>]+>", " ", text)
        return _compact_text(text, limit=max_chars)
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader

            reader = PdfReader(str(path))
            text = "\n\n".join((page.extract_text() or "").strip() for page in reader.pages).strip()
            return _compact_text(text, limit=max_chars)
        except Exception:
            logger.debug("pypdf extraction failed for %s; falling back to raw bytes decode", path, exc_info=True)
            return _compact_text(path.read_bytes().decode("utf-8", errors="ignore"), limit=max_chars)
    return ""


def _compact_text(text: str, *, limit: int) -> str:
    return re.sub(r"\s+", " ", text).strip()[:limit]


def _simple_summary(text: str) -> str:
    cleaned = _compact_text(text, limit=2000)
    if not cleaned:
        return "No readable content was extracted."
    sentences = re.split(r"(?<=[.!?])\s+", cleaned)
    return " ".join(sentences[:3]).strip()[:1000] or cleaned[:1000]


def _terminal_output(operation: str, output: dict[str, Any]) -> dict[str, Any]:
    content = _human_filesystem_output(operation, output)
    return {
        "content": content,
        "is_final": True,
        "exit_code": 0,
    }


def _human_filesystem_output(operation: str, output: dict[str, Any]) -> str:
    lines = [str(output.get("summary") or f"filesystem.manage {operation} completed.")]
    entries = output.get("entries")
    if isinstance(entries, list) and entries:
        lines.append("")
        lines.append("Entries:")
        for item in entries[:60]:
            if not isinstance(item, dict):
                continue
            kind = "folder" if item.get("is_dir") else "file"
            name = item.get("relative_path") or item.get("path") or ""
            size = item.get("size_bytes")
            suffix = f" ({size} bytes)" if size is not None and not item.get("is_dir") else ""
            lines.append(f"- [{kind}] {name}{suffix}")
            preview = item.get("content_preview") or item.get("content_summary")
            if preview:
                lines.append(f"  {str(preview)[:1200]}")
        if len(entries) > 60:
            lines.append(f"- ... {len(entries) - 60} more item(s)")
    text = output.get("text") or output.get("content_preview")
    if text and operation == "read_file":
        lines.append("")
        lines.append("Content:")
        lines.append(str(text)[:5000])
    manifest = output.get("manifest")
    if operation in {"organize_plan", "rename_plan"} and isinstance(manifest, list) and manifest:
        # The Operator only sees this human output on its next decide() call,
        # not the raw result object. Omitting the manifest meant it was told
        # merely "Prepared 3 actions" and then expected to invent those three
        # actions for apply_manifest. Keep the actionable hand-off bounded.
        lines.append("")
        lines.append("Manifest for the next apply_manifest call:")
        lines.append(json.dumps(manifest[:60], ensure_ascii=False, separators=(",", ":")))
    changed = output.get("changed_paths")
    if isinstance(changed, list) and changed:
        lines.append("")
        lines.append("Changed paths:")
        lines.extend(f"- {path}" for path in changed[:60])
    return "\n".join(lines)




def _verify_apply_manifest(request: ToolCallRequest, result: ToolCallResult) -> ToolVerification | None:
    """Re-reads the disk after a SUCCEEDED apply_manifest to confirm each
    manifest item's destination now actually exists, and that move/rename
    left nothing behind at the source (docs/ROADMAP.md "Proof": "34 files
    are now under Documents; confirmed" instead of trusting the adapter's
    own claim). Skipped for a dry run - nothing on disk changed, so there is
    nothing yet to re-check.

    Reads exclusively from the adapter's own reported output, never from
    the raw request: `manifest`'s source/destination are `_apply_manifest`'s
    already-resolved absolute paths (the request's own paths may be
    relative to `root`, which this function has no independent way to
    re-resolve), and `changed_paths` is the *actual* final destination -
    `_dedupe_destination` can rename around a collision after `manifest` is
    already recorded, so `manifest`'s own destination field is not always
    where the file really landed.
    """
    if request.input.get("dry_run"):
        return None
    manifest = result.output.get("manifest")
    changed_paths = result.output.get("changed_paths")
    if not isinstance(manifest, list) or not isinstance(changed_paths, list) or len(manifest) != len(changed_paths):
        return None
    checked = 0
    missing: list[str] = []
    for item, actual_destination in zip(manifest, changed_paths):
        if not isinstance(item, dict) or not isinstance(actual_destination, str) or not actual_destination:
            continue
        checked += 1
        if not Path(actual_destination).exists():
            missing.append(f"destination not found: {actual_destination}")
            continue
        operation = str(item.get("operation") or "move")
        source = item.get("source")
        if operation in {"move", "rename"} and isinstance(source, str) and source and Path(source).exists():
            missing.append(f"source still present after {operation}: {source}")
    if checked == 0:
        return None
    return ToolVerification(
        checked=checked,
        verified=checked - len(missing),
        missing=missing,
        detail=f"Re-checked {checked} manifest destination path(s) on disk after apply_manifest.",
    )


def _verify_filesystem_manage(request: ToolCallRequest, result: ToolCallResult) -> ToolVerification | None:
    if request.input.get("operation") != "apply_manifest":
        return None
    return _verify_apply_manifest(request, result)


def register(deps: RegistryDeps, definitions: Definitions, adapters: Adapters) -> None:
    settings = deps.settings
    enabled = (
        settings.adapters.computer_use.enabled
        and capability_enabled(settings, Capability.FILESYSTEM_WRITE)
    )
    definitions.append(
        ToolDefinition(
            name="filesystem.manage",
            capability=Capability.FILESYSTEM_WRITE,
            enabled=enabled,
            description=(
                "inspect, search, plan organization, and apply move/copy manifests inside configured "
                f"roots: {', '.join(settings.adapters.computer_use.allowed_roots) or '<none>'}"
            ),
            operations=(
                "inspect_folder",
                "search",
                "resolve_desktop_item",
                "find_by_description",
                "open_file",
                "read_file",
                "write_text_file",
                "collect_folder_snapshot",
                "describe_folder",
                "organize_plan",
                "rename_plan",
                "apply_manifest",
            ),
            operation_schemas={
                "inspect_folder": FilesystemInspectInput,
                "search": FilesystemSearchInput,
                "resolve_desktop_item": FilesystemResolveDesktopItemInput,
                "find_by_description": FilesystemFindByDescriptionInput,
                "open_file": FilesystemOpenFileInput,
                "read_file": FilesystemReadFileInput,
                "write_text_file": FilesystemWriteTextFileInput,
                "collect_folder_snapshot": FilesystemCollectFolderSnapshotInput,
                "describe_folder": FilesystemDescribeFolderInput,
                "organize_plan": FilesystemOrganizePlanInput,
                "rename_plan": FilesystemRenamePlanInput,
                "apply_manifest": FilesystemApplyManifestInput,
            },
            output_schema=FilesystemManageOutput,
            operation_output_schemas=same_output_schema(
                (
                    "inspect_folder",
                    "search",
                    "resolve_desktop_item",
                    "find_by_description",
                    "open_file",
                    "read_file",
                    "write_text_file",
                    "collect_folder_snapshot",
                    "describe_folder",
                    "organize_plan",
                    "rename_plan",
                    "apply_manifest",
                ),
                FilesystemManageOutput,
            ),
            default_operation="inspect_folder",
            operation_risks={
                "inspect_folder": RiskLevel.LOW,
                "search": RiskLevel.LOW,
                "resolve_desktop_item": RiskLevel.LOW,
                "find_by_description": RiskLevel.LOW,
                "read_file": RiskLevel.LOW,
                "collect_folder_snapshot": RiskLevel.LOW,
                "describe_folder": RiskLevel.LOW,
                "organize_plan": RiskLevel.LOW,
                "open_file": RiskLevel.MEDIUM,
                "write_text_file": RiskLevel.HIGH,
                "rename_plan": RiskLevel.HIGH,
                "apply_manifest": RiskLevel.HIGH,
            },
            verify=_verify_filesystem_manage,
            examples=(
                {"operation": "inspect_folder", "root": "{{folder_path}}"},
                {"operation": "search", "root": "desktop", "query": "resume"},
                {"operation": "read_file", "path": "{{last_entry_path}}", "max_chars": 8000},
                {"operation": "write_text_file", "path": "{{output_path}}", "content": "Report content"},
            ),
        )
    )
    if settings.adapters.computer_use.enabled:
        adapters["filesystem.manage"] = FilesystemManageAdapter(
            settings.adapters.computer_use.allowed_roots,
            provider=deps.provider,  # type: ignore[arg-type]
        )
