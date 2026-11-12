"""system.inventory: questions about this computer, answered from the places that already know."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from agent_control.tools import system_inventory
from agent_control.tools.contracts import SystemInventoryInput
from agent_control.tools.system_inventory import SystemInventoryAdapter, _index_sql, _words


def _run(adapter, **inputs):
    return asyncio.run(adapter.execute(SimpleNamespace(id="req", input=inputs)))


def _manifest(library: Path, app_id: int, name: str, size: int = 2 * 1024**3) -> None:
    apps = library / "steamapps"
    apps.mkdir(parents=True, exist_ok=True)
    (apps / f"appmanifest_{app_id}.acf").write_text(
        f'"AppState"\n{{\n\t"appid"\t\t"{app_id}"\n\t"name"\t\t"{name}"\n\t"SizeOnDisk"\t\t"{size}"\n}}\n', encoding="utf-8"
    )


@pytest.fixture
def steam(tmp_path, monkeypatch):
    root = tmp_path / "Steam"
    other = tmp_path / "SteamLibrary"
    _manifest(root, 220, "Half-Life 2")
    _manifest(root, 228980, "Steamworks Common Redistributables")
    _manifest(other, 892970, "Valheim")
    _manifest(other, 220, "Half-Life 2")  # the same game seen twice is one game
    (root / "steamapps" / "libraryfolders.vdf").write_text(
        '"libraryfolders"\n{\n\t"0"\n\t{\n\t\t"path"\t\t"' + str(root).replace("\\", "\\\\") + '"\n\t}\n'
        '\t"1"\n\t{\n\t\t"path"\t\t"' + str(other).replace("\\", "\\\\") + '"\n\t}\n}\n', encoding="utf-8"
    )
    monkeypatch.setattr(system_inventory, "_steam_roots", lambda: [root])
    return root


def test_steam_games_counts_every_library_and_skips_the_redistributables(steam):
    result = _run(SystemInventoryAdapter([]), operation="steam_games")
    assert result.output["count"] == 2
    assert [g["name"] for g in result.output["items"]] == ["Half-Life 2", "Valheim"]
    assert result.output["summary"].startswith("2 games are installed through Steam")
    assert "Redistributables" not in result.output["summary"]


def test_steam_not_installed_is_said_plainly(monkeypatch):
    monkeypatch.setattr(system_inventory, "_steam_roots", lambda: [])
    result = _run(SystemInventoryAdapter([]), operation="steam_games")
    assert result.output["count"] == 0
    assert "does not seem to be installed" in result.output["summary"]


@pytest.fixture
def files(tmp_path, monkeypatch):
    """A readable area with a few files, an unreadable area, and no Windows Search index."""
    monkeypatch.setattr(system_inventory, "search_index", lambda *args, **kwargs: None)
    readable = tmp_path / "home"
    (readable / "OneDrive").mkdir(parents=True)
    (readable / "Desktop").mkdir()
    secret = tmp_path / "elsewhere"
    secret.mkdir()
    old = readable / "OneDrive" / "Resume.docx"
    old.write_text("old")
    os.utime(old, (1_600_000_000, 1_600_000_000))
    (readable / "Desktop" / "resume-notes.txt").write_text("notes")
    (readable / "Desktop" / "resume-photo.png").write_bytes(b"x")
    (secret / "resume-private.docx").write_text("no")
    return SimpleNamespace(readable=readable, secret=secret)


def test_find_files_returns_documents_first_and_only_readable_folders(files):
    result = _run(SystemInventoryAdapter([str(files.readable)]), operation="find_files", query="resume")
    names = [i["name"] for i in result.output["items"]]
    assert "resume-private.docx" not in names
    assert names.index("resume-photo.png") > max(names.index("Resume.docx"), names.index("resume-notes.txt"))
    assert "a search of the folders I can read" in result.output["summary"]


def test_find_files_filters_by_extension(files):
    result = _run(SystemInventoryAdapter([str(files.readable)]), operation="find_files", query="resume", extensions=["docx"])
    assert [i["name"] for i in result.output["items"]] == ["Resume.docx"]


def test_find_files_says_when_matches_are_out_of_bounds(files, monkeypatch):
    monkeypatch.setattr(system_inventory, "search_index", lambda *a, **k: [
        {"path": str(files.secret / "resume-private.docx"), "size_bytes": 2, "modified": None}])
    result = _run(SystemInventoryAdapter([str(files.readable)]), operation="find_files", query="resume-private")
    assert result.output["count"] == 0
    assert "1 matches are in folders I am not allowed to read" in result.output["summary"]


def test_find_files_uses_the_index_when_there_is_one(files, monkeypatch):
    path = str(files.readable / "OneDrive" / "Resume.docx")
    monkeypatch.setattr(system_inventory, "search_index", lambda *a, **k: [{"path": path, "size_bytes": 3, "modified": "2021-10-27T00:00:00"}])
    result = _run(SystemInventoryAdapter([str(files.readable)]), operation="find_files", query="resume")
    assert result.output["source"] == "the Windows Search index"
    assert result.output["items"][0]["path"] == path


def test_find_files_without_a_query_fails_cleanly(files):
    result = _run(SystemInventoryAdapter([str(files.readable)]), operation="find_files", query="  ")
    assert result.status.value == "failed"


def test_the_index_query_cannot_be_injected_into():
    words = _words("resume'; DROP TABLE x -- %")
    joined = "".join(words)
    assert "'" not in joined and "%" not in joined and ";" not in joined
    sql = _index_sql(words, [".docx"], "name", 50)
    # One pair of quotes per LIKE word, one for the extension, one for the item type: nothing can close them early.
    assert sql.count("'") == 2 * (len(words) + 2)
    assert "LIKE '%resume%'" in sql and "System.FileExtension = '.docx'" in sql


def test_contract_requires_a_query_only_for_find_files():
    SystemInventoryInput(operation="steam_games")
    SystemInventoryInput(operation="disk_space")
    with pytest.raises(ValidationError):
        SystemInventoryInput(operation="find_files")


def test_disk_space_lists_the_drives(monkeypatch):
    monkeypatch.setattr(system_inventory, "disk_space", lambda: [{"drive": "C:\\", "free_gb": 10.0, "total_gb": 20.0}])
    result = _run(SystemInventoryAdapter([]), operation="disk_space")
    assert "C:\\ 10.0 GB free of 20.0 GB" in result.output["summary"]


def test_files_kept_near_the_top_of_a_folder_come_before_buried_copies(tmp_path, monkeypatch):
    monkeypatch.setattr(system_inventory, "search_index", lambda *a, **k: None)
    home = tmp_path / "home"
    (home / "Downloads").mkdir(parents=True)
    buried = home / "Desktop" / "Review" / "Copies" / "archive" / "old"
    buried.mkdir(parents=True)
    kept = home / "Downloads" / "resume.pdf"
    kept.write_text("kept")
    deep = buried / "resume.pdf"
    deep.write_text("deep")
    os.utime(kept, (1_600_000_000, 1_600_000_000))  # older, but where a person keeps it
    result = _run(SystemInventoryAdapter([str(home)]), operation="find_files", query="resume")
    assert [i["path"] for i in result.output["items"]] == [str(kept), str(deep)]


def test_the_top_results_start_with_a_preview_and_the_summary_says_what_to_do_next(tmp_path, monkeypatch):
    monkeypatch.setattr(system_inventory, "search_index", lambda *a, **k: None)
    home = tmp_path / "home"
    home.mkdir()
    (home / "resume-a.txt").write_text("Jane Doe\nSenior data engineer with eight years of experience\n")
    (home / "resume-b.txt").write_text("John Roe\nProduct designer\n")
    result = _run(SystemInventoryAdapter([str(home)]), operation="find_files", query="resume")
    summary = result.output["summary"]
    assert "starts: Jane Doe Senior data engineer with eight years of experience" in summary
    assert "ask which" in summary and "filesystem.manage read_file" in summary
    assert all("preview" in item for item in result.output["items"])


def test_a_single_result_does_not_ask_which_one(tmp_path, monkeypatch):
    monkeypatch.setattr(system_inventory, "search_index", lambda *a, **k: None)
    (tmp_path / "only-resume.txt").write_text("x")
    result = _run(SystemInventoryAdapter([str(tmp_path)]), operation="find_files", query="resume")
    assert "ask which" not in result.output["summary"]


def test_a_big_result_set_is_cut_to_the_best_few_and_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(system_inventory, "search_index", lambda *a, **k: None)
    for number in range(20):
        (tmp_path / f"resume-{number}.txt").write_text("x")
    result = _run(SystemInventoryAdapter([str(tmp_path)]), operation="find_files", query="resume")
    assert result.output["count"] == 8
    assert "the best 8 of 20" in result.output["summary"]


def test_a_file_with_the_persons_own_name_comes_first(tmp_path, monkeypatch):
    monkeypatch.setattr(system_inventory, "search_index", lambda *a, **k: None)
    (tmp_path / "Downloads").mkdir()
    mine = tmp_path / "OneDrive" / "Oney.Erge.Resume.2021.docx"
    mine.parent.mkdir()
    mine.write_text("a")
    theirs = tmp_path / "Downloads" / "2025.02_Onur_Resume.docx"
    theirs.write_text("b")
    os.utime(mine, (1_600_000_000, 1_600_000_000))  # the same depth, so which is newer decides; pin it, files made in one second tie
    os.utime(theirs, (1_700_000_000, 1_700_000_000))
    adapter = SystemInventoryAdapter([str(tmp_path)], lambda: ["Oney", "Erge"])
    names = [i["name"] for i in _run(adapter, operation="find_files", query="resume").output["items"]]
    assert names == ["Oney.Erge.Resume.2021.docx", "2025.02_Onur_Resume.docx"]
    # Without a known name the shallower, newer file is first, as before.
    plain = [i["name"] for i in _run(SystemInventoryAdapter([str(tmp_path)]), operation="find_files", query="resume").output["items"]]
    assert plain[0] == "2025.02_Onur_Resume.docx"


def test_name_tokens_come_from_name_facts_only():
    facts = [
        SimpleNamespace(content="Name: Oney Erge (from their Telegram profile)"),
        SimpleNamespace(content="GitHub username: oney-erge"),
        SimpleNamespace(content="T-shirt size: L"),
    ]
    assert system_inventory.name_tokens_from_facts(facts) == ["Oney", "Erge"]


def test_an_online_only_file_is_marked_and_not_called_corrupted(tmp_path, monkeypatch):
    monkeypatch.setattr(system_inventory, "search_index", lambda *a, **k: None)
    cloud = tmp_path / "Oney.Erge.Resume.2021.docx"
    cloud.write_bytes(b"not a zip")
    monkeypatch.setattr(system_inventory, "is_online_only", lambda path: str(path) == str(cloud))
    result = _run(SystemInventoryAdapter([str(tmp_path)]), operation="find_files", query="resume")
    summary = result.output["summary"]
    assert "[online only: not downloaded]" in summary
    assert "it is not corrupted" in summary and "Always keep on this device" in summary
    assert result.output["items"][0]["online_only"] is True
    assert "preview" not in result.output["items"][0]


def test_office_text_explains_an_online_only_file(tmp_path, monkeypatch):
    from agent_control.tools import office_text as module

    cloud = tmp_path / "plan.docx"
    cloud.write_bytes(b"placeholder")
    monkeypatch.setattr(module, "is_online_only", lambda path: True)
    with pytest.raises(ValueError, match="stored only in the cloud"):
        module.office_text(cloud)


def test_a_local_file_is_not_online_only(tmp_path):
    from agent_control.tools.path_utils import is_online_only

    local = tmp_path / "a.txt"
    local.write_text("x")
    assert is_online_only(local) is False
    assert is_online_only(tmp_path / "missing.txt") is False


def test_a_file_the_index_does_not_cover_is_still_found_by_name(tmp_path, monkeypatch):
    """The index answered (with nothing) because a hidden or just-created folder is not indexed yet."""
    monkeypatch.setattr(system_inventory, "search_index", lambda *a, **k: [])
    hidden = tmp_path / ".agent_control" / "fixtures"
    hidden.mkdir(parents=True)
    (tmp_path / "plain").mkdir()
    (tmp_path / "plain" / "quarterly-report.docx").write_text("x")
    result = _run(SystemInventoryAdapter([str(tmp_path)]), operation="find_files", query="quarterly-report")
    assert [i["name"] for i in result.output["items"]] == ["quarterly-report.docx"]
    assert result.output["source"] == "a search of the folders I can read"


def test_when_the_index_has_the_answer_the_walk_is_not_run(tmp_path, monkeypatch):
    path = str(tmp_path / "a-report.docx")
    (tmp_path / "a-report.docx").write_text("x")
    monkeypatch.setattr(system_inventory, "search_index", lambda *a, **k: [{"path": path, "size_bytes": 1, "modified": None}])

    def boom(*args, **kwargs):
        raise AssertionError("the walk must not run when the index answered")

    monkeypatch.setattr(system_inventory, "walk_search", boom)
    result = _run(SystemInventoryAdapter([str(tmp_path)]), operation="find_files", query="report")
    assert result.output["source"] == "the Windows Search index"


def test_nothing_anywhere_still_says_nothing_was_found(tmp_path, monkeypatch):
    monkeypatch.setattr(system_inventory, "search_index", lambda *a, **k: [])
    result = _run(SystemInventoryAdapter([str(tmp_path)]), operation="find_files", query="does-not-exist-anywhere")
    assert result.output["count"] == 0 and "No file matching 'does-not-exist-anywhere' was found" in result.output["summary"]


def test_a_content_search_does_not_fall_back_to_a_name_walk(tmp_path, monkeypatch):
    monkeypatch.setattr(system_inventory, "search_index", lambda *a, **k: [])
    (tmp_path / "invoice.txt").write_text("cardamom")
    result = _run(SystemInventoryAdapter([str(tmp_path)]), operation="find_files", query="cardamom", match="content")
    assert result.output["count"] == 0


# --- the walk says how hard it looked ---------------------------------------------------------------------------------


def test_a_file_in_a_hidden_folder_is_found_by_the_second_pass(tmp_path, monkeypatch):
    monkeypatch.setattr(system_inventory, "search_index", lambda *a, **k: [])
    deep = tmp_path / ".agent_control" / "e2e_fixtures" / "deep"
    deep.mkdir(parents=True)
    (deep / "h3-annual-budget-2026.xlsx").write_text("x")
    result = _run(SystemInventoryAdapter([str(tmp_path)]), operation="find_files", query="h3-annual-budget-2026")
    assert [i["name"] for i in result.output["items"]] == ["h3-annual-budget-2026.xlsx"]


def test_the_first_pass_leaves_hidden_and_system_folders_to_the_second(tmp_path):
    (tmp_path / ".hidden").mkdir()
    (tmp_path / ".hidden" / "needle.txt").write_text("x")
    (tmp_path / "Windows").mkdir()
    (tmp_path / "Windows" / "needle2.txt").write_text("x")
    assert system_inventory.walk_search([tmp_path], ["needle"], [], cap=10) == []
    found = system_inventory.walk_search([tmp_path], ["needle"], [], cap=10, include_hidden=True)
    assert sorted(Path(item["path"]).name for item in found) == ["needle.txt", "needle2.txt"]


def test_a_folder_that_cannot_be_opened_is_counted_and_skipped_not_fatal(tmp_path, monkeypatch):
    (tmp_path / "locked").mkdir()
    (tmp_path / "open").mkdir()
    (tmp_path / "open" / "needle.txt").write_text("x")
    real_walk = os.walk

    def walk(top, onerror=None, **kwargs):
        for folder, dirs, files in real_walk(top, onerror=onerror, **kwargs):
            if folder == str(tmp_path) and onerror is not None:
                onerror(PermissionError(13, "Access is denied", str(tmp_path / "locked")))
            yield folder, dirs, files

    monkeypatch.setattr(system_inventory.os, "walk", walk)
    report = system_inventory.WalkReport()
    found = system_inventory.walk_search([tmp_path], ["needle"], [], cap=10, report=report)
    assert [Path(item["path"]).name for item in found] == ["needle.txt"]
    assert report.denied == 1 and report.denied_examples == [str(tmp_path / "locked")] and report.complete is True


def test_a_walk_that_runs_out_of_time_says_it_is_not_complete(tmp_path):
    for n in range(5):
        (tmp_path / f"d{n}").mkdir()
    ticks = iter(range(0, 1000, 10))
    report = system_inventory.WalkReport()
    system_inventory.walk_search([tmp_path], ["needle"], [], cap=10, budget_seconds=25, report=report, clock=lambda: next(ticks))
    assert report.complete is False and 0 < report.folders < 6


def test_nothing_found_says_what_was_looked_through_and_whether_it_was_everything(tmp_path, monkeypatch):
    monkeypatch.setattr(system_inventory, "search_index", lambda *a, **k: [])
    (tmp_path / "a").mkdir()
    (tmp_path / ".b").mkdir()
    result = _run(SystemInventoryAdapter([str(tmp_path)]), operation="find_files", query="nothing-like-this")
    searched = result.output["searched"]
    assert result.output["count"] == 0 and searched["complete"] is True and searched["including_hidden_and_system_folders"] is True
    assert searched["folders"] == 3 and searched["roots"] == [str(tmp_path.resolve())]
    assert "I looked through all 3 folders" in result.output["summary"] and "so it is not in any folder I can read" in result.output["summary"]


def test_an_incomplete_walk_does_not_claim_the_file_is_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(system_inventory, "search_index", lambda *a, **k: [])
    monkeypatch.setattr(system_inventory, "_WALK_FIRST_PASS_SECONDS", -1.0)
    monkeypatch.setattr(system_inventory, "_WALK_DEEP_PASS_SECONDS", -1.0)
    (tmp_path / "a").mkdir()
    result = _run(SystemInventoryAdapter([str(tmp_path)]), operation="find_files", query="nothing-like-this")
    assert result.output["searched"]["complete"] is False
    assert "does not prove it is not there" in result.output["summary"]
