"""Preflight (`doctor`) and first-run (`setup`) checks for the YBM stack.

These exist so a missing dependency, missing config, or unreachable local
service produces one readable line instead of a stack trace three layers
deep in a supervised background process (see docs/HISTORY.md P0).
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import secrets as secrets_module
import shutil
import socket
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.parse import quote
from urllib.request import urlopen

from agent_control.config import AppSettings, backend_base_url, is_loopback_host, load_settings
from agent_control.config_sync import ConfigManager, read_env_value
from agent_control.schemas import Capability
from agent_control.storage.database import Database
from agent_control.storage.secrets import SecretVault


REQUIRED_MODULES = [
    "fastapi", "cryptography", "httpx", "json_repair", "mcp", "pandas",
    "pydantic", "pydantic_settings", "PIL", "pypdf", "yaml",
    "structlog", "uvicorn", "websocket",
]
DESKTOP_MODULES = ["mss", "pyautogui", "pygetwindow", "pywinauto"]
STATUS_SYMBOL = {"ok": "[OK]  ", "warn": "[WARN]", "fail": "[FAIL]"}
MIN_NODE_VERSION = (22, 22, 0)


@dataclass
class Check:
    name: str
    status: str  # "ok" | "warn" | "fail"
    detail: str = ""


def admin_console_url(settings: AppSettings | None = None, *, with_token: bool = True) -> str:
    """The address to open the console at, already signed in when a token exists.

    The admin token is generated for you and kept in ``.env``; nobody should
    have to find it and paste it. Every way of opening the console (the
    launchers, the tray icon, ``ybm admin-url``) goes through here so they all
    hand the browser the same one-time ``?token=`` link, which the console
    exchanges for a long-lived session cookie and strips from the address bar.
    """
    settings = settings or load_settings()
    url = backend_base_url(settings).rstrip("/") + "/admin"
    token = read_env_value(settings.server.admin_token_env) if with_token else None
    return f"{url}?token={quote(token, safe='')}" if token else url


def describe_active_model(settings: AppSettings, *, configured: bool) -> dict[str, Any]:
    """What the console should say the model is, with nothing secret in it.

    First run now picks a model without asking, so the console owes the person a
    plain statement of what it picked: the model, who provides it, and - when a
    key from their environment is being used - the variable's *name* (never its
    value). ``configured`` is whether that model is actually usable, computed
    once by the caller.
    """
    from agent_control.llm import catalog

    profile = settings.llm.profiles.get(settings.llm.default_profile)
    if profile is None:
        return {"configured": False, "model": None, "provider": None, "local": False, "key_env": None}
    base_url = (profile.base_url or "").rstrip("/")
    local = any(host in base_url for host in ("127.0.0.1", "localhost", "host.docker.internal"))
    spec = next(
        (
            candidate for candidate in catalog.PROVIDERS
            if candidate.key != "custom" and candidate.kind == profile.provider
            and (candidate.base_url or "").rstrip("/") == base_url
        ),
        None,
    )
    if spec is not None:
        provider = spec.label
    else:
        provider = "A model on this computer" if local else "Custom endpoint"
    key_env = profile.api_key_env if profile.api_key_env and read_env_value(profile.api_key_env) else None
    return {
        "configured": configured,
        "model": profile.model,
        "provider": provider,
        "local": local,
        "key_env": key_env,
    }


def _check_python() -> Check:
    v = sys.version_info
    version = f"{v.major}.{v.minor}.{v.micro}"
    if (v.major, v.minor) >= (3, 12):
        return Check("Python version", "ok", version)
    return Check("Python version", "fail", f"{version} - YBM requires >=3.12")


def _check_venv() -> Check:
    exe = Path(sys.executable)
    in_venv = sys.prefix != sys.base_prefix
    normalized = str(exe).replace("\\", "/")
    if in_venv and "backend/.venv" in normalized:
        return Check("Virtual environment", "ok", str(exe))
    if in_venv:
        return Check("Virtual environment", "warn", f"active venv is not backend/.venv: {exe}")
    return Check("Virtual environment", "warn", f"not running inside backend/.venv ({exe}); run `ybm setup`")


def _check_modules() -> list[Check]:
    return [
        Check(f"module: {mod}", "ok" if importlib.util.find_spec(mod) else "fail",
              "" if importlib.util.find_spec(mod) else "not installed - run `ybm setup`")
        for mod in REQUIRED_MODULES
    ]


def _desktop_capability_requested(settings: AppSettings) -> bool:
    for capability in (Capability.DESKTOP_SCREENSHOT, Capability.DESKTOP_CONTROL):
        policy = settings.capabilities.get(capability)
        if policy is not None and policy.enabled:
            return True
    return bool(settings.adapters.computer_use.enabled)


def is_headless_runtime() -> bool:
    """Whether this process has no desktop to control.

    Set explicitly by the container image (YBM_HEADLESS=1) and inferred from
    the usual container markers otherwise. Desktop control, screenshots and the
    VS Code bridge cannot work here - there is no session to attach to - so
    doctor should say "unavailable" rather than reporting a missing module as a
    failure the operator could fix by installing something.
    """
    if os.environ.get("YBM_HEADLESS", "").strip().lower() in {"1", "true", "yes"}:
        return True
    if Path("/.dockerenv").exists():
        return True
    try:
        return "docker" in Path("/proc/1/cgroup").read_text(encoding="utf-8")
    except OSError:
        return False


def _check_voice_modules(settings: AppSettings) -> Check:
    """Speech-to-text needs an optional extra that nothing checked for.

    Enabling STT without installing it failed at first use - a user sends a
    voice note and gets an error instead of an answer. Startup is a better
    place to find out than the first voice message.
    """
    stt = settings.adapters.stt
    if not stt.enabled:
        return Check("Voice transcription", "ok", "off - voice messages are answered with a note saying so")
    if stt.provider != "faster_whisper":
        return Check("Voice transcription", "ok", f"on, using {stt.provider}")
    if importlib.util.find_spec("faster_whisper") is None:
        return Check(
            "Voice transcription",
            "fail",
            "enabled but faster-whisper is not installed - run `uv sync --extra voice`, or turn voice off",
        )
    return Check("Voice transcription", "ok", f"on, faster-whisper model '{stt.model}'")


def _check_desktop_modules(settings: AppSettings) -> list[Check]:
    if not _desktop_capability_requested(settings):
        return [Check("Desktop control modules", "ok", "not requested by config - skipped")]
    if is_headless_runtime():
        return [Check(
            "Desktop control modules", "warn",
            "unavailable in a headless runtime (container) - desktop control, screenshots and "
            "the VS Code bridge need a real session; every other capability is unaffected",
        )]
    missing = [mod for mod in DESKTOP_MODULES if importlib.util.find_spec(mod) is None]
    if not missing:
        return [Check("Desktop control modules", "ok", "installed")]
    return [Check(
        "Desktop control modules", "fail",
        f"desktop capability enabled but missing: {', '.join(missing)} - run `ybm setup --desktop`",
    )]


def _load_settings_checked() -> tuple[Check, AppSettings | None]:
    path = Path("config/config.yaml")
    if not path.exists():
        # Same reasoning as is_headless_runtime's docstring: advice an operator
        # cannot act on is worse than none. "Run `ybm setup`" is right on a host
        # checkout, where setup is what creates this file, and wrong inside a
        # container, where the first-run wizard writes it and there is no setup
        # step to run. Still a failure either way - nothing can answer a message
        # until a model is configured.
        hint = (
            "missing - finish the first-run wizard at /admin"
            if is_headless_runtime()
            else "missing - run `ybm setup`"
        )
        return Check("config/config.yaml", "fail", hint), None
    try:
        settings = load_settings()
    except Exception as exc:  # noqa: BLE001 - reporting, not handling
        return Check("config/config.yaml", "fail", f"invalid: {exc}"), None
    return Check("config/config.yaml", "ok", "loaded"), settings


def _check_db(settings: AppSettings) -> Check:
    try:
        database = Database(settings.storage.database_url)
        database.initialize()
        return Check("Database", "ok", settings.storage.database_url)
    except Exception as exc:  # noqa: BLE001 - reporting, not handling
        return Check("Database", "fail", str(exc))


def _port_listening(port: int, host: str = "127.0.0.1", timeout: float = 0.5) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        try:
            sock.connect((host, port))
            return True
        except OSError:
            return False


def _localdeploy_expected(settings: AppSettings | None) -> bool:
    """Whether this install is meant to be running LocalDeploy at all.

    Most installs are not - the default model is detected from what is on the
    machine - so its port is not worth a line in every doctor report.
    """
    if read_env_value("YBM_LOCALDEPLOY_ROOT"):
        return True
    if settings is None:
        return False
    profile = settings.llm.profiles.get(settings.llm.default_profile)
    base_url = (profile.base_url or "") if profile else ""
    return "127.0.0.1:8000" in base_url or "localhost:8000" in base_url


def _check_ports(settings: AppSettings | None = None) -> list[Check]:
    # A free port is the normal state before `ybm start`, not a warning: this
    # used to print "[WARN] ... free - not running yet" on every first run.
    ports = [("Backend", 8765)]
    if _localdeploy_expected(settings):
        ports.insert(0, ("LocalDeploy", 8000))
    return [
        Check(f"Port {port} ({name})", "ok", "listening" if _port_listening(port) else "free - not running yet")
        for name, port in ports
    ]


def _http_ok(url: str, timeout: float = 6.0) -> bool:
    """Liveness probe for the doctor checks.

    Was 2.0s, which is under how long a real reply takes: LocalDeploy's
    /health enumerates Ollama's installed models before responding and
    measured ~2.06s on this machine. Doctor therefore reported "LocalDeploy
    not reachable ... fallback profile 'openai_saved' will be used" in the
    same run where it reported port 8000 listening - telling the user their
    local model was down and a paid API would be billed instead, when
    neither was true.
    """
    try:
        with urlopen(url, timeout=timeout) as resp:  # noqa: S310 - local health check only
            return 200 <= resp.status < 300
    except (URLError, OSError, ValueError):
        return False


def check_localdeploy(settings: AppSettings) -> Check:
    profile = settings.llm.profiles.get(settings.llm.default_profile)
    base_url = profile.base_url if profile else None
    # Same gap as check_llm_configured: from inside a container the local
    # runtime is host.docker.internal, and omitting it made doctor report a
    # working LocalDeploy as "not a local profile".
    if not base_url or not any(
        host in base_url for host in ("127.0.0.1", "localhost", "host.docker.internal")
    ):
        return Check("LocalDeploy", "ok", "default LLM profile is not local - skipped")
    health_url = base_url.rsplit("/v1", 1)[0].rstrip("/") + "/health"
    if _http_ok(health_url):
        return Check("LocalDeploy", "ok", f"reachable at {health_url}")
    root = read_env_value("YBM_LOCALDEPLOY_ROOT")
    detail = "not reachable" + (f" (YBM_LOCALDEPLOY_ROOT={root})" if root else " (YBM_LOCALDEPLOY_ROOT not set)")
    if settings.llm.fallback_profile:
        detail += f"; fallback profile '{settings.llm.fallback_profile}' will be used"
    else:
        detail += "; no fallback_profile configured, LLM calls will fail"
    return Check("LocalDeploy", "warn", detail)


OLLAMA_TAGS_URL = "http://127.0.0.1:11434/api/tags"


def check_llm_configured(settings: AppSettings) -> bool:
    """Best-effort "is the current default LLM profile actually usable"
    check - used to decide whether the first-run wizard still has something
    to do (admin.py's /api/bootstrap onboarding_complete).

    Deliberately broader than check_localdeploy(), which only validates a
    LocalDeploy-shaped local /health endpoint and reports "ok" for *any*
    non-local profile without checking anything - accurate for its own
    doctor-check purpose (LocalDeploy specifically) but wrong as a general
    "will this respond" signal: it would call a bare Ollama setup or an
    unconfigured cloud profile "reachable" with nothing actually verified.
    """
    profile = settings.llm.profiles.get(settings.llm.default_profile)
    if profile is None:
        return False
    base_url = (profile.base_url or "").rstrip("/")
    if base_url.startswith("http://127.0.0.1:11434") or base_url.startswith("http://localhost:11434"):
        return bool(_http_json(OLLAMA_TAGS_URL, timeout=2.0))
    # host.docker.internal is a local runtime seen from inside a container.
    # Leaving it out meant a working containerised LocalDeploy fell through to
    # the API-key branch below, returned False, and the console declared "no
    # model configured" while the model was answering questions.
    if any(host in base_url for host in ("127.0.0.1", "localhost", "host.docker.internal")):
        return check_localdeploy(settings).status == "ok"
    return bool(profile.api_key) or bool(profile.api_key_env and read_env_value(profile.api_key_env))


def _http_json(url: str, timeout: float = 2.0) -> dict | None:
    try:
        with urlopen(url, timeout=timeout) as resp:  # noqa: S310 - local-only probe URLs
            if 200 <= resp.status < 300:
                return json.loads(resp.read().decode("utf-8"))
    except (URLError, OSError, ValueError):
        return None
    return None


def _check_telegram(settings: AppSettings) -> Check:
    if not settings.channels.telegram.enabled:
        return Check("Telegram", "ok", "disabled in config")
    if read_env_value(settings.channels.telegram.token_env):
        return Check("Telegram", "ok", "token present")
    return Check("Telegram", "fail", f"enabled but {settings.channels.telegram.token_env} is not set")


def _node_needed(settings: AppSettings | None) -> bool:
    """Node.js is a contributor tool unless WhatsApp is on or the console is unbuilt.

    Release installs ship the console prebuilt, so telling a person with the
    default setup that "the admin console requires Node.js 22.22+" was a
    warning about a requirement they do not have.
    """
    if settings is not None and settings.channels.whatsapp.enabled:
        return True
    return not (Path("backend/src/agent_control/static/admin") / "index.html").exists()


def _check_node(settings: AppSettings | None = None, *, assume_needed: bool = True) -> Check:
    if not assume_needed and not _node_needed(settings):
        return Check("Node.js", "ok", "not needed - the admin console is prebuilt and WhatsApp is off")
    node = shutil.which("node")
    if node:
        version = _node_version(node)
        if version is not None and version < MIN_NODE_VERSION:
            actual = ".".join(str(part) for part in version)
            return Check(
                "Node.js",
                "warn",
                f"{node} is v{actual}; the admin console requires Node.js 22.22+",
            )
        detail = f"{node} (v{'.'.join(str(part) for part in version)})" if version else node
        return Check("Node.js", "ok", detail)
    return Check(
        "Node.js", "warn",
        "not found on PATH - only needed for the WhatsApp channel and building the admin "
        "console; install Node.js 22.22+ (https://nodejs.org) if you need either",
    )


def _node_version(node: str) -> tuple[int, int, int] | None:
    try:
        result = subprocess.run(
            [node, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = str(getattr(result, "stdout", "")).strip().removeprefix("v")
    parts = value.split(".")
    if result.returncode != 0 or len(parts) < 3 or not all(part.isdigit() for part in parts[:3]):
        return None
    return tuple(int(part) for part in parts[:3])


def _check_admin_console() -> Check:
    """Whether an admin console actually exists to serve.

    The Node check alone was a misleading signal: it reports a tool, while
    what the operator experiences is a console that either loads or doesn't.
    A machine with no Node and no build still passed doctor with "0 failures"
    while /admin served nothing but build instructions - so the one check that
    described the situation understated it as an optional dependency.
    """
    if (Path("backend/src/agent_control/static/admin") / "index.html").exists():
        return Check("Admin console", "ok", "built - served at /admin")
    if shutil.which("npm") is None:
        return Check(
            "Admin console", "warn",
            "NOT BUILT and npm is missing, so /admin has no console at all - install "
            "Node.js 22.22+ then run `ybm ui-build` (the JSON API at /admin/api/* still works)",
        )
    return Check("Admin console", "warn", "not built yet - run `ybm ui-build`")


def _check_whatsapp(settings: AppSettings) -> Check:
    if not settings.channels.whatsapp.enabled:
        return Check("WhatsApp", "ok", "disabled in config")
    from agent_control.channels.whatsapp_bridge_process import AUTH_DIR, BRIDGE_DIR, find_node_binary

    node = find_node_binary(settings.channels.whatsapp.node_path)
    if node is None:
        return Check(
            "WhatsApp", "fail",
            "enabled but node was not found on PATH (or channels.whatsapp.node_path) - "
            "install Node.js 22.22+ (https://nodejs.org)",
        )
    if not (BRIDGE_DIR / "node_modules").is_dir():
        return Check(
            "WhatsApp", "fail",
            f"enabled but {BRIDGE_DIR}/node_modules is missing - run `npm install` in "
            f"{BRIDGE_DIR}/, or re-run `ybm setup`",
        )
    linked = AUTH_DIR.exists() and any(AUTH_DIR.iterdir())
    if not linked:
        return Check(
            "WhatsApp", "warn",
            "node found, not yet linked - start the whatsapp service and scan the QR it "
            "prints (`ybm logs whatsapp -Follow`)",
        )
    return Check("WhatsApp", "ok", f"node found ({node}), linked")


def _check_admin_token(settings: AppSettings) -> Check:
    if read_env_value(settings.server.admin_token_env):
        return Check("Admin token", "ok", "set")
    if is_loopback_host(settings.server.host):
        return Check(
            "Admin token",
            "warn",
            "not set - admin API trusts any loopback caller (cross-origin requests are still "
            "rejected regardless; run `ybm setup` to generate a token for defense in depth)",
        )
    return Check("Admin token", "fail", f"not set and server.host={settings.server.host} is not loopback-only")


def _check_vault(settings: AppSettings) -> Check:
    if read_env_value(settings.secrets.key_env):
        return Check("Secret vault key", "ok", "set")
    return Check("Secret vault key", "warn", f"{settings.secrets.key_env} not set - run `ybm setup`")


def collect_checks() -> list[Check]:
    config_check, settings = _load_settings_checked()
    checks: list[Check] = [
        _check_python(), _check_venv(), *_check_modules(),
        _check_node(settings, assume_needed=False), _check_admin_console(),
    ]
    checks.append(config_check)
    if settings is not None:
        checks.extend(_check_desktop_modules(settings))
        checks.append(_check_voice_modules(settings))
        checks.append(_check_db(settings))
        checks.append(check_localdeploy(settings))
        checks.append(_check_telegram(settings))
        checks.append(_check_whatsapp(settings))
        checks.append(_check_admin_token(settings))
        checks.append(_check_vault(settings))
    checks.extend(_check_ports(settings))
    return checks


def run_doctor(*, quiet: bool = False) -> int:
    """Print the preflight report.

    ``quiet`` is what the launchers use: a person starting YBM does not need 25
    lines saying everything is fine, only the ones that need attention and a
    one-line verdict. The full list is one ``ybm doctor`` away.
    """
    checks = collect_checks()
    fails = [c for c in checks if c.status == "fail"]
    warns = [c for c in checks if c.status == "warn"]
    oks = len(checks) - len(fails) - len(warns)
    shown = [c for c in checks if c.status != "ok"] if quiet else checks
    width = max((len(c.name) for c in shown), default=0)
    for check in shown:
        line = f"{STATUS_SYMBOL[check.status]} {check.name.ljust(width)}"
        if check.detail:
            line += f"  {check.detail}"
        print(line)

    if quiet and not fails and not warns:
        print(f"All {oks} checks passed.")
    else:
        if shown:
            print()
        print(f"{oks} ok, {len(warns)} warning(s), {len(fails)} failure(s)")
    if fails:
        print("Fix the failures above (or run `ybm setup`) before starting the stack.")
        return 1
    return 0


def run_setup(*, telegram_token: str | None = None, quiet: bool = False) -> int:
    """Create config, secrets and the database, and pick a model.

    Everything here is automatic: the admin token and vault key are generated,
    a model is chosen from what is already on the machine (see
    llm/autodetect.py), and nothing is asked. ``quiet`` is what the launchers
    pass - it drops the developer hints ("Next: ybm doctor ...", "TELEGRAM_BOT_TOKEN
    not set ...") that mean nothing to someone who just wanted YBM to start, and
    keeps the lines that report a decision made on their behalf.
    """
    if not quiet:
        print("YBM setup")
        print("=========")

    config_manager = ConfigManager()
    config_path = Path("config/config.yaml")
    if not config_path.exists():
        example_path = Path("config/config.example.yaml")
        if not example_path.exists():
            print("FAIL: config/config.example.yaml is missing - cannot bootstrap config.")
            return 1
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(example_path.read_text(encoding="utf-8"), encoding="utf-8")
        if not quiet:
            print(f"created {config_path} from config.example.yaml (every capability starts disabled)")
    elif not quiet:
        print(f"{config_path} already exists - leaving it as is")

    env_updates: dict[str, str] = {}
    if not read_env_value("AGENT_ADMIN_TOKEN"):
        env_updates["AGENT_ADMIN_TOKEN"] = secrets_module.token_urlsafe(32)
        if not quiet:
            print("generated AGENT_ADMIN_TOKEN")
    elif not quiet:
        print("AGENT_ADMIN_TOKEN already set")

    if not read_env_value("AGENT_SECRET_VAULT_KEY"):
        env_updates["AGENT_SECRET_VAULT_KEY"] = SecretVault.generate_key()
        if not quiet:
            print("generated AGENT_SECRET_VAULT_KEY")
    elif not quiet:
        print("AGENT_SECRET_VAULT_KEY already set")

    if telegram_token:
        env_updates["TELEGRAM_BOT_TOKEN"] = telegram_token
        print("saved TELEGRAM_BOT_TOKEN")
    elif not quiet and not read_env_value("TELEGRAM_BOT_TOKEN"):
        print("NOTE: TELEGRAM_BOT_TOKEN not set - pass `ybm setup --telegram-token <token>` "
              "or set it in .env, then enable channels.telegram in config/config.yaml")

    if env_updates:
        config_manager.upsert_env(env_updates)

    # Pick the model from what is already here instead of leaving the choice to
    # a wizard that asks about things the machine can answer. Free and
    # read-only: it never calls a paid API (llm/autodetect.py).
    from agent_control.llm.autodetect import auto_configure_llm

    detected = auto_configure_llm(config_manager)
    if detected is not None:
        print(f"model: using {detected.label}")
    elif not quiet:
        print("model: none found yet - choose one in the console, or set a provider API key "
              "(for example OPENAI_API_KEY) in .env and run setup again.")

    settings = load_settings()
    database = Database(settings.storage.database_url)
    database.initialize()
    if not quiet:
        print(f"database ready at {settings.storage.database_url}")

    _build_admin_console(quiet=quiet)
    # Only when WhatsApp is switched on: the sidecar's Node dependencies are
    # otherwise ~90 packages (and an `npm audit` warning) for a channel most
    # installs never use. Turning it on later installs them on first start.
    if settings.channels.whatsapp.enabled:
        _install_whatsapp_bridge_deps()

    if not quiet:
        print()
        print("Next: `ybm doctor` to verify the environment, then `ybm start`.")
    return 0


def _install_whatsapp_bridge_deps() -> None:
    """Install the WhatsApp sidecar's Node dependencies (``whatsapp-bridge/``
    is a standalone process, not bundled into anything).

    Best-effort: a missing npm warns rather than fails, since the rest of YBM
    works without the WhatsApp channel. The bridge also installs these itself
    on first start (channels/whatsapp_bridge_process.py), so this is a
    convenience for ``ybm setup`` on a machine where WhatsApp is already on.
    """
    from agent_control.channels.whatsapp_bridge_process import BRIDGE_DIR, install_bridge_dependencies

    if not BRIDGE_DIR.exists() or (BRIDGE_DIR / "node_modules").exists():
        return
    if shutil.which("npm") is None:
        print("\nNOTE: npm not found - skipping whatsapp-bridge dependency install. Install "
              "Node.js 22.22+ (https://nodejs.org), then run `npm install` in whatsapp-bridge/ "
              "if you plan to use the WhatsApp channel.")
        return
    print("\n-- Installing whatsapp-bridge dependencies --")
    installed, detail = install_bridge_dependencies()
    if not installed:
        print(f"WARN: installing whatsapp-bridge dependencies failed ({detail}) - run `npm install` "
              "in whatsapp-bridge/ if you plan to use WhatsApp.")


_ADMIN_CONSOLE_SOURCE_GLOBS = ("src/**/*", "public/**/*", "index.html", "package.json", "package-lock.json", "vite.config.ts", "tsconfig*.json")


def _admin_console_fingerprint(frontend_dir: Path) -> str:
    """(mtime, size) per source file, not content - reading every file's
    bytes would cost more than the build this exists to skip. A real edit
    always changes mtime on save, and git checkout resetting mtimes just
    means one extra rebuild, not a wrong skip."""
    entries: list[str] = []
    for pattern in _ADMIN_CONSOLE_SOURCE_GLOBS:
        for path in sorted(frontend_dir.glob(pattern)):
            if path.is_file():
                stat = path.stat()
                entries.append(f"{path.relative_to(frontend_dir)}:{stat.st_mtime_ns}:{stat.st_size}")
    return hashlib.sha256("\n".join(entries).encode("utf-8")).hexdigest()


def _build_admin_console(*, quiet: bool = False) -> None:
    """Build the React admin console so `/admin` serves the real app instead
    of the "no build yet, run `ybm ui-build`" fallback page - without this,
    a fresh install runs fine but silently shows an unfinished-looking admin
    UI on first launch. Best-effort: a missing/broken Node toolchain warns
    loudly (with the exact fix) rather than failing the whole setup, since
    the backend and every non-admin-console feature works without it.

    Skips the actual `npm run build` (the slow part - several seconds of
    tsc + vite on every single call) when nothing under frontend/ has
    changed since the last successful build, tracked by a fingerprint next
    to the build output itself (docs/UI_UX_AUDIT.md Phase 10, second
    review - `ybm run` is meant to open the console in a few seconds, not
    rebuild the console every launch regardless of whether anything
    changed)."""
    frontend_dir = Path("frontend")
    if not frontend_dir.exists():
        return

    # Relative to this process's CWD, which is the repo root here (run_setup()
    # is invoked from ybm.ps1 without a Push-Location into backend/) - NOT
    # relative to frontend_dir or this file. Confirmed the hard way: an
    # earlier version of this path was missing the backend/ prefix and
    # silently wrote a bogus src/agent_control/static/admin/ at the repo
    # root instead of the real backend/src/agent_control/static/admin/.
    static_dir = Path("backend/src/agent_control/static/admin")
    fingerprint_path = static_dir / ".ybm_build_fingerprint"
    current_fingerprint = _admin_console_fingerprint(frontend_dir)
    built = (static_dir / "index.html").exists()
    if built and not fingerprint_path.exists():
        # A console with no build fingerprint did not come from this machine's
        # `ybm ui-build`: it is the prebuilt one a release archive or the
        # container image ships. There is nothing to build, and the Node.js
        # toolchain it would need is exactly what these installs avoid.
        return
    if built and fingerprint_path.read_text(encoding="utf-8").strip() == current_fingerprint:
        if not quiet:
            print("\nadmin console up to date - skipping build.")
        return

    print("\n-- Building the admin console --")
    npm = shutil.which("npm")
    if npm is None:
        print("WARN: npm not found - skipping the admin console build. Install Node.js 22.22+ "
              "(https://nodejs.org), then run `ybm ui-build`. Until then, /admin shows a "
              "build-instructions page instead of the real console.")
        return

    node = shutil.which("node")
    node_version = _node_version(node) if node else None
    if node_version is not None and node_version < MIN_NODE_VERSION:
        actual = ".".join(str(part) for part in node_version)
        print(
            f"WARN: Node.js v{actual} is too old to build the admin console; install "
            "Node.js 22.22+ (https://nodejs.org), then run `ybm ui-build`."
        )
        return

    use_shell = sys.platform == "win32"
    if not (frontend_dir / "node_modules").exists():
        print("installing admin console dependencies (npm install)...")
        install_result = subprocess.run(["npm", "install"], cwd=frontend_dir, shell=use_shell, check=False)
        if install_result.returncode != 0:
            print("WARN: `npm install` failed - run it manually in frontend/, then `ybm ui-build`.")
            return

    print("building the admin console (npm run build)...")
    build_result = subprocess.run(["npm", "run", "build"], cwd=frontend_dir, shell=use_shell, check=False)
    if build_result.returncode == 0:
        print("admin console built - /admin will serve the real app.")
        static_dir.mkdir(parents=True, exist_ok=True)
        # Reuses the fingerprint computed before the build, not a fresh one -
        # the build only writes into static_dir, which is outside frontend/
        # and therefore outside every glob _admin_console_fingerprint reads.
        fingerprint_path.write_text(current_fingerprint, encoding="utf-8")
    else:
        print("WARN: admin console build failed - run `ybm ui-build` to see the full error.")
