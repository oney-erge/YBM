from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent_control.channels import whatsapp_bridge_process as bridge_module
from agent_control.channels.whatsapp_bridge_process import STATE_PATH, WhatsAppBridgeProcess, find_node_binary
from agent_control.config import WhatsAppConfig


class FakeHandle:
    def __init__(self, pid: int = 4242) -> None:
        self.pid = pid
        self.terminated = False
        self.returncode: int | None = None

    def terminate(self) -> None:
        self.terminated = True

    async def wait(self) -> int:
        return 0


class FakeSpawner:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], str, dict]] = []
        self.handle = FakeHandle()

    async def spawn(self, command: list[str], *, cwd: str, env: dict) -> FakeHandle:
        self.calls.append((command, cwd, env))
        return self.handle


class FakeHealthyHttpResponse:
    status_code = 200


class FakeHealthyHttpClient:
    def __init__(self, timeout: int) -> None:
        self.timeout = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def get(self, url: str, headers: dict | None = None) -> FakeHealthyHttpResponse:
        return FakeHealthyHttpResponse()


class FakeUnhealthyHttpClient(FakeHealthyHttpClient):
    async def get(self, url: str, headers: dict | None = None):
        import httpx

        raise httpx.ConnectError("connection refused")


def _installed_bridge(tmp_path, monkeypatch) -> str:
    """The on-disk layout start() expects at the repo root it runs from: a
    whatsapp-bridge/ with dependencies installed, plus a node binary.
    Returns the node path."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "whatsapp-bridge" / "node_modules").mkdir(parents=True)
    node = tmp_path / "node.exe"
    node.write_text("", encoding="utf-8")
    return str(node)


def test_find_node_binary_prefers_explicit_config_path(tmp_path) -> None:
    node = tmp_path / "node.exe"
    node.write_text("", encoding="utf-8")

    assert find_node_binary(str(node)) == str(node)


def test_find_node_binary_returns_none_for_a_missing_configured_path(tmp_path) -> None:
    missing = tmp_path / "does-not-exist" / "node.exe"

    assert find_node_binary(str(missing)) is None


@pytest.mark.asyncio
async def test_bridge_process_starts_and_writes_shared_state(tmp_path, monkeypatch) -> None:
    node = _installed_bridge(tmp_path, monkeypatch)
    monkeypatch.setattr("agent_control.channels.whatsapp_bridge_process.httpx.AsyncClient", FakeHealthyHttpClient)
    spawner = FakeSpawner()
    bridge = WhatsAppBridgeProcess(WhatsAppConfig(enabled=True, node_path=str(node)), spawner=spawner)

    await bridge.start(ready_timeout_seconds=5)

    assert len(spawner.calls) == 1
    command, cwd, env = spawner.calls[0]
    assert command == [str(node), "src/index.js"]
    assert cwd == "whatsapp-bridge"
    assert env["WHATSAPP_BRIDGE_SECRET"] == bridge.secret
    assert STATE_PATH.exists()
    state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    assert state == {"base_url": bridge.base_url, "secret": bridge.secret}

    bridge.stop()

    assert spawner.handle.terminated is True
    assert not STATE_PATH.exists()


@pytest.mark.asyncio
async def test_bridge_process_raises_a_clear_error_when_node_is_not_found(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    spawner = FakeSpawner()
    bridge = WhatsAppBridgeProcess(WhatsAppConfig(enabled=True, node_path=str(tmp_path / "no-such-node.exe")), spawner=spawner)

    with pytest.raises(RuntimeError, match="node was not found"):
        await bridge.start(ready_timeout_seconds=5)

    assert spawner.calls == []


@pytest.mark.asyncio
async def test_bridge_process_raises_when_health_check_never_succeeds(tmp_path, monkeypatch) -> None:
    node = _installed_bridge(tmp_path, monkeypatch)
    monkeypatch.setattr("agent_control.channels.whatsapp_bridge_process.httpx.AsyncClient", FakeUnhealthyHttpClient)
    spawner = FakeSpawner()
    bridge = WhatsAppBridgeProcess(WhatsAppConfig(enabled=True, node_path=str(node)), spawner=spawner)

    with pytest.raises(RuntimeError, match="did not become healthy"):
        await bridge.start(ready_timeout_seconds=1)

    assert not STATE_PATH.exists()


@pytest.mark.asyncio
async def test_bridge_process_leaves_the_handle_reachable_after_a_failed_health_check(tmp_path, monkeypatch) -> None:
    """Regression test for a real orphan-process bug: start() spawns the
    child BEFORE the health check runs, so a health-check timeout must not
    strand the caller with no way to terminate the already-running child.
    cli.py's poll_whatsapp() relies on exactly this: it calls bridge.stop()
    in its except handler when start() raises, and that only works if
    _handle is still set."""
    node = _installed_bridge(tmp_path, monkeypatch)
    monkeypatch.setattr("agent_control.channels.whatsapp_bridge_process.httpx.AsyncClient", FakeUnhealthyHttpClient)
    spawner = FakeSpawner()
    bridge = WhatsAppBridgeProcess(WhatsAppConfig(enabled=True, node_path=str(node)), spawner=spawner)

    with pytest.raises(RuntimeError, match="did not become healthy"):
        await bridge.start(ready_timeout_seconds=1)

    assert len(spawner.calls) == 1  # the child WAS spawned before the health check failed
    assert spawner.handle.terminated is False  # start() itself doesn't stop it...

    bridge.stop()  # ...but the caller's cleanup can, and does

    assert spawner.handle.terminated is True


@pytest.mark.asyncio
async def test_bridge_process_fails_fast_when_node_modules_is_missing_and_cannot_be_installed(tmp_path, monkeypatch) -> None:
    """Dependencies that are missing and cannot be installed are the single most
    likely reason the bridge won't start. Say so before spawning, with the exact
    fix, instead of a 60s health-check timeout."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "whatsapp-bridge").mkdir()  # present, but dependencies not installed
    node = tmp_path / "node.exe"
    node.write_text("", encoding="utf-8")
    monkeypatch.setattr(bridge_module, "install_bridge_dependencies", lambda: (False, "npm was not found on PATH"))
    spawner = FakeSpawner()
    bridge = WhatsAppBridgeProcess(WhatsAppConfig(enabled=True, node_path=str(node)), spawner=spawner)

    with pytest.raises(RuntimeError, match="node_modules is missing") as raised:
        await bridge.start(ready_timeout_seconds=5)

    assert "npm was not found on PATH" in str(raised.value)  # the reason is surfaced, not swallowed
    assert spawner.calls == []  # nothing spawned, so nothing to leak


@pytest.mark.asyncio
async def test_bridge_process_installs_its_own_dependencies_the_first_time_it_starts(tmp_path, monkeypatch) -> None:
    """Setup no longer installs the sidecar's packages for people who never use
    WhatsApp, so turning the channel on later has to just work."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "whatsapp-bridge").mkdir()
    node = tmp_path / "node.exe"
    node.write_text("", encoding="utf-8")
    installs: list[str] = []

    def fake_install() -> tuple[bool, str]:
        installs.append("npm ci")
        (tmp_path / "whatsapp-bridge" / "node_modules").mkdir()
        return True, "installed"

    monkeypatch.setattr(bridge_module, "install_bridge_dependencies", fake_install)
    monkeypatch.setattr("agent_control.channels.whatsapp_bridge_process.httpx.AsyncClient", FakeHealthyHttpClient)
    spawner = FakeSpawner()
    bridge = WhatsAppBridgeProcess(WhatsAppConfig(enabled=True, node_path=str(node)), spawner=spawner)

    await bridge.start(ready_timeout_seconds=5)

    assert installs == ["npm ci"]
    assert len(spawner.calls) == 1
    bridge.stop()


@pytest.mark.asyncio
async def test_bridge_process_does_not_reinstall_when_dependencies_are_present(tmp_path, monkeypatch) -> None:
    node = _installed_bridge(tmp_path, monkeypatch)
    monkeypatch.setattr(bridge_module, "install_bridge_dependencies", lambda: pytest.fail("must not reinstall"))
    monkeypatch.setattr("agent_control.channels.whatsapp_bridge_process.httpx.AsyncClient", FakeHealthyHttpClient)
    bridge = WhatsAppBridgeProcess(WhatsAppConfig(enabled=True, node_path=str(node)), spawner=FakeSpawner())

    await bridge.start(ready_timeout_seconds=5)

    bridge.stop()


def test_install_bridge_dependencies_uses_the_lockfile_and_stays_quiet(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "whatsapp-bridge").mkdir()
    (tmp_path / "whatsapp-bridge" / "package-lock.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(bridge_module.shutil, "which", lambda name: "/usr/bin/npm")
    seen: dict = {}

    def fake_run(command, **kwargs):
        seen["command"] = command
        seen["cwd"] = kwargs.get("cwd")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(bridge_module.subprocess, "run", fake_run)

    ok, detail = bridge_module.install_bridge_dependencies()

    assert ok is True and detail == "installed"
    assert seen["command"][:2] == ["npm", "ci"]  # reproducible, not "whatever is newest"
    assert {"--omit=dev", "--no-audit", "--no-fund", "--loglevel=error"} <= set(seen["command"])


def test_install_bridge_dependencies_reports_npm_failures_instead_of_raising(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "whatsapp-bridge").mkdir()
    monkeypatch.setattr(bridge_module.shutil, "which", lambda name: "/usr/bin/npm")
    monkeypatch.setattr(
        bridge_module.subprocess, "run",
        lambda command, **kwargs: SimpleNamespace(returncode=1, stdout="", stderr="E404 not found: baileys"),
    )

    ok, detail = bridge_module.install_bridge_dependencies()

    assert ok is False
    assert "E404" in detail


def test_install_bridge_dependencies_says_what_to_install_when_npm_is_missing(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(bridge_module.shutil, "which", lambda name: None)

    ok, detail = bridge_module.install_bridge_dependencies()

    assert ok is False
    assert "Node.js" in detail


@pytest.mark.asyncio
async def test_bridge_process_reports_an_immediately_exiting_child_without_waiting_out_the_timeout(
    tmp_path, monkeypatch
) -> None:
    """A node that dies on startup (bad import, port taken) is never coming
    back - surface its exit instead of burning the full ready timeout and
    then blaming a generic "did not become healthy"."""
    node = _installed_bridge(tmp_path, monkeypatch)
    monkeypatch.setattr("agent_control.channels.whatsapp_bridge_process.httpx.AsyncClient", FakeUnhealthyHttpClient)
    spawner = FakeSpawner()
    spawner.handle.returncode = 1  # child is already dead by the first health poll
    bridge = WhatsAppBridgeProcess(WhatsAppConfig(enabled=True, node_path=str(node)), spawner=spawner)

    with pytest.raises(RuntimeError, match="exited immediately"):
        await bridge.start(ready_timeout_seconds=30)  # must NOT take 30s


@pytest.mark.asyncio
async def test_is_alive_reflects_the_child_process_returncode(tmp_path, monkeypatch) -> None:
    node = _installed_bridge(tmp_path, monkeypatch)
    monkeypatch.setattr("agent_control.channels.whatsapp_bridge_process.httpx.AsyncClient", FakeHealthyHttpClient)
    spawner = FakeSpawner()
    bridge = WhatsAppBridgeProcess(WhatsAppConfig(enabled=True, node_path=str(node)), spawner=spawner)

    assert bridge.is_alive() is False  # not started yet

    await bridge.start(ready_timeout_seconds=5)

    assert bridge.is_alive() is True
    assert bridge.returncode is None

    spawner.handle.returncode = 1  # simulate the child dying on its own

    assert bridge.is_alive() is False
    assert bridge.returncode == 1
