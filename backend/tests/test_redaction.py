"""Credential handling at the two sinks a secret can escape through.

A config file's API key reached both the Telegram reply and the audit trail
(docs/archive/E2E_FINDINGS.md P0-1): key-name matching only sees a credential that is
the value of its own field, and vault-value matching only sees secrets YBM
already knows. A key read out of a *user's* file is neither.
"""
from __future__ import annotations

from agent_control.schemas import TaskRecord, TaskStatus
from agent_control.storage.redaction import redact_payload, redact_text


CANARY = "sk-live-EVOLEAK-9931-DO-NOT-ECHO"
CONFIG_BLOB = (
    "# Billing exporter configuration\n"
    "EXPORTER_NAME=billing-exporter\n"
    "EXPORTER_REGION=eu-west-1\n"
    f"ACME_API_KEY={CANARY}\n"
    "RETRY_LIMIT=5\n"
    "TIMEOUT_SECONDS=45\n"
)


def test_credential_embedded_in_free_text_is_redacted() -> None:
    redacted = redact_text(CONFIG_BLOB)

    assert CANARY not in redacted
    assert "EVOLEAK" not in redacted


def test_redaction_keeps_everything_that_is_not_the_secret() -> None:
    """Over-redaction has its own cost: the answer the user actually asked for
    is the non-sensitive fields, and the key's *name* is legitimate context."""
    redacted = redact_text(CONFIG_BLOB)

    for kept in ("billing-exporter", "eu-west-1", "RETRY_LIMIT=5", "TIMEOUT_SECONDS=45"):
        assert kept in redacted
    assert "ACME_API_KEY" in redacted


def test_provider_token_shapes_are_redacted_without_a_key_name() -> None:
    """These turn up mid-sentence in summaries and error text, where there is
    no adjacent field name to match on."""
    samples = [
        "the token is ghp_abcdefghijklmnopqrstuvwxyz012345 here",
        "using xoxb-1234567890-abcdefghijkl now",
        "AKIAIOSFODNN7EXAMPLE was rejected",
        "bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r",
    ]
    for sample in samples:
        redacted = redact_text(sample)
        assert "***" in redacted, sample
        for token in sample.split():
            if len(token) > 20:
                assert token not in redacted, sample


def test_ordinary_text_is_untouched() -> None:
    plain = "The folder holds notes.txt, budget.csv, and a PDF. Retention is 400 days."

    assert redact_text(plain) == plain


def test_quoted_key_forms_are_redacted() -> None:
    """A JSON config is as likely to be summarized as a .env one. The separator
    used to be anchored to the key name, so `"api_key": "..."` slipped through
    while `API_KEY=...` was caught."""
    samples = (
        '{"api_key": "NOTAREALKEY-abc123def456ghi", "port": 8080}',
        '"secret" : "NOTAREALKEY-hunter2hunter2"',
        'api_key: NOTAREALKEY-abc123def456ghi',
        '<config apiKey="NOTAREALKEY-abc123def456"/>',
    )
    for sample in samples:
        redacted = redact_text(sample)
        assert "***" in redacted, sample
        for leaked in ("NOTAREALKEY-abc123def456ghi", "NOTAREALKEY-hunter2hunter2", "NOTAREALKEY-abc123def456"):
            assert leaked not in redacted, sample


def test_json_stays_parseable_after_redaction() -> None:
    """The value is replaced inside its quotes, so a redacted payload can still
    be read by whatever consumes it."""
    import json

    redacted = redact_text('{"api_key": "NOTAREALKEY-abc123def456ghi", "port": 8080}')

    assert json.loads(redacted) == {"api_key": "***", "port": 8080}


def test_whole_multi_word_value_is_redacted() -> None:
    """Stopping at the first space left `*** horse battery staple` - the
    placeholder implied the secret was handled while most of it survived."""
    redacted = redact_text("password = correct horse battery staple")

    for word in ("correct", "horse", "battery", "staple"):
        assert word not in redacted


def test_token_accounting_is_not_mistaken_for_a_credential() -> None:
    """`TOKEN` in the key pattern also matches LLM usage counters; redacting
    those destroys telemetry and teaches nobody anything."""
    plain = "used total_tokens: 1530 and completion_tokens: 210"

    assert redact_text(plain) == plain
    assert redact_text('{"max_tokens": "4096"}') == '{"max_tokens": "4096"}'


def test_numeric_value_is_still_redacted_for_non_token_names() -> None:
    """The numeric exemption is scoped to token-shaped names on purpose."""
    assert redact_text("SECRET=1234") == "SECRET=***"


def test_audit_payload_scans_string_values_not_just_key_names() -> None:
    """AuditLogger.append funnels every payload through redact_payload, so the
    content scan there covers the whole audit sink."""
    payload = redact_payload({"summary": CONFIG_BLOB, "count": 3})

    assert CANARY not in payload["summary"]
    assert payload["count"] == 3


def test_existing_key_name_and_vault_value_rules_still_apply() -> None:
    payload = redact_payload({"api_key": "abc123", "note": "vault value zzz9"}, secret_values=["zzz9"])

    assert payload["api_key"] == "***"
    assert "zzz9" not in payload["note"]


def test_persisted_task_answer_is_redacted_at_rest(tmp_path) -> None:
    """Scrubbing the outbound message is not enough: anything reading the task
    row directly - the admin API, a trace export, a database backup - would
    otherwise still hand back the raw credential."""
    from helpers import make_repos

    repositories, _ = make_repos(tmp_path)
    task = repositories.tasks.create(objective="summarize the config")

    stored = repositories.tasks.update_metadata(
        task.id, {"synthesized_answer": f"The config sets {CONFIG_BLOB}"}
    )

    assert CANARY not in stored.metadata["synthesized_answer"]
    assert "ACME_API_KEY" in stored.metadata["synthesized_answer"]

    # Straight out of the row, bypassing every application-level reader.
    with repositories.tasks.database.connect() as connection:
        raw = connection.execute(
            "SELECT metadata_json FROM tasks WHERE id = ?", (task.id,)
        ).fetchone()["metadata_json"]
    assert CANARY not in str(raw)


def test_operator_history_is_left_intact_for_the_model() -> None:
    """The history is the model's working context, not an output sink. Blanking
    it mid-task would change what the next step can reason about."""
    from agent_control.storage.repositories import _redact_answer_fields

    metadata = {"operator_history": [{"output": CONFIG_BLOB}], "synthesized_answer": CANARY}

    redacted = _redact_answer_fields(metadata)

    assert redacted["operator_history"][0]["output"] == CONFIG_BLOB
    assert CANARY not in redacted["synthesized_answer"]


def test_task_notification_text_is_redacted_for_every_channel() -> None:
    """format_task_message is the one formatter every channel renders, so it is
    where a credential can be stopped before it leaves as a chat message."""
    from agent_control.channels.task_notify import format_task_message

    task = TaskRecord(
        objective="summarize the config",
        status=TaskStatus.COMPLETED,
        metadata={"synthesized_answer": f"The service config contains {CONFIG_BLOB}"},
    )

    message = format_task_message(task)

    assert CANARY not in message
    assert "billing-exporter" in message


def test_redact_payload_covers_widened_default_patterns() -> None:
    """Regression: DEFAULT_PATTERNS originally missed several common
    secret-shaped key names entirely - a field literally named
    "private_key" or "credential" passed through audit/task output
    unredacted."""
    # Values here are deliberately NOT secret-shaped: redaction keys off the
    # field *name*, so realistic-looking material would add nothing to the
    # test while tripping the repository's own secret scanner in CI.
    payload = {
        "private_key": "placeholder-value",
        "access_key": "placeholder-value",
        "credentials": {"nested": "placeholder-value"},
        "db_pwd": "placeholder-value",
        "passwd": "placeholder-value",
        "bearer_token_value": "placeholder-value",
        "harmless_field": "keep me",
    }

    redacted = redact_payload(payload)

    assert redacted["private_key"] == "***"
    assert redacted["access_key"] == "***"
    assert redacted["credentials"] == "***"
    assert redacted["db_pwd"] == "***"
    assert redacted["passwd"] == "***"
    assert redacted["bearer_token_value"] == "***"
    assert redacted["harmless_field"] == "keep me"


def test_redact_payload_still_redacts_known_secret_values_in_strings() -> None:
    """Value-based redaction: a known secret is scrubbed out of free text even
    when the field name itself looks harmless."""
    injected = "vault-value-placeholder"
    redacted = redact_payload({"summary": f"used {injected} to call the API"}, secret_values=[injected])
    assert injected not in redacted["summary"]
