"""docs/archive/HISTORY.md Part 4 T2.5: a single global identity/preference document,
distinct from channels/memory.py's per-conversation ConversationMemoryService.
"""

from __future__ import annotations

import pytest

from agent_control.config import AppSettings, Capability, CapabilityPolicy, PersonaAdapterConfig, RiskLevel
from agent_control.persona import DEFAULT_PERSONA, persona_prompt_section, read_persona, write_persona
from agent_control.schemas import ToolCallRequest, ToolResultStatus
from agent_control.tools.registry import build_tool_registry
from agent_control.tools.persona import PersonaAdapter


def _request(operation: str, **payload) -> ToolCallRequest:
    return ToolCallRequest(
        task_id="task_persona",
        tool_name="persona.manage",
        capability=Capability.TELEGRAM_RECEIVE,
        input={"operation": operation, **payload},
    )


def test_read_persona_returns_default_when_file_missing(tmp_path) -> None:
    config = PersonaAdapterConfig(path=str(tmp_path / "persona.md"))

    assert read_persona(config) == DEFAULT_PERSONA


def test_write_then_read_round_trips(tmp_path) -> None:
    config = PersonaAdapterConfig(path=str(tmp_path / "persona.md"))

    write_persona(config, "Prefers concise answers. Timezone: America/Chicago.")

    assert read_persona(config) == "Prefers concise answers. Timezone: America/Chicago."


def test_write_persona_creates_parent_directories(tmp_path) -> None:
    config = PersonaAdapterConfig(path=str(tmp_path / "nested" / "dir" / "persona.md"))

    write_persona(config, "some preference")

    assert read_persona(config) == "some preference"


def test_write_persona_rejects_content_over_max_chars(tmp_path) -> None:
    config = PersonaAdapterConfig(path=str(tmp_path / "persona.md"), max_chars=10)

    with pytest.raises(ValueError):
        write_persona(config, "this is way more than ten characters")


def test_read_persona_truncates_to_max_chars(tmp_path) -> None:
    path = tmp_path / "persona.md"
    path.write_text("x" * 100, encoding="utf-8")
    config = PersonaAdapterConfig(path=str(path), max_chars=20)

    result = read_persona(config)

    assert len(result) == 20
    assert result.endswith("...")


def test_persona_prompt_section_empty_when_nothing_recorded(tmp_path) -> None:
    config = PersonaAdapterConfig(path=str(tmp_path / "persona.md"))

    assert persona_prompt_section(config) == ""


def test_persona_prompt_section_empty_when_disabled(tmp_path) -> None:
    path = tmp_path / "persona.md"
    path.write_text("some preference", encoding="utf-8")
    config = PersonaAdapterConfig(path=str(path), enabled=False)

    assert persona_prompt_section(config) == ""


def test_persona_prompt_section_includes_content_when_present(tmp_path) -> None:
    path = tmp_path / "persona.md"
    path.write_text("Prefers concise answers.", encoding="utf-8")
    config = PersonaAdapterConfig(path=str(path))

    section = persona_prompt_section(config)

    assert "Prefers concise answers." in section
    assert "persona" in section.lower()


@pytest.mark.asyncio
async def test_persona_adapter_get_returns_current_content(tmp_path) -> None:
    path = tmp_path / "persona.md"
    path.write_text("existing preference", encoding="utf-8")
    adapter = PersonaAdapter(PersonaAdapterConfig(path=str(path)))

    result = await adapter.execute(_request("get"))

    assert result.status == ToolResultStatus.SUCCEEDED
    assert result.output["content"] == "existing preference"


@pytest.mark.asyncio
async def test_persona_adapter_update_replaces_content(tmp_path) -> None:
    path = tmp_path / "persona.md"
    path.write_text("old preference", encoding="utf-8")
    adapter = PersonaAdapter(PersonaAdapterConfig(path=str(path)))

    result = await adapter.execute(_request("update", content="new preference"))

    assert result.status == ToolResultStatus.SUCCEEDED
    assert result.output["content"] == "new preference"
    assert path.read_text(encoding="utf-8") == "new preference"


@pytest.mark.asyncio
async def test_persona_adapter_update_without_content_fails_validation() -> None:
    from agent_control.tools.contracts import PersonaInput
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        PersonaInput(operation="update")


@pytest.mark.asyncio
async def test_persona_adapter_update_over_limit_fails_cleanly(tmp_path) -> None:
    adapter = PersonaAdapter(PersonaAdapterConfig(path=str(tmp_path / "persona.md"), max_chars=5))

    result = await adapter.execute(_request("update", content="way too long for the limit"))

    assert result.status == ToolResultStatus.FAILED
    assert "max_chars" in result.error_message


def test_registry_exposes_persona_manage_when_telegram_receive_is_enabled() -> None:
    settings = AppSettings(_env_file=None)

    registry = build_tool_registry(settings, "http://127.0.0.1:8765")

    definitions = {d.name: d for d in registry.definitions}
    assert definitions["persona.manage"].enabled is True
    assert "persona.manage" in registry.adapters


def test_registry_disables_persona_manage_when_adapter_disabled_in_config() -> None:
    settings = AppSettings(_env_file=None, adapters={"persona": {"enabled": False}})

    registry = build_tool_registry(settings, "http://127.0.0.1:8765")

    definitions = {d.name: d for d in registry.definitions}
    assert definitions["persona.manage"].enabled is False
    assert "persona.manage" not in registry.adapters


def test_registry_disables_persona_manage_when_telegram_receive_capability_is_off() -> None:
    settings = AppSettings(
        _env_file=None,
        capabilities={Capability.TELEGRAM_RECEIVE: CapabilityPolicy(enabled=False, max_risk_level=RiskLevel.LOW)},
    )

    registry = build_tool_registry(settings, "http://127.0.0.1:8765")

    definitions = {d.name: d for d in registry.definitions}
    assert definitions["persona.manage"].enabled is False


def test_worker_config_context_includes_persona_when_recorded(tmp_path, monkeypatch) -> None:
    """The Operator's actual prompt-building path (cli.py's
    _worker_config_context, which becomes config_context/config_context_factory
    on TaskWorker) must include the persona section - not just the standalone
    persona_prompt_section() helper in isolation."""
    from agent_control import cli
    from agent_control.tools.registry import build_tool_registry

    monkeypatch.chdir(tmp_path)
    persona_path = tmp_path / "persona.md"
    persona_path.write_text("Timezone: America/Chicago. Prefers terse answers.", encoding="utf-8")
    settings = AppSettings(_env_file=None, adapters={"persona": {"path": str(persona_path)}})
    registry = build_tool_registry(settings, "http://127.0.0.1:8765")

    context = cli._worker_config_context(registry, settings)

    assert "Timezone: America/Chicago. Prefers terse answers." in context


def test_worker_config_context_omits_persona_section_when_nothing_recorded(tmp_path, monkeypatch) -> None:
    from agent_control import cli
    from agent_control.tools.registry import build_tool_registry

    monkeypatch.chdir(tmp_path)
    settings = AppSettings(_env_file=None, adapters={"persona": {"path": str(tmp_path / "persona.md")}})
    registry = build_tool_registry(settings, "http://127.0.0.1:8765")

    context = cli._worker_config_context(registry, settings)

    assert "User persona and preferences" not in context
