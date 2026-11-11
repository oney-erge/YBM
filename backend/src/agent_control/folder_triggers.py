"""Bounded folder polling feeding the existing workflow replay pipeline."""
from __future__ import annotations

from datetime import datetime
import hashlib
import os
from pathlib import Path
import stat

from agent_control.config import AppSettings
from agent_control.policy import PolicyEngine
from agent_control.schemas import AuditEventType, Capability, RiskLevel, ScheduleRecord, TaskRecord, ToolCallRequest
from agent_control.storage import AuditLogger, Repositories

MAX_FOLDER_ENTRIES = 500
MAX_FILE_BYTES = 10 * 1024 * 1024
_TEMP_SUFFIXES = {'.tmp', '.part', '.crdownload', '.download'}


def validate_watch_folder(raw: str, settings: AppSettings) -> Path:
    folder = Path(raw).expanduser().resolve(strict=True)
    roots = [Path(root).expanduser().resolve() for root in (
        settings.adapters.workspace.root_dir, settings.storage.artifact_dir,
        *settings.adapters.computer_use.allowed_roots,
    )]
    if not folder.is_dir() or not any(folder == root or root in folder.parents for root in roots):
        raise ValueError('Choose a folder within the configured file-access roots.')
    decision = PolicyEngine(settings).evaluate(ToolCallRequest(
        task_id='folder-observation', tool_name='filesystem.manage', capability=Capability.FILESYSTEM_READ,
        risk_level=RiskLevel.LOW, scope_target=str(folder), input={'operation': 'inspect_folder', 'path': str(folder)},
    ))
    if not decision.allowed:
        raise ValueError(f'Folder observation requires permitted read access without a per-read approval: {decision.reason}.')
    return folder


def file_signature(info: os.stat_result) -> str:
    # On Windows Python's path stat and fstat disagree on ctime after writes.
    # Device/inode/size/mtime are consistent across both observation methods.
    change_time = info.st_ctime_ns if os.name != 'nt' else 0
    return f'{info.st_dev}:{info.st_ino}:{info.st_size}:{info.st_mtime_ns}:{change_time}'


def snapshot(folder: Path, suffixes: list[str], excluded: set[str] | None = None) -> dict[str, str]:
    files: dict[str, str] = {}
    with os.scandir(folder) as entries:
        for index, entry in enumerate(entries):
            if index >= MAX_FOLDER_ENTRIES:
                raise ValueError(f'Folder contains more than {MAX_FOLDER_ENTRIES} entries; choose a smaller inbox.')
            path = folder / entry.name
            if entry.name.startswith(('.', '~$')) or path.suffix.lower() in _TEMP_SUFFIXES:
                continue
            if suffixes and path.suffix.lower() not in suffixes:
                continue
            try:
                # Windows DirEntry.stat omits device/inode identity; use the
                # same path stat as the later open-handle identity check.
                info = path.stat(follow_symlinks=False)
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                continue  # Never follow symlinks or Windows reparse points.
            if not 0 < info.st_size <= MAX_FILE_BYTES or str(path) in (excluded or set()):
                continue
            if path.resolve() != path:
                continue
            files[str(path)] = file_signature(info)
    return files


def _stable_digest(path: Path, signature: str) -> str | None:
    # Recheck identity on the open handle; replacement between scan and open
    # must not turn a scoped file into a symlink or a different file version.
    if path.is_symlink() or file_signature(path.stat(follow_symlinks=False)) != signature:
        return None
    with path.open('rb') as stream:
        if file_signature(os.fstat(stream.fileno())) != signature:
            return None
        data = stream.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES or file_signature(os.fstat(stream.fileno())) != signature:
            return None
    return hashlib.sha256(data).hexdigest()


def poll_folder_watch(
    schedule: ScheduleRecord, repositories: Repositories, audit: AuditLogger, settings: AppSettings, now: datetime,
) -> TaskRecord | None:
    from agent_control.orchestration.workflows import instantiate_plan

    trigger = schedule.metadata['folder_trigger']
    folder = validate_watch_folder(trigger['folder'], settings)
    if str(folder) != trigger['folder']:
        raise ValueError('Watched folder identity changed; recreate this automation.')
    files = snapshot(folder, trigger['suffixes'], repositories.folder_watches.output_paths())
    pending = repositories.folder_watches.observe(schedule.id, files, now)
    previous = repositories.tasks.get(schedule.last_task_id) if schedule.last_task_id else None
    if previous and previous.status.value not in {'completed', 'failed', 'blocked', 'cancelled'}:
        return None
    for item in pending[:20]:
        if (now - datetime.fromisoformat(item['stable_since'])).total_seconds() < trigger['settle_seconds']:
            continue
        path = Path(item['path'])
        # A capability's scope/pattern policy can be narrower than its folder.
        decision = PolicyEngine(settings).evaluate(ToolCallRequest(
            task_id='folder-observation', tool_name='filesystem.manage', capability=Capability.FILESYSTEM_READ,
            risk_level=RiskLevel.LOW, scope_target=str(path), input={'operation': 'read_file', 'path': str(path)},
        ))
        if not decision.allowed:
            raise ValueError(f'Read access to a changed file is unavailable: {decision.reason}.')
        digest = _stable_digest(path, item['signature'])
        if digest is None:
            continue
        if digest == item['delivered_digest']:
            repositories.folder_watches.acknowledge(schedule.id, str(path), item['signature'], digest)
            continue
        values = {**schedule.metadata['workflow_run_values'], trigger['file_parameter']: str(path)}
        plan = instantiate_plan(schedule.metadata['workflow_plan_template'], values)
        conversation = repositories.conversations.get_or_create(schedule.source_channel, schedule.source_chat_id or 'local')
        task = TaskRecord(objective=f'{schedule.objective}: {path.name}', conversation_id=conversation, metadata={
            **{key: value for key, value in schedule.metadata.items() if key not in {
                'folder_trigger', 'workflow_plan_template', 'last_evaluated_task_id', 'consecutive_failures', 'watch_error',
            }},
            'replay_plan': plan, 'workflow_run_values': values, 'source_schedule_id': schedule.id,
            'source_channel': schedule.source_channel.value, 'source_chat_id': schedule.source_chat_id,
            'folder_trigger_event': {'folder': str(folder), 'path': str(path), 'sha256': digest, 'signature': item['signature']},
        })
        if repositories.folder_watches.enqueue(schedule.id, task, {**item, 'sha256': digest}, now):
            audit.append(AuditEventType.TASK_CREATED, actor='folder_trigger', task_id=task.id,
                         payload={'schedule_id': schedule.id, 'path': str(path), 'sha256': digest})
            return task
    return None
