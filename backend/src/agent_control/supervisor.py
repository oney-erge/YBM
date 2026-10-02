"""Cross-platform process supervisor for `ybm start`/`stop`/`status`/`logs`.

`scripts/ybm.ps1` is the long-established, Windows-tested lifecycle interface
(AGENTS.md: "the public lifecycle interface... keep service scripts behind
it") and is left untouched by this module. This is a parallel, OS-independent
supervisor for the `ybm` console-script entrypoint (`[project.scripts]` in
pyproject.toml) - the path a `pip install`/`uv tool install` user on Linux or
macOS actually has, where PowerShell's `Win32_Process`/CIM-based child
tracking in scripts/lib/common.ps1 does not exist. Every service here is
already a thin `python -m ...` invocation - see scripts/services/*.ps1 -
so spawning it directly needs no shell wrapper and,
unlike the PowerShell version, never needs to walk a wrapper process's
children to find the real PID.

State lives under `.agent_control/run/<name>.json` (pid, command, log path,
started_at) and `.agent_control/logs/<name>.log` (combined stdout+stderr) -
deliberately different filenames from ybm.ps1's `<name>.pid` /
`<name>.status.json` / `<name>.out.log` / `<name>.err.log` so the two
supervisors never read or clobber each other's state if a user tries both.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

from agent_control.config import backend_base_url, load_settings, local_backend_url
from agent_control.config_sync import read_env_value


def _repo_root() -> Path:
    # backend/src/agent_control/supervisor.py -> repo root is 3 parents up.
    return Path(__file__).resolve().parents[3]


def _run_dir() -> Path:
    path = _repo_root() / ".agent_control" / "run"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _log_dir() -> Path:
    path = _repo_root() / ".agent_control" / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


@dataclass
class ServiceSpec:
    name: str
    args: list[str]
    cwd: Path | None = None
    ready_url: str | None = None
    ready_timeout_seconds: float = 30.0
    required: bool = True
    env: dict[str, str] | None = None


def _backend_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(_repo_root() / "backend" / "src")
    return env


def _localdeploy_spec() -> ServiceSpec | None:
    root = read_env_value("YBM_LOCALDEPLOY_ROOT")
    if not root:
        return None
    root_path = Path(root)
    if not root_path.exists():
        print(f"WARNING: YBM_LOCALDEPLOY_ROOT={root} does not exist - skipping localdeploy")
        return None
    python = root_path / ".venv" / ("Scripts" if os.name == "nt" else "bin") / (
        "python.exe" if os.name == "nt" else "python"
    )
    python_exe = str(python) if python.exists() else sys.executable
    return ServiceSpec(
        name="localdeploy",
        args=[python_exe, "api_server.py"],
        cwd=root_path,
        ready_url="http://127.0.0.1:8000/health",
        ready_timeout_seconds=30,
        required=False,
    )


def _telegram_configured() -> bool:
    """Whether starting the Telegram poller can possibly succeed.

    `poll-telegram` raises `Telegram token not found in TELEGRAM_BOT_TOKEN` and
    exits when no token is set, which is the normal state of a fresh install -
    the web chat needs no setup and Telegram is opt-in. Starting it anyway gave
    every first run a required service that dies immediately: on the host that
    is a crash-loop and an alarming [FAIL], and in the container it takes the
    whole stack down with it, because a foreground supervisor treats a dead
    required service as fatal.

    Same treatment WhatsApp already gets, and for the same reason. Fails closed
    on an unreadable config so a config problem surfaces in `ybm doctor`.
    """
    try:
        settings = load_settings()
        if not settings.channels.telegram.enabled:
            return False
        return bool(read_env_value(settings.channels.telegram.token_env))
    except Exception:
        return False


def _whatsapp_enabled() -> bool:
    # Fail closed on a broken/unreadable config - `ybm doctor` (which runs
    # before `ybm start` unless -SkipDoctor) is where a config problem
    # should surface, not a silently-skipped WhatsApp service.
    try:
        return load_settings().channels.whatsapp.enabled
    except Exception:
        return False


def _backend_ready_url() -> str:
    """The backend's /health on the port it is configured to listen on.

    Hard-coding 8765 made `ybm start` wait 45 seconds for a backend that was
    healthy on any other `server.port`, then report a required service as failed.
    A config that cannot be read falls back to the default, so a broken file
    surfaces in `ybm doctor` rather than here.
    """
    try:
        return f"{local_backend_url(load_settings())}/health"
    except Exception:
        return "http://127.0.0.1:8765/health"


def build_service_specs(
    *, no_telegram: bool = False, no_whatsapp: bool = False, no_worker: bool = False,
    no_scheduler: bool = False, no_localdeploy: bool = False,
) -> list[ServiceSpec]:
    env = _backend_env()
    specs: list[ServiceSpec] = []

    if not no_localdeploy:
        localdeploy = _localdeploy_spec()
        if localdeploy is not None:
            specs.append(localdeploy)

    specs.append(ServiceSpec(
        name="backend",
        args=[sys.executable, "-m", "agent_control.serve_backend"],
        cwd=_repo_root(), env=env,
        ready_url=_backend_ready_url(), ready_timeout_seconds=45, required=True,
    ))
    if not no_telegram and _telegram_configured():
        specs.append(ServiceSpec(
            name="telegram_polling",
            args=[sys.executable, "-m", "agent_control.cli", "poll-telegram"],
            cwd=_repo_root(), env=env, required=True,
        ))
    if not no_whatsapp and _whatsapp_enabled():
        # required=False (unlike telegram_polling): even when enabled, a
        # WhatsApp hiccup (bridge crash, not yet linked) must not block the
        # rest of the stack the way a required service would.
        #
        # Gated on the config flag itself, unlike telegram_polling - most
        # installs never touch WhatsApp, and poll-whatsapp refuses to start
        # at all when disabled (cli.py's poll_whatsapp), so attempting it
        # anyway would just crash-loop 4 times (~20s wasted) every single
        # `ybm start`/`ybm run`, for every user, forever, and leave a
        # permanently alarming "failed" status line for a feature nobody
        # asked to run.
        specs.append(ServiceSpec(
            name="whatsapp",
            args=[sys.executable, "-m", "agent_control.cli", "poll-whatsapp"],
            cwd=_repo_root(), env=env, required=False,
        ))
    if not no_worker:
        specs.append(ServiceSpec(
            name="worker",
            args=[sys.executable, "-m", "agent_control.cli", "run-worker"],
            cwd=_repo_root(), env=env, required=True,
        ))
        specs.append(ServiceSpec(
            name="coding_session_watcher",
            args=[sys.executable, "-m", "agent_control.cli", "run-coding-session-watcher"],
            cwd=_repo_root(), env=env, required=True,
        ))
    if not no_scheduler:
        specs.append(ServiceSpec(
            name="scheduler",
            args=[sys.executable, "-m", "agent_control.cli", "run-scheduler"],
            cwd=_repo_root(), env=env, required=True,
        ))
    return specs


_STATE_SUFFIX = ".ybmpy.json"  # distinct from ybm.ps1's own <name>.pid / <name>.status.json


def _state_path(name: str) -> Path:
    return _run_dir() / f"{name}{_STATE_SUFFIX}"


def _log_path(name: str) -> Path:
    return _log_dir() / f"{name}.ybmpy.log"


def _write_state(name: str, pid: int, command: list[str]) -> None:
    _state_path(name).write_text(
        json.dumps({
            "pid": pid,
            "command": command,
            "log_path": str(_log_path(name)),
            "started_at": datetime.now(timezone.utc).isoformat(),
        }),
        encoding="utf-8",
    )


def _read_state(name: str) -> dict | None:
    path = _state_path(name)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _pid_alive(pid: int) -> bool:
    if os.name == "nt":
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, check=False,
        )
        return str(pid) in result.stdout
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    return True


def _spawn(spec: ServiceSpec) -> int:
    log_path = _log_path(spec.name)
    log_file = open(log_path, "ab")  # noqa: SIM115 - handed to the detached child, closed with the parent fd via inheritance
    popen_kwargs: dict = {
        "stdout": log_file, "stderr": subprocess.STDOUT, "stdin": subprocess.DEVNULL,
        "cwd": str(spec.cwd) if spec.cwd else None, "env": spec.env,
    }
    if os.name == "nt":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | getattr(
            subprocess, "DETACHED_PROCESS", 0x00000008
        )
    else:
        popen_kwargs["start_new_session"] = True
    process = subprocess.Popen(spec.args, **popen_kwargs)  # noqa: S603 - fixed, internally-built argv, not user input
    log_file.close()
    return process.pid


def _wait_ready(url: str, timeout_seconds: float) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            with urlopen(url, timeout=2.0) as resp:  # noqa: S310 - local health check only
                if 200 <= resp.status < 400:
                    return True
        except (URLError, OSError, ValueError):
            pass
        time.sleep(0.5)
    return False


def start_all(*, open_browser: bool = False, **flags) -> int:
    specs = build_service_specs(**flags)
    print(f"Starting {len(specs)} service(s)...")
    hard_failure = False
    for spec in specs:
        existing = _read_state(spec.name)
        if existing and _pid_alive(existing["pid"]):
            print(f"[OK]   {spec.name:<24} already running (pid {existing['pid']})")
            continue
        try:
            pid = _spawn(spec)
        except FileNotFoundError as exc:
            print(f"[FAIL] {spec.name:<24} failed to start: {exc}")
            if spec.required:
                hard_failure = True
            continue
        _write_state(spec.name, pid, spec.args)
        if spec.ready_url:
            ready = _wait_ready(spec.ready_url, spec.ready_timeout_seconds)
            status = "ready" if ready else "started (not ready yet)"
            symbol = "[OK]  " if ready else "[WARN]"
            print(f"{symbol} {spec.name:<24} {status} (pid {pid}) - log: {_log_path(spec.name)}")
            if not ready and spec.required:
                hard_failure = True
        else:
            time.sleep(1.0)
            alive = _pid_alive(pid)
            symbol = "[OK]  " if alive else "[FAIL]"
            print(f"{symbol} {spec.name:<24} {'running' if alive else 'exited immediately - check log'} "
                  f"(pid {pid}) - log: {_log_path(spec.name)}")
            if not alive and spec.required:
                hard_failure = True
    print()
    if hard_failure:
        print("One or more required services failed to start. Check the logs above, then `ybm status` / `ybm logs <name>`.")
        return 1
    from agent_control.bootstrap import admin_console_url

    settings = load_settings()
    admin_url = admin_console_url(settings, with_token=False)
    print(f"Admin UI:   {admin_url}")
    print(f"Backend:    {backend_base_url(settings).rstrip('/')}/health")
    print("Stop with:  ybm stop")
    if open_browser:
        # Carries AGENT_ADMIN_TOKEN (if set) as a one-time ?token= URL param
        # so the browser's very first request is already authenticated -
        # without this, a fresh install (which always generates a real
        # token, see bootstrap.run_setup) would 401 on every page with no
        # way to recover, since nothing else in the UI ever collects one.
        # lib/api.ts strips it from the URL/history on load. Only requested
        # explicitly (`ybm start --open`, used by the installers) - a bare
        # `ybm start` during normal development never pops a browser tab.
        import webbrowser

        target = admin_console_url(settings)
        try:
            if not webbrowser.open(target):
                raise webbrowser.Error("no browser handler available")
        except webbrowser.Error:
            # Headless/SSH-only environment - real and expected there, not a
            # failure of `start` itself (every service above already started).
            print(f"Could not open a browser automatically. Open {target} manually.")
    return 0


def run_foreground(*, poll_seconds: float = 5.0, open_browser: bool = False, **flags) -> int:
    """Start everything, then stay in the foreground until it stops.

    ``start_all`` spawns detached children and returns, which is right for an
    interactive `ybm start` and wrong for anything that supervises this process
    itself: a container whose PID 1 exits is a container that stopped, and
    systemd Type=simple treats the same exit as the service dying.

    Blocks until a required service exits or a signal arrives, then shuts the
    rest down so nothing is orphaned. Exit code is non-zero when a service died
    on its own, so a restart policy can act on it.
    """
    # open_browser is consumed here rather than left in **flags: start_all
    # takes it, build_service_specs does not, and forwarding the whole bag to
    # both raised TypeError the first time this ran in a container.
    started = start_all(open_browser=open_browser, **flags)
    if started != 0:
        stop_all()
        return started

    stopping = False

    def _request_stop(signum, _frame):  # noqa: ANN001 - signal handler signature
        nonlocal stopping
        stopping = True
        print(f"\nReceived signal {signum} - stopping services...")

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _request_stop)
        except (ValueError, OSError):
            # No signal handling on this thread/platform; Ctrl+C still raises.
            pass

    required_names = {spec.name for spec in build_service_specs(**flags) if spec.required}

    print("Supervising services in the foreground. Send SIGTERM (or Ctrl+C) to stop.")
    exit_code = 0
    reported: set[str] = set()
    try:
        while not stopping:
            dead = _first_dead_service(ignore=reported)
            if dead is not None:
                # Only a *required* service takes the stack down. An optional
                # one exiting is worth saying out loud and worth nothing more -
                # taking everything with it turns "WhatsApp is not linked yet"
                # into "the container stopped".
                if dead in required_names:
                    print(f"[FAIL] {dead} exited - shutting the rest down. Log: {_log_path(dead)}")
                    exit_code = 1
                    break
                print(f"[WARN] {dead} exited; continuing without it. Log: {_log_path(dead)}")
                reported.add(dead)
                continue
            time.sleep(poll_seconds)
    except KeyboardInterrupt:
        pass

    stop_all()
    return exit_code


def _first_dead_service(*, ignore: set[str] | None = None) -> str | None:
    ignore = ignore or set()
    for state_file in sorted(_run_dir().glob(f"*{_STATE_SUFFIX}")):
        name = state_file.name.removesuffix(_STATE_SUFFIX)
        if name in ignore:
            continue
        state = _read_state(name)
        if state and not _pid_alive(int(state["pid"])):
            return name
    return None


def stop_all() -> int:
    stopped_any = False
    for state_file in sorted(_run_dir().glob(f"*{_STATE_SUFFIX}")):
        name = state_file.name.removesuffix(_STATE_SUFFIX)
        state = _read_state(name)
        if not state:
            continue
        pid = state["pid"]
        if not _pid_alive(pid):
            state_file.unlink(missing_ok=True)
            continue
        stopped_any = True
        print(f"stopping {name} (pid {pid})...")
        _terminate(pid)
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and _pid_alive(pid):
            time.sleep(0.3)
        if _pid_alive(pid):
            print(f"  {name} did not exit in time, killing")
            _kill(pid)
        state_file.unlink(missing_ok=True)
    print("stopped." if stopped_any else "nothing running.")
    return 0


def _terminate(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T"], capture_output=True, check=False)
    else:
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass


def _kill(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, check=False)
    else:
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def status_all() -> int:
    state_files = sorted(_run_dir().glob(f"*{_STATE_SUFFIX}"))
    if not state_files:
        print("nothing running. Start with `ybm start`.")
        return 0
    width = max(len(f.name.removesuffix(_STATE_SUFFIX)) for f in state_files)
    any_running = False
    for state_file in state_files:
        name = state_file.name.removesuffix(_STATE_SUFFIX)
        state = _read_state(name)
        if not state:
            continue
        alive = _pid_alive(state["pid"])
        any_running = any_running or alive
        symbol = "[OK]  " if alive else "[DEAD]"
        print(f"{symbol} {name.ljust(width)}  pid {state['pid']}  {state.get('started_at', '')}")
    return 0 if any_running else 1


def read_log_tail(name: str, lines: int = 200) -> tuple[str, list[str]] | None:
    """The admin Diagnostics page's "view log" action (docs/archive/UI_UX_AUDIT.md
    Phase 9) - the same file `ybm logs <service>` tails, returned as text
    instead of printed. Returns None if the log doesn't exist yet.
    """
    log_path = _log_path(name)
    if not log_path.exists():
        return None
    with open(log_path, "rb") as fh:
        data = fh.read()
    text = data.decode("utf-8", errors="replace")
    return str(log_path), text.splitlines()[-lines:]


def tail_log(name: str, *, follow: bool = False, lines: int = 60) -> int:
    log_path = _log_path(name)
    if not log_path.exists():
        print(f"no log file for '{name}' yet: {log_path}")
        return 1
    _print_tail(log_path, lines)
    if not follow:
        return 0
    print(f"-- following {log_path} (Ctrl+C to stop) --")
    try:
        with open(log_path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            while True:
                chunk = fh.readline()
                if chunk:
                    sys.stdout.write(chunk.decode("utf-8", errors="replace"))
                else:
                    time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    return 0


def _print_tail(path: Path, lines: int) -> None:
    with open(path, "rb") as fh:
        data = fh.read()
    text = data.decode("utf-8", errors="replace")
    for line in text.splitlines()[-lines:]:
        print(line)
