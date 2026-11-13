"""The Operator is shown only the tools its request's route needs, and gets the full list back when a task goes badly."""

from __future__ import annotations

import re

import pytest

from agent_control.orchestration import TaskWorker
from agent_control.orchestration.tool_scope import ALWAYS, ROUTE_TOOLS, scope_catalog_text, scope_for_task, tools_for_route
from helpers import make_repos

CATALOG = """Available worker tools:
- filesystem.manage (filesystem.write): read and write files operations=inspect_folder,read_file
    example tool_input: {"operation": "read_file", "path": "a.txt"}
- document.manage (filesystem.read): documents operations=summarize_pdf
- schedule.manage (schedule.manage): schedules operations=create,list
    example tool_input: {"operation": "list"}
- browser.open (browser.open): open pages operations=open
- skills.use (skills.use): skills operations=list
- server="filesystem" tool="edit_file" - an MCP server tool; capability=terminal.run

Disabled (cannot be called; tell the user to enable it in Access): memory.manage

Prefer conservative plans. Use registered tool names exactly."""


def _names(text: str) -> list[str]:
    return re.findall(r"^- ([a-z][\w.]*) \(", text, re.M)


def test_a_route_keeps_its_tools_with_their_examples_and_drops_the_rest() -> None:
    scoped = scope_catalog_text(CATALOG, tools_for_route("schedule.manage"), "schedule.manage")

    assert _names(scoped) == ["schedule.manage", "skills.use"]  # the route's tool plus the always-on one
    assert '"operation": "list"' in scoped  # its example stays with it
    assert "read_file" not in scoped  # a dropped tool's example goes with it
    assert "Disabled (cannot be called" in scoped and scoped.rstrip().endswith("registered tool names exactly.")
    assert "- server=" not in scoped  # MCP server tools belong to the adapter route only


def test_the_adapter_route_keeps_mcp_server_tools() -> None:
    scoped = scope_catalog_text(CATALOG, tools_for_route("adapter.factory"), "adapter.factory")
    assert "- server=" in scoped


@pytest.mark.parametrize("route", ["conversation", "status", "unknown", None, "something.new"])
def test_other_routes_get_the_full_catalog(route) -> None:
    assert tools_for_route(route) is None
    assert scope_catalog_text(CATALOG, tools_for_route(route), route) == CATALOG


def test_every_route_keeps_the_tool_it_is_named_after() -> None:
    for route, tools in ROUTE_TOOLS.items():
        if route == "configuration":
            assert tools == frozenset()  # "remember that ..." needs only the always-on memory and persona tools
        elif route == "desktop.observe":
            assert "computer.use" in tools  # a route name that is not a tool: screenshots go through computer.use
        else:
            assert route in tools, route


def test_a_task_that_keeps_failing_gets_the_full_catalog_back() -> None:
    metadata = {"orchestration_intent": {"route": "schedule.manage"}, "operator_history": [{"status": "succeeded"}]}
    assert scope_for_task(metadata) is not None

    metadata["operator_history"] = [{"status": "failed"}, {"status": "denied"}]
    assert scope_for_task(metadata) is None

    metadata["operator_history"] = [{"status": "audit_gap"}, {"status": "already_done"}]
    assert scope_for_task(metadata) is None


def test_the_worker_narrows_the_catalog_only_for_a_task_with_a_route(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    worker = TaskWorker(repos, audit, executor=None, operator=None, config_context=CATALOG, scope_tools=True)
    scheduled = {"orchestration_intent": {"route": "schedule.manage"}}

    assert _names(worker._planner_context(metadata=scheduled)) == ["schedule.manage", "skills.use"]
    assert _names(worker._planner_context(metadata={})) == _names(CATALOG)  # no route: everything
    assert _names(worker._planner_context()) == _names(CATALOG)  # a caller with no task: everything
    assert worker._planner_context(" extra", metadata=scheduled).endswith(" extra")  # added context follows the catalog


def test_scoping_is_off_unless_asked_for(tmp_path) -> None:
    # It did not improve accuracy when measured (docs/ARCHITECTURE.md), so the full catalog is the default.
    repos, audit = make_repos(tmp_path)
    worker = TaskWorker(repos, audit, executor=None, operator=None, config_context=CATALOG)

    assert _names(worker._planner_context(metadata={"orchestration_intent": {"route": "schedule.manage"}})) == _names(CATALOG)


def test_a_remember_request_is_shown_only_the_always_on_tools_not_the_whole_catalog() -> None:
    """With every tool in front of it the local model answered "remember my dog is Biscuit" by reading a file from an earlier turn."""
    allowed = tools_for_route("configuration")
    assert allowed == ALWAYS and "memory.manage" in allowed and "filesystem.manage" not in allowed
