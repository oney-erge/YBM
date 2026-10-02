"""Structured logging - the one thing every service entry point calls first.

Before this module existed there was no logging configuration anywhere in the
codebase (docs/archive/HISTORY.md §2.1): the stdlib root logger defaulted to WARNING
with a last-resort stderr handler, so every `logger.debug(...)` call in the
app (17 of them) was silently discarded, `structlog` was a declared dependency
imported by nothing, and service logs were whatever `print()` happened to
write to stdout (589 bytes after a full run).

Wires stdlib `logging` (what every module already calls via
`logging.getLogger(__name__)`) through structlog's processor pipeline via the
standard `ProcessorFormatter` bridge, so existing `logger.debug/warning/error`
calls need no code changes to start producing real output. Two sinks per
service: a JSON-lines file for grepping/tailing, and a readable console
stream. Both go through the same redaction the audit log already uses
(`storage/redaction.py`) so secrets don't end up in a plaintext log file
right next to the DB that's supposed to be protecting them.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any

import structlog

from agent_control.storage.redaction import redact_payload


def _secret_values_from_environment(patterns: list[str]) -> tuple[str, ...]:
    lowered_patterns = tuple(pattern.lower() for pattern in patterns)
    return tuple(
        value
        for name, value in os.environ.items()
        if value and any(pattern in name.lower() for pattern in lowered_patterns)
    )


def _redact_event(patterns: list[str], secret_values: tuple[str, ...] = ()) -> Any:
    def processor(logger: Any, method_name: str, event_dict: dict) -> dict:
        return redact_payload(event_dict, patterns, secret_values=secret_values)

    return processor


def configure_logging(settings: Any, service_name: str, *, log_dir: Path | None = None) -> Path:
    """Idempotent - safe to call once per process, at the very top of every
    cli.py entry point (`main()`, and each of `poll_telegram`/`run_worker`/
    `run_scheduler`/etc. for when they're invoked directly, e.g. from tests).

    Returns the path of the JSON-lines log file, mainly so `ybm logs` /
    `ybm trace` callers don't have to re-derive the naming convention.
    """
    level_name = getattr(settings, "logging", None)
    level = getattr(logging, getattr(level_name, "level", "INFO"), logging.INFO)
    json_logs = bool(getattr(level_name, "json_logs", True))
    redact_patterns = list(getattr(level_name, "redact_patterns", None) or [])
    secret_values = _secret_values_from_environment(redact_patterns)

    log_dir = log_dir or Path(".agent_control") / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{service_name}.jsonl"

    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        _redact_event(redact_patterns, secret_values),
    ]

    structlog.configure(
        processors=[*shared_processors, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    json_formatter = structlog.stdlib.ProcessorFormatter(
        processor=structlog.processors.JSONRenderer(),
        foreign_pre_chain=shared_processors,
    )
    console_formatter = structlog.stdlib.ProcessorFormatter(
        processor=structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
        if not json_logs
        else structlog.processors.JSONRenderer(),
        foreign_pre_chain=shared_processors,
    )

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(json_formatter)

    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setFormatter(console_formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(file_handler)
    root.addHandler(console_handler)
    root.setLevel(level)

    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(service=service_name)

    return log_path


def bind_task_context(**kwargs: Any) -> None:
    """Replace the bound context with exactly these keys (plus whatever
    `service` configure_logging bound). Called fresh at the top of each unit
    of work (e.g. TaskWorker.process_task()) - not additive, so a stale
    task_id from a previous task on the same long-lived worker loop can't
    leak into the next one's log lines.
    """
    service = structlog.contextvars.get_contextvars().get("service")
    structlog.contextvars.clear_contextvars()
    if service:
        structlog.contextvars.bind_contextvars(service=service)
    structlog.contextvars.bind_contextvars(**kwargs)
