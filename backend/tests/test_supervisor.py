from __future__ import annotations

from pathlib import Path

from agent_control.supervisor import build_service_specs


def _isolate(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("YBM_LOCALDEPLOY_ROOT", raising=False)


def _write_config(tmp_path: Path, contents: str) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.yaml").write_text(contents, encoding="utf-8")


def test_build_service_specs_skips_unconfigured_channels_on_a_fresh_install(monkeypatch, tmp_path) -> None:
    """No config.yaml at all -> AppSettings defaults -> neither channel is
    usable, so neither is attempted.

    WhatsApp has always been gated this way: poll-whatsapp refuses to start when
    disabled, so attempting it anyway crash-looped four times on every
    `ybm start` for everyone who never touched the feature
    (docs/archive/UI_UX_AUDIT.md Phase 16 review).

    Telegram used to be attempted unconditionally, and has exactly the same
    problem for exactly the same reason: `poll-telegram` raises "Telegram token
    not found in TELEGRAM_BOT_TOKEN" and exits when no token is set, which is
    the normal state of a fresh install - the built-in web chat needs no setup
    and Telegram is opt-in. It is required=True, so in a container it took the
    entire stack down on first run rather than merely looking alarming.
    """
    _isolate(monkeypatch, tmp_path)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)

    specs = build_service_specs()

    by_name = {spec.name: spec for spec in specs}
    assert "whatsapp" not in by_name
    assert "telegram_polling" not in by_name
    # The stack a fresh install actually needs is still there.
    assert "backend" in by_name
    assert by_name["backend"].required is True


def test_build_service_specs_starts_telegram_once_it_is_enabled_and_has_a_token(monkeypatch, tmp_path) -> None:
    """Both halves are required, and each fails on its own for a different
    reason: without `enabled` the user never asked for it, and without a token
    `poll-telegram` cannot start at all."""
    _isolate(monkeypatch, tmp_path)
    _write_config(tmp_path, "channels:\n  telegram:\n    enabled: true\n")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:AA-not-a-real-token")

    by_name = {spec.name: spec for spec in build_service_specs()}
    assert "telegram_polling" in by_name
    assert by_name["telegram_polling"].required is True

    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    assert "telegram_polling" not in {spec.name for spec in build_service_specs()}


def test_build_service_specs_includes_whatsapp_as_not_required_when_enabled(monkeypatch, tmp_path) -> None:
    _isolate(monkeypatch, tmp_path)
    _write_config(tmp_path, "channels:\n  whatsapp:\n    enabled: true\n")

    specs = build_service_specs()

    by_name = {spec.name: spec for spec in specs}
    assert "whatsapp" in by_name
    assert by_name["whatsapp"].required is False
    assert by_name["whatsapp"].args[-1] == "poll-whatsapp"


def test_build_service_specs_no_whatsapp_excludes_the_service_even_when_enabled(monkeypatch, tmp_path) -> None:
    _isolate(monkeypatch, tmp_path)
    _write_config(tmp_path, "channels:\n  whatsapp:\n    enabled: true\n")

    specs = build_service_specs(no_whatsapp=True)

    assert "whatsapp" not in {spec.name for spec in specs}


def test_build_service_specs_excludes_whatsapp_when_config_is_unreadable(monkeypatch, tmp_path) -> None:
    """Fail closed: a broken config.yaml must not crash `ybm start` itself
    just from trying to decide whether to attempt WhatsApp - `ybm doctor`
    (which runs first, unless -SkipDoctor) is where that belongs."""
    _isolate(monkeypatch, tmp_path)
    _write_config(tmp_path, "channels: [this is not a mapping")

    specs = build_service_specs()

    assert "whatsapp" not in {spec.name for spec in specs}


def test_backend_readiness_probe_follows_the_configured_port(monkeypatch, tmp_path) -> None:
    """The probe was hard-coded to 8765, so a backend on any other `server.port` was healthy but
    never "ready": `ybm start` waited out the 45-second budget and reported a required service
    as failed."""
    _isolate(monkeypatch, tmp_path)
    _write_config(tmp_path, "server:\n  port: 9123\n")

    backend = {spec.name: spec for spec in build_service_specs()}["backend"]

    assert backend.ready_url == "http://127.0.0.1:9123/health"


def test_backend_readiness_probe_ignores_the_public_base_url(monkeypatch, tmp_path) -> None:
    """A tunnel or proxy address cannot be dialled from here, and probing it would call a healthy
    backend down."""
    _isolate(monkeypatch, tmp_path)
    _write_config(tmp_path, "server:\n  port: 9123\n  public_base_url: https://ybm.example.test\n")

    backend = {spec.name: spec for spec in build_service_specs()}["backend"]

    assert backend.ready_url == "http://127.0.0.1:9123/health"


def test_backend_readiness_probe_defaults_to_8765_without_a_config(monkeypatch, tmp_path) -> None:
    _isolate(monkeypatch, tmp_path)

    backend = {spec.name: spec for spec in build_service_specs()}["backend"]

    assert backend.ready_url == "http://127.0.0.1:8765/health"
