"""Run every YBM E2E case back-to-back through Telegram, capture detailed traces.

This is built to run UNATTENDED. Goals:

* Load every case from ``e2e/all_cases.json`` (trimmed 2026-07-28 to 11 smoke cases, one
  per major capability - see docs/archive/HISTORY.md P2).
* Use the same fixture setup as ``e2e/live_telegram_e2e.py`` so ``{{documents_folder}}``
  / ``{{episode_url}}`` template variables resolve to real paths/URLs.
* Send each message from your real Telegram user account; poll the admin API for
  task progress; honor each case's ``timeout_seconds``.
* For multi-turn cases (``follow_ups``), keep conversation memory across turns.
* Never crash mid-run - every per-stage failure is captured as a FAIL and the
  suite continues. Summary is rewritten after every stage so partial runs survive.
* By default, skip ``guarded`` cases (codex / copilot / external-quota cases that
  need real credentials). Pass ``--include-guarded`` to run them too.

Outputs:

    .agent_control/e2e_results/run_<timestamp>/
        summary.md           # at-a-glance pass/fail table
        summary.json         # same data, machine-readable
        <NN>_<case_id>/
            result.json      # full structured stage result
            timeline.txt     # human-readable status flow + plan + answer
            audit.json       # every audit event for the task
            decision_trace.json # structured decisions + tool outcomes, every run
            diagnosis.md     # only present for failed stages

Usage:
    python scripts/run_all_e2e_tests.py
    python scripts/run_all_e2e_tests.py --only browser_dizibox_5_episodes,fibonacci
    python scripts/run_all_e2e_tests.py --include-guarded
    python scripts/run_all_e2e_tests.py --sizes small,medium
    python scripts/run_all_e2e_tests.py --suite smoke
"""
from __future__ import annotations

import argparse
import asyncio
import atexit
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import traceback
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / ".env"
RESULTS_ROOT = ROOT / ".agent_control" / "e2e_results"
CASES_PATH = ROOT / "e2e" / "all_cases.json"
CONFIG_PATH = ROOT / "config" / "config.yaml"

ADMIN_BASE = "http://127.0.0.1:8765"
LOCALDEPLOY_BASE = "http://127.0.0.1:8000"

# Tags whose cases need external credentials or services we don't have local copies of.
GUARDED_TAGS = {"codex", "copilot", "external", "quota", "limit", "presentation"}

# Per-case absolute ceiling regardless of what the JSON declares. Protects the
# whole run from getting stuck on one runaway case. It must stay above the
# largest declared `timeout_seconds` plus the worker-budget headroom below, or
# the longest cases are quietly cut short - at 900 it under-budgeted every
# 900s case by the 30s margin. `_turn_ceiling_seconds` reports any clipping and
# a catalogue test fails if a case declares a budget this cannot honor.
HARD_CEILING_S = 1260
TASK_SPAWN_TIMEOUT_S = 180

# The runner must wait AT LEAST this long after the case's declared timeout so
# the worker's own per-task budget (settings.limits.task_budget_seconds,
# default 600s) has time to fire. Without this safety margin the runner
# moves on while the worker is still busy, blocking the next case in queue.
WORKER_BUDGET_SAFETY_S = 640

# Chat-route turns settle in seconds, not minutes - they run no tools. The
# settle pause lets the substantive reply land after the acknowledgment ping.
CHAT_REPLY_TIMEOUT_S = 90
CHAT_REPLY_SETTLE_S = 6


# ---------- HTTP / DB helpers ----------


def _database_path() -> Path:
    """Resolve the same configured SQLite file used by the running services."""
    from agent_control.config import load_settings

    database_url = load_settings(ROOT / "config" / "config.yaml", _env_file=ENV_PATH).storage.database_url
    prefix = "sqlite:///"
    if not database_url.startswith(prefix):
        raise RuntimeError(f"live E2E requires SQLite storage, got {database_url!r}")
    configured = Path(database_url.removeprefix(prefix))
    return configured if configured.is_absolute() else ROOT / configured


DB_PATH = _database_path()


def admin_get(path: str, timeout: int = 10) -> Any:
    url = f"{ADMIN_BASE}{path}"
    headers: dict[str, str] = {}
    admin_token = os.getenv("AGENT_ADMIN_TOKEN")
    if admin_token:
        headers["X-Agent-Control-Admin-Token"] = admin_token
    request = urlrequest.Request(url, headers=headers)
    with urlrequest.urlopen(request, timeout=timeout) as r:
        return json.loads(r.read())


def admin_post(path: str, payload: dict[str, Any], timeout: int = 10) -> Any:
    url = f"{ADMIN_BASE}{path}"
    headers = {"Content-Type": "application/json"}
    admin_token = os.getenv("AGENT_ADMIN_TOKEN")
    if admin_token:
        headers["X-Agent-Control-Admin-Token"] = admin_token
    request = urlrequest.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urlrequest.urlopen(request, timeout=timeout) as r:
        return json.loads(r.read())


def admin_summary() -> dict:
    return admin_get("/admin/api/summary?task_limit=30")


def admin_trace(task_id: str) -> dict:
    return admin_get(f"/admin/api/tasks/{task_id}/trace")


def admin_post(path: str, body: dict | None = None, timeout: int = 10) -> Any:
    url = f"{ADMIN_BASE}{path}"
    headers = {"Content-Type": "application/json"}
    admin_token = os.getenv("AGENT_ADMIN_TOKEN")
    if admin_token:
        headers["X-Agent-Control-Admin-Token"] = admin_token
    request = urlrequest.Request(
        url, data=json.dumps(body or {}).encode("utf-8"), headers=headers, method="POST"
    )
    with urlrequest.urlopen(request, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


async def auto_approve_loop(stop: asyncio.Event, granted: list[str]) -> None:
    """Grant pending approvals while the suite runs.

    e2e/README.md says the runner works unattended once the Telethon session
    exists, but nothing here ever answered an approval - and approvals are not
    optional for these cases. Some are forced by the runtime itself
    (ToolDefinition.approval_required_operations covers schedule.manage:create,
    mcp.client:install_server, memory.manage:forget and more), so no config
    change can make them go away. Every affected case therefore sat in
    awaiting_approval until it timed out, which reads as the product being
    broken rather than the harness being incomplete.

    Approving here rather than lowering approval_policy would also be the only
    honest option: the point of these cases is to exercise the real gate, and a
    run with the gate switched off proves nothing about it. Each grant is still
    a normal, audited APPROVAL_DECIDED event, attributed to this harness.
    """
    while not stop.is_set():
        try:
            # Each item wraps the record: {"approval": {...}, "task_objective": ...}
            for item in admin_get("/admin/api/approvals").get("approvals", []):
                approval = item.get("approval") or {}
                if approval.get("status") != "pending":
                    continue
                approval_id = approval.get("id")
                if not approval_id:
                    continue
                try:
                    admin_post(f"/admin/api/approvals/{approval_id}/decide", {"decision": "approve"})
                except urlerror.HTTPError as exc:
                    # 409 = decided between listing it and deciding it, which
                    # is the normal outcome of polling a queue the worker is
                    # draining. Reporting it as an error buries the failures
                    # that actually matter.
                    if exc.code == 409:
                        continue
                    raise
                granted.append(approval_id)
                print(f"    [auto-approve] {approval.get('capability') or '?'} ({approval_id})")
        except Exception as exc:  # noqa: BLE001 - never let this kill the run
            print(f"    [auto-approve] transient error: {exc}")
        try:
            await asyncio.wait_for(stop.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            pass


def ping(url: str, timeout: float = 2.0) -> bool:
    try:
        with urlrequest.urlopen(url, timeout=timeout) as r:
            return 200 <= r.status < 500
    except Exception:
        return False


def clear_conversation_memory() -> None:
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("DELETE FROM conversation_memory")
        conn.commit()
        conn.close()
    except Exception:
        pass


def _memory_fact_ids() -> set[str]:
    try:
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute("SELECT id FROM memory_facts").fetchall()
        conn.close()
        return {str(row[0]) for row in rows}
    except Exception:
        return set()


def _delete_memory_facts_created_after(existing_ids: set[str]) -> None:
    """Remove only facts created by the E2E stage, preserving real user memory."""
    try:
        conn = sqlite3.connect(DB_PATH)
        current = {str(row[0]) for row in conn.execute("SELECT id FROM memory_facts").fetchall()}
        created = sorted(current - existing_ids)
        if created:
            conn.executemany("DELETE FROM memory_facts WHERE id = ?", [(item,) for item in created])
        conn.commit()
        conn.close()
    except Exception:
        pass


def cleanup_catalog_memory_facts(cases: list[dict]) -> None:
    """Clean exact standing-instruction fixtures left by an interrupted run.

    A preference is intentionally global in production. Without scoped E2E
    cleanup, EVO-1's three-bullet rule contaminates every later case and even
    the user's real assistant after the suite exits. Exact-content matching
    removes test-created rows without touching unrelated user facts.
    """
    fixture_texts = {
        str(case.get("message") or "").strip()
        for case in cases
        if "memory" in (case.get("tags") or []) and not bool(case.get("expects_task", True))
    }
    fixture_texts.discard("")
    if not fixture_texts:
        return
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.executemany("DELETE FROM memory_facts WHERE content = ?", [(text,) for text in fixture_texts])
        conn.commit()
        conn.close()
    except Exception:
        pass


def cancel_task(task_id: str) -> bool:
    """Cancel a deadline-exceeded task through YBM's public control path.

    Going through the admin signal endpoint records the signal and audit event,
    clears task-scoped approvals, and stops an external coding session when
    applicable.  Direct database status writes bypass all of those guarantees.
    """
    try:
        response = admin_post(f"/admin/api/tasks/{task_id}/signals", {"signal": "cancel"})
        return str(response.get("task", {}).get("status") or "") == "cancelled"
    except Exception:
        return False


async def _wait_for_status_notification(
    task_id: str,
    status: str,
    *,
    timeout_s: float = 10.0,
    poll_s: float = 0.25,
) -> None:
    """Let the worker finish the channel notification for a settled state.

    Task status is persisted before ``process_next`` calls its notification
    sink. Capturing immediately can therefore assign the approval/completion
    message to the next E2E turn even though it belongs to this one. The worker
    records successful sends in ``notified_statuses``, which gives the runner
    a durable synchronization point without guessing a fixed sleep.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            task = admin_trace(task_id).get("task") or {}
            notified = (task.get("metadata") or {}).get("notified_statuses") or []
            if status in notified:
                return
        except Exception:
            pass
        await asyncio.sleep(poll_s)


def _iso_minutes_ago(minutes: int) -> str:
    """A cutoff string comparable to the stored ``created_at`` column.

    Audit rows store ``datetime.isoformat()`` (``2026-08-09T14:05:00+00:00``),
    so SQLite's own ``datetime('now', ...)`` is the wrong shape to compare
    against: its space separator sorts below ``T``, which made every same-day
    row satisfy the predicate and silently widened a "last N minutes" window to
    "any time today or later".
    """
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()


def fetch_classifier_verdict_for_text(message: str, *, within_minutes: int = 10) -> dict | None:
    """Find the latest message_classified audit event matching ``message``.

    Returns a small dict of the most useful classifier fields, or None if not
    found. Used both to surface route + reason + confidence in per-stage
    diagnostics and to attribute a missing task to the classifier rather than
    to intake, so failures at the classifier are visible without grepping
    audit.json.
    """
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute(
            "SELECT payload_json FROM audit_events "
            "WHERE event_type='message_classified' "
            "AND created_at >= ? "
            "ORDER BY created_at DESC LIMIT 25",
            (_iso_minutes_ago(within_minutes),),
        )
        for (payload_json,) in c.fetchall():
            try:
                p = json.loads(payload_json)
            except Exception:
                continue
            text = str(p.get("text") or "").strip()
            if text and text == message.strip():
                return {
                    "is_task": p.get("is_task"),
                    "route": (p.get("intent") or {}).get("route"),
                    "task_type": p.get("task_type"),
                    "confidence": p.get("confidence"),
                    "reason": (p.get("reason") or "")[:400],
                }
        return None
    except Exception:
        return None
    finally:
        try:
            conn.close()  # type: ignore[has-type]
        except Exception:
            pass


def fetch_task_audit(task_id: str) -> list[dict]:
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute(
            "SELECT actor, event_type, payload_json, created_at "
            "FROM audit_events WHERE task_id=? ORDER BY created_at ASC",
            (task_id,),
        )
        out: list[dict] = []
        for actor, etype, payload_json, created_at in c.fetchall():
            try:
                payload = json.loads(payload_json)
            except Exception:
                payload = {"raw": payload_json}
            out.append({"time": created_at, "actor": actor, "event": etype, "payload": payload})
        conn.close()
        return out
    except Exception:
        return []


def fetch_message_sent_events(since_iso: str) -> list[dict]:
    """Durable truth source for outbound Telegram sends: what YBM actually
    called sendMessage/sendPhoto/sendDocument with, independent of whatever
    the task's own metadata claims happened. Used to corroborate (not just
    trust) the internal task-state-derived reply text/media count."""
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute(
            "SELECT actor, payload_json, created_at "
            "FROM audit_events WHERE event_type=? AND created_at >= ? ORDER BY created_at ASC",
            ("message_sent", since_iso),
        )
        out: list[dict] = []
        for actor, payload_json, created_at in c.fetchall():
            try:
                payload = json.loads(payload_json)
            except Exception:
                payload = {"raw": payload_json}
            out.append({"time": created_at, "actor": actor, "payload": payload})
        conn.close()
        return out
    except Exception:
        return []


# ---------- Fixtures ----------


def _ensure_fixtures_path() -> None:
    """Make ``e2e/fixtures.py`` importable from this script."""
    e2e_dir = str(ROOT / "e2e")
    if e2e_dir not in sys.path:
        sys.path.insert(0, e2e_dir)


def prepare_fixtures(start_web: bool) -> dict[str, str]:
    """Build the file/folder fixtures cases depend on. Returns template values."""
    _ensure_fixtures_path()
    from fixtures import prepare_fixtures as _prep  # type: ignore[import]
    fx = _prep(start_web_server=start_web)
    return dict(fx.values)


def required_fixture_names(cases: list[dict]) -> set[str]:
    names: set[str] = set()
    for case in cases:
        texts = [str(case.get("message") or "")]
        texts.extend(str(item.get("message") or "") for item in case.get("follow_ups") or [])
        for value in texts:
            names.update(re.findall(r"\{\{(\w+)\}\}", value))
    return names


@dataclass
class TemporaryMCPFixture:
    """Exact local state needed to restore the operator after an E2E MCP run."""

    config_path: Path
    original_config: bytes
    catalog_path: Path
    original_catalog: bytes | None
    restart_services: Callable[[], None]
    active: bool = True

    def restore(self, *, restart: bool = True) -> None:
        if not self.active:
            return
        self.active = False
        self.config_path.write_bytes(self.original_config)
        if self.original_catalog is None:
            self.catalog_path.unlink(missing_ok=True)
        else:
            self.catalog_path.parent.mkdir(parents=True, exist_ok=True)
            self.catalog_path.write_bytes(self.original_catalog)
        if restart:
            self.restart_services()


def _restart_ybm_services() -> None:
    if os.name != "nt":
        raise RuntimeError("automatic E2E fixture reload currently requires Windows/PowerShell")
    command = [
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(ROOT / "scripts" / "ybm.ps1"),
        "restart",
        "-SkipDoctor",
        "-NoLocalDeploy",
    ]
    result = subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        # YBM starts detached supervisors. Capturing a pipe here lets those
        # grandchildren inherit its write handle, so communicate() waits for
        # EOF forever even after PowerShell itself exits. They have their own
        # service logs; discard lifecycle chatter here.
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=180,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"YBM restart failed while loading E2E fixtures (exit {result.returncode})")


def provision_fake_mcp_fixture(
    server_path: Path,
    *,
    restart_services: Callable[[], None] | None = None,
) -> TemporaryMCPFixture:
    """Temporarily register the catalogue's local fake MCP echo server."""
    import yaml

    if not server_path.is_file():
        raise FileNotFoundError(f"fake MCP server fixture was not created: {server_path}")
    if not CONFIG_PATH.is_file():
        raise FileNotFoundError(f"YBM config does not exist: {CONFIG_PATH}")

    original_config = CONFIG_PATH.read_bytes()
    config = yaml.safe_load(original_config.decode("utf-8")) or {}
    if not isinstance(config, dict):
        raise TypeError(f"{CONFIG_PATH} must contain a YAML object")
    mcp = config.setdefault("mcp", {})
    if not isinstance(mcp, dict):
        raise TypeError("config mcp section must be an object")
    servers = mcp.setdefault("servers", {})
    if not isinstance(servers, dict):
        raise TypeError("config mcp.servers section must be an object")

    catalog_value = str(mcp.get("catalog_path") or ".agent_control/mcp/tool_catalog.json")
    catalog_path = Path(catalog_value)
    if not catalog_path.is_absolute():
        catalog_path = ROOT / catalog_path
    original_catalog = catalog_path.read_bytes() if catalog_path.exists() else None
    restart = restart_services or _restart_ybm_services
    fixture = TemporaryMCPFixture(
        config_path=CONFIG_PATH,
        original_config=original_config,
        catalog_path=catalog_path,
        original_catalog=original_catalog,
        restart_services=restart,
    )

    mcp["enabled"] = True
    servers["fake"] = {
        "enabled": True,
        "command": sys.executable,
        "args": [str(server_path.resolve())],
        "cwd": str(ROOT),
        "timeout_seconds": 30,
        "capability": "terminal.run",
        "risk_level": "high",
        "disabled_tools": [],
        "max_output_chars": 20000,
    }
    CONFIG_PATH.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8", newline="")
    catalog_path.parent.mkdir(parents=True, exist_ok=True)
    catalog_path.write_text(
        json.dumps(
            {
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "servers": [{"name": "fake", "enabled": True, "healthy": True, "tool_count": 1}],
                "tools": [
                    {
                        "server": "fake",
                        "name": "echo",
                        "tool": "echo",
                        "description": "Echo text exactly as received",
                        "capability": "terminal.run",
                        "risk_level": "high",
                        "disabled": False,
                    }
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    try:
        restart()
    except Exception:  # noqa: BLE001 - any reload failure must restore private config
        fixture.restore(restart=False)
        raise
    return fixture


def render_text(text: str, fixtures: dict[str, str]) -> str:
    out = text
    for k, v in fixtures.items():
        out = out.replace(f"{{{{{k}}}}}", v)
    return out


# ---------- Case selection ----------


def load_cases() -> list[dict]:
    return json.loads(CASES_PATH.read_text(encoding="utf-8"))


def is_guarded(case: dict) -> bool:
    return bool(set(case.get("tags") or []) & GUARDED_TAGS)


def select_cases(
    cases: list[dict],
    *,
    only: set[str],
    skip: set[str],
    sizes: set[str],
    suites: set[str],
    include_guarded: bool,
) -> list[dict]:
    selected: list[dict] = []
    for c in cases:
        cid = c.get("id")
        if only and cid not in only:
            continue
        if cid in skip:
            continue
        if sizes and (c.get("size") or "small") not in sizes:
            continue
        if suites and "full" not in suites and not (_case_suites(c) & suites):
            continue
        if not include_guarded and is_guarded(c):
            continue
        selected.append(c)
    return selected


def _case_suites(case: dict) -> set[str]:
    declared = case.get("suites", case.get("suite", []))
    if isinstance(declared, str):
        suites = {declared}
    elif isinstance(declared, list):
        suites = {str(item) for item in declared if str(item).strip()}
    else:
        suites = set()
    tags = {str(item) for item in case.get("tags") or []}
    tools = {str(item) for item in case.get("tools_required") or []}
    if case.get("size") == "small":
        suites.add("smoke")
    if tools or tags:
        suites.add("tools")
    if any(item.startswith("code.interpreter:") for item in tools) or "code_interpreter" in tags:
        suites.add("code_interpreter")
    if any(item.startswith("mcp.client:") for item in tools) or "mcp" in tags:
        suites.add("mcp")
    if "recovery" in tags or "fallback" in tags:
        suites.add("recovery")
    if any(tag in tags for tag in ("codex", "copilot", "external", "quota", "limit")):
        suites.add("external_agent")
    suites.add("full")
    return suites


# ---------- Test execution ----------


@dataclass
class TurnResult:
    """One Telegram turn - initial message OR a follow-up."""
    label: str = ""                       # "initial" or follow-up id
    message: str = ""
    task_id: str | None = None
    final_status: str | None = None
    duration_s: float = 0.0
    replan_count: int = 0
    synth_answer: str | None = None
    last_tool_output: str | None = None
    last_worker_error: str | None = None
    planning_error: str | None = None
    last_replan_reason: str | None = None
    fulfillment_gap: str | None = None
    plan_steps: list[dict] = field(default_factory=list)
    tool_invocations: list[dict] = field(default_factory=list)
    tools_seen: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    artifacts: list[dict] = field(default_factory=list)
    artifact_count: int = 0
    telegram_media_count: int = 0
    bot_reply_text: str | None = None
    # Ground truth from the audit log (agent_control.storage.audit MESSAGE_SENT
    # events), not from task metadata - corroborates that a send actually
    # happened rather than trusting the adapter's/task's self-report.
    telegram_sent_events: list[dict] = field(default_factory=list)
    telegram_confirmed_text: str = ""
    telegram_confirmed_media_count: int = 0
    telegram_update_count: int = 0
    telegram_max_update_gap_seconds: float | None = None
    changed_paths_count: int = 0
    tool_failure_count: int = 0
    tool_approval_count: int = 0
    operator_decision_count: int = 0
    status_transitions: list[dict] = field(default_factory=list)
    audit_event_count: int = 0
    error: str | None = None              # exception in the runner itself
    # Captured from the classifier's audit event for this turn's message -
    # surfaced in timeline.txt and diagnosis.md so model-judgment failures
    # don't require grepping audit.json.
    classifier_verdict: dict | None = None
    forced_failed_by_runner: bool = False
    # True when the runner answered a clarifying question during this turn, so
    # a reader can tell an unprompted result from an assisted one.
    clarification_sent: bool = False


@dataclass
class StageResult:
    case_id: str
    category: str
    size: str
    message: str
    source_file: str
    started_at: str = ""
    ended_at: str = ""
    total_duration_s: float = 0.0
    turns: list[TurnResult] = field(default_factory=list)
    passed: bool = False
    failure_reason: str | None = None
    runner_exception: str | None = None    # set if the stage itself crashed


def _load_env() -> None:
    if not ENV_PATH.exists():
        return
    for line in ENV_PATH.read_text().splitlines():
        if "=" in line and not line.strip().startswith("#"):
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


async def _send_message(message: str) -> int:
    from telethon import TelegramClient

    api_id = int(os.environ["TELEGRAM_API_ID"])
    api_hash = os.environ["TELEGRAM_API_HASH"]
    bot_username = os.environ["TELEGRAM_BOT_USERNAME"]
    session = str(ROOT / ".agent_control" / "telegram_e2e_user")

    client = TelegramClient(session, api_id, api_hash)
    async with client:
        peer = await client.get_entity(bot_username)
        sent = await client.send_message(peer, message)
        return sent.id


async def _find_new_task(
    known_ids: set[str],
    original_message_text: str,
    spawn_timeout_s: int = TASK_SPAWN_TIMEOUT_S,
) -> str | None:
    """Find the task created for this exact Telegram message.

    Matching only on "not in the initial snapshot" lets a delayed task from
    an earlier turn steal the next case. Telegram intake persists the exact
    original text and source channel in task metadata. Use those durable
    values: Telethon's MTProto sent-message id is not guaranteed to equal the
    Bot API update id stored by YBM.
    """
    deadline = time.monotonic() + spawn_timeout_s
    while time.monotonic() < deadline:
        await asyncio.sleep(2)
        try:
            data = admin_summary()
        except Exception:
            continue
        for t in data.get("tasks", []):
            metadata = t.get("metadata") if isinstance(t.get("metadata"), dict) else {}
            if (
                t["id"] not in known_ids
                and metadata.get("source_channel") == "telegram"
                and metadata.get("original_message_text") == original_message_text
            ):
                return t["id"]
    return None


def _diagnose_turn(
    case: dict,
    turn: TurnResult,
    *,
    is_followup: bool,
    fixtures: dict[str, str] | None = None,
) -> tuple[bool, str | None]:
    """Return (passed, reason). Uses case assertions when present, plus generic checks."""
    if turn.error:
        return False, f"runner_error: {turn.error[:200]}"

    status = (turn.final_status or "").lower()
    assertions = (case.get("assertions") or {})
    final_status_allowed = assertions.get("final_status_in")
    if final_status_allowed and status not in [s.lower() for s in final_status_allowed]:
        why_parts: list[str] = [f"status={status} not in {final_status_allowed}"]
        if turn.last_worker_error:
            why_parts.append(f"worker_error: {turn.last_worker_error[:200]}")
        if turn.planning_error:
            why_parts.append(f"planning_error: {turn.planning_error[:200]}")
        if turn.last_replan_reason:
            why_parts.append(f"last_replan_reason: {turn.last_replan_reason[:200]}")
        if turn.fulfillment_gap:
            why_parts.append(f"fulfillment_gap: {turn.fulfillment_gap}")
        return False, "; ".join(why_parts)

    if not final_status_allowed and status not in {"completed", "blocked"}:
        return False, f"unexpected terminal status: {status or '(timed out)'}"

    blocked_requirements = assertions.get("blocked_requires_any") or {}
    if status == "blocked" and blocked_requirements:
        blocked_evidence: list[bool] = []
        for dotted_key, expected_value in (blocked_requirements.get("metadata_equals") or {}).items():
            blocked_evidence.append(_metadata_value(turn.metadata, str(dotted_key)) == expected_value)
        for dotted_key, expected_values in (blocked_requirements.get("metadata_in") or {}).items():
            blocked_evidence.append(
                _metadata_value(turn.metadata, str(dotted_key)) in list(expected_values or [])
            )
        worker_error = str(turn.last_worker_error or "").casefold()
        for needle in blocked_requirements.get("last_worker_error_contains_any") or []:
            blocked_evidence.append(str(needle).casefold() in worker_error)
        if not any(blocked_evidence):
            return False, (
                "blocked without required terminal evidence; "
                f"worker_error={turn.last_worker_error!r}"
            )

    missing_tools = [
        requirement
        for requirement in assertions.get("tools_all") or []
        if not _tool_requirement_satisfied(str(requirement), turn)
    ]
    if missing_tools:
        return False, f"missing required tool invocation(s): {missing_tools}; saw={turn.tools_seen}"

    # Safety cases assert what must NOT have happened: an approval that was
    # denied must leave no write behind, and a "prepare the installer" case must
    # not have executed one. A positive-only assertion set cannot express that.
    forbidden_tools = [
        requirement
        for requirement in assertions.get("tools_none") or []
        if _tool_requirement_satisfied(str(requirement), turn)
    ]
    if forbidden_tools:
        return False, f"forbidden tool invocation(s) occurred: {forbidden_tools}; saw={turn.tools_seen}"

    metadata_any = [str(item) for item in assertions.get("metadata_any") or []]
    if metadata_any and not any(_metadata_key_present(turn.metadata, key) for key in metadata_any):
        return False, f"none of metadata_any present: {metadata_any}"

    metadata_equals = assertions.get("metadata_equals") or {}
    if status == "completed":
        metadata_equals = {**metadata_equals, **(assertions.get("completed_metadata_equals") or {})}
    for dotted_key, expected_value in metadata_equals.items():
        if fixtures and isinstance(expected_value, str):
            expected_value = render_text(expected_value, fixtures)
        actual_value = _metadata_value(turn.metadata, str(dotted_key))
        if actual_value != expected_value:
            return False, (
                f"metadata {dotted_key!r} was {actual_value!r}, expected {expected_value!r}"
            )

    artifacts_min = assertions.get("artifacts_min")
    if artifacts_min is not None and turn.artifact_count < int(artifacts_min):
        return False, f"artifact_count={turn.artifact_count} below artifacts_min={artifacts_min}"

    telegram_media_min = assertions.get("telegram_media_min")
    if telegram_media_min is not None:
        # Cross-check the adapter's self-reported "delivered" claim against the
        # audit log's independent record of actual sendPhoto/sendDocument calls
        # - the higher of the two is the effective count; either alone can be
        # under-counted, but this catches a claim with zero confirmed sends.
        effective_media_count = max(turn.telegram_media_count, turn.telegram_confirmed_media_count)
        if effective_media_count < int(telegram_media_min):
            return False, (
                f"telegram_media_count={turn.telegram_media_count} "
                f"(audit-confirmed={turn.telegram_confirmed_media_count}) "
                f"below telegram_media_min={telegram_media_min}"
            )

    changed_paths_min = assertions.get("changed_paths_min")
    if status == "completed" and assertions.get("completed_changed_paths_min") is not None:
        changed_paths_min = assertions["completed_changed_paths_min"]
    if changed_paths_min is not None and turn.changed_paths_count < int(changed_paths_min):
        return False, f"changed_paths_count={turn.changed_paths_count} below changed_paths_min={changed_paths_min}"

    if status == "completed":
        for requirement in assertions.get("completed_files_exist_under") or []:
            root_value = str(requirement.get("root") or "")
            root_value = render_text(root_value, fixtures or {})
            root = Path(root_value)
            missing_under = [
                str(relative)
                for relative in requirement.get("files") or []
                if not (root / str(relative)).is_file()
            ]
            if missing_under:
                return False, (
                    f"required file(s) missing under explicit workspace {root}: {missing_under}"
                )

        for requirement in assertions.get("completed_files_exist_any_under") or []:
            root_value = str(requirement.get("root") or "")
            root_value = render_text(root_value, fixtures or {})
            root = Path(root_value)
            candidates = [
                str(relative)
                for relative in requirement.get("files") or []
                if str(relative).strip()
            ]
            if candidates and not any((root / relative).is_file() for relative in candidates):
                return False, (
                    f"none of the acceptable file(s) exist under explicit workspace {root}: "
                    f"{candidates}"
                )

    required_files = [str(item) for item in assertions.get("files_exist_all") or [] if str(item).strip()]
    if required_files:
        observed_files = _observed_file_paths(turn.metadata)
        missing_files = [
            required
            for required in required_files
            if not any(
                _path_matches(path, required) and path.is_file()
                for path in observed_files
            )
        ]
        if missing_files:
            return False, (
                f"required produced file(s) missing on disk: {missing_files}; "
                f"observed={[str(path) for path in observed_files]}"
            )

    file_contains = assertions.get("file_contains_all") or {}
    for expected_path, needles in file_contains.items():
        observed_files = _observed_file_paths(turn.metadata)
        matches = [path for path in observed_files if _path_matches(path, str(expected_path)) and path.is_file()]
        if not matches:
            return False, f"cannot inspect required produced file {expected_path!r}"
        content = matches[0].read_text(encoding="utf-8", errors="replace").lower()
        missing_needles = [str(needle) for needle in needles if str(needle).lower() not in content]
        if missing_needles:
            return False, f"{expected_path!r} is missing required content: {missing_needles}"

    tool_failures_min = assertions.get("tool_failures_min")
    if tool_failures_min is not None and turn.tool_failure_count < int(tool_failures_min):
        return False, f"tool_failure_count={turn.tool_failure_count} below tool_failures_min={tool_failures_min}"

    tool_approvals_min = assertions.get("tool_approvals_min")
    if tool_approvals_min is not None and turn.tool_approval_count < int(tool_approvals_min):
        return False, f"tool_approval_count={turn.tool_approval_count} below tool_approvals_min={tool_approvals_min}"

    operator_decisions_min = assertions.get("operator_decisions_min")
    if operator_decisions_min is not None and turn.operator_decision_count < int(operator_decisions_min):
        return False, (
            f"operator_decision_count={turn.operator_decision_count} "
            f"below operator_decisions_min={operator_decisions_min}"
        )

    telegram_updates_min = assertions.get("telegram_updates_min")
    if telegram_updates_min is not None and turn.telegram_update_count < int(telegram_updates_min):
        return False, f"telegram_update_count={turn.telegram_update_count} below telegram_updates_min={telegram_updates_min}"

    max_update_gap = assertions.get("telegram_update_max_gap_seconds")
    if max_update_gap is not None:
        if turn.telegram_max_update_gap_seconds is None:
            return False, "telegram update cadence could not be calculated"
        if turn.telegram_max_update_gap_seconds > float(max_update_gap):
            return False, (
                f"telegram_max_update_gap_seconds={turn.telegram_max_update_gap_seconds} "
                f"exceeds telegram_update_max_gap_seconds={max_update_gap}"
            )

    reply_needles = [str(item).lower() for item in assertions.get("bot_reply_contains_any") or [] if str(item).strip()]
    if reply_needles:
        # Truth source is the union of the task-metadata-derived reply AND the
        # audit log's record of what was actually sent to Telegram - a task
        # that "completed" internally but never confirmed a send is a bug the
        # metadata-only check could not see.
        if not turn.telegram_sent_events:
            return False, (
                "bot reply expected but no message_sent audit record exists for this turn "
                "- task metadata may claim completion without a confirmed Telegram send"
            )
        reply = _turn_reply_text(turn).lower()
        confirmed = turn.telegram_confirmed_text.lower()
        if not any(needle in reply or needle in confirmed for needle in reply_needles):
            return False, (
                f"bot reply did not contain any of {reply_needles}; "
                f"reply={reply[:240]!r}; audit_confirmed={confirmed[:240]!r}"
            )

    # Leak checks. A secret that reaches the user is a failure even when every
    # positive assertion passed, so these are evaluated against both the
    # task-derived reply and what Telegram actually received.
    excluded = [str(item) for item in assertions.get("bot_reply_excludes_all") or [] if str(item).strip()]
    if excluded:
        haystack = f"{_turn_reply_text(turn)}\n{turn.telegram_confirmed_text}".lower()
        leaked = [needle for needle in excluded if needle.lower() in haystack]
        if leaked:
            return False, f"reply leaked forbidden content: {leaked}"

    # The audit trail is persisted and operator-visible, so a secret redacted
    # from the reply but written into an event payload is still a leak. Fetched
    # only when asserted, to keep the common path free of extra queries.
    audit_excluded = [str(item) for item in assertions.get("audit_excludes_all") or [] if str(item).strip()]
    if audit_excluded and turn.task_id:
        audit_blob = json.dumps(fetch_task_audit(turn.task_id), default=str).lower()
        leaked_audit = [needle for needle in audit_excluded if needle.lower() in audit_blob]
        if leaked_audit:
            return False, f"audit trail leaked forbidden content: {leaked_audit}"

    return True, None


def _tool_requirement_satisfied(requirement: str, turn: TurnResult) -> bool:
    required_tool, _, required_operation = requirement.partition(":")
    required_tool = required_tool.strip()
    required_operation = required_operation.strip()
    if not required_tool:
        return True
    for seen in turn.tools_seen:
        tool, _, operation = seen.partition(":")
        if tool == required_tool and (not required_operation or operation == required_operation):
            return True
    return False


def _metadata_key_present(value: Any, key: str) -> bool:
    if isinstance(value, dict):
        if key in value and value.get(key) not in (None, "", [], {}):
            return True
        return any(_metadata_key_present(item, key) for item in value.values())
    if isinstance(value, list):
        return any(_metadata_key_present(item, key) for item in value)
    return False


def _metadata_value(value: Any, dotted_key: str) -> Any:
    current = value
    for part in dotted_key.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _reported_file_values(metadata: dict[str, Any]) -> list[str]:
    values: list[str] = []

    def collect(container: Any) -> None:
        if not isinstance(container, dict):
            return
        for key in ("changed_paths", "changed_files", "files", "files_created", "files_modified"):
            found = container.get(key)
            if isinstance(found, list):
                values.extend(str(item) for item in found if str(item).strip())

    collect(metadata)
    collect(metadata.get("coding_agent_session"))
    last_result = metadata.get("last_tool_result")
    if isinstance(last_result, dict):
        collect(last_result.get("output"))
    return values


def _observed_file_paths(metadata: dict[str, Any]) -> list[Path]:
    roots: list[Path] = []
    for key in ("coding_agent_workspace", "workspace_dir"):
        value = metadata.get(key)
        if value:
            roots.append(Path(str(value)))
    last_result = metadata.get("last_tool_result")
    output = last_result.get("output") if isinstance(last_result, dict) else None
    if isinstance(output, dict) and output.get("workspace_dir"):
        roots.append(Path(str(output["workspace_dir"])))

    resolved: list[Path] = []
    for raw_value in _reported_file_values(metadata):
        raw = Path(raw_value)
        if raw.is_absolute():
            candidates = [raw]
        else:
            candidates = [root / raw for root in roots]
        if not candidates:
            continue
        existing = next((candidate for candidate in candidates if candidate.is_file()), None)
        resolved.append((existing or candidates[0]).resolve())
    return list(dict.fromkeys(resolved))


def _path_matches(path: Path, expected: str) -> bool:
    actual = str(path).replace("\\", "/").lower()
    suffix = expected.replace("\\", "/").strip().lstrip("./").lower()
    return actual == suffix or actual.endswith(f"/{suffix}")


def _count_changed_paths(metadata: dict[str, Any]) -> int:
    reported = {
        value.replace("\\", "/").lower()
        for value in _reported_file_values(metadata)
        if value.replace("\\", "/").rsplit("/", 1)[-1].lower() != "task.md"
    }
    if reported:
        return len(reported)
    value = metadata.get("organized_paths")
    return len(value) if isinstance(value, list) else (1 if value else 0)


def _turn_reply_text(turn: TurnResult) -> str:
    parts = [
        turn.bot_reply_text,
        turn.synth_answer,
        turn.last_tool_output,
        turn.metadata.get("last_tool_output_text") if isinstance(turn.metadata, dict) else None,
        turn.metadata.get("fulfillment_gap") if isinstance(turn.metadata, dict) else None,
        turn.metadata.get("last_worker_error") if isinstance(turn.metadata, dict) else None,
    ]
    return "\n".join(str(item) for item in parts if item)


def _compact_tool_invocations(invocations: list[dict]) -> list[dict]:
    compact: list[dict] = []
    for item in invocations:
        if not isinstance(item, dict):
            continue
        request_payload = item.get("request") if isinstance(item.get("request"), dict) else {}
        request_input = request_payload.get("input") if isinstance(request_payload.get("input"), dict) else {}
        result_payload = item.get("result") if isinstance(item.get("result"), dict) else {}
        result_output = result_payload.get("output") if isinstance(result_payload.get("output"), dict) else {}
        compact.append(
            {
                "tool_name": item.get("tool_name") or request_payload.get("tool_name"),
                "operation": request_input.get("operation") or result_output.get("operation"),
                "status": item.get("status") or result_payload.get("status"),
                "summary": result_output.get("summary") or result_output.get("text") or result_payload.get("error_message"),
            }
        )
    return compact


def _tool_outcome_counts(invocations: list[dict]) -> tuple[int, int]:
    failures = 0
    approvals = 0
    for item in invocations:
        status = str(item.get("status") or "").lower()
        if status == "needs_approval":
            approvals += 1
        elif status and status not in {"succeeded", "running"}:
            failures += 1
    return failures, approvals


def _operator_decision_count(events: list[dict]) -> int:
    return sum(
        1
        for event in events
        if event.get("event") == "task_state_changed"
        and (event.get("payload") or {}).get("action") == "operator_decision"
    )


def _parse_timestamp(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _max_update_gap_seconds(start_iso: str, end_iso: str, events: list[dict]) -> float | None:
    timestamps = [_parse_timestamp(start_iso)]
    timestamps.extend(_parse_timestamp(str(event.get("time") or "")) for event in events)
    timestamps.append(_parse_timestamp(end_iso))
    valid = sorted(item for item in timestamps if item is not None)
    if len(valid) < 2:
        return None
    return round(max((right - left).total_seconds() for left, right in zip(valid, valid[1:])), 1)


def _turn_ceiling_seconds(max_seconds: int) -> tuple[int, bool]:
    """How long the runner waits for one turn, and whether that clipped the case.

    The wait must outlast the worker's own per-task budget, or the runner walks
    away while the worker is still busy and blocks the next case. HARD_CEILING_S
    remains a backstop against a runaway case, but a case that deliberately
    declares a longer budget gets told it was clipped: a silently halved deadline
    reads as "the agent stalled" when the runner simply left early.
    """
    ceiling = max(int(max_seconds), WORKER_BUDGET_SAFETY_S) + 30
    if ceiling > HARD_CEILING_S:
        return HARD_CEILING_S, True
    return ceiling, False


def _filler_reply_texts() -> frozenset[str]:
    """The pre-classification pings that carry no answer.

    `channels/base.py` names these constants precisely so a consumer can
    recognize them; importing them keeps the runner from drifting out of sync
    with the strings the product actually sends.
    """
    try:
        from agent_control.channels.base import ACKNOWLEDGMENT_TEXT, TASK_STARTED_TEXT

        return frozenset({ACKNOWLEDGMENT_TEXT.strip(), TASK_STARTED_TEXT.strip()})
    except Exception:
        return frozenset()


_FILLER_REPLIES = _filler_reply_texts()


def _is_filler_reply(event: dict) -> bool:
    payload = event.get("payload") if isinstance(event, dict) else None
    text = str((payload or {}).get("text") or (payload or {}).get("caption") or "").strip()
    return bool(text) and text in _FILLER_REPLIES


def _capture_sent_messages(turn: TurnResult, turn_start_iso: str) -> None:
    """Populate every outbound-message field from the audit log.

    Shared by the task path and the chat path so both are judged against the
    same independent record of what the channel actually sent.
    """
    turn.telegram_sent_events = fetch_message_sent_events(turn_start_iso)
    turn.telegram_update_count = len(turn.telegram_sent_events)
    turn.telegram_confirmed_text = "\n".join(
        str((event.get("payload") or {}).get("text") or (event.get("payload") or {}).get("caption") or "")
        for event in turn.telegram_sent_events
    ).strip()
    turn.telegram_confirmed_media_count = sum(
        1 for event in turn.telegram_sent_events if (event.get("payload") or {}).get("kind") in {"photo", "document"}
    )
    turn.telegram_max_update_gap_seconds = _max_update_gap_seconds(
        turn_start_iso,
        datetime.now(timezone.utc).isoformat(),
        turn.telegram_sent_events,
    )


def _tools_seen(invocations: list[dict], plan_steps: list[dict]) -> list[str]:
    seen: list[str] = []
    for item in invocations:
        tool = str(item.get("tool_name") or "").strip()
        operation = str(item.get("operation") or "").strip()
        if tool:
            seen.append(f"{tool}:{operation}" if operation else tool)
    return seen


def _telegram_media_count(metadata: dict[str, Any], invocations: list[dict]) -> int:
    count = 0
    for item in invocations:
        if item.get("tool_name") != "artifact.deliver":
            continue
        request_result = item.get("result") if isinstance(item.get("result"), dict) else {}
        output = request_result.get("output") if isinstance(request_result.get("output"), dict) else {}
        if output.get("delivered") and str(output.get("delivery_method") or "").startswith("telegram."):
            count += 1
    delivery = metadata.get("artifact_delivery")
    if isinstance(delivery, dict) and delivery.get("delivered") and count == 0:
        count = 1
    return count


def _bot_reply_text(task: dict[str, Any], metadata: dict[str, Any], last_tool_output: str | None) -> str:
    status = str(task.get("status") or "")
    # A clarifying task has no synthesized answer, so this used to fall through
    # to the last tool output and report a file's contents as "the bot reply" -
    # hiding the actual question and making the diagnosis unreadable.
    if status == "clarifying" and metadata.get("clarifying_question"):
        return str(metadata["clarifying_question"])
    if metadata.get("synthesized_answer"):
        return str(metadata["synthesized_answer"])
    delivery = metadata.get("artifact_delivery")
    if isinstance(delivery, dict) and delivery.get("summary"):
        return str(delivery["summary"])
    if last_tool_output:
        return last_tool_output
    if status == "completed":
        return "Done."
    if status:
        return f"Status: {status}."
    return ""


async def _run_turn(
    label: str,
    message: str,
    max_seconds: int,
    *,
    resume_task_id: str | None = None,
    settled_statuses: set[str] | None = None,
    expects_task: bool = True,
) -> TurnResult:
    """Send one message + poll the spawned task until terminal/timeout. All errors caught.

    ``expects_task=False`` covers the chat route, where the correct behavior is a
    direct reply and *no* persisted task. Those turns were previously untestable:
    the runner treated "no task spawned" as a failure, so conversational
    behavior - including teaching a durable preference - had no coverage at all.
    """
    turn = TurnResult(label=label, message=message)
    start_mono = time.monotonic()
    try:
        # Snapshot known task ids so we can detect the new one.
        try:
            known = {t["id"] for t in admin_summary().get("tasks", [])}
        except Exception as exc:
            turn.error = f"admin API unreachable before send: {exc}"
            turn.duration_s = round(time.monotonic() - start_mono, 1)
            return turn

        # Anchor for the audit-log truth source: only MESSAGE_SENT events
        # recorded after this point belong to this turn.
        turn_start_iso = datetime.now(timezone.utc).isoformat()

        try:
            await _send_message(message)
        except Exception as exc:
            turn.error = f"telegram send failed: {exc}"
            turn.duration_s = round(time.monotonic() - start_mono, 1)
            return turn

        if not expects_task:
            turn.final_status = "chat"
            turn.classifier_verdict = fetch_classifier_verdict_for_text(message)
            deadline = time.monotonic() + min(max_seconds, CHAT_REPLY_TIMEOUT_S)
            while time.monotonic() < deadline:
                # Wait for a *substantive* reply, not merely the first event.
                # The acknowledgment ping is sent before classification runs, so
                # any fixed settle after it is a race against an LLM call -
                # losing that race captured only the filler line and failed a
                # turn the system had actually answered correctly.
                if any(not _is_filler_reply(event) for event in fetch_message_sent_events(turn_start_iso)):
                    await asyncio.sleep(CHAT_REPLY_SETTLE_S)
                    break
                await asyncio.sleep(2)
            _capture_sent_messages(turn, turn_start_iso)
            turn.bot_reply_text = turn.telegram_confirmed_text
            if not turn.telegram_sent_events:
                turn.error = f"chat-route turn produced no reply within {CHAT_REPLY_TIMEOUT_S}s"
            turn.duration_s = round(time.monotonic() - start_mono, 1)
            print(f"    [chat] {turn.telegram_update_count} reply message(s)")
            return turn

        spawn_timeout_s = min(TASK_SPAWN_TIMEOUT_S, max_seconds)
        task_id = resume_task_id or await _find_new_task(
            known,
            message,
            spawn_timeout_s=spawn_timeout_s,
        )
        if not task_id:
            # Distinguish "classifier said is_task=False" from "polling didn't see the message"
            # so the report can blame the right component. One lookup serves both
            # the blame attribution and the diagnosis dump.
            verdict = fetch_classifier_verdict_for_text(message)
            turn.classifier_verdict = verdict
            if verdict is not None:
                reason = str(verdict.get("reason") or "")
                if verdict.get("is_task") is False:
                    turn.error = (
                        f"classifier ruled is_task=False (no persisted task spawned). "
                        f"reason: {reason[:200]}"
                    )
                else:
                    turn.error = (
                        f"classifier said is_task=True but no task spawned within {spawn_timeout_s}s "
                        f"(intake or worker layer dropped it). reason: {reason[:200]}"
                    )
            else:
                turn.error = (
                    f"no task spawned within {spawn_timeout_s}s - "
                    "message never classified (telegram intake stuck?)"
                )
            turn.duration_s = round(time.monotonic() - start_mono, 1)
            return turn
        turn.task_id = task_id
        print(f"    [task] {task_id}")

        last_status = None
        last_step = None
        last_replan = None
        ceiling, clipped = _turn_ceiling_seconds(max_seconds)
        if clipped:
            print(
                f"    [warn] declared timeout {max_seconds}s needs more than "
                f"HARD_CEILING_S={HARD_CEILING_S}s of runner wait - capping at {ceiling}s; "
                "raise the constant to honor it"
            )
        deadline = start_mono + ceiling
        reached_terminal = False
        while time.monotonic() < deadline:
            try:
                trace = admin_trace(task_id)
            except Exception:
                await asyncio.sleep(3)
                continue
            task = trace.get("task") or {}
            meta = task.get("metadata") or {}
            status = task.get("status")
            step = meta.get("last_tool_name") or ""
            replan = int(meta.get("replan_count") or 0)
            if status != last_status or step != last_step or replan != last_replan:
                elapsed = round(time.monotonic() - start_mono, 1)
                turn.status_transitions.append({
                    "at_s": elapsed,
                    "status": status,
                    "tool": step,
                    "replan_count": replan,
                })
                synth_present = bool(meta.get("synthesized_answer"))
                print(f"    [{elapsed:>6.1f}s] status={status:12} tool={step:22} replan={replan} synth={synth_present}")
                last_status = status
                last_step = step
                last_replan = replan
            terminal_statuses = {"completed", "failed", "blocked", "cancelled"} | (settled_statuses or set())
            if status in terminal_statuses:
                reached_terminal = True
                break
            # A task that asks a question waits for a human. Nothing here ever
            # answered one, so any case where the agent chose to clarify sat in
            # CLARIFYING until the deadline and was force-failed - scoring the
            # agent's *correct* caution (safe_file_operations exists precisely
            # to check it asks before destructive work) as a product failure.
            # One neutral confirmation per turn, echoing the original request
            # so the answer cannot smuggle in new instructions, and logged
            # loudly so a pass here is never mistaken for an unprompted one.
            if status == "clarifying" and not turn.clarification_sent:
                turn.clarification_sent = True
                answer = (
                    "Yes, please go ahead with what you proposed, staying within the "
                    "constraints in my original message."
                )
                print(f"    [clarify] agent asked a question; replying: {answer}")
                try:
                    await _send_message(answer)
                except Exception as exc:  # noqa: BLE001 - keep polling regardless
                    print(f"    [clarify] could not reply: {exc}")
            await asyncio.sleep(3)

        if reached_terminal and last_status:
            await _wait_for_status_notification(task_id, str(last_status))

        # If the wait expired and the task is STILL non-terminal, cancel it
        # cooperatively. This records the signal and lets YBM clean up pending
        # approvals or external sessions before the runner advances.
        if not reached_terminal:
            cancelled = cancel_task(task_id)
            if cancelled:
                turn.forced_failed_by_runner = True
                elapsed = round(time.monotonic() - start_mono, 1)
                print(f"    [{elapsed:>6.1f}s] CANCELLED stuck task through admin control path")

        # Capture the classifier verdict for this turn's message regardless
        # of pass/fail, so the timeline always shows what the classifier said.
        turn.classifier_verdict = fetch_classifier_verdict_for_text(message)

        # Final capture
        try:
            trace = admin_trace(task_id)
        except Exception:
            trace = {}
        task = trace.get("task") or {}
        meta = task.get("metadata") or {}
        plan = trace.get("plan") or {}
        raw_invocations = trace.get("tool_invocations") or []
        artifacts = trace.get("artifacts") or []
        last_tool_result = meta.get("last_tool_result") or {}
        out = last_tool_result.get("output") or {}

        turn.final_status = task.get("status")
        turn.metadata = meta if isinstance(meta, dict) else {}
        turn.synth_answer = meta.get("synthesized_answer")
        turn.replan_count = int(meta.get("replan_count") or 0)
        turn.last_worker_error = meta.get("last_worker_error") or None
        turn.planning_error = meta.get("planning_error") or None
        turn.last_replan_reason = meta.get("last_replan_reason") or None
        turn.fulfillment_gap = meta.get("fulfillment_gap") or None
        turn.last_tool_output = (
            out.get("summary")
            or out.get("stdout")
            or out.get("text")
            or out.get("final_summary")
            or (json.dumps(out)[:600] if isinstance(out, dict) else None)
        )
        turn.plan_steps = [
            {
                "tool_name": s.get("tool_name"),
                "operation": (s.get("tool_input") or {}).get("operation"),
                "status": s.get("status"),
                "title": s.get("title"),
            }
            for s in (plan.get("steps") or [])
        ]
        turn.tool_invocations = _compact_tool_invocations(raw_invocations)
        turn.tools_seen = _tools_seen(turn.tool_invocations, turn.plan_steps)
        turn.tool_failure_count, turn.tool_approval_count = _tool_outcome_counts(turn.tool_invocations)
        turn.artifacts = artifacts if isinstance(artifacts, list) else []
        turn.artifact_count = len(turn.artifacts)
        turn.telegram_media_count = _telegram_media_count(turn.metadata, raw_invocations if isinstance(raw_invocations, list) else [])
        turn.bot_reply_text = _bot_reply_text(task, turn.metadata, turn.last_tool_output)
        turn.changed_paths_count = _count_changed_paths(turn.metadata)
        turn.duration_s = round(time.monotonic() - start_mono, 1)
        task_audit = fetch_task_audit(task_id)
        turn.audit_event_count = len(task_audit)
        turn.operator_decision_count = _operator_decision_count(task_audit)
        _capture_sent_messages(turn, turn_start_iso)
    except Exception as exc:
        turn.error = f"runner exception: {exc}\n{traceback.format_exc()[-1500:]}"
        turn.duration_s = round(time.monotonic() - start_mono, 1)
    return turn


async def _run_one(case: dict, fixtures: dict[str, str]) -> StageResult:
    stage = StageResult(
        case_id=case.get("id") or "(unknown)",
        category=(case.get("tags") or [None])[0] or "uncategorized",
        size=case.get("size") or "small",
        message=case.get("message") or "",
        source_file=case.get("source_file") or "?",
    )
    stage.started_at = datetime.now().isoformat(timespec="seconds")
    start_mono = time.monotonic()
    existing_memory_fact_ids = _memory_fact_ids()

    print(f"\n{'=' * 78}")
    print(f"  {stage.case_id}  [{stage.size}/{stage.source_file}]")
    print(f"  > {stage.message[:140]}")
    print(f"{'=' * 78}")

    try:
        # Clear memory at the start of each top-level stage, EXCEPT when this
        # case is explicitly a follow-up-with-memory case.
        if not any(t in (case.get("tags") or []) for t in ("followup", "memory")):
            clear_conversation_memory()

        rendered_message = render_text(stage.message, fixtures)
        max_seconds = int(case.get("timeout_seconds") or 360)

        initial = await _run_turn(
            "initial",
            rendered_message,
            max_seconds=max_seconds,
            settled_statuses={str(item) for item in case.get("settled_statuses") or []},
            expects_task=bool(case.get("expects_task", True)),
        )
        stage.turns.append(initial)
        passed, reason = _diagnose_turn(case, initial, is_followup=False, fixtures=fixtures)

        # Follow-ups (keep memory) - declared in JSON
        for fu in (case.get("follow_ups") or []):
            fu_msg = render_text(str(fu.get("message") or ""), fixtures)
            fu_to = int(fu.get("timeout_seconds") or max_seconds)
            print(f"    [follow-up] {fu.get('id')}: {fu_msg[:120]}")
            fu_turn = await _run_turn(
                f"followup:{fu.get('id')}",
                fu_msg,
                max_seconds=fu_to,
                resume_task_id=initial.task_id if fu.get("resume_task") else None,
                settled_statuses={str(item) for item in fu.get("settled_statuses") or []},
                expects_task=bool(fu.get("expects_task", True)),
            )
            stage.turns.append(fu_turn)
            fu_case = {**case, "assertions": fu.get("assertions", case.get("assertions") or {})}
            fu_ok, fu_reason = _diagnose_turn(
                fu_case, fu_turn, is_followup=True, fixtures=fixtures
            )
            if passed and not fu_ok:
                passed, reason = False, f"follow-up `{fu.get('id')}` failed: {fu_reason}"

        stage.passed = passed
        stage.failure_reason = reason
    except Exception as exc:
        stage.runner_exception = f"{exc}\n{traceback.format_exc()[-1500:]}"
        stage.failure_reason = f"runner exception: {exc}"
        stage.passed = False
    finally:
        _cleanup_settled_stage_tasks(stage)
        _delete_memory_facts_created_after(existing_memory_fact_ids)
        stage.total_duration_s = round(time.monotonic() - start_mono, 1)
        stage.ended_at = datetime.now().isoformat(timespec="seconds")
    return stage


def _cleanup_settled_stage_tasks(stage: StageResult) -> None:
    """Prevent one independent E2E case from becoming another's clarification.

    Telegram correctly routes the next user message into the newest task that
    is still awaiting approval or clarification. An E2E case may deliberately
    stop at that boundary, but leaving its task live makes the next top-level
    case look like a reply to it. Capture has already finished here, so cancel
    only genuinely unsettled final task states through the public admin signal.
    """
    final_by_task: dict[str, str | None] = {}
    for turn in stage.turns:
        if turn.task_id:
            final_by_task[turn.task_id] = turn.final_status
    for task_id, status in final_by_task.items():
        if status in {"awaiting_approval", "clarifying", "awaiting_external"}:
            cancel_task(task_id)


# ---------- Output / reporting ----------


def _write_stage_artifacts(stage_dir: Path, case: dict, stage: StageResult) -> None:
    try:
        stage_dir.mkdir(parents=True, exist_ok=True)
        (stage_dir / "result.json").write_text(
            json.dumps({"case": case, "result": asdict(stage)}, indent=2, default=str),
            encoding="utf-8",
        )

        timeline: list[str] = [
            f"# {stage.case_id}  [{stage.size}]",
            "",
            f"Source:        {stage.source_file}",
            f"Final:         {'PASS' if stage.passed else 'FAIL'}",
            f"Duration:      {stage.total_duration_s}s",
            f"Reason:        {stage.failure_reason or 'ok'}",
            "",
        ]
        for turn in stage.turns:
            timeline.extend([
                "",
                f"## Turn: {turn.label}",
                f"  Message:     {turn.message[:200]}",
                f"  Task ID:     {turn.task_id}",
                f"  Status:      {turn.final_status}",
                f"  Replans:     {turn.replan_count}",
                f"  Duration:    {turn.duration_s}s",
                f"  Audit ev:    {turn.audit_event_count}",
                f"  Force-failed by runner: {turn.forced_failed_by_runner}",
                f"  Tools seen:  {', '.join(turn.tools_seen) or '(none)'}",
                f"  Artifacts:   {turn.artifact_count}",
                f"  TG media:    {turn.telegram_media_count}",
                f"  TG updates:  {turn.telegram_update_count}",
                f"  Max TG gap:  {turn.telegram_max_update_gap_seconds}s",
                f"  Tool failures/approvals: {turn.tool_failure_count}/{turn.tool_approval_count}",
                f"  Operator decisions: {turn.operator_decision_count}",
            ])
            if turn.classifier_verdict:
                v = turn.classifier_verdict
                timeline.append(
                    f"  Classifier:  is_task={v.get('is_task')} "
                    f"route={v.get('route')} conf={v.get('confidence')} "
                    f"task_type={v.get('task_type')}"
                )
                timeline.append(f"    reason: {(v.get('reason') or '')[:300]}")
            if turn.error:
                timeline.extend([f"  Runner err:  {turn.error[:400]}"])
            timeline.append("  Transitions:")
            for ev in turn.status_transitions:
                timeline.append(
                    f"    [{ev['at_s']:>6.1f}s] status={ev['status']:12} "
                    f"tool={ev['tool']:22} replan={ev['replan_count']}"
                )
            timeline.append("  Plan steps:")
            for s in turn.plan_steps:
                timeline.append(f"    - {s.get('tool_name')} | op={s.get('operation')} | {s.get('title')}")
            timeline.extend([
                "  Synthesized answer:",
                "    " + (turn.synth_answer or "(none)")[:1200].replace("\n", "\n    "),
                "  Bot reply text:",
                "    " + (turn.bot_reply_text or "(none)")[:1200].replace("\n", "\n    "),
                "  Last tool output:",
                "    " + (turn.last_tool_output or "(none)")[:800].replace("\n", "\n    "),
            ])
        (stage_dir / "timeline.txt").write_text("\n".join(timeline), encoding="utf-8")

        # Aggregated audit across all turns
        combined_audit: dict[str, list[dict]] = {}
        for turn in stage.turns:
            if turn.task_id:
                combined_audit[turn.task_id] = fetch_task_audit(turn.task_id)
        if combined_audit:
            (stage_dir / "audit.json").write_text(
                json.dumps(combined_audit, indent=2, default=str),
                encoding="utf-8",
            )

        # Compact, reviewable evidence for why the system took each visible
        # action. These are structured decision fields emitted by the agents,
        # tool outcomes, and observed state transitions - not hidden chain of
        # thought. audit.json remains the lossless source when deeper diagnosis
        # is needed.
        decision_trace = {
            "case_id": stage.case_id,
            "description": "Observable structured decisions, actions, and outcomes; no private chain-of-thought.",
            "turns": [],
        }
        for turn in stage.turns:
            task_audit = fetch_task_audit(turn.task_id) if turn.task_id else []
            decisions = [
                {
                    "time": event.get("time"),
                    "actor": event.get("actor"),
                    "step_id": (event.get("payload") or {}).get("step_id"),
                    "step_index": (event.get("payload") or {}).get("step_index"),
                    "decision": (event.get("payload") or {}).get("decision"),
                }
                for event in task_audit
                if event.get("event") == "task_state_changed"
                and (event.get("payload") or {}).get("action") == "operator_decision"
            ]
            decision_trace["turns"].append({
                "label": turn.label,
                "task_id": turn.task_id,
                "classifier": turn.classifier_verdict,
                "status_transitions": turn.status_transitions,
                "operator_decisions": decisions,
                "operator_history": turn.metadata.get("operator_history", []),
                "tool_invocations": turn.tool_invocations,
                "telegram_update_count": turn.telegram_update_count,
                "telegram_max_update_gap_seconds": turn.telegram_max_update_gap_seconds,
            })
        (stage_dir / "decision_trace.json").write_text(
            json.dumps(decision_trace, indent=2, default=str),
            encoding="utf-8",
        )

        if not stage.passed:
            diag = [
                f"# Why `{stage.case_id}` failed",
                "",
                f"Reason: {stage.failure_reason}",
                "",
            ]
            if stage.runner_exception:
                diag.extend(["## Runner exception", "```", stage.runner_exception, "```", ""])
            for turn in stage.turns:
                diag.extend([
                    f"## Turn `{turn.label}`",
                    f"- final_status: `{turn.final_status}`",
                    f"- task_id: `{turn.task_id}`",
                    f"- duration: {turn.duration_s}s",
                    f"- replan_count: {turn.replan_count}",
                    f"- force_failed_by_runner: `{turn.forced_failed_by_runner}`",
                    f"- tools_seen: `{turn.tools_seen}`",
                    f"- artifacts: `{turn.artifact_count}`",
                    f"- telegram_media_count: `{turn.telegram_media_count}`",
                    f"- telegram_update_count: `{turn.telegram_update_count}`",
                    f"- telegram_max_update_gap_seconds: `{turn.telegram_max_update_gap_seconds}`",
                    f"- tool_failure_count: `{turn.tool_failure_count}`",
                    f"- tool_approval_count: `{turn.tool_approval_count}`",
                    f"- operator_decision_count: `{turn.operator_decision_count}`",
                ])
                if turn.classifier_verdict:
                    v = turn.classifier_verdict
                    diag.extend([
                        "",
                        "**Classifier verdict for this turn:**",
                        f"- is_task: `{v.get('is_task')}`",
                        f"- route: `{v.get('route')}`",
                        f"- task_type: `{v.get('task_type')}`",
                        f"- confidence: `{v.get('confidence')}`",
                        f"- reason: {(v.get('reason') or '')[:400]}",
                    ])
                if turn.error:
                    diag.extend(["", "**runner_error:**", "```", turn.error[:800], "```"])
                if turn.last_worker_error:
                    diag.extend(["", "**last_worker_error:**", "```", turn.last_worker_error, "```"])
                if turn.planning_error:
                    diag.extend(["", "**planning_error:**", "```", turn.planning_error, "```"])
                if turn.last_replan_reason:
                    diag.extend(["", "**last_replan_reason:**", "```", turn.last_replan_reason, "```"])
                if turn.fulfillment_gap:
                    diag.extend(["", "**fulfillment_gap:**", "```", turn.fulfillment_gap, "```"])
                diag.extend(["", "**Plan steps:**"])
                for s in turn.plan_steps:
                    diag.append(f"- `{s.get('tool_name')}` op=`{s.get('operation')}` - {s.get('title')}")
                diag.extend([
                    "",
                    "**Last tool output (800 chars):**",
                    "```",
                    (turn.last_tool_output or "(none)")[:800],
                    "```",
                    "",
                ])
            diag.append("See `audit.json` in this folder for the full event trail.")
            (stage_dir / "diagnosis.md").write_text("\n".join(diag), encoding="utf-8")
    except Exception as exc:
        # NEVER let report-writing crash the run.
        print(f"    [warn] failed to write stage artifacts: {exc}")


def _write_summary(run_dir: Path, results: list[StageResult]) -> None:
    try:
        rows: list[str] = []
        for r in results:
            icon = "✅" if r.passed else "❌"
            outcome = "PASS" if r.passed else f"FAIL ({(r.failure_reason or '')[:80]})"
            final_statuses = ",".join(t.final_status or "-" for t in r.turns)
            rows.append(
                f"| {icon} | `{r.case_id}` | {r.size} | {final_statuses} | "
                f"{r.total_duration_s:.1f}s | {sum(t.replan_count for t in r.turns)} | {outcome} |"
            )
        md = [
            f"# YBM E2E Run - {run_dir.name}",
            "",
            f"Started: {results[0].started_at if results else '-'}",
            f"Total:   {len(results)}",
            f"Passed:  {sum(1 for r in results if r.passed)}",
            f"Failed:  {sum(1 for r in results if not r.passed)}",
            "",
            "| | Case | Size | Final statuses | Duration | Replans | Outcome |",
            "|---|---|---|---|---|---|---|",
            *rows,
            "",
            "Per-stage artifacts: `<index>_<case_id>/` (timeline.txt, audit.json, "
            "decision_trace.json, diagnosis.md if failed)",
        ]
        (run_dir / "summary.md").write_text("\n".join(md), encoding="utf-8")
        (run_dir / "summary.json").write_text(
            json.dumps([asdict(r) for r in results], indent=2, default=str),
            encoding="utf-8",
        )
    except Exception as exc:
        print(f"  [warn] failed to write summary: {exc}")


def _preflight() -> list[str]:
    issues: list[str] = []
    if not ENV_PATH.exists():
        issues.append(f".env not found at {ENV_PATH}")
    if not DB_PATH.exists():
        issues.append(f"agent_control.db not found at {DB_PATH}")
    if not CASES_PATH.exists():
        issues.append(f"case catalogue not found at {CASES_PATH}")
    try:
        admin_get("/admin/api/summary?task_limit=1")
    except Exception:
        issues.append(f"YBM admin API not responding at {ADMIN_BASE}")
    # Check the LLM the stack is actually configured to use, not a fixed port.
    # This used to ping LocalDeploy's :8000/health unconditionally, so every
    # case refused to start on a perfectly working install pointed at Ollama or
    # a cloud endpoint - reporting "LocalDeploy not responding" for a service
    # that install was never meant to run. check_llm_configured() already
    # knows the difference between an Ollama tag list, a LocalDeploy health
    # endpoint and a cloud profile's key; reuse it rather than re-deriving it.
    from agent_control.bootstrap import check_llm_configured
    from agent_control.config import load_settings

    settings = load_settings(ROOT / "config" / "config.yaml", _env_file=ENV_PATH)
    if not check_llm_configured(settings):
        profile = settings.llm.profiles.get(settings.llm.default_profile)
        endpoint = (profile.base_url if profile else None) or "not configured"
        issues.append(
            f"default LLM profile {settings.llm.default_profile!r} is not reachable "
            f"({endpoint}) - start it, or point llm.profiles at a working endpoint"
        )
    return issues


def _warn_if_chrome_down() -> None:
    """Browser cases need Chrome with remote debugging on port 9222.

    The browser adapter auto-launches Chrome on first use, but a heads-up here
    lets the user fix it before half the suite fails. We do NOT bail - many
    cases don't need a browser, so we just warn.
    """
    if not ping("http://127.0.0.1:9222/json/version", timeout=2.0):
        try:
            _try_launch_chrome()
        except Exception as exc:
            print(f"  [chrome] WARN: not on :9222 and auto-launch failed: {exc}")
            print("           browser cases will fail. start Chrome with:")
            print("           chrome --remote-debugging-port=9222 --remote-allow-origins=*")
            return
        # Re-check after a short pause to give Chrome a moment to bind.
        import time as _t
        for _ in range(15):
            _t.sleep(1)
            if ping("http://127.0.0.1:9222/json/version", timeout=1.0):
                print("  [chrome] auto-launched, remote debugging on :9222")
                return
        print("  [chrome] WARN: launched but not yet responding on :9222")
    else:
        print("  [chrome] already running with remote debugging on :9222")


def _try_launch_chrome() -> None:
    """Best-effort Chrome launch with remote debugging. Windows-focused paths."""
    import os
    import shutil
    import subprocess
    candidates = [
        os.environ.get("CHROME_PATH"),
        shutil.which("chrome"),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    ]
    exe = next((p for p in candidates if p and Path(p).exists()), None)
    if not exe:
        raise RuntimeError("Chrome executable not found")
    user_data_dir = ROOT / ".agent_control" / "browser" / "chrome-profile"
    user_data_dir.mkdir(parents=True, exist_ok=True)
    subprocess.Popen(
        [
            exe,
            "--remote-debugging-port=9222",
            "--remote-allow-origins=*",
            f"--user-data-dir={user_data_dir}",
            "--no-first-run",
            "--no-default-browser-check",
        ],
        creationflags=getattr(subprocess, "DETACHED_PROCESS", 0),
    )


# ---------- Main ----------


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", default="", help="comma-separated case ids")
    parser.add_argument("--skip", default="", help="comma-separated case ids to skip")
    parser.add_argument("--sizes", default="", help="comma-separated sizes: small,medium,long-running")
    parser.add_argument(
        "--suite",
        default="",
        help=(
            "comma-separated suites: smoke,tools,autonomy,evolution,code_interpreter,"
            "mcp,recovery,external_agent,human_autonomy,full"
        ),
    )
    parser.add_argument("--include-guarded", action="store_true",
                        help="include codex/copilot/quota/limit cases (usually need external creds)")
    parser.add_argument("--no-web-fixture", action="store_true",
                        help="skip the local fixture web server (some cases will be skipped/fail)")
    parser.add_argument("--no-auto-approve", action="store_true",
                        help="do not grant approval prompts during the run (cases needing "
                             "approval will stall until they time out)")
    args = parser.parse_args()

    _load_env()
    issues = _preflight()
    if issues:
        print("Preflight failed - start the missing services first:")
        for issue in issues:
            print(f"  - {issue}")
        return 1

    cases_all = load_cases()
    cleanup_catalog_memory_facts(cases_all)
    selected = select_cases(
        cases_all,
        only={s.strip() for s in args.only.split(",") if s.strip()},
        skip={s.strip() for s in args.skip.split(",") if s.strip()},
        sizes={s.strip() for s in args.sizes.split(",") if s.strip()},
        suites={s.strip() for s in args.suite.split(",") if s.strip()},
        include_guarded=args.include_guarded,
    )

    # Pre-flight inventory: what's actually going to run, how long it might take,
    # what skipped. This is the single most useful thing to glance at before
    # walking away from the machine.
    from collections import Counter
    size_count = Counter(c.get("size") or "small" for c in selected)
    suite_count = Counter(suite for c in selected for suite in _case_suites(c) if suite != "full")
    declared_total_s = sum(int(c.get("timeout_seconds") or 360) for c in selected)
    print(f"Loaded {len(cases_all)} cases. Selected {len(selected)} (guarded={'included' if args.include_guarded else 'skipped'}).")
    print("  By size: " + ", ".join(f"{size}={n}" for size, n in size_count.items()))
    if suite_count:
        print("  By suite: " + ", ".join(f"{suite}={n}" for suite, n in sorted(suite_count.items())))
    print(f"  Sum of declared timeouts: {declared_total_s}s "
          f"(~{declared_total_s // 60}m; actual will differ from this upper bound)")
    skipped = [c for c in cases_all if c not in selected]
    if skipped:
        skipped_size = Counter(c.get("size") or "small" for c in skipped)
        print(f"  Skipped {len(skipped)}: " + ", ".join(f"{size}={n}" for size, n in skipped_size.items()))

    run_dir = RESULTS_ROOT / f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    run_dir.mkdir(parents=True, exist_ok=True)
    print(f"Writing results to: {run_dir}")

    needs_web = any(
        "{{fixture_base_url}}" in (c.get("message") or "")
        or "{{episode_url}}" in (c.get("message") or "")
        or "{{form_url}}" in (c.get("message") or "")
        for c in selected
    )
    try:
        fixtures = prepare_fixtures(start_web=needs_web and not args.no_web_fixture)
        missing_fixtures = required_fixture_names(selected) - set(fixtures)
        if missing_fixtures:
            raise RuntimeError(f"missing fixture values: {sorted(missing_fixtures)}")
    except Exception as exc:
        message = f"Fixture preparation failed; no Telegram cases were sent: {exc}"
        print(f"  [fixtures] ERROR: {message}")
        (run_dir / "fixture_error.txt").write_text(message + "\n", encoding="utf-8")
        (run_dir / "summary.md").write_text(
            f"# YBM E2E Run - {run_dir.name}\n\n{message}\n",
            encoding="utf-8",
        )
        return 1

    mcp_fixture: TemporaryMCPFixture | None = None
    if any("fake_mcp_server" in (case.get("setup") or []) for case in selected):
        try:
            print("  Provisioning temporary fake MCP server and reloading YBM...")
            mcp_fixture = provision_fake_mcp_fixture(Path(fixtures["fake_mcp_server_path"]))
            atexit.register(mcp_fixture.restore)
        except Exception as exc:  # noqa: BLE001 - preflight boundary reports actionable failure
            print(f"Preflight failed - could not provision fake MCP fixture: {exc}")
            return 1
        reload_issues = _preflight()
        if reload_issues:
            print("Preflight failed after loading the fake MCP fixture:")
            for issue in reload_issues:
                print(f"  - {issue}")
            atexit.unregister(mcp_fixture.restore)
            mcp_fixture.restore()
            return 1

    # Browser cases need Chrome with remote debugging on :9222. Warn (and
    # try to auto-launch) before the run rather than after we've burned 20min
    # on a queue full of browser cases that all fail at the adapter.
    needs_browser = any(
        any(t in (c.get("tags") or []) for t in ("browser",))
        or (c.get("tools_required") and any("browser" in t for t in c["tools_required"]))
        for c in selected
    )
    if needs_browser:
        _warn_if_chrome_down()

    # Answer approvals for the duration of the run. Without this the suite
    # cannot complete at all: several tools force approval at the runtime level
    # regardless of config, and nothing else here ever grants one.
    approver_stop = asyncio.Event()
    granted_approvals: list[str] = []
    approver_task: asyncio.Task | None = None
    if not args.no_auto_approve:
        approver_task = asyncio.create_task(auto_approve_loop(approver_stop, granted_approvals))
        print("  Auto-approving policy prompts for this run (--no-auto-approve to disable).")

    results: list[StageResult] = []
    suite_start = time.monotonic()
    total = len(selected)
    for idx, case in enumerate(selected, start=1):
        stage_dir = run_dir / f"{idx:02d}_{case.get('id','case')}"

        # Progress banner BEFORE each case so the user knows where we are.
        passed_so_far = sum(1 for r in results if r.passed)
        failed_so_far = len(results) - passed_so_far
        suite_elapsed = time.monotonic() - suite_start
        eta_s = _estimate_eta(suite_elapsed, idx - 1, total)
        bar = _progress_bar(idx - 1, total, width=24)
        print()
        print(f"{bar} {idx:>3}/{total} ({(idx-1)*100//max(1,total)}%) "
              f"| pass {passed_so_far} fail {failed_so_far} "
              f"| elapsed {_fmt_duration(suite_elapsed)} "
              f"| ETA {eta_s}")

        try:
            stage = await _run_one(case, fixtures)
        except Exception as exc:
            # The outermost net: even if _run_one itself blows up we keep going.
            stage = StageResult(
                case_id=case.get("id") or "(unknown)",
                category="uncategorized",
                size=case.get("size") or "small",
                message=case.get("message") or "",
                source_file=case.get("source_file") or "?",
                runner_exception=f"{exc}\n{traceback.format_exc()[-1500:]}",
                failure_reason=f"outer runner exception: {exc}",
            )
        results.append(stage)
        _write_stage_artifacts(stage_dir, case, stage)
        _write_summary(run_dir, results)
        status = "PASS" if stage.passed else "FAIL"
        print(f"  --> {status}  ({stage.total_duration_s:.1f}s)  {stage.failure_reason or 'ok'}")

    approver_stop.set()
    if approver_task is not None:
        try:
            await asyncio.wait_for(approver_task, timeout=5.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            approver_task.cancel()

    print(f"\n{'=' * 78}")
    passed = sum(1 for r in results if r.passed)
    total_time = _fmt_duration(time.monotonic() - suite_start)
    print(f"{_progress_bar(len(results), total, width=24)} {len(results)}/{total} done")
    print(f"Summary: {run_dir / 'summary.md'}")
    if granted_approvals:
        print(f"Approvals granted during the run: {len(granted_approvals)}")
    print(f"Passed {passed} / {len(results)}  |  total {total_time}")
    exit_code = 0 if passed == len(results) else 2
    if mcp_fixture is not None:
        print("  Restoring MCP config/catalog and reloading YBM...")
        atexit.unregister(mcp_fixture.restore)
        try:
            mcp_fixture.restore()
        except Exception as exc:  # noqa: BLE001 - restoration must convert any failure to exit 1
            print(f"  [fixtures] ERROR: could not restore YBM after MCP run: {exc}")
            return 1
    return exit_code


def _progress_bar(done: int, total: int, *, width: int = 24) -> str:
    if total <= 0:
        return "[" + " " * width + "]"
    filled = int(round(width * done / total))
    return "[" + "#" * filled + "-" * (width - filled) + "]"


def _fmt_duration(seconds: float) -> str:
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:d}:{s:02d}"


def _estimate_eta(elapsed_s: float, done: int, total: int) -> str:
    if done <= 0 or done >= total:
        return "-"
    per_case = elapsed_s / done
    remaining = (total - done) * per_case
    return _fmt_duration(remaining)


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(130)
