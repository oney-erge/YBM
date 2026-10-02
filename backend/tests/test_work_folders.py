"""Choosing the folders YBM may work in: one action, conservative rules."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_control.admin import create_admin_router
from agent_control.config import AppSettings, load_settings
from agent_control.schemas import AuditEventType, Capability, CapabilityAccessMode
from agent_control.storage import Database, Repositories
from agent_control.tools.registry import build_tool_registry
from agent_control.tools.vscode_bridge import VSCodeBridgeStore
from agent_control.work_folders import (
    DEFAULT_ROOT,
    FolderRejected,
    apply_work_folders,
    current_state,
    suggested_folders,
    validate_work_folder,
)


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    return home


# ---- suggestions ------------------------------------------------------------------------


def test_suggestions_are_the_usual_places_that_actually_exist(tmp_path) -> None:
    home = _home(tmp_path)
    (home / "Downloads").mkdir()
    (home / "Documents").mkdir()

    found = suggested_folders(home)

    assert [(f["name"], f["path"]) for f in found] == [
        ("Downloads", str(home / "Downloads")),
        ("Documents", str(home / "Documents")),
    ]


def test_onedrive_redirected_folders_are_found_when_the_plain_one_is_absent(tmp_path) -> None:
    home = _home(tmp_path)
    (home / "OneDrive" / "Desktop").mkdir(parents=True)

    assert suggested_folders(home) == [{"name": "Desktop", "path": str(home / "OneDrive" / "Desktop")}]


def test_a_machine_with_none_of_them_gets_no_suggestions(tmp_path) -> None:
    assert suggested_folders(_home(tmp_path)) == []


# ---- what may be granted ------------------------------------------------------------------


def test_an_ordinary_folder_is_accepted_and_resolved(tmp_path) -> None:
    home = _home(tmp_path)
    folder = home / "Downloads"
    folder.mkdir()

    assert validate_work_folder(str(folder), home=home) == folder.resolve()


def test_quotes_pasted_from_a_file_manager_are_tolerated(tmp_path) -> None:
    home = _home(tmp_path)
    folder = home / "Downloads"
    folder.mkdir()

    assert validate_work_folder(f'"{folder}"', home=home) == folder.resolve()


@pytest.mark.parametrize("raw", ["", "   "])
def test_an_empty_choice_is_refused(tmp_path, raw) -> None:
    with pytest.raises(FolderRejected, match="Choose a folder"):
        validate_work_folder(raw, home=_home(tmp_path))


def test_a_relative_path_is_refused_with_an_example(tmp_path) -> None:
    home = _home(tmp_path)

    with pytest.raises(FolderRejected, match="full path"):
        validate_work_folder("Downloads", home=home)


def test_a_missing_folder_is_refused(tmp_path) -> None:
    home = _home(tmp_path)

    with pytest.raises(FolderRejected, match="does not exist"):
        validate_work_folder(str(home / "Nope"), home=home)


def test_a_file_is_not_a_folder(tmp_path) -> None:
    home = _home(tmp_path)
    (home / "notes.txt").write_text("x", encoding="utf-8")

    with pytest.raises(FolderRejected, match="file, not a folder"):
        validate_work_folder(str(home / "notes.txt"), home=home)


def test_a_whole_drive_is_refused(tmp_path) -> None:
    with pytest.raises(FolderRejected, match="whole drive"):
        validate_work_folder(Path(tmp_path.anchor).as_posix(), home=_home(tmp_path))


def test_the_home_folder_itself_and_anything_above_it_are_refused(tmp_path) -> None:
    home = _home(tmp_path)

    with pytest.raises(FolderRejected, match="home folder"):
        validate_work_folder(str(home), home=home)
    with pytest.raises(FolderRejected, match="home folder"):
        validate_work_folder(str(tmp_path), home=home)  # an ancestor of home


def test_an_operating_system_folder_is_refused(tmp_path) -> None:
    home = _home(tmp_path)
    system = Path(os.environ["SystemRoot"]) / "System32" if sys.platform == "win32" else Path("/etc")
    if not system.exists():
        pytest.skip("no such system folder on this machine")

    with pytest.raises(FolderRejected, match="system folder"):
        validate_work_folder(str(system), home=home)


# ---- writing the config ---------------------------------------------------------------------


def test_folders_are_added_to_the_roots_with_the_scratch_workspace_kept_first(tmp_path) -> None:
    config: dict = {"adapters": {"computer_use": {"allowed_roots": ["D:/Existing", DEFAULT_ROOT]}}}

    apply_work_folders(config, [tmp_path / "A", tmp_path / "B"], CapabilityAccessMode.WRITE_ACCESS)

    roots = config["adapters"]["computer_use"]["allowed_roots"]
    assert roots[0] == DEFAULT_ROOT
    assert roots[1] == "D:/Existing"  # existing grants are never dropped
    assert roots[2:] == [str(tmp_path / "A"), str(tmp_path / "B")]


def test_granting_the_same_folder_twice_does_not_duplicate_it(tmp_path) -> None:
    config: dict = {}

    apply_work_folders(config, [tmp_path / "A"], CapabilityAccessMode.WRITE_ACCESS)
    apply_work_folders(config, [tmp_path / "A"], CapabilityAccessMode.WRITE_ACCESS)

    assert config["adapters"]["computer_use"]["allowed_roots"].count(str(tmp_path / "A")) == 1


def test_a_missing_config_section_gets_the_default_workspace_too() -> None:
    config: dict = {}

    apply_work_folders(config, [], CapabilityAccessMode.READ_ONLY)

    assert config["adapters"]["computer_use"]["allowed_roots"] == [DEFAULT_ROOT]


@pytest.mark.parametrize("mode", [CapabilityAccessMode.OFF, CapabilityAccessMode.FULL_ACCESS])
def test_only_the_two_gentle_modes_can_be_granted_this_way(mode) -> None:
    with pytest.raises(ValueError, match="read-only or write-with-approval"):
        apply_work_folders({}, [], mode)


def test_the_file_access_mode_matches_what_the_access_page_would_set(tmp_path) -> None:
    config: dict = {}

    apply_work_folders(config, [tmp_path], CapabilityAccessMode.WRITE_ACCESS)

    state = current_state(AppSettings(_env_file=None, **config))
    assert state["file_access"] == "write_access"
    assert state["work_folders"] == [str(tmp_path)]
    assert state["allowed_roots"][0] == DEFAULT_ROOT


# ---- through the admin API --------------------------------------------------------------------


def _client(monkeypatch, tmp_path) -> tuple[TestClient, Repositories]:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AGENT_ADMIN_TOKEN", raising=False)
    monkeypatch.delenv("AGENT_CAPABILITIES", raising=False)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "config.yaml").write_text("{}\n", encoding="utf-8")
    database = Database(f"sqlite:///{tmp_path / 'admin.db'}")
    database.initialize()
    repositories = Repositories.for_database(database)
    app = FastAPI()
    app.include_router(create_admin_router(load_settings, lambda: repositories, VSCodeBridgeStore()))
    return TestClient(app), repositories


def test_a_fresh_install_reports_file_access_off_and_offers_suggestions(monkeypatch, tmp_path) -> None:
    client, _ = _client(monkeypatch, tmp_path)

    body = client.get("/admin/api/setup/folders").json()

    assert body["file_access"] == "off"
    assert body["work_folders"] == []
    assert isinstance(body["suggested"], list)


def test_one_post_grants_the_folder_and_makes_the_file_tool_available(monkeypatch, tmp_path) -> None:
    client, repositories = _client(monkeypatch, tmp_path)
    folder = tmp_path / "Downloads"
    folder.mkdir()

    response = client.post("/admin/api/setup/work-folders", json={"folders": [str(folder)], "mode": "write_access"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["file_access"] == "write_access"
    assert body["work_folders"] == [str(folder.resolve())]

    # The part that matters: the agent can now actually use it.
    settings = load_settings()
    registry = build_tool_registry(settings, "http://127.0.0.1:8765")
    definition = registry.definition_index["filesystem.manage"]
    assert definition.enabled is True
    assert settings.capabilities[Capability.FILESYSTEM_WRITE].requires_approval is True  # still asks first
    assert any(Path(root) == folder.resolve() for root in registry.adapters["filesystem.manage"].allowed_roots)
    assert any(Path(root) == folder.resolve() for root in settings.adapters.computer_use.allowed_roots)

    audited = [e for e in repositories.audit.list_recent(20) if e.type == AuditEventType.CONFIG_UPDATED]
    assert any(e.payload.get("section") == "work_folders" for e in audited)


def test_read_only_grants_looking_but_not_changing(monkeypatch, tmp_path) -> None:
    client, _ = _client(monkeypatch, tmp_path)
    folder = tmp_path / "Reports"
    folder.mkdir()

    body = client.post("/admin/api/setup/work-folders", json={"folders": [str(folder)], "mode": "read_only"}).json()

    assert body["file_access"] == "read_only"


def test_a_dangerous_folder_is_refused_with_a_message_and_nothing_is_written(monkeypatch, tmp_path) -> None:
    client, _ = _client(monkeypatch, tmp_path)
    before = (tmp_path / "config" / "config.yaml").read_text(encoding="utf-8")

    response = client.post("/admin/api/setup/work-folders", json={"folders": [Path(tmp_path.anchor).as_posix()]})

    assert response.status_code == 400
    assert "whole drive" in response.json()["detail"]
    assert (tmp_path / "config" / "config.yaml").read_text(encoding="utf-8") == before


def test_full_autonomy_cannot_be_granted_through_this_shortcut(monkeypatch, tmp_path) -> None:
    client, _ = _client(monkeypatch, tmp_path)
    folder = tmp_path / "Downloads"
    folder.mkdir()

    response = client.post("/admin/api/setup/work-folders", json={"folders": [str(folder)], "mode": "full_access"})

    assert response.status_code == 422


def test_at_least_one_folder_is_required(monkeypatch, tmp_path) -> None:
    client, _ = _client(monkeypatch, tmp_path)

    assert client.post("/admin/api/setup/work-folders", json={"folders": []}).status_code == 422


def test_granting_a_second_folder_keeps_the_first(monkeypatch, tmp_path) -> None:
    client, _ = _client(monkeypatch, tmp_path)
    first, second = tmp_path / "A", tmp_path / "B"
    first.mkdir()
    second.mkdir()

    client.post("/admin/api/setup/work-folders", json={"folders": [str(first)]})
    body = client.post("/admin/api/setup/work-folders", json={"folders": [str(second)]}).json()

    assert body["work_folders"] == [str(first.resolve()), str(second.resolve())]


def test_the_written_config_is_valid_yaml_the_app_can_reload(monkeypatch, tmp_path) -> None:
    client, _ = _client(monkeypatch, tmp_path)
    folder = tmp_path / "Downloads"
    folder.mkdir()
    client.post("/admin/api/setup/work-folders", json={"folders": [str(folder)]})

    written = yaml.safe_load((tmp_path / "config" / "config.yaml").read_text(encoding="utf-8"))

    assert written["adapters"]["computer_use"]["allowed_roots"][0] == DEFAULT_ROOT
    assert load_settings().adapters.computer_use.allowed_roots[0] == DEFAULT_ROOT
