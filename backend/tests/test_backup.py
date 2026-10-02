"""docs/archive/UI_UX_AUDIT.md Phase 6: back up the state that can't be
regenerated (database, config, .env, secret vault) into a timestamped
zip - not artifacts/workspaces/logs, which are task output or caches.
"""

from __future__ import annotations

import zipfile

from agent_control.backup import run_backup


def _write(path, content: bytes = b"data") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def test_backup_includes_every_file_that_exists(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _write(tmp_path / ".agent_control" / "agent_control.db", b"sqlite bytes")
    _write(tmp_path / "config" / "config.yaml", b"server:\n  port: 8765\n")
    _write(tmp_path / ".env", b"AGENT_ADMIN_TOKEN=secret\n")
    _write(tmp_path / ".agent_control" / "secrets" / "vault.json", b"{}")

    exit_code = run_backup()

    assert exit_code == 0
    zips = list((tmp_path / ".agent_control" / "backups").glob("*.zip"))
    assert len(zips) == 1
    with zipfile.ZipFile(zips[0]) as archive:
        names = set(archive.namelist())
    assert names == {"agent_control.db", "config.yaml", ".env", "vault.json"}


def test_backup_includes_only_what_exists(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _write(tmp_path / ".agent_control" / "agent_control.db", b"sqlite bytes")

    exit_code = run_backup()

    assert exit_code == 0
    zips = list((tmp_path / ".agent_control" / "backups").glob("*.zip"))
    with zipfile.ZipFile(zips[0]) as archive:
        assert archive.namelist() == ["agent_control.db"]


def test_backup_fails_cleanly_when_nothing_exists_to_back_up(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    exit_code = run_backup()

    assert exit_code == 1
    assert not (tmp_path / ".agent_control" / "backups").exists()


def test_backup_writes_to_a_custom_out_dir(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _write(tmp_path / ".agent_control" / "agent_control.db", b"sqlite bytes")
    custom_out = tmp_path / "elsewhere"

    exit_code = run_backup(str(custom_out))

    assert exit_code == 0
    assert list(custom_out.glob("*.zip"))
