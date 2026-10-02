"""The headline first task, end to end: "organize my Downloads folder".

On a fresh install this could not be done by following the product's own steps:
file access was off, the console's switch for it did not make the file tool
available, and even once it did, a running worker never noticed. This runs the
whole path with a scripted operator (no model, no network, no cost) against the
real worker, policy engine, file tool and admin API, on a real temporary folder:

  ask -> blocked, and told exactly why
  grant the folder through the one-step endpoint
  ask again -> reads freely, asks once before moving anything, then does it
  files are where the plan said they would be

The only thing replaced is the model's decisions; everything that enforces,
executes and records is real.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_control import cli
from agent_control.admin import create_admin_router
from agent_control.config import load_settings
from agent_control.orchestration import TaskWorker
from agent_control.orchestration.signals import requeue_after_approval_decision
from agent_control.schemas import (
    ApprovalStatus,
    OperatorAction,
    OperatorDecision,
    RiskLevel,
    TaskStatus,
)
from agent_control.storage import Database, Repositories
from agent_control.tools.vscode_bridge import VSCodeBridgeStore
from helpers import make_repos

OBJECTIVE = "Organize my Downloads folder by file type"
FILES = ("invoice.pdf", "notes.txt", "photo.jpg", "song.mp3")


class _ScriptedOperator:
    """The model, replaced by a fixed script of decisions."""

    def __init__(self) -> None:
        self.decisions: list[OperatorDecision] = []

    async def decide(self, objective, config_context, history, *, memory_context="", prefer_major=False):
        return self.decisions.pop(0)


def _call(operation: str, risk: RiskLevel, **fields) -> OperatorDecision:
    return OperatorDecision(
        action=OperatorAction.CALL_TOOL,
        tool_name="filesystem.manage",
        tool_input={"operation": operation, **fields},
        risk_level=risk,
    )


class _World:
    def __init__(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)
        for name in ("AGENT_ADMIN_TOKEN", "AGENT_CAPABILITIES", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
            monkeypatch.delenv(name, raising=False)
        (tmp_path / "config").mkdir()
        (tmp_path / "config" / "config.yaml").write_text("{}\n", encoding="utf-8")
        self.downloads = tmp_path / "Downloads"
        self.downloads.mkdir()
        for name in FILES:
            (self.downloads / name).write_text(f"contents of {name}", encoding="utf-8")

        self.repos, self.audit = make_repos(tmp_path)
        self.operator = _ScriptedOperator()
        # The real worker runtime, with only the model swapped for the script.
        self.reloader = cli.WorkerRuntimeReloader(
            lambda: {**cli._worker_kwargs(load_settings(), self.repos, self.audit), "operator": self.operator},
            self.audit,
        )
        self.worker = TaskWorker(self.repos, self.audit, **self.reloader.kwargs, runtime_refresh=self.reloader)

        database = Database(f"sqlite:///{tmp_path / 'admin.db'}")
        database.initialize()
        app = FastAPI()
        app.include_router(
            create_admin_router(load_settings, lambda: Repositories.for_database(database), VSCodeBridgeStore())
        )
        self.api = TestClient(app)

    def organize_manifest(self) -> list[dict]:
        """What organize_plan will propose, computed the same deterministic way."""
        groups = {"pdf": "documents", "txt": "documents", "jpg": "images", "mp3": "audio"}
        return [
            {
                "operation": "move",
                "source": str(self.downloads / name),
                "destination": str(self.downloads / groups[name.rsplit(".", 1)[1]] / name),
            }
            for name in FILES
        ]

    async def run_until_idle(self, task_id: str, limit: int = 12) -> TaskStatus:
        for _ in range(limit):
            await self.worker.process_next()
            status = self.repos.tasks.get(task_id).status
            if status not in {TaskStatus.RECEIVED, TaskStatus.RUNNING, TaskStatus.RETRYING}:
                return status
        return self.repos.tasks.get(task_id).status


@pytest.fixture
def world(tmp_path, monkeypatch) -> _World:
    return _World(tmp_path, monkeypatch)


# ---------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_first_request_is_blocked_and_the_reason_names_the_fix(world) -> None:
    task = world.repos.tasks.create(OBJECTIVE)
    world.operator.decisions = [
        _call("inspect_folder", RiskLevel.LOW, root=str(world.downloads)),
        OperatorDecision(action=OperatorAction.BLOCKED),
    ]

    status = await world.run_until_idle(task.id)

    blocked = world.repos.tasks.get(task.id)
    assert status == TaskStatus.BLOCKED
    assert blocked.metadata["blocked_access"]["evidence"] == "denied"
    assert [g["group"] for g in blocked.metadata["blocked_access"]["groups"]] == ["filesystem"]
    assert "YBM tried to use File system" in blocked.metadata["last_worker_error"]
    for name in FILES:  # nothing was touched
        assert (world.downloads / name).exists()


@pytest.mark.asyncio
async def test_granting_the_folder_then_asking_again_organizes_it_after_one_approval(world) -> None:
    # 1. The first attempt fails exactly as a new user would see it.
    first = world.repos.tasks.create(OBJECTIVE)
    world.operator.decisions = [
        _call("inspect_folder", RiskLevel.LOW, root=str(world.downloads)),
        OperatorDecision(action=OperatorAction.BLOCKED),
    ]
    assert await world.run_until_idle(first.id) == TaskStatus.BLOCKED

    # 2. One action grants the folder, asking before changes. The worker is still
    #    the same running process; nothing is restarted.
    response = world.api.post(
        "/admin/api/setup/work-folders", json={"folders": [str(world.downloads)], "mode": "write_access"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["file_access"] == "write_access"

    # 3. "Try again": the same request, now with access.
    retry = world.repos.tasks.create(OBJECTIVE)
    world.operator.decisions = [
        _call("inspect_folder", RiskLevel.LOW, root=str(world.downloads)),
        _call("organize_plan", RiskLevel.LOW, root=str(world.downloads)),
        _call("apply_manifest", RiskLevel.HIGH, root=str(world.downloads), manifest=world.organize_manifest()),
        OperatorDecision(action=OperatorAction.DONE, final_answer="Sorted the four files into documents, images and audio."),
    ]

    status = await world.run_until_idle(retry.id)

    # Looking was free; changing anything stopped for a decision, once.
    assert status == TaskStatus.AWAITING_APPROVAL
    approvals = world.repos.approvals.list_for_task(retry.id)
    assert len(approvals) == 1
    assert approvals[0].status == ApprovalStatus.PENDING
    assert approvals[0].capability.value == "filesystem.write"
    for name in FILES:  # still untouched while waiting
        assert (world.downloads / name).exists()

    world.repos.approvals.set_status(approvals[0].id, ApprovalStatus.APPROVED)
    requeue_after_approval_decision(world.repos, retry.id)
    final = await world.run_until_idle(retry.id)

    assert final == TaskStatus.COMPLETED, world.repos.tasks.get(retry.id).metadata.get("last_worker_error")
    for name, folder in (("invoice.pdf", "documents"), ("notes.txt", "documents"), ("photo.jpg", "images"), ("song.mp3", "audio")):
        assert (world.downloads / folder / name).read_text(encoding="utf-8") == f"contents of {name}"
        assert not (world.downloads / name).exists()
    assert len(world.repos.approvals.list_for_task(retry.id)) == 1  # never asked twice


@pytest.mark.asyncio
async def test_read_only_access_lets_it_look_but_never_move_anything(world, tmp_path) -> None:
    world.api.post("/admin/api/setup/work-folders", json={"folders": [str(world.downloads)], "mode": "read_only"})
    task = world.repos.tasks.create(OBJECTIVE)
    world.operator.decisions = [
        _call("inspect_folder", RiskLevel.LOW, root=str(world.downloads)),
        _call("apply_manifest", RiskLevel.HIGH, root=str(world.downloads), manifest=world.organize_manifest()),
        OperatorDecision(action=OperatorAction.BLOCKED),
    ]

    await world.run_until_idle(task.id)

    history = world.repos.tasks.get(task.id).metadata["operator_history"]
    assert history[0]["status"] == "succeeded"  # reading worked
    assert history[1]["status"] == "denied" and history[1]["error"] == "capability_disabled"  # moving did not
    for name in FILES:
        assert (world.downloads / name).exists()


@pytest.mark.asyncio
async def test_a_folder_outside_the_granted_one_is_still_out_of_reach(world, tmp_path) -> None:
    other = tmp_path / "Private"
    other.mkdir()
    (other / "secret.txt").write_text("keep out", encoding="utf-8")
    world.api.post("/admin/api/setup/work-folders", json={"folders": [str(world.downloads)]})
    task = world.repos.tasks.create("Look at my private folder")
    world.operator.decisions = [
        _call("inspect_folder", RiskLevel.LOW, root=str(other)),
        OperatorDecision(action=OperatorAction.BLOCKED, reason="That folder is not one I may use."),
    ]

    await world.run_until_idle(task.id)

    history = world.repos.tasks.get(task.id).metadata["operator_history"]
    assert history[0]["status"] != "succeeded"
    assert "keep out" not in json.dumps(history)
    assert (other / "secret.txt").read_text(encoding="utf-8") == "keep out"
