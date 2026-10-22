"""The command-line pieces the launchers and the container lean on."""

from __future__ import annotations

import sys
from urllib.parse import parse_qs, urlparse

import pytest

from agent_control import cli


def _run(monkeypatch, tmp_path, *argv: str) -> int:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["ybm", *argv])
    with pytest.raises(SystemExit) as raised:
        cli.main()
    return int(raised.value.code or 0)


def test_admin_url_prints_a_signed_in_link(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setenv("AGENT_ADMIN_TOKEN", "the-generated-token")

    code = _run(monkeypatch, tmp_path, "admin-url")

    assert code == 0
    parsed = urlparse(capsys.readouterr().out.strip())
    assert (parsed.scheme, parsed.netloc, parsed.path) == ("http", "127.0.0.1:8765", "/admin")
    assert parse_qs(parsed.query)["token"] == ["the-generated-token"]


def test_admin_url_is_a_plain_address_when_no_token_is_set(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.delenv("AGENT_ADMIN_TOKEN", raising=False)

    assert _run(monkeypatch, tmp_path, "admin-url") == 0

    assert capsys.readouterr().out.strip() == "http://127.0.0.1:8765/admin"


def test_doctor_accepts_quiet(monkeypatch, tmp_path) -> None:
    calls: list[bool] = []
    monkeypatch.setattr(cli, "run_doctor", lambda *, quiet=False: calls.append(quiet) or 0)

    assert _run(monkeypatch, tmp_path, "doctor", "--quiet") == 0
    assert _run(monkeypatch, tmp_path, "doctor") == 0

    assert calls == [True, False]


def test_setup_accepts_quiet(monkeypatch, tmp_path) -> None:
    calls: list[dict] = []
    monkeypatch.setattr(cli, "run_setup", lambda **kwargs: calls.append(kwargs) or 0)

    assert _run(monkeypatch, tmp_path, "setup", "--quiet") == 0
    assert _run(monkeypatch, tmp_path, "setup", "--telegram-token", "tok") == 0

    assert calls == [{"telegram_token": None, "quiet": True}, {"telegram_token": "tok", "quiet": False}]


def _write_channels(tmp_path, contents: str) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.yaml").write_text(contents, encoding="utf-8")


def test_channel_enabled_telegram_needs_a_token_like_the_supervisor(monkeypatch, tmp_path) -> None:
    """scripts/ybm.ps1 gates the Telegram poller on this exit code. Enabled
    without a token used to start a required service that died at once."""
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    _write_channels(tmp_path, "channels:\n  telegram:\n    enabled: true\n")
    assert _run(monkeypatch, tmp_path, "channel-enabled", "telegram") == 1

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:AA-not-a-real-token")
    assert _run(monkeypatch, tmp_path, "channel-enabled", "telegram") == 0


def test_channel_enabled_telegram_is_off_on_a_fresh_install(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:AA-not-a-real-token")

    assert _run(monkeypatch, tmp_path, "channel-enabled", "telegram") == 1


def test_channel_enabled_whatsapp_follows_its_flag_and_unknown_is_off(monkeypatch, tmp_path) -> None:
    _write_channels(tmp_path, "channels:\n  whatsapp:\n    enabled: true\n")
    assert _run(monkeypatch, tmp_path, "channel-enabled", "whatsapp") == 0
    assert _run(monkeypatch, tmp_path, "channel-enabled", "carrier-pigeon") == 1
