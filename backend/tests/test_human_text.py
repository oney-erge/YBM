"""What a person reads in a chat: paths, ids and listings in plain language."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agent_control import human_text
from agent_control.human_text import friendly_path, humanize, progress_phrase


# A Windows-style path (C:\...) that does not exist is only recognised as one on Windows; a POSIX path is rewritten
# only when it exists, which the tests below also cover on every platform.
windows_only = pytest.mark.skipif(os.name != "nt", reason="a missing C:\ path is only rewritten as a Windows path")


@pytest.fixture
def home(tmp_path, monkeypatch):
    user = tmp_path / "Users" / "sam"
    user.mkdir(parents=True)
    monkeypatch.setattr(human_text, "_home", lambda: user)
    return user


def _windows_style(path: Path) -> str:
    return str(path)


def test_friendly_path_names_known_folders(home):
    (home / "Downloads").mkdir()
    assert friendly_path(str(home / "Downloads" / "resume.pdf")) == "resume.pdf (in Downloads)"


def test_friendly_path_names_onedrive_and_keeps_inner_folders(home):
    inner = home / "OneDrive" / "Career" / "CV"
    assert friendly_path(str(inner / "me.docx")) == "me.docx (in OneDrive > Career > CV)"


def test_friendly_path_shortens_deep_folders(home):
    deep = home / "Desktop" / "a" / "b" / "c" / "d"
    assert friendly_path(str(deep / "notes.txt")) == "notes.txt (in Desktop > … > c > d)"


def test_friendly_path_hides_the_task_workspace(home):
    path = r"C:\for fun\YBM\.agent_control\workspaces\task_4df3f037de7a4b9da38998125ed3d642\index.html"
    assert friendly_path(path) == "index.html (in the project folder I made for this task)"


def test_friendly_path_for_the_user_folder_itself(home):
    assert friendly_path(str(home)) == "your user folder"


def test_humanize_keeps_the_sentence_after_an_existing_path(home):
    target = home / "Documents" / "my notes.txt"
    target.parent.mkdir()
    target.write_text("hi")
    out = humanize(f"I read {target} and it has 3 lines.")
    assert out == "I read my notes.txt (in Documents) and it has 3 lines."


@windows_only
def test_humanize_trims_a_missing_file_at_its_extension(home):
    out = humanize(rf"Cannot open {home}\Desktop\gone.txt because it was moved.")
    assert "gone.txt (in Desktop)" in out
    assert "because it was moved." in out
    assert "\\" not in out


def test_humanize_leaves_urls_alone(home):
    text = "It is running at http://127.0.0.1:8891/ now."
    assert humanize(text) == text


def test_humanize_replaces_task_ids():
    out = humanize("There is a blocked task (task_a2f6b7e0e36b4149905db0617cc797d8) from earlier.")
    assert "task_a2f6" not in out
    assert "an earlier task" in out


def test_humanize_turns_a_long_listing_into_a_short_list(home):
    lines = "\n".join(f"- [file] folder\\doc-{n}.pdf ({n * 1000} bytes)" for n in range(30))
    out = humanize("I found 30 matching item(s).\n" + lines)
    assert "[file]" not in out and "bytes" not in out
    assert out.count("\n- ") == 8
    assert "…and 22 more." in out


def test_humanize_explains_known_reason_codes():
    assert "safety settings" in humanize("Reason: risk_exceeds_capability_policy")


def test_humanize_is_idempotent(home):
    text = rf"See {home}\Desktop\a.txt and task_0123456789abcdef"
    once = humanize(text)
    assert humanize(once) == once


def test_humanize_of_empty_text():
    assert humanize("") == ""


@pytest.mark.parametrize(
    ("tool", "operation", "expected"),
    [
        ("filesystem.manage", "search", "looking through your files"),
        ("filesystem.manage", "something_new", "working with your files"),
        ("browser.open", "open", "opening a web page"),
        ("workspace.manage", "launch_static", "starting the app"),
        ("delegate", "", "splitting the work into smaller parts"),
        ("unknown.tool", "x", "working on it"),
    ],
)
def test_progress_phrase(tool, operation, expected):
    assert progress_phrase(tool, operation) == expected


def test_progress_phrase_names_the_coding_assistant():
    assert progress_phrase("coding.agent", "start", {"provider": "claude_code"}) == "working with Claude Code"


@windows_only
def test_a_path_in_backticks_keeps_the_sentence_around_it(home):
    out = humanize(rf"The file was found at `{home}\Downloads\Resume 2025.docx`. It belongs to Jo Doe and is two pages.")
    assert out == "The file was found at `Resume 2025.docx (in Downloads)`. It belongs to Jo Doe and is two pages."


def test_an_existing_path_in_backticks_keeps_the_sentence_around_it(home):
    target = home / "Documents" / "plan v2.txt"
    target.parent.mkdir()
    target.write_text("x")
    out = humanize(f"Saved to `{target}`. Open it later.")
    assert out == "Saved to `plan v2.txt (in Documents)`. Open it later."


@windows_only
def test_a_missing_folder_path_stops_at_sentence_punctuation(home):
    out = humanize(rf"I looked in {home}\Desktop\Old Stuff. Nothing there, so I moved on.")
    assert out.endswith("Nothing there, so I moved on.")
    assert "\\" not in out


@windows_only
def test_a_dotted_folder_name_does_not_end_the_path_early(home):
    out = humanize(rf"See {home}\Desktop\sam.smith\notes.txt for details.")
    assert out == "See notes.txt (in Desktop > sam.smith) for details."


def test_a_path_that_does_not_exist_on_this_computer_is_left_alone(home):
    text = f"It was at {home / 'Desktop' / 'moved-away.txt'} until yesterday."
    assert "(in Desktop)" not in humanize(text) or os.name == "nt"


def test_a_slash_in_ordinary_text_is_not_a_path(home):
    text = "Use either/or, see /etc, and http://127.0.0.1:8891/app/ for 5/6 of it."
    assert humanize(text) == text
