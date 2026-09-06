SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS conversations (
        id TEXT PRIMARY KEY,
        channel TEXT NOT NULL,
        external_id TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(channel, external_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS messages (
        id TEXT PRIMARY KEY,
        conversation_id TEXT,
        channel TEXT NOT NULL,
        kind TEXT NOT NULL,
        sender_id TEXT NOT NULL,
        chat_id TEXT NOT NULL,
        text TEXT,
        attachments_json TEXT NOT NULL,
        raw_json TEXT,
        correlation_id TEXT NOT NULL,
        received_at TEXT NOT NULL,
        FOREIGN KEY(conversation_id) REFERENCES conversations(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS conversation_memory (
        conversation_id TEXT PRIMARY KEY,
        summary TEXT NOT NULL,
        facts_json TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(conversation_id) REFERENCES conversations(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tasks (
        id TEXT PRIMARY KEY,
        objective TEXT NOT NULL,
        status TEXT NOT NULL,
        conversation_id TEXT,
        metadata_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(conversation_id) REFERENCES conversations(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS task_signals (
        id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL,
        signal TEXT NOT NULL,
        actor TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY(task_id) REFERENCES tasks(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS approvals (
        id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL,
        capability TEXT NOT NULL,
        risk_level TEXT NOT NULL,
        summary TEXT NOT NULL,
        action_payload_json TEXT NOT NULL,
        status TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY(task_id) REFERENCES tasks(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tool_invocations (
        id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL,
        tool_name TEXT NOT NULL,
        capability TEXT NOT NULL,
        request_json TEXT NOT NULL,
        result_json TEXT,
        status TEXT NOT NULL,
        created_at TEXT NOT NULL,
        completed_at TEXT,
        FOREIGN KEY(task_id) REFERENCES tasks(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS artifacts (
        id TEXT PRIMARY KEY,
        task_id TEXT,
        artifact_type TEXT NOT NULL,
        uri TEXT,
        content_preview TEXT,
        metadata_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY(task_id) REFERENCES tasks(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS schedules (
        id TEXT PRIMARY KEY,
        source_channel TEXT NOT NULL,
        source_chat_id TEXT,
        objective TEXT NOT NULL,
        cadence TEXT NOT NULL,
        timezone TEXT NOT NULL,
        status TEXT NOT NULL,
        next_run_at TEXT NOT NULL,
        last_run_at TEXT,
        last_task_id TEXT,
        metadata_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS audit_events (
        id TEXT PRIMARY KEY,
        event_type TEXT NOT NULL,
        actor TEXT NOT NULL,
        task_id TEXT,
        correlation_id TEXT,
        payload_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS approval_grants (
        id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL,
        tool_name TEXT NOT NULL,
        capability TEXT NOT NULL,
        granted_from_approval_id TEXT NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        scope TEXT,
        max_operations INTEGER,
        operations_used INTEGER NOT NULL DEFAULT 0,
        revoked INTEGER NOT NULL DEFAULT 0,
        FOREIGN KEY(task_id) REFERENCES tasks(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS llm_calls (
        id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL,
        source TEXT NOT NULL,
        model TEXT,
        step_index INTEGER,
        step_id TEXT,
        messages_json TEXT NOT NULL,
        response_text TEXT,
        prompt_tokens INTEGER,
        completion_tokens INTEGER,
        total_tokens INTEGER,
        latency_ms REAL,
        created_at TEXT NOT NULL,
        FOREIGN KEY(task_id) REFERENCES tasks(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS memory_facts (
        id TEXT PRIMARY KEY,
        category TEXT NOT NULL,
        content TEXT NOT NULL,
        source TEXT NOT NULL,
        confidence REAL NOT NULL,
        task_id TEXT,
        supersedes_id TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(task_id) REFERENCES tasks(id)
    )
    """,
]


# Additive migrations that need to run on existing databases (idempotent).
# Use sqlite_master inspection to add columns only if they're missing.
ADDITIVE_MIGRATIONS = [
    # Atomic-claim columns on `tasks` so the worker can serialize concurrent
    # workers via UPDATE...RETURNING. Without this, two workers can both
    # SELECT the same row and race on it (the cause of duplicate-worker
    # symptoms we hit earlier).
    ("tasks", "claimed_by",        "ALTER TABLE tasks ADD COLUMN claimed_by TEXT"),
    ("tasks", "claim_expires_at",  "ALTER TABLE tasks ADD COLUMN claim_expires_at TEXT"),
    # step_id (docs/UI_UX_AUDIT.md Phase 14e) landed one commit after
    # llm_calls itself - a database created between those two commits (this
    # machine's own included) has the table but not the column yet.
    ("llm_calls", "step_id",       "ALTER TABLE llm_calls ADD COLUMN step_id TEXT"),
]


def apply_additive_migrations(connection) -> None:
    """Run each additive migration if its column is missing.

    Called from ``Database.initialize`` AFTER ``SCHEMA_STATEMENTS`` so the
    table exists. Idempotent.
    """
    for table, column, statement in ADDITIVE_MIGRATIONS:
        rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
        existing = {row[1] for row in rows}
        if column not in existing:
            connection.execute(statement)
