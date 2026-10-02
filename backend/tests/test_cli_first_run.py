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
