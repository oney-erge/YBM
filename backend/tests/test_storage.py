from __future__ import annotations

from datetime import timedelta

from agent_control.schemas import (
    ApprovalRequest,
    ApprovalStatus,
    AuditEventType,
    Capability,
    ChannelType,
    InboundMessage,
    MessageKind,
    RiskLevel,
    TaskStatus,
    utc_now,
)
from agent_control.storage import AuditLogger, Database, Repositories


def test_task_lifecycle_and_audit(tmp_path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'agent.db'}")
    database.initialize()
    repos = Repositories.for_database(database)
    audit = AuditLogger(repos.audit)

    conversation_id = repos.conversations.get_or_create(ChannelType.TELEGRAM, "123")
    message = InboundMessage(
        channel=ChannelType.TELEGRAM,
        kind=MessageKind.TEXT,
        sender_id="42",
        chat_id="123",
        text="Build a todo app",
    )
    repos.messages.create(message, conversation_id)
    task = repos.tasks.create(message.text or "", conversation_id)

    audit.append(AuditEventType.TASK_CREATED, actor="test", task_id=task.id, payload={"token": "secret"})
    updated = repos.tasks.update_status(task.id, TaskStatus.PAUSED)
    audit.task_state_changed("test", task.id, task.status, updated.status)

    events = repos.audit.list_for_task(task.id)

    assert updated.status == TaskStatus.PAUSED
    assert events[0].payload["token"] == "***"
    assert events[1].payload == {"old_status": "received", "new_status": "paused"}


def test_message_repository_duplicate_create_is_idempotent(tmp_path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'agent.db'}")
    database.initialize()
    repos = Repositories.for_database(database)

    conversation_id = repos.conversations.get_or_create(ChannelType.TELEGRAM, "123")
    message = InboundMessage(
        id="telegram_1",
        channel=ChannelType.TELEGRAM,
        kind=MessageKind.TEXT,
        sender_id="42",
        chat_id="123",
        text="Build a todo app",
    )

    assert repos.messages.try_create(message, conversation_id) is True
    assert repos.messages.try_create(message, conversation_id) is False
    repos.messages.create(message, conversation_id)


def test_task_claim_next_is_atomic_across_concurrent_workers(tmp_path) -> None:
    """Two workers calling claim_next on the same task must NOT both succeed.

    This is the structural guarantee that prevents the duplicate-worker race
    we hit during e2e runs (the cause of the artifact_delivered cluster):
    one worker gets the row, the other gets None.
    """
    database = Database(f"sqlite:///{tmp_path / 'agent.db'}")
    database.initialize()
    repos = Repositories.for_database(database)

    conversation_id = repos.conversations.get_or_create(ChannelType.TELEGRAM, "1")
    task = repos.tasks.create("do a thing", conversation_id)
    assert task.status == TaskStatus.RECEIVED

    # Two workers race to claim. SQLite serializes writes; exactly one wins.
    claim_a = repos.tasks.claim_next([TaskStatus.RECEIVED], worker_id="A")
    claim_b = repos.tasks.claim_next([TaskStatus.RECEIVED], worker_id="B")

    assert claim_a is not None
    assert claim_b is None  # B was locked out by A's claim
    assert claim_a.id == task.id

    # The same worker (A) CAN re-claim its own task on a subsequent poll.
    claim_a_again = repos.tasks.claim_next([TaskStatus.RECEIVED], worker_id="A")
    assert claim_a_again is not None
    assert claim_a_again.id == task.id


def test_stale_worker_metadata_cannot_revive_cancelled_task(tmp_path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'agent.db'}")
    database.initialize()
    repos = Repositories.for_database(database)
    task = repos.tasks.create("cancel while a model call is running")
    stale_metadata = {**task.metadata, "operator_loop": True}

    cancelled = repos.tasks.update_metadata(
        task.id,
        {**task.metadata, "last_control_signal": "cancel"},
        TaskStatus.CANCELLED,
    )
    result = repos.tasks.update_metadata(task.id, stale_metadata, TaskStatus.RUNNING)

    assert cancelled.status == TaskStatus.CANCELLED
    assert result.status == TaskStatus.CANCELLED
    assert result.metadata["last_control_signal"] == "cancel"
    assert "operator_loop" not in result.metadata


def test_task_claim_releases_when_terminal(tmp_path) -> None:
    """release_claim clears the worker hint so the task is free for reclaim
    (mostly cosmetic; the claim would also expire naturally)."""
    database = Database(f"sqlite:///{tmp_path / 'agent.db'}")
    database.initialize()
    repos = Repositories.for_database(database)

    conversation_id = repos.conversations.get_or_create(ChannelType.TELEGRAM, "1")
    task = repos.tasks.create("do a thing", conversation_id)
    repos.tasks.claim_next([TaskStatus.RECEIVED], worker_id="A")

    # Without release, worker B can't grab it.
    assert repos.tasks.claim_next([TaskStatus.RECEIVED], worker_id="B") is None

    repos.tasks.release_claim(task.id)

    # After release, worker B can claim it.
    claim_b = repos.tasks.claim_next([TaskStatus.RECEIVED], worker_id="B")
    assert claim_b is not None
    assert claim_b.id == task.id


def test_task_claim_recovers_after_expiry(tmp_path) -> None:
    """An expired claim becomes eligible again, so a crashed worker doesn't
    permanently strand the task."""
    database = Database(f"sqlite:///{tmp_path / 'agent.db'}")
    database.initialize()
    repos = Repositories.for_database(database)

    conversation_id = repos.conversations.get_or_create(ChannelType.TELEGRAM, "1")
    task = repos.tasks.create("do a thing", conversation_id)

    # Worker A claims with a 0-second expiry → immediately expired.
    repos.tasks.claim_next([TaskStatus.RECEIVED], worker_id="A", claim_expiry_seconds=0)

    # Worker B can then claim it.
    claim_b = repos.tasks.claim_next([TaskStatus.RECEIVED], worker_id="B")
    assert claim_b is not None
    assert claim_b.id == task.id


def test_approval_decision_and_consumption_are_atomic_and_expiry_aware(tmp_path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'agent.db'}")
    database.initialize()
    repos = Repositories.for_database(database)
    task = repos.tasks.create("approve one exact action")
    live = repos.approvals.create(
        ApprovalRequest(
            task_id=task.id,
            capability=Capability.TERMINAL_RUN,
            risk_level=RiskLevel.HIGH,
            summary="Approve command",
            expires_at=utc_now() + timedelta(minutes=1),
        )
    )
    expired = repos.approvals.create(
        ApprovalRequest(
            task_id=task.id,
            capability=Capability.TERMINAL_RUN,
            risk_level=RiskLevel.HIGH,
            summary="Expired command",
            expires_at=utc_now() - timedelta(seconds=1),
        )
    )

    assert repos.approvals.decide_pending(live.id, ApprovalStatus.APPROVED) is True
    assert repos.approvals.decide_pending(live.id, ApprovalStatus.REJECTED) is False
    assert repos.approvals.consume_approved(live.id) is True
    assert repos.approvals.consume_approved(live.id) is False
    assert repos.approvals.get(live.id).status == ApprovalStatus.CONSUMED

    assert repos.approvals.decide_pending(expired.id, ApprovalStatus.APPROVED) is False
    assert repos.approvals.get(expired.id).status == ApprovalStatus.EXPIRED


def test_task_list_for_conversation_is_oldest_first_and_scoped(tmp_path) -> None:
    """docs/archive/HISTORY.md Part 4 T2.8: the local web chat channel renders one
    conversation's tasks as a transcript, oldest first - the opposite order
    of list_recent - and must not leak another conversation's tasks in."""
    database = Database(f"sqlite:///{tmp_path / 'agent.db'}")
    database.initialize()
    repos = Repositories.for_database(database)

    conv_a = repos.conversations.get_or_create(ChannelType.WEB, "local")
    conv_b = repos.conversations.get_or_create(ChannelType.TELEGRAM, "999")
    first = repos.tasks.create("first message", conversation_id=conv_a)
    repos.tasks.create("unrelated conversation", conversation_id=conv_b)
    second = repos.tasks.create("second message", conversation_id=conv_a)

    tasks = repos.tasks.list_for_conversation(conv_a)

    assert [task.id for task in tasks] == [first.id, second.id]


def test_task_list_for_conversation_respects_limit(tmp_path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'agent.db'}")
    database.initialize()
    repos = Repositories.for_database(database)
    conv = repos.conversations.get_or_create(ChannelType.WEB, "local")
    for i in range(5):
        repos.tasks.create(f"message {i}", conversation_id=conv)

    tasks = repos.tasks.list_for_conversation(conv, limit=2)

    assert len(tasks) == 2


def test_task_list_for_conversation_empty_when_none_exist(tmp_path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'agent.db'}")
    database.initialize()
    repos = Repositories.for_database(database)

    assert repos.tasks.list_for_conversation("conv_nonexistent") == []


def test_an_expired_claim_is_reclaimable_in_the_same_clock_tick(tmp_path, monkeypatch) -> None:
    """Windows clocks can return one instant twice; a claim expiring at T is expired at T."""
    import agent_control.storage.repositories as repositories_module

    database = Database(f"sqlite:///{tmp_path / 'agent.db'}")
    database.initialize()
    repos = Repositories.for_database(database)
    task = repos.tasks.create("do a thing", repos.conversations.get_or_create(ChannelType.TELEGRAM, "1"))
    frozen = utc_now()
    monkeypatch.setattr(repositories_module, "utc_now", lambda: frozen)

    repos.tasks.claim_next([TaskStatus.RECEIVED], worker_id="A", claim_expiry_seconds=0)
    claim_b = repos.tasks.claim_next([TaskStatus.RECEIVED], worker_id="B")

    assert claim_b is not None and claim_b.id == task.id


def test_rows_created_in_the_same_clock_tick_keep_their_insertion_order(tmp_path) -> None:
    """The latest task, and every oldest-first list, must not depend on how equal timestamps happen to sort."""
    database = Database(f"sqlite:///{tmp_path / 'agent.db'}")
    database.initialize()
    repos = Repositories.for_database(database)
    conversation_id = repos.conversations.get_or_create(ChannelType.TELEGRAM, "1")
    first = repos.tasks.create("an earlier task", conversation_id)
    second = repos.tasks.create("what's the current task status?", conversation_id)
    with database.connect() as connection:
        connection.execute("UPDATE tasks SET created_at = ?", (first.created_at.isoformat(),))

    assert [t.id for t in repos.tasks.list_recent(2)] == [second.id, first.id]
    assert [t.id for t in repos.tasks.list_for_conversation(conversation_id)] == [first.id, second.id]
