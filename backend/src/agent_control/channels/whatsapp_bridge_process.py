"""Spawns and owns the whatsapp-bridge Node sidecar as a child process
(docs/archive/UI_UX_AUDIT.md Phase 16) - so `ybm.ps1`'s service list doesn't need to
learn a new, non-Python process type. From its perspective this is still
just one more Python entry point (`poll-whatsapp`, cli.py); Node is an
internal implementation detail that entry point happens to spawn.

Paths are relative to the current working directory, the same convention
`config.py`'s `StorageConfig.database_url`/`artifact_dir` already use -
every service script (`scripts/services/run_*.ps1`) `Set-Location`s to the
repo root before invoking Python, so this holds for real runs the same way
it already does for `.agent_control/agent_control.db`.

Deliberately does NOT redirect the child's stdout/stderr to its own log
file the way `tools/coding_agent.py`'s sessions do (those are long-lived,
detached background runs that need a durable log regardless of what
spawned them). This sidecar's whole lifetime is scoped to one `poll-whatsapp`
process, which is itself already running under `run_supervised.ps1` with its
own captured log - inheriting stdio means the Node process's own console
output (notably the QR code on first link) lands in that exact same
`ybm logs whatsapp` output a user already knows to check, not a second file.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

import httpx

from agent_control.config import WhatsAppConfig


if TYPE_CHECKING:
    from agent_control.channels.whatsapp import WhatsAppBridgeClient


logger = logging.getLogger(__name__)

BRIDGE_DIR = Path("whatsapp-bridge")
AUTH_DIR = Path(".agent_control/whatsapp_auth")
# Where the running bridge's base_url/secret are shared with OTHER local
# processes (notably run-worker, a separate process from poll-whatsapp,
# which needs them to send task-completion notifications) - the same
# ".agent_control/run/<name>.json" convention run_supervised.ps1's own
# status.json files already use for cross-process ephemeral state. Never
# holds anything longer-lived than the current bridge's own lifetime -
# stop() removes it.
STATE_PATH = Path(".agent_control/run/whatsapp_bridge.json")


class BridgeProcessHandle(Protocol):
    pid: int
    # None while the process is still running, an exit code once it isn't -
    # `WhatsAppBridgeProcess.is_alive()`'s only signal that the node child
    # died without anyone calling stop() (crashed, port taken, killed
    # externally), so the poll loop can notice within one tick instead of
    # hammering a dead bridge with HTTP requests forever.
    returncode: int | None

    def terminate(self) -> None: ...

    async def wait(self) -> int: ...


class BridgeProcessSpawner(Protocol):
    async def spawn(self, command: list[str], *, cwd: str, env: dict[str, str]) -> BridgeProcessHandle: ...


class AsyncBridgeProcessSpawner:
    async def spawn(self, command: list[str], *, cwd: str, env: dict[str, str]) -> BridgeProcessHandle:
        process = await asyncio.create_subprocess_exec(*command, cwd=cwd, env=env)
        return _AsyncioBridgeProcessHandle(process)


class _AsyncioBridgeProcessHandle:
    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self._process = process
        self.pid = process.pid

    @property
    def returncode(self) -> int | None:
        return self._process.returncode

    def terminate(self) -> None:
        try:
            self._process.terminate()
        except ProcessLookupError:
            pass

    async def wait(self) -> int:
        return await self._process.wait()


def install_bridge_dependencies() -> tuple[bool, str]:
    """Install the sidecar's Node dependencies from its lockfile, quietly.

    The sidecar's ~90 packages are only needed once WhatsApp is switched on, so
    first-run setup no longer installs them (it used to, and printed an
    ``npm audit`` warning at people who never use WhatsApp). Whoever turns the
    channel on gets them installed here the first time the bridge starts.
    Returns ``(ok, detail)``; never raises, so the caller decides how to report.
    """
    if shutil.which("npm") is None:
        return False, "npm was not found on PATH - install Node.js 22.22+ (https://nodejs.org)"
    use_lockfile = (BRIDGE_DIR / "package-lock.json").exists()
    command = ["npm", "ci" if use_lockfile else "install", "--omit=dev", "--no-audit", "--no-fund", "--loglevel=error"]
    try:
        result = subprocess.run(
            command, cwd=BRIDGE_DIR, shell=(sys.platform == "win32"),
            capture_output=True, text=True, check=False,
        )
    except OSError as exc:
        return False, str(exc)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        return False, detail[-400:] or f"npm exited with {result.returncode}"
    return True, "installed"


def find_node_binary(node_path: str | None) -> str | None:
    if node_path:
        return node_path if Path(node_path).exists() else None
    return shutil.which("node")


class WhatsAppBridgeProcess:
    """Owns exactly one whatsapp-bridge child for the lifetime of a
    `poll-whatsapp` process. `secret` is generated fresh per instance and
    handed to the child via env var; `start()` also writes it to
    `STATE_PATH` (world-readable only by whoever can read
    `.agent_control/`, never committed - see STATE_PATH's own docstring)
    so `run-worker`, a separate process, can reach this bridge to send
    notifications. `stop()` removes that file again.
    """

    def __init__(self, config: WhatsAppConfig, *, spawner: BridgeProcessSpawner | None = None) -> None:
        self.config = config
        self.spawner = spawner or AsyncBridgeProcessSpawner()
        self.secret = secrets.token_urlsafe(32)
        self._handle: BridgeProcessHandle | None = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.config.bridge_port}"

    @property
    def headers(self) -> dict[str, str]:
        return {"X-Bridge-Secret": self.secret}

    @property
    def returncode(self) -> int | None:
        return self._handle.returncode if self._handle is not None else None

    def is_alive(self) -> bool:
        """False once the node child has exited on its own (crash, killed
        externally, port taken) - lets the poll loop in cli.py's
        poll_whatsapp() notice a dead bridge within one tick instead of
        calling a closed port every 2s forever."""
        return self._handle is not None and self._handle.returncode is None

    async def start(self, *, ready_timeout_seconds: float = 60.0) -> None:
        node_binary = find_node_binary(self.config.node_path)
        if node_binary is None:
            raise RuntimeError(
                "node was not found on PATH (or config.node_path) - install Node.js to use "
                "the WhatsApp channel, or leave channels.whatsapp.enabled: false."
            )
        if not (BRIDGE_DIR / "node_modules").is_dir():
            installed, detail = await asyncio.to_thread(install_bridge_dependencies)
            if not installed:
                raise RuntimeError(
                    f"{BRIDGE_DIR}/node_modules is missing and could not be installed automatically "
                    f"({detail}) - run `npm install` in {BRIDGE_DIR}/ before enabling the WhatsApp channel."
                )
            logger.info("installed whatsapp-bridge dependencies on first start")
        AUTH_DIR.mkdir(parents=True, exist_ok=True)
        env = {
            **os.environ,
            "WHATSAPP_BRIDGE_PORT": str(self.config.bridge_port),
            "WHATSAPP_BRIDGE_SECRET": self.secret,
            "WHATSAPP_AUTH_DIR": str(AUTH_DIR),
        }
        self._handle = await self.spawner.spawn(
            [node_binary, "src/index.js"], cwd=str(BRIDGE_DIR), env=env,
        )
        await self._wait_until_healthy(ready_timeout_seconds)
        self._write_state()

    async def _wait_until_healthy(self, timeout_seconds: float) -> None:
        deadline = asyncio.get_event_loop().time() + timeout_seconds
        async with httpx.AsyncClient(timeout=5) as client:
            while asyncio.get_event_loop().time() < deadline:
                try:
                    response = await client.get(f"{self.base_url}/health", headers=self.headers)
                    if response.status_code == 200:
                        return
                except httpx.HTTPError:
                    pass
                # A node that dies on startup (bad import, port taken,
                # missing dependency) is never coming back - waiting out the
                # full timeout to say "did not become healthy" buries the
                # real error, which node already printed, under a minute of
                # silence.
                if not self.is_alive():
                    raise RuntimeError(
                        f"whatsapp-bridge exited immediately (code {self.returncode}) - "
                        "its error output is in `ybm logs whatsapp`."
                    )
                await asyncio.sleep(1)
        raise RuntimeError(
            f"whatsapp-bridge did not become healthy within {timeout_seconds:.0f}s - "
            "check its output via `ybm logs whatsapp`."
        )

    def stop(self) -> None:
        if self._handle is not None:
            self._handle.terminate()
        try:
            STATE_PATH.unlink(missing_ok=True)
        except OSError:
            logger.warning("failed to remove whatsapp bridge state file", exc_info=True)

    async def wait_stopped(self) -> None:
        if self._handle is not None:
            await self._handle.wait()

    def _write_state(self) -> None:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps({"base_url": self.base_url, "secret": self.secret}), encoding="utf-8")


def load_whatsapp_bridge_client() -> "WhatsAppBridgeClient | None":
    """Reads the running bridge's base_url/secret written by
    `WhatsAppBridgeProcess.start()` - re-read fresh on every call (not
    cached) so a caller in a different, longer-lived process (run-worker)
    always talks to whichever bridge instance is actually running, across
    that bridge's own restarts."""
    from agent_control.channels.whatsapp import WhatsAppBridgeClient

    if not STATE_PATH.exists():
        return None
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return WhatsAppBridgeClient(str(state["base_url"]), str(state["secret"]))
    except (OSError, ValueError, KeyError):
        logger.warning("failed to read whatsapp bridge state file", exc_info=True)
        return None
