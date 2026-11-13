from datetime import timedelta
from pathlib import Path
from xml.etree import ElementTree
from zipfile import ZipFile

from fastapi.testclient import TestClient
import pytest

from agent_control.scheduler import run_scheduler_once
from agent_control.schemas import ScheduleRecord, TaskStatus, utc_now
from project_system import make_system


@pytest.mark.asyncio
async def test_project_to_artifacts_to_scheduled_replay_requires_fresh_approval(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AGENT_ADMIN_TOKEN", raising=False)
    app, repos, audit, worker, operator = make_system(tmp_path / "work")
    with TestClient(app) as client:
        project = client.post("/admin/api/projects", json={"name": "Proposals", "instructions": "Keep source evidence."}).json()["project"]
        task = client.post("/admin/api/chat/messages", json={"text": "Compare proposals", "project_id": project["id"]}).json()["task"]
        # A queued task retains its original snapshot after editing the project.
        client.put(f"/admin/api/projects/{project['id']}", json={"name": "Proposals", "instructions": "Revised criteria."})
        waiting = await worker.process_task(task["id"])
        assert waiting.status == TaskStatus.AWAITING_APPROVAL
        assert "Keep source evidence." in operator.contexts[-1]
        assert "Revised criteria." not in operator.contexts[-1]
        assert not (tmp_path / "work/comparison.xlsx").exists()
        approval = client.get("/admin/api/approvals").json()["approvals"][0]["approval"]
        assert client.post(f"/admin/api/approvals/{approval['id']}/decide", json={"decision": "approve"}).status_code == 200
        for _ in range(5):
            finished = await worker.process_task(task["id"])
            if finished.status == TaskStatus.COMPLETED:
                break
        assert finished.status == TaskStatus.COMPLETED, finished.metadata
        receipt = client.get(f"/admin/api/tasks/{task['id']}/receipt").json()
        assert receipt["verification"]["verified"] == 2
        effects = {Path(item["value"]).name: item["effect"] for item in receipt["changes"]["files"]}
        assert effects == {"a.txt": "read", "b.txt": "read", "comparison.xlsx": "created", "comparison.md": "created"}
        artifacts = receipt["artifacts"]
        assert len(artifacts) == 2
        workbook = next(Path(item["uri"]) for item in artifacts if item["uri"].endswith(".xlsx"))
        with ZipFile(workbook) as archive:
            sheet = ElementTree.fromstring(archive.read("xl/worksheets/sheet1.xml"))
            assert "10 USD" in "".join(sheet.itertext())
        workflow_response = client.post(f"/admin/api/tasks/{task['id']}/save_workflow", json={"name": "Compare proposals"})
        assert workflow_response.status_code == 200, workflow_response.text
        workflow = workflow_response.json()["workflow"]
        scheduled = client.post(f"/admin/api/workflows/{workflow['id']}/schedule", json={"cadence": "daily", "timezone": "UTC", "project_id": project["id"]})
        assert scheduled.status_code == 200, scheduled.text
        schedule = scheduled.json()["schedule"]
        due = utc_now() + timedelta(days=2)
        runs = await run_scheduler_once(repos, audit, now=due)
        assert len(runs) == 1
        assert runs[0].metadata["source_channel"] == "web"
        assert runs[0].metadata["project_context"]["instructions"] == "Revised criteria."
        assert (await worker.process_task(runs[0].id)).status == TaskStatus.AWAITING_APPROVAL
        assert len(repos.approvals.list_pending()) == 1  # The old approval carries no authority.
        assert await run_scheduler_once(repos, audit, now=due) == []  # No overlap.
        for status in ("paused", "enabled", "deleted"):
            response = client.patch(f"/admin/api/schedules/{schedule['id']}", json={"status": status})
            assert response.status_code == 200, response.text
        assert client.get("/admin/api/schedules").json()["schedules"] == []
        project_tasks = client.get("/admin/api/chat/messages", params={"project_id": project["id"]}).json()["tasks"]
        assert {item["id"] for item in project_tasks} == {task["id"], runs[0].id}
        assert client.get("/admin/api/chat/messages").json()["tasks"] == []


def test_projects_do_not_resume_each_others_questions_and_validate_schedules(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AGENT_ADMIN_TOKEN", raising=False)
    app, repos, _, _, _ = make_system(tmp_path / "work")
    with TestClient(app) as client:
        a = client.post("/admin/api/projects", json={"name": "A"}).json()["project"]["id"]
        b = client.post("/admin/api/projects", json={"name": "B"}).json()["project"]["id"]
        first = client.post("/admin/api/chat/messages", json={"text": "First task", "project_id": a}).json()["task"]
        repos.tasks.update_status(first["id"], TaskStatus.CLARIFYING)
        second = client.post("/admin/api/chat/messages", json={"text": "Second task", "project_id": b}).json()["task"]
        assert first["id"] != second["id"]
        assert repos.tasks.get(first["id"]).status == TaskStatus.CLARIFYING
        resumed = client.post("/admin/api/chat/messages", json={"text": "Answer A", "project_id": a}).json()["task"]
        assert resumed["id"] == first["id"]
        for invalid in ({"cadence": "tomorrow at 9"}, {"cadence": "daily", "timezone": "Wrong/Zone"}):
            assert client.post("/admin/api/workflows/unknown/schedule", json=invalid).status_code == 422
        assert client.get("/admin/api/chat/messages?project_id=missing").status_code == 404


@pytest.mark.asyncio
async def test_comparison_preserves_existing_report_and_refuses_bad_pdf(tmp_path):
    from agent_control.schemas import Capability, ToolCallRequest
    from agent_control.tools.document_manage import DocumentManageAdapter, _verify_document_manage

    _, repos, _, _, _ = make_system(tmp_path)
    task = repos.tasks.create("comparison")
    report = tmp_path / "comparison.md"
    report.write_text("Keep this report", encoding="utf-8")
    adapter = DocumentManageAdapter(repos.artifacts, allowed_roots=[str(tmp_path)])
    request = ToolCallRequest(task_id=task.id, tool_name="document.manage", capability=Capability.FILESYSTEM_WRITE, input={
        "operation": "compare_documents", "source_paths": [str(tmp_path / "a.txt"), str(tmp_path / "b.txt")],
        "criteria": ["cost"], "output_name": str(tmp_path / "comparison.xlsx"),
    })
    result = await adapter.execute(request)
    assert result.status.value == "succeeded", result.error_message
    assert report.read_text() == "Keep this report"
    Path(result.output["report_path"]).write_text("tampered", encoding="utf-8")
    assert not _verify_document_manage(request, result).ok
    Path(result.output["workbook_path"]).unlink()
    assert _verify_document_manage(request, result).verified == 0
    pdf = tmp_path / "bad.pdf"
    pdf.write_text("not a PDF", encoding="utf-8")
    request.input["source_paths"] = [str(pdf), str(tmp_path / "b.txt")]
    assert (await adapter.execute(request)).status.value == "failed"


@pytest.mark.asyncio
async def test_waiting_automations_do_not_starve_others_and_failed_runs_are_counted_once(tmp_path):
    _, repos, audit, _, _ = make_system(tmp_path)
    now = utc_now()
    pending = repos.tasks.create("Waiting for approval")
    repos.tasks.update_status(pending.id, TaskStatus.AWAITING_APPROVAL)
    repos.schedules.create(ScheduleRecord(
        objective="Pending automation", cadence="daily", next_run_at=now - timedelta(days=2),
        last_task_id=pending.id,
    ))
    ready = repos.schedules.create(ScheduleRecord(
        objective="Ready automation", cadence="daily", next_run_at=now - timedelta(days=1),
    ))
    runs = await run_scheduler_once(repos, audit, now=now, limit=1, max_consecutive_failures=1)
    assert len(runs) == 1 and runs[0].metadata["source_schedule_id"] == ready.id
    repos.tasks.update_status(runs[0].id, TaskStatus.FAILED)
    assert await run_scheduler_once(repos, audit, now=now + timedelta(days=2), max_consecutive_failures=1) == []
    assert repos.schedules.get(ready.id).status.value == "paused"
    repos.schedules.resume(ready.id, now)
    retried = await run_scheduler_once(repos, audit, now=now, max_consecutive_failures=1)
    assert len(retried) == 1  # Resuming does not re-count the already handled failure.
    assert repos.schedules.get(ready.id).metadata["consecutive_failures"] == 0
