"""Security review (docs/ROADMAP.md "Finish the Proof"): one page reporting
this machine's actual exposure - what's reachable from the network,
whether an admin token is set, which grants are currently live, how often
code execution actually ran unsandboxed, which external hosts were
actually contacted, and which capabilities can act without asking first.

Every fact here is read from configuration or a recorded audit/invocation
row; nothing here probes the network or spawns Docker to find out live -
`ybm doctor` already does live capability checks (docs/GAPS.md). Kept
fast and honest about the difference between "asked and it said yes" and
"assumed" - the same "computed on read, nothing new persisted" approach
analytics.py's reliability dashboard already uses.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from agent_control.config import AppSettings, is_loopback_host
from agent_control.config_sync import read_env_value
from agent_control.schemas import AuditEventType, utc_now
from agent_control.storage.repositories import Repositories


def build_security_review(repositories: Repositories, settings: AppSettings, window_days: int = 7) -> dict[str, Any]:
    network = {
        "host": settings.server.host,
        "port": settings.server.port,
        # is_loopback_host is the same fail-closed test config.py's own
        # auth gate uses - a server bound to anything else accepts
        # connections beyond this machine.
        "reachable_beyond_this_machine": not is_loopback_host(settings.server.host),
        "admin_enabled": settings.server.admin_enabled,
        "admin_token_set": bool(read_env_value(settings.server.admin_token_env)),
    }

    active_grants = [
        {
            "id": grant.id,
            "task_id": grant.task_id,
            "tool_name": grant.tool_name,
            "capability": grant.capability.value,
            "scope": grant.scope,
            "expires_at": grant.expires_at.isoformat(),
            "operations_used": grant.operations_used,
            "max_operations": grant.max_operations,
        }
        for grant in repositories.approval_grants.list_active()
    ]

    enabled_capabilities = 0
    no_approval_required: list[str] = []
    for capability, policy in settings.capabilities.items():
        if not policy.enabled:
            continue
        enabled_capabilities += 1
        if not policy.requires_approval:
            no_approval_required.append(capability.value)

    mcp_servers = [
        {"name": name, "enabled": server.enabled, "command": server.command, "risk_level": server.risk_level.value}
        for name, server in settings.mcp.servers.items()
    ]

    cutoff = utc_now() - timedelta(days=window_days)
    hosts_contacted: set[str] = set()
    sandboxed_runs = 0
    unsandboxed_runs = 0
    for task in repositories.tasks.list_since(cutoff):
        for event in repositories.audit.list_for_task(task.id):
            if event.type != AuditEventType.EGRESS_CONTACTED:
                continue
            host = event.payload.get("host")
            if isinstance(host, str) and host:
                hosts_contacted.add(host)
        for invocation in repositories.tool_invocations.list_for_task(task.id):
            if invocation.get("tool_name") != "code.interpreter":
                continue
            if str(invocation.get("status") or "") != "succeeded":
                continue
            result = invocation.get("result")
            output = result.get("output") if isinstance(result, dict) else None
            sandboxed = output.get("sandboxed") if isinstance(output, dict) else None
            if sandboxed is True:
                sandboxed_runs += 1
            elif sandboxed is False:
                unsandboxed_runs += 1

    return {
        "window_days": window_days,
        "network": network,
        "active_grants": active_grants,
        "capability_access": {
            "enabled": enabled_capabilities,
            "no_approval_required": sorted(no_approval_required),
        },
        "mcp_servers": mcp_servers,
        "external_hosts_contacted": sorted(hosts_contacted),
        "code_execution": {"sandboxed_runs": sandboxed_runs, "unsandboxed_runs": unsandboxed_runs},
    }
