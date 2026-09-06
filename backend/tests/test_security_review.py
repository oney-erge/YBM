from __future__ import annotations

from datetime import timedelta

from agent_control.config import AppSettings, CapabilityPolicy, MCPServerConfig
from agent_control.egress import record_egress
from agent_control.schemas import (
    ApprovalGrant,
    Capability,
    RiskLevel,
    ToolCallRequest,
    ToolCallResult,
    ToolResultStatus,
    utc_now,
)
from agent_control.security_review import build_security_review
from helpers import make_repos


def test_security_review_reports_a_loopback_server_with_no_token_as_not_reachable(tmp_path) -> None:
    repos, _audit = make_repos(tmp_path)
    settings = AppSettings(_env_file=None)

    review = build_security_review(repos, settings)

    assert review["network"]["reachable_beyond_this_machine"] is False
    assert review["network"]["admin_token_set"] is False


def test_security_review_flags_a_non_loopback_bind_as_reachable(tmp_path, monkeypatch) -> None:
    repos, _audit = make_repos(tmp_path)
    settings = AppSettings(_env_file=None, server={"host": "0.0.0.0", "port": 8765})
    monkeypatch.setenv("AGENT_ADMIN_TOKEN", "a-real-token")

    review = build_security_review(repos, settings)

    assert review["network"]["reachable_beyond_this_machine"] is True
    assert review["network"]["admin_token_set"] is True


def test_security_review_lists_active_grants_but_not_expired_ones(tmp_path) -> None:
    repos, _audit = make_repos(tmp_path)
    settings = AppSettings(_env_file=None)
    task = repos.tasks.create("move files")
    live_grant = ApprovalGrant(
        task_id=task.id,
        tool_name="filesystem.manage",
        capability=Capability.FILESYSTEM_WRITE,
        granted_from_approval_id="appr_1",
        expires_at=utc_now() + timedelta(minutes=10),
    )
    expired_grant = ApprovalGrant(
        task_id=task.id,
        tool_name="terminal.run",
        capability=Capability.TERMINAL_RUN,
        granted_from_approval_id="appr_2",
        expires_at=utc_now() - timedelta(minutes=10),
    )
    repos.approval_grants.create(live_grant)
    repos.approval_grants.create(expired_grant)

    review = build_security_review(repos, settings)

    assert [g["id"] for g in review["active_grants"]] == [live_grant.id]


def test_security_review_names_which_enabled_capabilities_need_no_approval(tmp_path) -> None:
    repos, _audit = make_repos(tmp_path)
    settings = AppSettings(
        _env_file=None,
        capabilities={
            Capability.FILESYSTEM_WRITE: CapabilityPolicy(enabled=True, requires_approval=False, max_risk_level=RiskLevel.HIGH),
            Capability.TERMINAL_RUN: CapabilityPolicy(enabled=True, requires_approval=True, max_risk_level=RiskLevel.HIGH),
            Capability.BROWSER_CONTROL: CapabilityPolicy(enabled=False, requires_approval=False, max_risk_level=RiskLevel.CRITICAL),
        },
    )

    review = build_security_review(repos, settings)

    assert review["capability_access"]["enabled"] == 2  # browser.control is disabled, doesn't count
    assert review["capability_access"]["no_approval_required"] == ["filesystem.write"]


def test_security_review_lists_configured_mcp_servers(tmp_path) -> None:
    repos, _audit = make_repos(tmp_path)
    settings = AppSettings(
        _env_file=None,
        mcp={"enabled": True, "servers": {"fake": MCPServerConfig(command="npx", risk_level=RiskLevel.LOW)}},
    )

    review = build_security_review(repos, settings)

    assert review["mcp_servers"] == [{"name": "fake", "enabled": True, "command": "npx", "risk_level": "low"}]


def test_security_review_lists_external_hosts_contacted_in_the_window(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    settings = AppSettings(_env_file=None)
    task = repos.tasks.create("look something up")
    record_egress(audit, task.id, "example.com", "http.request")
    record_egress(audit, task.id, "127.0.0.1", "code.interpreter")  # loopback - record_egress itself excludes this

    review = build_security_review(repos, settings)

    assert review["external_hosts_contacted"] == ["example.com"]


def test_security_review_counts_sandboxed_and_unsandboxed_code_execution(tmp_path) -> None:
    repos, _audit = make_repos(tmp_path)
    settings = AppSettings(_env_file=None)
    task = repos.tasks.create("run a script")

    sandboxed_request = ToolCallRequest(task_id=task.id, tool_name="code.interpreter", capability=Capability.TERMINAL_RUN, input={})
    repos.tool_invocations.create(sandboxed_request)
    repos.tool_invocations.complete(
        ToolCallResult(request_id=sandboxed_request.id, status=ToolResultStatus.SUCCEEDED, output={"sandboxed": True})
    )
    unsandboxed_request = ToolCallRequest(task_id=task.id, tool_name="code.interpreter", capability=Capability.TERMINAL_RUN, input={})
    repos.tool_invocations.create(unsandboxed_request)
    repos.tool_invocations.complete(
        ToolCallResult(request_id=unsandboxed_request.id, status=ToolResultStatus.SUCCEEDED, output={"sandboxed": False})
    )

    review = build_security_review(repos, settings)

    assert review["code_execution"] == {"sandboxed_runs": 1, "unsandboxed_runs": 1}


def test_security_review_excludes_tasks_outside_the_window(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    settings = AppSettings(_env_file=None)
    old_task = repos.tasks.create("an old task")
    record_egress(audit, old_task.id, "old-host.example", "http.request")
    with repos.tasks.database.connect() as connection:
        connection.execute(
            "UPDATE tasks SET created_at = ? WHERE id = ?",
            ((utc_now() - timedelta(days=30)).isoformat(), old_task.id),
        )

    review = build_security_review(repos, settings, window_days=7)

    assert review["external_hosts_contacted"] == []
