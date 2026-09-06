"""Records when a task's own tool call reaches beyond this machine.

Backs Task Receipts' "did anything leave the machine" line
(docs/UI_UX_AUDIT.md Phase 2) with a real signal instead of a guess: an
EGRESS_CONTACTED audit event, task-scoped like every other audit event, so
a receipt just filters the same audit_events table it already reads.
Loopback hosts (local Ollama/LocalDeploy, the VS Code bridge) are excluded
on purpose - they never leave the machine, so counting them would make
every task look like it phoned home.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from agent_control.config import is_loopback_host
from agent_control.schemas import AuditEventType


def record_egress(audit: Any | None, task_id: str | None, host: str, tool_name: str) -> None:
    if audit is None or not task_id or is_loopback_host(host):
        return
    audit.append(
        AuditEventType.EGRESS_CONTACTED,
        actor=tool_name,
        task_id=task_id,
        payload={"host": host, "tool_name": tool_name},
    )


# Same key set admin.py's _EVIDENCE_URL_KEYS already scans for the "what did
# this touch" evidence view - a tool that already reports its destination
# under one of these names (http.request's "url", browser's "browser_url"/
# "url"/"visited_urls") needs no tool-specific egress code at all.
_URL_KEYS = ("url", "urls", "visited_urls", "browser_url", "preview_url")


def extract_egress_hosts(*sources: dict[str, Any]) -> list[str]:
    """Pulls every hostname a tool call's validated input/output reports
    visiting, deduped and in first-seen order. Used by ToolExecutor for any
    operation a ToolDefinition marks in `operation_egress` - the output is
    checked ahead of the input on purpose: an adapter's actual result is
    what it really contacted (a "search" operation's input has no URL at
    all until the adapter picks one), the input is only a fallback for a
    tool that doesn't echo its destination back.
    """
    seen: dict[str, None] = {}
    for source in sources:
        if not isinstance(source, dict):
            continue
        for key in _URL_KEYS:
            value = source.get(key)
            values = value if isinstance(value, list) else [value] if value else []
            for item in values:
                if not isinstance(item, str) or not item:
                    continue
                host = urlparse(item).hostname or item
                seen.setdefault(host, None)
    return list(seen)
