"""docs/archive/HISTORY.md Part 4 T1.3: user-droppable capability packs. A skill is
one markdown file (YAML frontmatter + instructions body) under
adapters.skills.root_dir, discovered fresh on every list/read call - no
code change, no restart.
"""

from __future__ import annotations

import pytest

from agent_control.config import AppSettings, Capability, CapabilityPolicy, RiskLevel, SkillsAdapterConfig
from agent_control.schemas import ToolCallRequest, ToolResultStatus
from agent_control.tools.registry import build_tool_registry
from agent_control.tools.skills import (
    SkillsAdapter,
    delete_skill_file,
    detect_referenced_tools,
    list_skills_detailed,
    skills_context_section,
    slugify,
    write_skill_file,
)


def _write_skill(tmp_path, filename: str, *, name: str | None = "invoice-extraction", description: str = "How to pull the total from an invoice PDF.", body: str = "1. Read the PDF.\n2. Find the total line.") -> None:
    frontmatter_lines = ["---"]
    if name is not None:
        frontmatter_lines.append(f"name: {name}")
    frontmatter_lines.append(f"description: {description}")
    frontmatter_lines.append("---")
    text = "\n".join(frontmatter_lines) + "\n" + body
    (tmp_path / filename).write_text(text, encoding="utf-8")


def _request(operation: str, **payload) -> ToolCallRequest:
    return ToolCallRequest(
        task_id="task_skills",
        tool_name="skills.use",
        capability=Capability.TELEGRAM_RECEIVE,
        input={"operation": operation, **payload},
    )


@pytest.mark.asyncio
async def test_list_returns_name_and_description_but_never_the_body(tmp_path) -> None:
    """Progressive disclosure is the whole point: list must be cheap and
    must never leak full skill content into every single prompt."""
    _write_skill(tmp_path, "invoice.md")
    adapter = SkillsAdapter(SkillsAdapterConfig(root_dir=str(tmp_path)))

    result = await adapter.execute(_request("list"))

    assert result.status == ToolResultStatus.SUCCEEDED
    assert result.output["skills"] == [
        {"name": "invoice-extraction", "description": "How to pull the total from an invoice PDF."}
    ]
    assert "body" not in result.output["skills"][0]
    assert "Find the total line" not in str(result.output)


@pytest.mark.asyncio
async def test_read_returns_full_body_for_the_named_skill(tmp_path) -> None:
    _write_skill(tmp_path, "invoice.md")
    adapter = SkillsAdapter(SkillsAdapterConfig(root_dir=str(tmp_path)))

    result = await adapter.execute(_request("read", name="invoice-extraction"))

    assert result.status == ToolResultStatus.SUCCEEDED
    assert len(result.output["skills"]) == 1
    assert result.output["skills"][0]["body"] == "1. Read the PDF.\n2. Find the total line."
    assert "Find the total line" in result.output["terminal_output"][0]["content"]


@pytest.mark.asyncio
async def test_read_unknown_skill_fails_with_available_names_listed(tmp_path) -> None:
    _write_skill(tmp_path, "invoice.md", name="invoice-extraction")
    _write_skill(tmp_path, "resume.md", name="resume-formatting", description="How resumes should look.")
    adapter = SkillsAdapter(SkillsAdapterConfig(root_dir=str(tmp_path)))

    result = await adapter.execute(_request("read", name="nonexistent-skill"))

    assert result.status == ToolResultStatus.FAILED
    assert "nonexistent-skill" in result.error_message
    assert "invoice-extraction" in result.error_message
    assert "resume-formatting" in result.error_message


@pytest.mark.asyncio
async def test_read_requires_name_field() -> None:
    from agent_control.tools.contracts import SkillsInput
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        SkillsInput(operation="read")


@pytest.mark.asyncio
async def test_list_on_empty_or_missing_directory_returns_no_skills_not_an_error(tmp_path) -> None:
    adapter = SkillsAdapter(SkillsAdapterConfig(root_dir=str(tmp_path / "does_not_exist")))

    result = await adapter.execute(_request("list"))

    assert result.status == ToolResultStatus.SUCCEEDED
    assert result.output["skills"] == []


@pytest.mark.asyncio
async def test_malformed_skill_file_is_skipped_without_hiding_valid_ones(tmp_path) -> None:
    """A broken skill file (bad YAML, missing name, no frontmatter at all)
    must not crash listing or hide every OTHER valid skill in the same
    directory - just itself."""
    _write_skill(tmp_path, "good.md", name="good-skill")
    (tmp_path / "no_frontmatter.md").write_text("just plain text, no frontmatter", encoding="utf-8")
    (tmp_path / "bad_yaml.md").write_text("---\nname: [unterminated\n---\nbody", encoding="utf-8")
    (tmp_path / "no_name.md").write_text("---\ndescription: has a description but no name\n---\nbody", encoding="utf-8")
    adapter = SkillsAdapter(SkillsAdapterConfig(root_dir=str(tmp_path)))

    result = await adapter.execute(_request("list"))

    assert result.status == ToolResultStatus.SUCCEEDED
    assert [s["name"] for s in result.output["skills"]] == ["good-skill"]


@pytest.mark.asyncio
async def test_list_is_sorted_and_capped_at_max_skills_listed(tmp_path) -> None:
    for i in range(5):
        _write_skill(tmp_path, f"skill_{i}.md", name=f"skill-{i:02d}", description=f"desc {i}")
    adapter = SkillsAdapter(SkillsAdapterConfig(root_dir=str(tmp_path), max_skills_listed=3))

    result = await adapter.execute(_request("list"))

    names = [s["name"] for s in result.output["skills"]]
    assert names == sorted(names)
    assert len(names) == 3


@pytest.mark.asyncio
async def test_unsupported_operation_fails_cleanly(tmp_path) -> None:
    adapter = SkillsAdapter(SkillsAdapterConfig(root_dir=str(tmp_path)))

    result = await adapter.execute(_request("delete"))

    assert result.status == ToolResultStatus.FAILED
    assert "unsupported" in result.error_message


def test_registry_exposes_skills_use_when_telegram_receive_is_enabled() -> None:
    settings = AppSettings(_env_file=None)  # TELEGRAM_RECEIVE is enabled by default

    registry = build_tool_registry(settings, "http://127.0.0.1:8765")

    definitions = {d.name: d for d in registry.definitions}
    assert definitions["skills.use"].enabled is True
    assert "skills.use" in registry.adapters
    assert "read" in definitions["skills.use"].operations


def test_registry_disables_skills_use_when_adapter_disabled_in_config() -> None:
    settings = AppSettings(_env_file=None, adapters={"skills": {"enabled": False}})

    registry = build_tool_registry(settings, "http://127.0.0.1:8765")

    definitions = {d.name: d for d in registry.definitions}
    assert definitions["skills.use"].enabled is False
    assert "skills.use" not in registry.adapters


def test_registry_disables_skills_use_when_telegram_receive_capability_is_off() -> None:
    settings = AppSettings(
        _env_file=None,
        capabilities={Capability.TELEGRAM_RECEIVE: CapabilityPolicy(enabled=False, max_risk_level=RiskLevel.LOW)},
    )

    registry = build_tool_registry(settings, "http://127.0.0.1:8765")

    definitions = {d.name: d for d in registry.definitions}
    assert definitions["skills.use"].enabled is False


# ---- Lifecycle: manifest, install, uninstall (docs/archive/UI_UX_AUDIT.md Phase 5) ----


def test_slugify_produces_a_safe_filename_stem() -> None:
    assert slugify("Invoice Extraction!") == "invoice-extraction"
    assert slugify("  weird__chars***here  ") == "weird-chars-here"


def test_slugify_falls_back_to_a_generated_name_when_nothing_survives() -> None:
    assert slugify("!!!").startswith("skill-")


def test_write_skill_file_creates_a_well_formed_markdown_file_with_frontmatter(tmp_path) -> None:
    root = tmp_path / "skills"

    written = write_skill_file(
        str(root), "Invoice Extraction", "Pulls totals from PDFs.", "1. Read the PDF.\n2. Find the total.",
        version="2", tools=["filesystem.manage", "document.manage"],
    )

    assert written["name"] == "Invoice Extraction"
    assert written["version"] == "2"
    assert written["tools"] == ["document.manage", "filesystem.manage"]
    assert written["tools_declared"] is True
    assert written["body"] == "1. Read the PDF.\n2. Find the total."
    assert (root / "invoice-extraction.md").exists()


def test_write_skill_file_overwrites_an_existing_skill_with_the_same_name(tmp_path) -> None:
    root = tmp_path / "skills"
    write_skill_file(str(root), "My Skill", "v1 description", "v1 body")

    updated = write_skill_file(str(root), "My Skill", "v2 description", "v2 body", version="2")

    assert updated["description"] == "v2 description"
    assert updated["version"] == "2"
    assert len(list(root.glob("*.md"))) == 1


def test_delete_skill_file_removes_it_by_name_and_reports_whether_it_existed(tmp_path) -> None:
    root = tmp_path / "skills"
    write_skill_file(str(root), "Removable", "desc", "body")

    assert delete_skill_file(str(root), "Removable") is True
    assert delete_skill_file(str(root), "Removable") is False
    assert list(root.glob("*.md")) == []


def test_detect_referenced_tools_finds_known_tool_names_mentioned_in_the_body() -> None:
    body = "Use filesystem.manage to read the file, then http.request to post it. Never mention scheduling."
    known = ["filesystem.manage", "http.request", "schedule.manage", "browser.control"]

    assert detect_referenced_tools(body, known) == ["filesystem.manage", "http.request"]


def test_list_skills_detailed_infers_tools_only_when_not_declared(tmp_path) -> None:
    root = tmp_path / "skills"
    write_skill_file(str(root), "Declared", "desc", "mentions filesystem.manage", tools=["http.request"])
    write_skill_file(str(root), "Inferred", "desc", "mentions filesystem.manage in passing")

    skills = list_skills_detailed(str(root), ["filesystem.manage", "http.request"])
    by_name = {s["name"]: s for s in skills}

    assert by_name["Declared"]["tools"] == ["http.request"]
    assert by_name["Inferred"]["tools"] == ["filesystem.manage"]
    assert by_name["Inferred"]["tools_declared"] is False


def test_context_section_lists_skills_without_leaking_bodies(tmp_path) -> None:
    """The Operator must be told which skills exist.

    Nothing in prompts/ mentioned skills, and the Operator's context was only
    objective + memory + history + tool catalog - so an authored procedure for
    exactly the job at hand was reachable only if the model happened to guess
    that a `skills` tool existed and called `list` unprompted. Authored
    guidance sat unread while the model improvised.

    Bodies stay out for the same reason `list` omits them: this block is built
    into every single step's prompt, so it has to stay cheap.
    """
    _write_skill(tmp_path, "invoice.md")

    section = skills_context_section(str(tmp_path))

    assert "invoice-extraction" in section
    assert "How to pull the total from an invoice PDF." in section
    # Tells the model how to act on the list, not just that it exists.
    assert "read" in section.lower()
    assert "Find the total line" not in section


def test_context_section_is_empty_when_no_skills_are_installed(tmp_path) -> None:
    """No skills means no section at all - not an empty heading. A stray
    'Skills available' header with nothing under it invites the model to
    invent skill names to read."""
    assert skills_context_section(str(tmp_path)) == ""
