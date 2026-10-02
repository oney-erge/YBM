from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
from pathlib import Path
from typing import Any

from agent_control.config import AppSettings


logger = logging.getLogger(__name__)

SERVICE_STALE_SECONDS = 30

# The complete set of names ybm.ps1/supervisor.py ever supervise - static
# regardless of config (whether each is currently *expected* to run does
# vary, via _expected_services below). The admin log-viewer endpoint
# (docs/archive/UI_UX_AUDIT.md Phase 9) validates against this before turning a
# name into a file path, since a log for a now-disabled service (e.g.
# telegram_polling) should still be viewable from a past run.
KNOWN_SERVICE_NAMES = frozenset(
    {"localdeploy", "backend", "worker", "coding_session_watcher", "scheduler", "telegram_polling", "whatsapp"}
)


def service_summary(settings: AppSettings) -> dict[str, Any]:
    services = []
    for name, expected in _expected_services(settings).items():
        services.append(_service_record(name, expected))
    ready = all(not item["expected"] or item["ok"] for item in services)
    return {
        "ready": ready,
        "stale_after_seconds": SERVICE_STALE_SECONDS,
        "items": services,
    }


def _expected_services(settings: AppSettings) -> dict[str, bool]:
    # No separate "admin_ui" entry: that used to be the Streamlit process
    # (its own supervised service, own status.json). The React admin
    # console removed at cutover (docs/archive/UI_REWRITE_PLAN.md §19) is served by
    # this same backend process - server.admin_enabled gates the /admin
    # router directly, with nothing extra to supervise or health-check.
    return {
        "localdeploy": _expects_localdeploy(settings),
        "backend": True,
        "worker": True,
        "coding_session_watcher": True,
        "scheduler": bool(settings.scheduler.enabled),
        "telegram_polling": bool(settings.channels.telegram.enabled and settings.channels.telegram.polling),
        "whatsapp": bool(settings.channels.whatsapp.enabled),
    }


def _expects_localdeploy(settings: AppSettings) -> bool:
    profile = settings.llm.profiles.get(settings.llm.default_profile)
    if profile is None:
        return False
    base_url = (profile.base_url or "").lower()
    return "127.0.0.1:8000" in base_url or "localhost:8000" in base_url or "localdeploy" in settings.llm.default_profile.lower()


def _service_record(name: str, expected: bool) -> dict[str, Any]:
    status_path = _run_dir() / f"{name}.status.json"
    status = _read_json(status_path)
    updated_at = _parse_datetime(status.get("updated_at")) if status else None
    age_seconds = _age_seconds(updated_at)
    state = str(status.get("status") or "missing") if status else "missing"
    supervisor_pid = status.get("supervisor_pid") if status else None
    child_pid = status.get("child_pid") if status else None
    ok = (
        expected
        and state == "running"
        and age_seconds is not None
        and age_seconds <= SERVICE_STALE_SECONDS
        and bool(supervisor_pid)
    )
    if not expected:
        ok = True
    return {
        "name": name,
        "expected": expected,
        "ok": ok,
        "status": state,
        "updated_at": updated_at.isoformat() if updated_at else None,
        "age_seconds": age_seconds,
        "supervisor_pid": supervisor_pid,
        "child_pid": child_pid,
        "restart_count": status.get("restart_count") if status else None,
        "last_exit_code": status.get("last_exit_code") if status else None,
        "message": status.get("message") if status else "no supervisor status file",
    }


def _read_json(path: Path) -> dict[str, Any]:
    # A missing file is routine - this is polled every few seconds by the
    # admin UI, and "service not started yet" is a normal state, not a
    # problem worth logging. A file that exists but won't parse is different:
    # that means a supervisor wrote a truncated/corrupt status file, which is
    # worth knowing about.
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        logger.warning("service status file exists but failed to parse: %s", path, exc_info=True)
        return {}


def _run_dir() -> Path:
    cwd_run_dir = Path.cwd() / ".agent_control" / "run"
    if cwd_run_dir.exists():
        return cwd_run_dir
    return Path(__file__).resolve().parents[3] / ".agent_control" / "run"


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _age_seconds(value: datetime | None) -> int | None:
    if value is None:
        return None
    return max(0, int((datetime.now(timezone.utc) - value.astimezone(timezone.utc)).total_seconds()))
