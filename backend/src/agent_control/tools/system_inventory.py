"""Read-only questions about this computer: what is installed, how much room is left, where a file is.

"How many Steam games do I have?" and "where is my resume?" are the questions a person asks a
computer-wide assistant first, and the file tools cannot answer either: Steam's library lives
outside every folder YBM may browse, and the model only guesses at one folder to search (it
chose the Desktop; the file was in OneDrive). This tool answers from the places that already
know:

    - ``steam_games``    Steam's own library manifests (every library folder, every drive)
    - ``installed_apps`` the list Windows keeps for "Apps & features"
    - ``disk_space``     free and total space per drive
    - ``find_files``     the Windows Search index (a full-text index of the user's files that
                         already exists and answers in milliseconds), or a bounded folder walk
                         where there is no index

Everything here only reads. ``find_files`` only returns files inside the folders YBM is allowed
to read, so that what it finds can be opened by the next step.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent_control.schemas import Capability, RiskLevel, ToolCallRequest, ToolCallResult, ToolResultStatus
from agent_control.tools.contracts import SystemInventoryInput, SystemInventoryOutput
from agent_control.tools.spec import (
    Adapters,
    Definitions,
    RegistryDeps,
    ToolDefinition,
    capability_enabled,
    failed_result,
    same_output_schema,
)

OPERATIONS = ("steam_games", "installed_apps", "disk_space", "find_files")

#: Steam installs these next to the games; they are not games a person would count.
_STEAM_NOT_GAMES = {228980}
_STEAM_NOT_GAMES_NAMES = re.compile(r"^(Steamworks Common Redistributables|Steam Linux Runtime|Proton )", re.IGNORECASE)

_DOCUMENT_EXTENSIONS = {".docx", ".doc", ".pdf", ".xlsx", ".xls", ".pptx", ".ppt", ".txt", ".md", ".rtf", ".odt", ".csv"}
_MAX_WALKED = 200_000
_INDEX_TIMEOUT_SECONDS = 25

_INDEX_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$conn = New-Object -ComObject ADODB.Connection
$rs = New-Object -ComObject ADODB.Recordset
$conn.Open("Provider=Search.CollatorDSO;Extended Properties='Application=Windows';")
$rs.Open($env:YBM_SQL, $conn)
$rows = @()
while (-not $rs.EOF) {
  $m = $rs.Fields.Item(2).Value
  $when = if ($m) { ([datetime]$m).ToUniversalTime().ToString('s') } else { '' }
  $rows += [pscustomobject]@{ path = [string]$rs.Fields.Item(0).Value; size = $rs.Fields.Item(1).Value; modified = $when }
  $rs.MoveNext()
}
$rs.Close()
ConvertTo-Json -InputObject @($rows) -Compress
"""


# ---- Steam ----------------------------------------------------------------------------------

def _steam_roots() -> list[Path]:
    roots: list[Path] = []
    if sys.platform == "win32":
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
                roots.append(Path(str(winreg.QueryValueEx(key, "SteamPath")[0])))
        except OSError:
            pass
        for variable in ("ProgramFiles(x86)", "ProgramFiles"):
            if os.environ.get(variable):
                roots.append(Path(os.environ[variable]) / "Steam")
    else:
        home = Path.home()
        roots += [home / ".steam" / "steam", home / ".local" / "share" / "Steam",
                  home / "Library" / "Application Support" / "Steam"]
    seen: set[str] = set()
    return [r for r in roots if r.is_dir() and not (str(r).lower() in seen or seen.add(str(r).lower()))]


def _acf_value(text: str, key: str) -> str | None:
    match = re.search(rf'"{key}"\s+"([^"]*)"', text, re.IGNORECASE)
    return match.group(1) if match else None


def steam_libraries(steam_root: Path) -> list[Path]:
    """Every library folder Steam knows about, the Steam folder itself first."""
    libraries = [steam_root]
    vdf = steam_root / "steamapps" / "libraryfolders.vdf"
    if vdf.is_file():
        for raw in re.findall(r'"path"\s+"([^"]+)"', vdf.read_text(encoding="utf-8", errors="replace")):
            path = Path(raw.replace("\\\\", "\\"))
            if path.is_dir() and path not in libraries:
                libraries.append(path)
    return libraries


def steam_games() -> list[dict[str, Any]]:
    games: dict[str, dict[str, Any]] = {}
    for root in _steam_roots():
        for library in steam_libraries(root):
            for manifest in sorted((library / "steamapps").glob("appmanifest_*.acf")):
                try:
                    text = manifest.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                app_id, name = _acf_value(text, "appid"), _acf_value(text, "name")
                if not app_id or not name or int(app_id) in _STEAM_NOT_GAMES or _STEAM_NOT_GAMES_NAMES.match(name):
                    continue
                size = _acf_value(text, "SizeOnDisk")
                games.setdefault(app_id, {
                    "name": name, "app_id": app_id, "library": str(library),
                    "size_gb": round(int(size) / 1024**3, 1) if size and size.isdigit() else None,
                })
    return sorted(games.values(), key=lambda g: g["name"].casefold())


# ---- Installed apps and disks ----------------------------------------------------------------

def installed_apps() -> list[dict[str, str]]:
    if sys.platform != "win32":
        return []
    import winreg

    paths = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]
    apps: dict[str, dict[str, str]] = {}
    for hive, path in paths:
        try:
            parent = winreg.OpenKey(hive, path)
        except OSError:
            continue
        with parent:
            for index in range(winreg.QueryInfoKey(parent)[0]):
                try:
                    with winreg.OpenKey(parent, winreg.EnumKey(parent, index)) as child:
                        def value(name: str) -> Any:
                            try:
                                return winreg.QueryValueEx(child, name)[0]
                            except OSError:
                                return None
                        name = str(value("DisplayName") or "").strip()
                        if not name or value("SystemComponent") == 1 or value("ParentKeyName"):
                            continue
                        apps.setdefault(name.casefold(), {
                            "name": name, "version": str(value("DisplayVersion") or ""),
                            "publisher": str(value("Publisher") or ""),
                        })
                except OSError:
                    continue
    return sorted(apps.values(), key=lambda a: a["name"].casefold())


def disk_space() -> list[dict[str, Any]]:
    drives = [f"{letter}:\\" for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"] if sys.platform == "win32" else ["/"]
    disks = []
    for drive in drives:
        if os.path.exists(drive):
            try:
                usage = shutil.disk_usage(drive)
            except OSError:
                continue
            disks.append({"drive": drive, "free_gb": round(usage.free / 1024**3, 1), "total_gb": round(usage.total / 1024**3, 1)})
    return disks


# ---- Finding files ---------------------------------------------------------------------------

_SAFE_WORD = re.compile(r"[^\w.\- ]", re.UNICODE)


def _words(query: str) -> list[str]:
    return [w for w in (_SAFE_WORD.sub("", part) for part in query.split()) if w]


def _index_sql(words: list[str], extensions: list[str], match: str, cap: int) -> str:
    clauses = ["System.ItemType <> 'Directory'"]
    if match == "content":
        clauses.append("CONTAINS('" + " AND ".join(f'\"{w}\"' for w in words) + "')")
    else:
        clauses += [f"System.FileName LIKE '%{w}%'" for w in words]
    if extensions:
        clauses.append("(" + " OR ".join(f"System.FileExtension = '{e}'" for e in extensions) + ")")
    return (
        f"SELECT TOP {cap} System.ItemPathDisplay, System.Size, System.DateModified FROM SystemIndex "
        f"WHERE {' AND '.join(clauses)} ORDER BY System.DateModified DESC"
    )


def search_index(words: list[str], extensions: list[str], match: str, cap: int) -> list[dict[str, Any]] | None:
    """Files from the Windows Search index, or None when there is no index to ask."""
    if sys.platform != "win32":
        return None
    try:
        done = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", _INDEX_SCRIPT],
            env={**os.environ, "YBM_SQL": _index_sql(words, extensions, match, cap)},
            capture_output=True, text=True, timeout=_INDEX_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if done.returncode != 0 or not done.stdout.strip():
        return None
    try:
        rows = json.loads(done.stdout)
    except ValueError:
        return None
    return [{"path": r["path"], "size_bytes": r.get("size"), "modified": r.get("modified") or None} for r in rows if r.get("path")]


def walk_search(roots: list[Path], words: list[str], extensions: list[str], cap: int) -> list[dict[str, Any]]:
    """The fallback where there is no index: a bounded walk of the allowed folders, by file name."""
    found: list[dict[str, Any]] = []
    visited = 0
    wanted = [w.casefold() for w in words]
    for root in roots:
        for folder, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d not in {"node_modules", "__pycache__", "AppData"}]
            for name in files:
                visited += 1
                if visited > _MAX_WALKED:
                    return found
                lowered = name.casefold()
                if all(w in lowered for w in wanted) and (not extensions or Path(name).suffix.casefold() in extensions):
                    path = Path(folder) / name
                    try:
                        info = path.stat()
                    except OSError:
                        continue
                    found.append({
                        "path": str(path), "size_bytes": info.st_size,
                        "modified": datetime.fromtimestamp(info.st_mtime, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
                    })
                    if len(found) >= cap:
                        return found
    return found


def _inside(path: str, roots: list[Path]) -> bool:
    candidate = Path(path)
    return any(root == candidate or root in candidate.parents for root in roots)


def _ranked(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Documents before everything else, newest first within each group."""
    newest_first = sorted(items, key=lambda item: item.get("modified") or "", reverse=True)
    return sorted(newest_first, key=lambda item: Path(item["path"]).suffix.casefold() not in _DOCUMENT_EXTENSIONS)


class SystemInventoryAdapter:
    def __init__(self, allowed_roots: list[str]) -> None:
        self.allowed_roots = [Path(root).expanduser().resolve() for root in allowed_roots]

    async def execute(self, request: ToolCallRequest) -> ToolCallResult:
        operation = str(request.input.get("operation") or "")
        try:
            if operation == "steam_games":
                output = self._steam()
            elif operation == "installed_apps":
                output = self._apps(str(request.input.get("query") or ""), int(request.input.get("limit") or 60))
            elif operation == "disk_space":
                output = self._disks()
            elif operation == "find_files":
                output = self._find(request)
            else:
                return failed_result(request, f"unsupported system.inventory operation: {operation}")
        except Exception as exc:  # noqa: BLE001 - the adapter reports, the loop decides
            return failed_result(request, f"system.inventory {operation} failed: {exc}")
        output["operation"] = operation
        output["terminal_output"] = [{"content": output["summary"], "is_final": True, "exit_code": 0}]
        return ToolCallResult(request_id=request.id, status=ToolResultStatus.SUCCEEDED, output=output)

    @staticmethod
    def _steam() -> dict[str, Any]:
        games = steam_games()
        if not _steam_roots():
            return {"count": 0, "items": [], "summary": "Steam does not seem to be installed on this computer."}
        if not games:
            return {"count": 0, "items": [], "summary": "Steam is installed but no games are installed in it."}
        total = sum(g["size_gb"] or 0 for g in games)
        names = ", ".join(g["name"] for g in games)
        return {
            "count": len(games), "items": games,
            "summary": f"{len(games)} games are installed through Steam (about {total:.0f} GB in all): {names}.",
        }

    @staticmethod
    def _apps(query: str, limit: int) -> dict[str, Any]:
        apps = installed_apps()
        if query:
            apps = [a for a in apps if query.casefold() in a["name"].casefold() or query.casefold() in a["publisher"].casefold()]
        shown = apps[:limit]
        lines = [f"- {a['name']}" + (f" {a['version']}" if a["version"] else "") for a in shown]
        more = f"\n...and {len(apps) - len(shown)} more." if len(apps) > len(shown) else ""
        scope = f" matching '{query}'" if query else ""
        return {
            "count": len(apps), "items": shown,
            "summary": f"{len(apps)} installed apps{scope}." + ("\n" + "\n".join(lines) if lines else "") + more,
        }

    @staticmethod
    def _disks() -> dict[str, Any]:
        disks = disk_space()
        lines = [f"- {d['drive']} {d['free_gb']} GB free of {d['total_gb']} GB" for d in disks]
        return {"count": len(disks), "items": disks, "summary": "Disk space:\n" + "\n".join(lines)}

    def _find(self, request: ToolCallRequest) -> dict[str, Any]:
        query = str(request.input.get("query") or "")
        words = _words(query)
        if not words:
            raise ValueError("query is required")
        limit = max(1, min(int(request.input.get("limit") or 20), 100))
        extensions = [e if e.startswith(".") else f".{e}" for e in (request.input.get("extensions") or [])]
        extensions = [e.casefold() for e in extensions if re.fullmatch(r"\.[A-Za-z0-9]{1,8}", e)]
        match = "content" if request.input.get("match") == "content" else "name"
        rows = search_index(words, extensions, match, cap=limit * 12)
        source = "the Windows Search index"
        if rows is None:
            rows = walk_search(self.allowed_roots, words, extensions, cap=limit * 12) if match == "name" else []
            source = "a search of the folders I can read"
        readable = [r for r in rows if _inside(r["path"], self.allowed_roots)]
        skipped = len(rows) - len(readable)
        items = _ranked(readable)[:limit]
        for item in items:
            item["name"] = Path(item["path"]).name
        if not items:
            note = f" ({skipped} matches are in folders I am not allowed to read)" if skipped else ""
            return {"count": 0, "items": [], "source": source, "summary": f"No file matching '{query}' was found{note}."}
        lines = [f"- {i['path']} ({(i.get('modified') or '')[:10] or 'date unknown'})" for i in items]
        note = f"\n({skipped} more matches are in folders I am not allowed to read.)" if skipped else ""
        return {
            "count": len(items), "items": items, "source": source,
            "summary": f"Found {len(items)} file(s) for '{query}' using {source}:\n" + "\n".join(lines) + note,
        }


def register(deps: RegistryDeps, definitions: Definitions, adapters: Adapters) -> None:
    settings = deps.settings
    enabled = capability_enabled(settings, Capability.SYSTEM_INVENTORY)
    definitions.append(
        ToolDefinition(
            name="system.inventory",
            capability=Capability.SYSTEM_INVENTORY,
            enabled=enabled,
            named_when_disabled=False,
            description=(
                "answer questions about this computer from the places that already know: how many Steam "
                "games are installed, which apps are installed, free disk space, or find a file anywhere "
                "by name or content (Windows Search). Read-only; prefer it over opening apps or searching "
                "one folder at a time"
            ),
            operations=OPERATIONS,
            input_schema=SystemInventoryInput,
            output_schema=SystemInventoryOutput,
            operation_output_schemas=same_output_schema(OPERATIONS, SystemInventoryOutput),
            default_operation="find_files",
            operation_risks={operation: RiskLevel.LOW for operation in OPERATIONS},
            examples=(
                {"operation": "steam_games"},
                {"operation": "find_files", "query": "resume", "extensions": ["docx", "pdf"]},
                {"operation": "installed_apps", "query": "python"},
                {"operation": "disk_space"},
            ),
        )
    )
    if enabled:
        adapters["system.inventory"] = SystemInventoryAdapter(settings.adapters.computer_use.allowed_roots)  # type: ignore[arg-type]
