"""What happens to a tool call before it runs is one ordered list (orchestration/call_policy.py)."""

from __future__ import annotations

import pytest

from agent_control.orchestration.call_policy import REWRITES, Refusal, vet_call
from agent_control.schemas import TaskRecord

DEFS: dict = {}


def _task(text: str, **metadata) -> TaskRecord:
    return TaskRecord(objective=text, metadata={"original_message_text": text, **metadata})


def test_the_rewrites_run_in_this_order():
    assert [label for label, _ in REWRITES] == [
        "artifact delivery in order",
        "search by content when asked",
        "dates in a listing when asked for newest or oldest",
        "path relative to a listed folder",
        "add to the end of a file when asked",
        "overwrite a file when asked to change it",
        "apply the manifest the plan made",
        "summarize a PDF when asked what it says",
        "schedule id from this task",
        "keep the user's URLs in a schedule",
        "the one file in the folder the user named",
        "a read of a file changed since it was read",
        "coding agent defaults for this task",
        "coding agent in the workspace the user named",
        "a made-up password for a new account becomes the vault's",
    ]


def test_a_call_that_shows_none_of_the_known_habits_goes_through_unchanged():
    call = {"operation": "inspect_folder", "root": "C:/work"}
    vetted = vet_call(_task("What is in the folder?"), "filesystem.manage", call, [], DEFS)
    assert vetted.refusal is None and vetted.tool_name == "filesystem.manage" and vetted.tool_input == call
    assert vetted.requested_name == "filesystem.manage" and vetted.requested_input == call


@pytest.mark.parametrize("text, tool, call, kind", [
    ("Delete all the files in the junk folder", "filesystem.manage", {"operation": "organize_plan", "root": "C:/junk"}, "simulated_delete"),
    ("Tidy up my downloads", "coding.agent", {"operation": "start", "provider": "codex", "objective": "x"}, "unrequested_coding_agent"),
    ("Remember that my cat is Miso", "persona.manage", {"operation": "update", "content": "cat is Miso"}, "blind_persona_replace"),
])
def test_each_kind_of_call_that_must_not_run_is_refused(text, tool, call, kind):
    vetted = vet_call(_task(text), tool, call, [], DEFS)
    assert vetted.refusal == Refusal(kind)


def test_a_delete_that_also_asks_to_organise_keeps_its_organising_call():
    vetted = vet_call(_task("Delete the junk and organise the rest"), "filesystem.manage", {"operation": "organize_plan", "root": "C:/junk"}, [], DEFS)
    assert vetted.refusal is None


def test_a_coding_agent_the_user_named_is_not_refused():
    vetted = vet_call(_task("Use Codex to write a script"), "coding.agent", {"operation": "start", "provider": "codex", "objective": "x"}, [], DEFS)
    assert vetted.refusal is None


def test_a_path_from_an_earlier_turn_is_refused_for_a_request_to_remember_and_the_refusal_names_it():
    task = _task(
        "Remember that my cat is Miso", orchestration_intent={"route": "configuration", "reasoning": "x"},
        memory_context="User asked: read D:\old\notes.txt\nRecent turns:\n- user: read D:\old\notes.txt",
    )
    vetted = vet_call(task, "filesystem.manage", {"operation": "read_file", "path": "D:\old\notes.txt"}, [], DEFS)
    assert vetted.refusal == Refusal("ungrounded_path", ("D:\old\notes.txt",))
    assert vetted.requested_input == {"operation": "read_file", "path": "D:\old\notes.txt"}
    # once is enough: the same path is let through the second time
    again = vet_call(_task(task.objective, **{**task.metadata, "ungrounded_paths_seen": ["D:\old\notes.txt"]}), "filesystem.manage",
                     {"operation": "read_file", "path": "D:\old\notes.txt"}, [], DEFS)
    assert again.refusal is None


def test_a_refusal_comes_before_any_rewrite_can_hide_what_was_asked(tmp_path):
    existing = tmp_path / "a.txt"
    existing.write_text("old", encoding="utf-8")
    # "delete ... on a new line" would trigger the append rewrite on a write; the refusal on the organise call must still win first
    vetted = vet_call(_task("Delete the old lines, add pears on a new line"), "filesystem.manage", {"operation": "organize_plan", "root": str(tmp_path)}, [], DEFS)
    assert vetted.refusal == Refusal("simulated_delete")


def test_the_rewrites_see_the_request_through_the_one_set_of_facts(tmp_path):
    existing = tmp_path / "a.txt"
    existing.write_text("old", encoding="utf-8")
    write = {"operation": "write_text_file", "path": str(existing), "content": "pears"}
    assert vet_call(_task("Add pears on a new line"), "filesystem.manage", write, [], DEFS).tool_input.get("append") is True
    assert vet_call(_task("Change it to say pears"), "filesystem.manage", write, [], DEFS).tool_input.get("overwrite") is True
    listing = {"operation": "inspect_folder", "root": str(tmp_path)}
    assert vet_call(_task("Which file is the newest?"), "filesystem.manage", listing, [], DEFS).tool_input.get("with_dates") is True


MOVE_MANIFEST = [{"operation": "move", "source": "C:/x/a.txt", "destination": "C:/x/documents/a.txt"}]
RENAME_MANIFEST = [{"operation": "rename", "source": "C:/x/a.txt", "destination": "C:/x/2026-a.txt"}]


@pytest.mark.parametrize("text", ["Add the prefix 2026- to every file name in C:/x.", "Rename every file in C:/x so it ends with -old.", "Put a suffix _v2 on all the file names in C:/x"])
def test_a_request_for_new_names_does_not_get_files_moved_into_folders(text):
    """Sealed Verdict 2 (fin2_s1): an organize plan was applied for a request to add a prefix, four files were moved into a new folder, and only the renaming was reported."""
    for call in ({"operation": "organize_plan", "root": "C:/x", "strategy": "by_type"}, {"operation": "apply_manifest", "root": "C:/x", "manifest": MOVE_MANIFEST}):
        assert vet_call(_task(text), "filesystem.manage", call, [], DEFS).refusal == Refusal("unrequested_move"), (text, call["operation"])
    renamed = vet_call(_task(text), "filesystem.manage", {"operation": "apply_manifest", "root": "C:/x", "manifest": RENAME_MANIFEST}, [], DEFS)
    assert renamed.refusal is None  # the rename in place is what was asked for


@pytest.mark.parametrize("text", ["Organize C:/x and add the prefix 2026- to the names", "Sort the files in C:/x into folders by type", "Move the photos to C:/x/pics and rename them"])
def test_a_request_that_also_asks_to_organize_or_move_keeps_those_calls(text):
    for call in ({"operation": "organize_plan", "root": "C:/x"}, {"operation": "apply_manifest", "root": "C:/x", "manifest": MOVE_MANIFEST}):
        assert vet_call(_task(text), "filesystem.manage", call, [], DEFS).refusal is None, (text, call["operation"])
