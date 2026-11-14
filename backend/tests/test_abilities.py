"""The ability index: one place that knows what the assistant can do, could do with help, or cannot do (abilities/)."""

from __future__ import annotations

import json

import pytest

from agent_control.abilities.catalog import AbilityCatalog
from agent_control.abilities.index import AbilityIndex, tokens
from agent_control.abilities.model import Ability, AbilityKind, AbilityState
from agent_control.abilities.sources import agent_abilities, build_index, mcp_abilities, skill_abilities, tool_abilities, workflow_abilities
from agent_control.config import AppSettings
from agent_control.schemas import ToolCallRequest, ToolResultStatus
from agent_control.tools.registry import build_tool_registry


def _ability(name: str, summary: str, *, group: str = "g", provides: tuple[str, ...] = (), state: AbilityState = AbilityState.READY) -> Ability:
    return Ability(id=f"tool:{name}", kind=AbilityKind.TOOL, name=name, summary=summary, group=group, provides=provides, state=state)


# --- search -----------------------------------------------------------------------------------------------------------------------------------


def test_words_are_reduced_to_stems_and_filler_is_dropped():
    assert tokens("Ordering the Files, please") == ["order", "file"]
    assert tokens("filesystem.manage") == ["filesystem", "manage"]
    assert tokens("") == []


def test_a_request_finds_the_ability_whose_words_it_shares_and_a_rare_word_counts_more():
    index = AbilityIndex([
        _ability("files", "list, read and move files and folders", provides=("file", "folder")),
        _ability("docs", "summarize a pdf or a slide deck", provides=("pdf", "slides")),
        _ability("web", "search the web", provides=("search", "google")),
    ])
    assert [a.name for a, _ in index.search("move these files into a folder")] == ["files"]
    assert index.search("summarize the pdf")[0][0].name == "docs"
    assert index.search("quantum chromodynamics") == []  # nothing shared, nothing returned: the caller says so


def test_search_can_be_limited_to_states_and_ties_are_stable():
    index = AbilityIndex([
        _ability("a", "convert a video", state=AbilityState.DISABLED),
        _ability("b", "convert a video", state=AbilityState.READY),
    ])
    assert [a.name for a, _ in index.search("convert video", states=[AbilityState.READY])] == ["b"]
    assert [a.name for a, _ in index.search("convert video")] == ["a", "b"]


def test_the_first_record_of_an_id_wins_and_names_resolve_in_any_case():
    first = _ability("files", "the specific one")
    index = AbilityIndex([first, _ability("files", "the general one")])
    assert len(index) == 1 and index.get("tool:files") is first
    assert index.find_by_name("FILES") is first and index.find_by_name("tool:files") is first and index.find_by_name("nope") is None


def test_groups_collect_abilities_by_domain():
    index = AbilityIndex([_ability("a", "x y", group="files"), _ability("b", "x y", group="web"), _ability("c", "x y", group="files")])
    assert {group: [a.name for a in items] for group, items in index.groups().items()} == {"files": ["a", "c"], "web": ["b"]}


# --- sources ----------------------------------------------------------------------------------------------------------------------------------


def _settings(tmp_path, **overrides) -> AppSettings:
    skills = {"enabled": True, "root_dir": str(tmp_path / "skills")}
    return AppSettings(_env_file=None, abilities={"enabled": True}, adapters={"skills": skills}, **overrides)


def test_every_tool_declares_what_it_is_for():
    registry = build_tool_registry(AppSettings(_env_file=None), "http://127.0.0.1:8765")
    for definition in registry.definitions:
        assert definition.group, f"{definition.name} has no group"
        assert definition.provides, f"{definition.name} says nothing about what people use it for"


def test_a_tool_that_is_off_is_known_and_says_how_to_turn_it_on():
    registry = build_tool_registry(AppSettings(_env_file=None), "http://127.0.0.1:8765")
    records = {a.name: a for a in tool_abilities(registry.definitions)}
    off = records["browser.control"]
    assert off.state == AbilityState.DISABLED and "enable" in off.needs and "Access" in off.guidance and off.call == {}
    on = records["skills.use"]
    assert on.state == AbilityState.READY and on.call == {"tool": "skills.use"}


def test_skills_and_kept_scripts_are_read_from_the_folder(tmp_path):
    root = tmp_path / "skills"
    root.mkdir()
    (root / "invoice.md").write_text("---\nname: Invoice Totals\ndescription: find the total on an invoice\n---\n1. read it\n", encoding="utf-8")
    (root / "helper.md").write_text("---\nname: helper-count-lines\ndescription: count lines in text files\n---\nprint(1)\n", encoding="utf-8")
    (root / "planner.md").write_text(
        "---\nname: Plan It\ndescription: plan a big goal\nwhen: Is it a big goal?\nwhen_at: 0.8\nroute: plan\n---\nsteps\n", encoding="utf-8"
    )
    records = {a.name: a for a in skill_abilities(_settings(tmp_path))}
    assert records["Invoice Totals"].kind == AbilityKind.SKILL and records["Invoice Totals"].call["input"]["name"] == "Invoice Totals"
    assert (records["Invoice Totals"].confirm, records["Invoice Totals"].route) == ("", "")
    planner = records["Plan It"]
    assert (planner.confirm, planner.confirm_at, planner.route) == ("Is it a big goal?", 0.8, "plan")
    assert records["helper-count-lines"].kind == AbilityKind.HELPER


def test_mcp_tools_come_from_the_catalog_and_a_missing_catalog_is_an_empty_source(tmp_path):
    catalog = tmp_path / "mcp.json"
    settings = _settings(tmp_path, mcp={"enabled": True, "catalog_path": str(catalog)})
    assert mcp_abilities(settings) == []
    catalog.write_text(json.dumps({"servers": [], "tools": [
        {"server": "gh", "name": "create_issue", "description": "Open an issue on a repository"},
        {"server": "gh", "name": "delete_repo", "description": "Delete a repository", "disabled": True},
        {"name": "orphan"},
    ]}), encoding="utf-8")
    records = {a.name: a for a in mcp_abilities(settings)}
    assert set(records) == {"gh/create_issue", "gh/delete_repo"}
    assert records["gh/create_issue"].call["input"] == {"operation": "call_tool", "server": "gh", "tool": "create_issue"}
    assert records["gh/delete_repo"].state == AbilityState.DISABLED


def test_workflows_and_installed_coding_agents_are_records():
    class Flow:
        name = "weekly report"
        objective_template = "Make the weekly report for {{team}}"
        parameters = ["team"]

    flow = workflow_abilities([Flow()])[0]
    assert flow.kind == AbilityKind.WORKFLOW and 'run weekly report' in flow.guidance and "team" in flow.guidance
    agents = agent_abilities(["claude_code"], enabled=True)
    assert [a.id for a in agents] == ["agent:claude_code"] and agents[0].call["input"]["provider"] == "claude_code"
    assert agent_abilities([], enabled=True) == []  # not installed: not something this machine can offer
    assert agent_abilities(["codex"], enabled=False)[0].state == AbilityState.DISABLED


def test_the_index_puts_what_is_specific_before_what_is_general_and_knows_what_cannot_be_done(tmp_path):
    root = tmp_path / "skills"
    root.mkdir()
    (root / "s.md").write_text("---\nname: Tidy Downloads\ndescription: sort the downloads folder by type\n---\nsteps\n", encoding="utf-8")
    settings = _settings(tmp_path)
    registry = build_tool_registry(settings, "http://127.0.0.1:8765")
    index = build_index(registry, settings)
    hits = [a.name for a, _ in index.search("tidy the downloads folder")]
    assert hits[0] == "Tidy Downloads" and "filesystem.manage" in hits
    gap, _ = index.search("order me a pizza")[0]
    assert gap.kind == AbilityKind.GAP and gap.state == AbilityState.UNAVAILABLE and "payment" in gap.guidance


def test_the_catalog_rebuilds_after_its_ttl_and_on_demand(tmp_path):
    settings = _settings(tmp_path)
    registry = build_tool_registry(settings, "http://127.0.0.1:8765")
    now = [0.0]
    catalog = AbilityCatalog(registry, settings, ttl_seconds=5, clock=lambda: now[0])
    first = catalog.index()
    assert catalog.index() is first
    now[0] = 6.0
    second = catalog.index()
    assert second is not first
    catalog.invalidate()
    assert catalog.index() is not second


# --- the tool ---------------------------------------------------------------------------------------------------------------------------------


def _request(**payload) -> ToolCallRequest:
    return ToolCallRequest(task_id="task_x", tool_name="ability.search", capability="agent.core", input=payload, origin="operator")


@pytest.mark.asyncio
async def test_the_tool_searches_and_describes(tmp_path):
    registry = build_tool_registry(_settings(tmp_path), "http://127.0.0.1:8765")
    adapter = registry.adapters["ability.search"]
    found = await adapter.execute(_request(operation="search", query="order me a pizza"))
    assert found.status == ToolResultStatus.SUCCEEDED
    top = found.output["abilities"][0]
    assert top["state"] == "unavailable" and "payment" in top["guidance"].lower()
    assert "cannot be done" in found.output["terminal_output"][0]["content"]
    described = await adapter.execute(_request(operation="describe", name="skills.use"))
    assert described.output["abilities"][0]["how_to_use"] == {"tool": "skills.use"}
    missing = await adapter.execute(_request(operation="describe", name="nothing here"))
    assert missing.status == ToolResultStatus.FAILED


@pytest.mark.asyncio
async def test_the_overview_lists_what_cannot_be_done_first_and_tells_the_model_to_answer_from_it(tmp_path):
    """Live (challenge a7, g1, g2): asked what it cannot do, what it needs and what it can do, chat answered from nowhere."""
    settings = _settings(tmp_path)
    registry = build_tool_registry(settings, "http://127.0.0.1:8765")
    result = await registry.adapters["ability.search"].execute(_request(operation="overview"))
    assert result.status == ToolResultStatus.SUCCEEDED
    text = result.output["terminal_output"][0]["content"]
    assert text.index("Cannot be done by this assistant") < text.index("Ready now")  # the common question first
    assert "payment" in text.lower()  # a gap says why and what to do instead
    assert "Answer from this list only" in text and "recommend the two or three" in text and "no dates" in text
    assert result.output["operation"] == "overview" and result.output["abilities"]


def test_the_overview_caps_each_section_and_says_how_many_more(tmp_path):
    from agent_control.tools.abilities import _overview

    many = tuple(_ability(f"tool{n:02d}", f"does thing {n}") for n in range(30))
    text, output = _overview(many)
    assert text.count("\n- tool") == 25 and "- ...and 5 more." in text and "Ready now:" in text
    assert "Cannot be done" not in text  # a section with nothing in it is left out
    assert len(output["abilities"]) == 25
    assert _overview(())[0].startswith("Nothing is listed.")


def test_the_overview_counts_the_tools_of_mcp_servers_instead_of_listing_them_and_cuts_text_at_a_word():
    from agent_control.tools.abilities import _overview

    served = tuple(
        Ability(id=f"mcp:fs/t{n}", kind=AbilityKind.MCP_TOOL, name=f"filesystem/tool{n}", summary="reads a file", state=AbilityState.READY)
        for n in range(7)
    )
    long_gap = Ability(
        id="gap:pay", kind=AbilityKind.GAP, name="pay for things", summary="buy or order something", state=AbilityState.UNAVAILABLE,
        guidance="It has no payment method. " * 20,
    )
    text, output = _overview((*served, _ability("files", "reads and moves files"), long_gap))
    assert "filesystem/tool0" not in text and "- 7 more tools from connected MCP servers." in text
    assert "- files: reads and moves files" in text and len(output["abilities"]) == 2
    gap_line = next(line for line in text.splitlines() if line.startswith("- pay for things"))
    assert gap_line.endswith("…)") and "payment method." in gap_line  # shortened at a word, not in the middle of one


@pytest.mark.asyncio
async def test_an_empty_search_says_what_to_do_next(tmp_path):
    registry = build_tool_registry(_settings(tmp_path), "http://127.0.0.1:8765")
    result = await registry.adapters["ability.search"].execute(_request(operation="search", query="zzzz qqqq"))
    summary = result.output["summary"]
    assert result.output["abilities"] == [] and "code.interpreter" in summary and "cannot do it" in summary


def test_the_tool_is_off_and_unnamed_unless_enabled_so_recorded_prompts_do_not_change():
    registry = build_tool_registry(AppSettings(_env_file=None), "http://127.0.0.1:8765")
    definition = registry.definition_index["ability.search"]
    assert not definition.enabled and not definition.named_when_disabled
    assert "ability.search" not in registry.context()
    enabled = build_tool_registry(AppSettings(_env_file=None, abilities={"enabled": True}), "http://127.0.0.1:8765")
    assert "ability.search" in enabled.context()
