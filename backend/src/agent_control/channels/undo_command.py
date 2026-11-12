"""Undo the last move or rename: "undo that", "put them back".

YBM can reorganise a folder in one request ("sort my Downloads by type"). Until now the only way back was to ask for the reverse and hope
the model remembered where everything was. Each applied move now records where every file came from (`undo_moves` on its task, see
`orchestration/worker.py`), and this command turns that into a new task that moves them back. It goes through the ordinary replay pipeline,
so the move back asks for approval like the move did, and nothing is moved without that. It never overwrites: a file that is no longer
where YBM left it is skipped, and the tool saves a name that is taken as a copy.
"""

from __future__ import annotations

import re
from pathlib import Path

from agent_control.schemas import AuditEventType, ChannelType, InboundMessage, OutboundMessage, TaskRecord, TaskStatus
from agent_control.storage.audit import AuditLogger
from agent_control.storage.repositories import Repositories

_UNDO = re.compile(
    r"^\s*(?:please\s+)?(?:undo|revert|put\s+(?:them|it|everything|those|these)\s+back|move\s+(?:them|it|those|these)\s+back)"
    r"(?:\s+(?:that|it|this|the\s+last\s+(?:move|change|one)|what\s+you\s+(?:just\s+)?did|the\s+move))?\s*[.!]*\s*$",
    re.IGNORECASE,
)
MAX_UNDO_MOVES = 300


def _reply(inbound: InboundMessage, text: str) -> OutboundMessage:
    return OutboundMessage(channel=inbound.channel, chat_id=inbound.chat_id, text=text)


def _last_undoable(repositories: Repositories, inbound: InboundMessage, conversation_id: str | None) -> TaskRecord | None:
    for task in repositories.tasks.list_recent(40):
        mine = (conversation_id and task.conversation_id == conversation_id) or str(task.metadata.get("source_chat_id") or "") == str(inbound.chat_id)
        if mine and task.status == TaskStatus.COMPLETED and task.metadata.get("undo_moves"):
            return task
    return None


def undo_moves_from_output(output: dict) -> dict | None:
    """What to record on the task after `apply_manifest`: where each moved file is now and where it came from."""
    manifest, changed = output.get("manifest"), output.get("changed_paths")
    if output.get("dry_run") or not isinstance(manifest, list) or not isinstance(changed, list) or len(manifest) != len(changed):
        return None
    moves = [
        {"from": str(new), "to": str(item.get("source"))}
        for item, new in zip(manifest, changed)
        if isinstance(item, dict) and str(item.get("operation") or "move") in {"move", "rename"} and item.get("source")
    ]
    if not moves:
        return None
    return {"root": str(output.get("root") or ""), "moves": moves[:MAX_UNDO_MOVES]}


def merge_undo_moves(previous: object, new: dict) -> dict:
    """Add one applied manifest to what the task has moved so far, as where each file is now and where it started.

    A task often moves the same file twice (it sorts a folder by type, then moves the pictures somewhere else). "Undo that" has to
    put the file back where it began, not where it was one step ago, so a file moved again keeps its first origin, and a file moved
    back to where it started drops out. Moves made under a different root start a new record.
    """
    if not isinstance(previous, dict) or previous.get("root") != new.get("root") or not isinstance(previous.get("moves"), list):
        return new
    moves = [dict(move) for move in previous["moves"]]
    for step in new["moves"]:
        earlier = next((move for move in moves if move["from"] == step["to"]), None)
        if earlier is None:
            moves.append(dict(step))
        elif earlier["to"] == step["from"]:
            moves.remove(earlier)
        else:
            earlier["from"] = step["from"]
    return {"root": new["root"], "moves": moves[:MAX_UNDO_MOVES]}


def undo_command_reply(
    repositories: Repositories, audit: AuditLogger, inbound: InboundMessage, conversation_id: str | None
) -> OutboundMessage | None:
    text = (inbound.text or "").strip()
    if not text or len(text) > 80 or inbound.channel != ChannelType.TELEGRAM or not _UNDO.match(text):
        return None
    source = _last_undoable(repositories, inbound, conversation_id)
    if source is None:
        return _reply(inbound, "I don't have a move to undo.")
    record = source.metadata["undo_moves"]
    if source.metadata.get("undo_task_id"):
        return _reply(inbound, "I already put those back.")
    pairs = [m for m in record["moves"] if Path(m["from"]).is_file()]
    if not pairs:
        return _reply(inbound, "Those files are not where I left them any more, so there is nothing to undo.")
    manifest = [{"source": m["from"], "destination": m["to"], "operation": "move", "reason": "undo"} for m in pairs]
    plan = [{"tool_name": "filesystem.manage", "tool_input": {"operation": "apply_manifest", "root": record["root"], "manifest": manifest}}]
    task = repositories.tasks.create(
        f"Undo: put {len(pairs)} file(s) back where they were",
        conversation_id=conversation_id,
        metadata={
            "replay_plan": plan, "undo_of_task_id": source.id, "source_message_id": inbound.id,
            "source_channel": inbound.channel.value, "source_chat_id": inbound.chat_id, "source_sender_id": inbound.sender_id,
            "original_message_text": inbound.text,
        },
    )
    repositories.tasks.update_metadata(source.id, {**source.metadata, "undo_task_id": task.id})
    audit.append(
        AuditEventType.TASK_CREATED, actor=f"{inbound.channel.value}:user:{inbound.sender_id}", task_id=task.id,
        payload={"undo_of": source.id, "files": len(pairs)},
    )
    skipped = len(record["moves"]) - len(pairs)
    note = f" ({skipped} are not where I left them, so I'll leave those.)" if skipped else ""
    return _reply(inbound, f"Putting {len(pairs)} file(s) back where they were{note} I'll ask you to approve the move, then tell you when it's done.")
