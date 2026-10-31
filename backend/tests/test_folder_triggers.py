import asyncio
from datetime import timedelta
from pathlib import Path
from xml.etree import ElementTree
from zipfile import ZipFile
import sqlite3

from fastapi.testclient import TestClient
import pytest

from agent_control.scheduler import run_scheduler_once
from agent_control.schemas import Artifact, ArtifactType, Capability, TaskRecord, TaskStatus, TaskWorkflow, utc_now
from agent_control.storage import Repositories
from agent_control.folder_triggers import snapshot
from project_system import make_system


def setup_watch(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv('AGENT_ADMIN_TOKEN', raising=False)
    app, repos, audit, worker, _ = make_system(tmp_path / 'work')
    inbox = tmp_path / 'work/inbox'
    inbox.mkdir()
    (inbox / 'existing.txt').write_text('Cost: 7 USD.', encoding='utf-8')
    source = repos.tasks.create('Source comparison')
    workflow = repos.workflows.create(TaskWorkflow(
        name='Review incoming proposal', source_task_id=source.id, objective_template='Review an incoming proposal',
        parameters=['input_file'], plan=[{'tool_name': 'document.manage', 'tool_input': {
            'operation': 'compare_documents', 'source_paths': ['{{input_file}}', str(tmp_path / 'work/b.txt')],
            'criteria': ['cost'], 'output_name': str(inbox / 'comparison.xlsx'), 'scope_target': str(tmp_path / 'work'),
        }}],
    ))
    return TestClient(app), repos, audit, worker, inbox, workflow


def watch(client, workflow, inbox, **extra):
    response = client.post(f'/admin/api/workflows/{workflow.id}/watch', json={
        'folder': str(inbox), 'file_parameter': 'input_file', 'suffixes': ['.txt', '.md'],
        'settle_seconds': 10, **extra,
    })
    assert response.status_code == 200, response.text
    return response.json()['schedule']


@pytest.mark.asyncio
async def test_file_to_workflow_artifacts_with_restart_stability_and_fresh_approval(monkeypatch, tmp_path):
    client, repos, audit, worker, inbox, workflow = setup_watch(monkeypatch, tmp_path)
    settings = worker.executor.policy.settings
    project = client.post('/admin/api/projects', json={'name': 'Inbox', 'instructions': 'Cite the incoming proposal.'}).json()['project']
    schedule = watch(client, workflow, inbox, project_id=project['id'])
    now = utc_now()

    async def poll(seconds):
        return await run_scheduler_once(repos, audit, now=now + timedelta(seconds=seconds), settings=settings)

    assert await poll(0) == []  # Existing files form a baseline.
    incoming = inbox / 'new.txt'
    incoming.write_text('Cost: 2', encoding='utf-8')
    assert await poll(1) == []
    incoming.write_text('Cost: 20 USD.', encoding='utf-8')
    assert await poll(5) == []
    # A new repository instance observes the same on-disk pending state.
    repos = Repositories.for_database(repos.tasks.database)
    assert await poll(11) == []
    runs = await poll(16)
    assert len(runs) == 1, repos.schedules.get(schedule['id']).metadata.get('watch_error')
    task = runs[0]
    assert task.metadata['replay_plan'][0]['tool_input']['source_paths'][0] == str(incoming)
    assert task.metadata['project_context']['instructions'] == 'Cite the incoming proposal.'
    assert task.metadata['folder_trigger_event']['path'] == str(incoming)
    assert len(task.metadata['folder_trigger_event']['sha256']) == 64
    assert (await worker.process_task(task.id)).status == TaskStatus.AWAITING_APPROVAL
    assert not (inbox / 'comparison.xlsx').exists()
    second = inbox / 'second.txt'
    second.write_text('Cost: 30 USD.', encoding='utf-8')
    assert await poll(17) == []
    assert await poll(30) == []  # Pending files persist while the first run waits.
    pending = client.get('/admin/api/approvals').json()['approvals'][0]['approval']
    assert pending['action_payload']['input']['source_paths'][0] == str(incoming)
    assert client.post(f"/admin/api/approvals/{pending['id']}/decide", json={'decision': 'approve'}).status_code == 200
    for _ in range(6):
        finished = await worker.process_task(task.id)
        if finished.status == TaskStatus.COMPLETED:
            break
    assert finished.status == TaskStatus.COMPLETED, finished.metadata
    receipt = client.get(f'/admin/api/tasks/{task.id}/receipt').json()
    assert receipt['verification']['verified'] == 2
    assert Path(receipt['artifacts'][0]['uri']).exists()
    workbook = next(item['uri'] for item in receipt['artifacts'] if item['uri'].endswith('.xlsx'))
    with ZipFile(workbook) as archive:
        text = ''.join(ElementTree.fromstring(archive.read('xl/worksheets/sheet1.xml')).itertext())
    assert '20 USD' in text  # The changed file, rather than the source run's file, reached the adapter.
    next_runs = await poll(31)
    assert len(next_runs) == 1 and next_runs[0].metadata['folder_trigger_event']['path'] == str(second)
    assert (await worker.process_task(next_runs[0].id)).status == TaskStatus.AWAITING_APPROVAL
    assert len(repos.approvals.list_pending()) == 1
    repos.tasks.update_status(next_runs[0].id, TaskStatus.CANCELLED)
    assert await poll(32) == []  # Generated Markdown is excluded even though it matches the filter.
    assert await poll(50) == []
    assert repos.schedules.get(schedule['id']).last_task_id == next_runs[0].id


@pytest.mark.asyncio
async def test_duplicate_ticks_and_same_content_do_not_duplicate_runs(monkeypatch, tmp_path):
    client, repos, audit, worker, inbox, workflow = setup_watch(monkeypatch, tmp_path)
    settings = worker.executor.policy.settings
    schedule = watch(client, workflow, inbox)
    now = utc_now()
    path = inbox / 'new.txt'
    path.write_text('Cost: 50 USD.', encoding='utf-8')
    await run_scheduler_once(repos, audit, now=now, settings=settings)
    simultaneous = await asyncio.gather(*[
        run_scheduler_once(repos, audit, now=now + timedelta(seconds=11), settings=settings) for _ in range(2)
    ])
    runs = [run for batch in simultaneous for run in batch]
    assert len(runs) == 1
    repos.tasks.update_status(runs[0].id, TaskStatus.COMPLETED)
    path.write_text('Cost: 50 USD.', encoding='utf-8')
    await run_scheduler_once(repos, audit, now=now + timedelta(seconds=15), settings=settings)
    assert await run_scheduler_once(repos, audit, now=now + timedelta(seconds=30), settings=settings) == []
    assert client.patch(f"/admin/api/schedules/{schedule['id']}", json={'status': 'paused'}).status_code == 200
    path.write_text('Cost: 60 USD.', encoding='utf-8')
    assert await run_scheduler_once(repos, audit, now=now + timedelta(seconds=45), settings=settings) == []
    assert client.patch(f"/admin/api/schedules/{schedule['id']}", json={'status': 'enabled'}).status_code == 200
    assert await run_scheduler_once(repos, audit, now=now + timedelta(seconds=46), settings=settings) == []
    assert len(await run_scheduler_once(repos, audit, now=now + timedelta(seconds=60), settings=settings)) == 1


@pytest.mark.asyncio
async def test_scope_and_revoked_access_fail_closed_and_recover(monkeypatch, tmp_path):
    client, repos, audit, worker, inbox, workflow = setup_watch(monkeypatch, tmp_path)
    settings = worker.executor.policy.settings
    outside = tmp_path / 'outside'
    outside.mkdir()
    response = client.post(f'/admin/api/workflows/{workflow.id}/watch', json={
        'folder': str(outside), 'file_parameter': 'input_file',
    })
    assert response.status_code == 400
    schedule = watch(client, workflow, inbox)
    (inbox / 'new.txt').write_text('Cost: 15 USD.', encoding='utf-8')
    generated = inbox / 'generated.md'
    generated.write_text('Cost: 8 USD.', encoding='utf-8')
    other_task = repos.tasks.create('Output from another workflow')
    repos.artifacts.create(Artifact(task_id=other_task.id, type=ArtifactType.DOCUMENT, uri=str(generated)))
    now = utc_now()
    await run_scheduler_once(repos, audit, now=now, settings=settings)
    settings.capabilities[Capability.FILESYSTEM_WRITE].enabled = False
    assert await run_scheduler_once(repos, audit, now=now + timedelta(seconds=20), settings=settings) == []
    assert 'read access' in repos.schedules.get(schedule['id']).metadata['watch_error']
    settings.capabilities[Capability.FILESYSTEM_WRITE].enabled = True
    runs = await run_scheduler_once(repos, audit, now=now + timedelta(seconds=21), settings=settings)
    assert len(runs) == 1
    assert repos.schedules.get(schedule['id']).metadata['watch_error'] is None
    repos.tasks.update_status(runs[0].id, TaskStatus.CANCELLED)
    assert await run_scheduler_once(repos, audit, now=now + timedelta(seconds=40), settings=settings) == []


def test_watch_validates_parameters_filters_and_folder_limits(monkeypatch, tmp_path):
    client, repos, _, _, inbox, workflow = setup_watch(monkeypatch, tmp_path)
    for extra, status in [({'file_parameter': 'unknown'}, 400), ({'suffixes': []}, 422),
                           ({'suffixes': ['../*']}, 422), ({'settle_seconds': 0}, 422)]:
        response = client.post(f'/admin/api/workflows/{workflow.id}/watch', json={
            'folder': str(inbox), 'file_parameter': 'input_file', **extra,
        })
        assert response.status_code == status
    for index in range(500):
        (inbox / f'file-{index}.tmp').touch()
    assert client.post(f'/admin/api/workflows/{workflow.id}/watch', json={
        'folder': str(inbox), 'file_parameter': 'input_file',
    }).status_code == 400
    assert repos.folder_watches.list_enabled() == []


@pytest.mark.asyncio
async def test_failed_enqueue_rolls_back_file_acknowledgement(monkeypatch, tmp_path):
    client, repos, audit, worker, inbox, workflow = setup_watch(monkeypatch, tmp_path)
    settings = worker.executor.policy.settings
    schedule = watch(client, workflow, inbox)
    path = inbox / 'new.txt'
    path.write_text('Cost: 55 USD.', encoding='utf-8')
    now = utc_now()
    files = snapshot(inbox, ['.txt'])
    pending = repos.folder_watches.observe(schedule['id'], files, now)[0]
    duplicate_id = repos.tasks.list_recent()[0].id
    with pytest.raises(sqlite3.IntegrityError):
        repos.folder_watches.enqueue(schedule['id'], TaskRecord(id=duplicate_id, objective='duplicate'),
                                    {**pending, 'sha256': 'test'}, now)
    runs = await run_scheduler_once(repos, audit, now=now + timedelta(seconds=11), settings=settings)
    assert len(runs) == 1  # The failed task insert did not consume the pending file.


def test_snapshot_ignores_temporary_oversized_empty_and_nested_files(tmp_path):
    inbox = tmp_path / 'inbox'
    inbox.mkdir()
    (inbox / '.hidden.txt').write_text('hidden')
    (inbox / 'draft.tmp').write_text('temporary')
    (inbox / 'empty.txt').touch()
    with (inbox / 'large.txt').open('wb') as stream:
        stream.truncate(10 * 1024 * 1024 + 1)
    (inbox / 'nested').mkdir()
    (inbox / 'nested/file.txt').write_text('nested')
    regular = inbox / 'regular.txt'
    regular.write_text('regular')
    assert set(snapshot(inbox, ['.txt', '.tmp'])) == {str(regular)}


def test_snapshot_does_not_follow_symlinks(tmp_path):
    inbox = tmp_path / 'inbox'
    inbox.mkdir()
    outside = tmp_path / 'outside.txt'
    outside.write_text('outside')
    try:
        (inbox / 'linked.txt').symlink_to(outside)
    except OSError:
        pytest.skip('Creating a symlink requires additional privileges on this machine.')
    assert snapshot(inbox, ['.txt']) == {}
