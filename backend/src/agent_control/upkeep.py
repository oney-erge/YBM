"""Staying up: the chores that keep the assistant alive for years, and the one line each morning that says whether it is.

Four things, once a day at `upkeep.hour`, by the scheduler process (scheduler.py), with nothing for the person to start:

    backup         a backup (backup.py) when the last one is older than a day
    restore check  once a month, the newest backup is opened in a scratch folder and what it holds is counted (tasks, facts, episodes, projects), so a backup that cannot be
                   read is found while it still matters, not on the day it is needed
    project tidy   notes a project holds twice are kept once, and the readable copy of the memory (.agent_control/memory: INDEX.md, about-you.md, one PROJECT.md per project)
                   is written again, so it is there to open
    morning line   one message: whether the model answers, how much disk is free, what the background jobs are doing, how old the last backup is and what the last restore
                   check found; "all good" in one line, or what needs a look

Everything here is plain code over files and tables: it works the same with any model behind the assistant, which is the point of checking it from outside.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from agent_control.jobs.model import JobStatus

MIN_FREE_GB = 20.0
BACKUP_MAX_AGE = timedelta(days=2)
STATE_FILE = Path(".agent_control") / "upkeep.json"
COUNTED_TABLES = ("tasks", "memory_facts", "episodes", "projects", "project_notes", "goals", "messages")


def _read_state(path: Path = STATE_FILE) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_state(state: dict[str, Any], path: Path = STATE_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def newest_backup(folder: str | Path = Path(".agent_control") / "backups") -> Path | None:
    files = sorted(Path(folder).glob("ybm-backup-*.zip"), key=lambda p: p.stat().st_mtime) if Path(folder).is_dir() else []
    return files[-1] if files else None


def restore_check(archive: Path, scratch: Path = Path(".agent_control") / "restore-check") -> dict[str, Any]:
    """Open a backup the way a restore would and count what it holds. The scratch folder is made and removed here; nothing of the live data is touched."""
    result: dict[str, Any] = {"archive": archive.name, "ok": False, "counts": {}, "problem": ""}
    if scratch.exists():
        shutil.rmtree(scratch, ignore_errors=True)
    scratch.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(archive) as bundle:
            bad = bundle.testzip()
            if bad:
                result["problem"] = f"the archive is damaged at {bad}"
                return result
            names = bundle.namelist()
            database = next((n for n in names if n.endswith(".db")), None)
            if database is None:
                result["problem"] = "the archive holds no database"
                return result
            bundle.extract(database, scratch)
        connection = sqlite3.connect(f"file:{(scratch / database).as_posix()}?mode=ro", uri=True)
        try:
            if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                result["problem"] = "the database in the backup fails its integrity check"
                return result
            existing = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            for table in COUNTED_TABLES:
                if table in existing:
                    result["counts"][table] = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608 - a fixed list of table names
        finally:
            connection.close()
        result["ok"] = True
        return result
    except (OSError, zipfile.BadZipFile, sqlite3.Error) as exc:
        result["problem"] = f"{type(exc).__name__}: {str(exc)[:120]}"
        return result
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _model_answers(base_url: str) -> bool:
    try:
        with urllib.request.urlopen(base_url.rstrip("/") + "/health", timeout=3) as response:  # noqa: S310 - the configured local model server
            return 200 <= response.status < 500
    except Exception:  # noqa: BLE001
        return False


def morning_line(settings: Any, repositories: Any, *, state: dict[str, Any] | None = None, now: datetime | None = None, model_check=_model_answers, disk_path: str = ".") -> tuple[str, list[str]]:
    """The one-line health message and the list of things that need a look (empty when all is good)."""
    now = now or datetime.now(timezone.utc)
    state = state if state is not None else _read_state()
    problems: list[str] = []
    facts: list[str] = []
    base_url = str(getattr(settings.decision, "base_url", "") or "")
    if base_url:
        if model_check(base_url):
            facts.append("the model answers")
        else:
            problems.append("the model server is not answering")
    free_gb = shutil.disk_usage(disk_path).free / 1e9
    (problems if free_gb < MIN_FREE_GB else facts).append(f"{free_gb:.0f} GB free" if free_gb >= MIN_FREE_GB else f"only {free_gb:.0f} GB free on this drive")
    jobs = repositories.jobs.list(statuses=[JobStatus.RUNNING, JobStatus.PAUSED, JobStatus.FAILED], limit=50)
    running = [job for job in jobs if job.status == JobStatus.RUNNING and job.kind != "watch"]
    watches = [job for job in jobs if job.status == JobStatus.RUNNING and job.kind == "watch"]
    failed = [job for job in jobs if job.status == JobStatus.FAILED]
    if failed:
        problems.append(f"{len(failed)} background job(s) stopped on errors (say 'resume')")
    if running or watches:
        facts.append(", ".join(part for part in (f"{len(running)} job(s) running" if running else "", f"{len(watches)} watch(es)" if watches else "") if part))
    backup = newest_backup()
    if backup is None:
        problems.append("there is no backup yet")
    else:
        age = now - datetime.fromtimestamp(backup.stat().st_mtime, tz=timezone.utc)
        (problems if age > BACKUP_MAX_AGE else facts).append(f"last backup {age.days} day(s) old" if age > BACKUP_MAX_AGE else "backed up")
    check = state.get("restore_check") or {}
    if check and not check.get("ok"):
        problems.append(f"the last restore check failed: {check.get('problem') or 'unknown'}")
    if problems:
        return "Morning check: " + "; ".join(problems) + (f". Otherwise: {', '.join(facts)}." if facts else "."), problems
    return "Morning check: all good. " + ", ".join(facts).capitalize() + ".", []


async def run_daily(settings: Any, repositories: Any, say, *, now: datetime | None = None, state_path: Path = STATE_FILE, backup=None, model_check=_model_answers) -> str | None:
    """Do the day's chores if it is time and they have not been done today. Returns the message that was sent, or None."""
    config = getattr(settings, "upkeep", None)
    if config is None or not config.enabled:
        return None
    local = (now or datetime.now().astimezone())
    state = _read_state(state_path)
    today = local.date().isoformat()
    if local.hour < config.hour or state.get("last_run") == today:
        return None
    state["last_run"] = today
    if config.backup and (newest_backup() is None or local.timestamp() - newest_backup().stat().st_mtime > 20 * 3600):
        try:
            (backup or _run_backup)()
        except Exception:  # noqa: BLE001 - the line below says the backup is old; a failed chore must not stop the others
            pass
    last_check = state.get("restore_checked_on")
    due = not last_check or (local.date() - datetime.fromisoformat(last_check).date()).days >= config.restore_check_days
    latest = newest_backup()
    if due and latest is not None:
        state["restore_check"] = restore_check(latest)
        state["restore_checked_on"] = today
    try:
        repositories.schedules.purge_deleted((local - timedelta(days=30)).astimezone(timezone.utc).isoformat())  # deleted a month ago: gone
    except Exception:  # noqa: BLE001 - a chore that fails must not stop the others
        pass
    try:
        from agent_control import projects_memory

        projects_memory.tidy(repositories)  # notes written twice are kept once, and the readable copy under .agent_control/memory is rewritten
    except Exception:  # noqa: BLE001 - a chore that fails must not stop the morning line
        pass
    _write_state(state, state_path)
    if not config.message:
        return None
    line, _problems = morning_line(settings, repositories, state=state, now=local.astimezone(timezone.utc), model_check=model_check)
    await say(line)
    return line


def _run_backup() -> None:
    from agent_control.backup import run_backup

    run_backup(None)
