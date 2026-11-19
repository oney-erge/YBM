"""Which projects are active, which are parked, and where each stands: the index the model always sees, the page it reads, the files the owner can open (projects_memory.py)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from pydantic import ValidationError

from agent_control import projects_memory as pm
from agent_control.orchestration import TaskWorker
from agent_control.schemas import ProjectRecord, ToolCallRequest, ToolResultStatus, utc_now
from agent_control.tools.contracts import MemoryManageInput
from agent_control.tools.memory_manage import MemoryManageAdapter
from helpers import make_repos


def _repos(tmp_path):
    repos, _ = make_repos(tmp_path)
    pool = pm.note_to_project(repos, "pool", ["backyard"], "decision", "Decided to build a pool in the backyard from scratch.")
    pm.note_to_project(repos, "pool", [], "question", "Which filter: sand or cartridge?")
    pm.note_to_project(repos, "diaper calculator website", [], "step", "Plan a local page that turns a baby's age in days into a diaper count.")
    return repos, pool


def _call(adapter, task_id, **payload):
    return ToolCallRequest(task_id=task_id, tool_name="memory.manage", capability="memory.manage", input=payload, origin="operator")


def test_a_project_not_touched_for_two_weeks_is_parked_and_the_person_decides_done_and_dropped(tmp_path):
    repos, pool = _repos(tmp_path)
    now = utc_now()
    states = {project.name: state for project, state, _ in pm.listing(repos, now)}
    assert states == {"pool": "active", "diaper calculator website": "active"}
    later = now + timedelta(days=15)
    assert {project.name: state for project, state, _ in pm.listing(repos, later)} == {"pool": "parked", "diaper calculator website": "parked"}
    repos.projects.set_memory(pool.id, status="done")
    assert {project.name: state for project, state, _ in pm.listing(repos, now)}["pool"] == "done"


def test_the_index_names_the_active_projects_and_the_parked_ones_and_is_empty_with_none(tmp_path):
    repos, _ = _repos(tmp_path)
    text = pm.index_text(repos)
    assert "## Projects you are holding for this person" in text and "- pool: active, last touched today" in text and "- diaper calculator website: active" in text
    parked = pm.index_text(repos, utc_now() + timedelta(days=20))
    assert "- Parked: " in parked and "pool (" in parked and ": active" not in parked
    empty, _ = make_repos(tmp_path / "empty")
    assert pm.index_text(empty) == ""


def test_a_project_the_model_gave_no_words_for_still_gets_words_that_point_to_it(tmp_path):
    repos, _ = _repos(tmp_path)
    robot = pm.note_to_project(repos, "humanoid robot", [], "step", "Asked for the steps and the parts to buy for an aluminium frame.")
    assert {"humanoid", "robot"} <= set(robot.hooks) and "aluminium" in robot.hooks and len(robot.hooks) <= 6
    assert pm.derive_hooks("the pool", "") == ["pool"]


def test_a_project_page_says_where_it_stands_and_what_was_decided_and_what_is_open(tmp_path):
    repos, pool = _repos(tmp_path)
    page = pm.render_project(repos, pool)
    assert page.startswith("# pool") and "Status: active (last touched today)." in page
    assert "## Where we are" in page and "Decision: Decided to build a pool in the backyard from scratch." in page and "Which filter: sand or cartridge?" in page
    assert "## Decisions" in page and "## Open questions" in page and "backyard" in page
    assert "Nothing has been written down" in pm.render_project(repos, repos.projects.create(ProjectRecord(name="empty one")))


def test_a_project_is_found_by_its_name_or_by_words_that_point_to_it(tmp_path):
    repos, _ = _repos(tmp_path)
    assert pm.describe(repos, "pool")[0].name == "pool"
    assert pm.describe(repos, "the diaper site")[0].name == "diaper calculator website"
    assert pm.describe(repos, "quantum chess") is None


def test_the_readable_copy_is_written_and_rewritten_and_never_breaks_a_task(tmp_path):
    repos, _ = _repos(tmp_path / "db")
    repos.memory_facts.create(__import__("agent_control.schemas", fromlist=["MemoryFact"]).MemoryFact(category="life", content="Lives in Pearland, Texas"))
    root = tmp_path / "memory"
    written = pm.write_files(repos, root)
    names = sorted(str(path.relative_to(root)).replace("\\", "/") for path in written)
    assert names == ["INDEX.md", "about-you.md", "projects/diaper-calculator-website/PROJECT.md", "projects/pool/PROJECT.md"]
    assert "[pool](projects/pool/PROJECT.md): active" in (root / "INDEX.md").read_text(encoding="utf-8")
    assert "Lives in Pearland, Texas" in (root / "about-you.md").read_text(encoding="utf-8")
    assert "Which filter" in (root / "projects" / "pool" / "PROJECT.md").read_text(encoding="utf-8")
    blocked = tmp_path / "a-file"
    blocked.write_text("not a folder", encoding="utf-8")
    assert pm.write_files(repos, blocked / "inside") == []  # cannot be written: nothing raised


def test_tidying_keeps_one_of_a_note_written_again_and_again(tmp_path):
    repos, pool = _repos(tmp_path / "db")
    for _ in range(3):
        repos.project_notes.add(pool.id, "decision", "Decided to build a pool in the backyard from scratch.")
    result = pm.tidy(repos, tmp_path / "memory")
    assert result["duplicates_removed"] == 3 and result["files_written"] == 4
    assert sum(1 for n in repos.project_notes.list(pool.id) if n["text"].startswith("Decided to build a pool")) == 1


@pytest.mark.asyncio
async def test_the_model_can_list_projects_read_one_and_record_the_persons_word(tmp_path):
    repos, pool = _repos(tmp_path / "db")
    adapter = MemoryManageAdapter(repos, tmp_path / "memory")
    task = repos.tasks.create("which projects are we working on?")
    listed = await adapter.execute(_call(adapter, task.id, operation="projects"))
    summary = listed.output["summary"]
    assert listed.status == ToolResultStatus.SUCCEEDED and "Active (2):" in summary and "- pool, last touched today" in summary and "words: " in summary
    one = await adapter.execute(_call(adapter, task.id, operation="project", name="the pool"))
    assert "Decision: Decided to build a pool" in one.output["summary"] and one.output["project_id"] == pool.id
    done = await adapter.execute(_call(adapter, task.id, operation="set_project", name="pool", status="done", hooks=["Backyard Pool"]))
    assert done.status == ToolResultStatus.SUCCEEDED
    again = (await adapter.execute(_call(adapter, task.id, operation="projects"))).output["summary"]
    assert "Done (1):" in again and "Active (1):" in again and "backyard pool" in again
    missing = await adapter.execute(_call(adapter, task.id, operation="project", name="quantum chess"))
    assert missing.status == ToolResultStatus.FAILED and "no project matches" in missing.error_message


def test_the_contract_asks_for_a_name_and_for_something_to_set():
    assert MemoryManageInput(operation="project", name="pool").name == "pool"
    with pytest.raises(ValidationError, match="requires 'name'"):
        MemoryManageInput(operation="project")
    with pytest.raises(ValidationError, match="requires 'status'"):
        MemoryManageInput(operation="set_project", name="pool")
    assert MemoryManageInput(operation="projects").operation == "projects"


def test_the_index_is_part_of_what_the_model_always_sees(tmp_path):
    repos, _ = _repos(tmp_path)
    task = repos.tasks.create("What projects are active?", metadata={"original_message_text": "What projects are active?"})
    seen = TaskWorker(repos, make_repos(tmp_path / "x")[1])._situation(task)
    assert "## Projects you are holding for this person" in seen and "- pool: active" in seen
