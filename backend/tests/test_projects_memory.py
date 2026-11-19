"""Projects the assistant remembers: hooks find candidates, the notebook is the memory, and the model confirms before it is shown (projects_memory.py)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from agent_control.channels.memory_learning import FactLearner, _Changes, _Note
from agent_control.projects_memory import candidates, confirmed, context_text, find_project, note_to_project, project_context, project_context_sync
from agent_control.schemas import ProjectRecord, utc_now
from helpers import make_repos


def _pool(repos):
    note_to_project(repos, "Backyard pool", ["pool", "backyard", "filter", "contractor"], "decision", "Chose a 14 by 28 foot fiberglass pool over concrete: faster to install, about 55,000 dollars")
    note_to_project(repos, "backyard pool", ["pool", "pump"], "decision", "The filter will be a cartridge filter, not sand, to save water")
    note_to_project(repos, "Backyard pool", [], "question", "Which county permit applies, and does the HOA need to approve it")
    return find_project(repos, "Backyard pool")


def test_a_note_makes_the_project_once_and_the_same_project_is_found_again_by_name_or_by_hooks(tmp_path) -> None:
    repos, _audit = make_repos(tmp_path)
    pool = _pool(repos)
    assert len(repos.projects.list_all()) == 1  # "Backyard pool" and "backyard pool" are one
    assert set(pool.hooks) >= {"pool", "backyard", "filter", "contractor", "pump"}
    assert find_project(repos, "Pool in the backyard", ["pool"]) is not None  # a different phrasing still finds it by what it shares
    assert find_project(repos, "Humanoid robot", ["robot"]) is None
    assert [n["kind"] for n in repos.project_notes.list(pool.id)] == ["question", "decision", "decision"]  # newest first
    assert repos.episodes.search("cartridge filter")[0]["project_id"] == pool.id  # the notebook is searchable with everything else said


def test_hooks_only_produce_candidates_and_a_project_that_is_done_is_not_one(tmp_path) -> None:
    repos, _audit = make_repos(tmp_path)
    pool = _pool(repos)
    note_to_project(repos, "Humanoid robot", ["robot", "actuators", "servo"], "step", "Compared three servo brands")
    assert [p.name for _, p in candidates(repos, "back to the pool, which filter did we pick?")][:1] == ["Backyard pool"]
    assert candidates(repos, "what is 12 times 12") == []
    repos.projects.set_memory(pool.id, status="done")
    assert all(p.name != "Backyard pool" for _, p in candidates(repos, "the pool filter"))


@pytest.mark.asyncio
async def test_without_a_decision_model_a_project_needs_two_of_its_words_and_with_one_the_model_confirms(tmp_path) -> None:
    repos, _audit = make_repos(tmp_path)
    _pool(repos)
    assert await confirmed(repos, "what did we decide about the pool filter?", None)  # two words: the pool and the filter
    assert await confirmed(repos, "back to the pool", None)  # a word of the project's own name
    assert not await confirmed(repos, "which contractor should I call?", None)  # a hook alone is a candidate, not a project

    class _Off:
        mode = "off"

    assert await confirmed(repos, "back to the pool", _Off())
    assert not await confirmed(repos, "what is 12 times 12", _Off())

    class _Cascade:
        mode = "cascade"
        base_url = "http://127.0.0.1:9"  # nothing listens: the model cannot be reached, so the rule above applies
        timeout_seconds = 1.0
        profile = None
        api_token_env = None

    assert await confirmed(repos, "back to the pool", _Cascade())


@pytest.mark.asyncio
async def test_the_notebook_goes_into_the_context_with_when_the_project_was_last_touched(tmp_path) -> None:
    repos, _audit = make_repos(tmp_path)
    pool = _pool(repos)
    text = await project_context(repos, "back to the pool, where were we?", None)  # one word of its name is enough without a model
    assert "Backyard pool" in text and "last touched today" in text and "cartridge filter" in text and "fiberglass" in text
    assert text.index("fiberglass") < text.index("cartridge filter")  # oldest first, so it reads in order
    assert "Which county permit" in text and "offer to pick the project up again" in text
    assert await project_context(repos, "what is 12 times 12", None) == ""
    # three weeks later
    old = utc_now() - timedelta(days=21)
    with repos.project_notes.database.connect() as connection:
        connection.execute("UPDATE project_notes SET at = ? WHERE project_id = ?", (old.isoformat(), pool.id))
    assert "weeks ago" in context_text(repos, [pool])
    assert project_context_sync(repos, "back to the pool filter").startswith("\n\nProjects the person has been working on")
    assert project_context_sync(repos, "ok") == ""
    assert project_context_sync(repos, "back to the pool, where were we?").startswith("\n\nProjects")


@pytest.mark.asyncio
async def test_what_the_owner_says_about_something_they_are_building_becomes_notebook_entries_through_the_learner(tmp_path) -> None:
    repos, _audit = make_repos(tmp_path)

    class _Provider:
        def __init__(self):
            self.asked = []

        async def generate_structured(self, system, user, schema, **kwargs):
            self.asked.append(user)
            return _Changes(notes=[
                _Note(project="Humanoid robot", hooks=["robot", "humanoid", "servo"], kind="decision", text="Will start with a 12-servo torso, budget about 3,000 dollars"),
                _Note(project="Wifi", hooks=["wifi"], kind="fact", text="The wifi password is hunter2"),  # a secret is never kept
            ])

    provider = _Provider()
    kept = await FactLearner(repos, provider).learn("I am building a humanoid robot and we will start with a 12-servo torso, about 3000 dollars", conversation_id="c1")
    assert kept == ["noted in Humanoid robot: decision: Will start with a 12-servo torso, budget about 3,000 dollars"]
    robot = find_project(repos, "Humanoid robot")
    assert robot is not None and "servo" in robot.hooks and len(repos.projects.list_all()) == 1
    assert "The projects already known:\n(none yet)" in provider.asked[0]
    await FactLearner(repos, provider).learn("We also picked the actuators for the humanoid robot, I think", conversation_id="c1")
    assert "- Humanoid robot (" in provider.asked[1]  # the model is shown the known projects so it reuses the name


def test_the_consoles_own_update_of_a_project_leaves_what_the_assistant_remembers(tmp_path) -> None:
    repos, _audit = make_repos(tmp_path)
    pool = _pool(repos)
    repos.projects.update(ProjectRecord(id=pool.id, name="Backyard pool", instructions="Keep receipts in the Pool folder", created_at=pool.created_at))
    again = repos.projects.get(pool.id)
    assert again.instructions == "Keep receipts in the Pool folder" and "filter" in again.hooks and len(repos.project_notes.list(pool.id)) == 3
