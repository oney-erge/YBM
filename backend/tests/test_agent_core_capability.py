"""YBM's built-in helpers have their own capability instead of borrowing Telegram's.

task.status, skills.use, persona.manage and knowledge.search are local reads of
YBM's own state. They ran under `telegram.receive`, so the Tools page described a
status check as "can read incoming Telegram messages" and turning Telegram's
receiving off would have silently removed all four. These pin the split, and the
upgrade path: a config written before it must keep working unchanged.
"""

from __future__ import annotations

import pytest

from agent_control.config import AppSettings, CapabilityPolicy
from agent_control.policy import PolicyEngine
from agent_control.schemas import Capability, RiskLevel, ToolCallRequest
from agent_control.tools.registry import build_tool_registry
from helpers import make_repos

CORE_TOOLS = ("task.status", "skills.use", "persona.manage", "knowledge.search")


def _definitions(settings: AppSettings, tmp_path) -> dict:
    repos, audit = make_repos(tmp_path)
    registry = build_tool_registry(
        settings, "http://127.0.0.1:8765", repositories=repos, task_repository=repos.tasks,
        artifact_repository=repos.artifacts, audit_logger=audit,
    )
    return {definition.name: definition for definition in registry.definitions}


def _on(**kw) -> CapabilityPolicy:
    return CapabilityPolicy(enabled=True, requires_approval=False, max_risk_level=RiskLevel.LOW, **kw)


def test_a_fresh_install_ships_agent_core_enabled_with_no_approval() -> None:
    policy = AppSettings(_env_file=None).capability_policy(Capability.AGENT_CORE)

    assert policy is not None and policy.enabled is True
    assert policy.requires_approval is False and policy.max_risk_level is RiskLevel.LOW


def test_the_four_helpers_declare_agent_core_not_telegram(tmp_path) -> None:
    definitions = _definitions(AppSettings(_env_file=None), tmp_path)

    for name in CORE_TOOLS:
        assert definitions[name].capability is Capability.AGENT_CORE, name
        assert definitions[name].enabled is True, name


def test_turning_telegram_receiving_off_no_longer_removes_them(tmp_path) -> None:
    settings = AppSettings(_env_file=None)
    settings.capabilities[Capability.TELEGRAM_RECEIVE] = CapabilityPolicy(enabled=False)

    definitions = _definitions(settings, tmp_path)

    for name in CORE_TOOLS:
        assert definitions[name].enabled is True, name


def test_agent_core_can_be_switched_off_on_its_own(tmp_path) -> None:
    settings = AppSettings(_env_file=None)
    settings.capabilities[Capability.AGENT_CORE] = CapabilityPolicy(enabled=False)

    definitions = _definitions(settings, tmp_path)

    for name in CORE_TOOLS:
        assert definitions[name].enabled is False, name


# ---- configs written before agent.core existed keep working ---------------------------------


def _legacy(**policies) -> AppSettings:
    """A config.yaml that lists capabilities but has never heard of agent.core."""
    return AppSettings(_env_file=None, capabilities={Capability.TELEGRAM_RECEIVE: policies["receive"]})


def test_an_older_config_follows_telegram_receive_so_nothing_disappears_on_upgrade(tmp_path) -> None:
    settings = _legacy(receive=_on())

    assert Capability.AGENT_CORE not in settings.capabilities  # as written
    definitions = _definitions(settings, tmp_path)
    for name in CORE_TOOLS:
        assert definitions[name].enabled is True, name


def test_an_older_config_that_had_receiving_off_still_has_the_helpers_off(tmp_path) -> None:
    """Following telegram.receive is the previous behaviour; the upgrade must not
    quietly widen what someone had deliberately switched off."""
    settings = _legacy(receive=CapabilityPolicy(enabled=False))

    definitions = _definitions(settings, tmp_path)

    for name in CORE_TOOLS:
        assert definitions[name].enabled is False, name


def test_an_explicit_agent_core_entry_wins_over_the_legacy_fallback() -> None:
    settings = _legacy(receive=_on())
    settings.capabilities[Capability.AGENT_CORE] = CapabilityPolicy(enabled=False)

    assert settings.capability_policy(Capability.AGENT_CORE).enabled is False


def test_policy_enforcement_uses_the_same_resolution(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    engine = PolicyEngine(_legacy(receive=_on()), audit)
    request = ToolCallRequest(
        task_id="t", tool_name="task.status", capability=Capability.AGENT_CORE,
        risk_level=RiskLevel.LOW, input={"operation": "status"},
    )

    decision = engine.evaluate(request)

    assert decision.allowed is True and decision.needs_approval is False
    assert repos  # the repositories fixture is only here to build the audit logger


@pytest.mark.parametrize("capability", [Capability.TELEGRAM_RECEIVE, Capability.TELEGRAM_SEND, Capability.LLM_GENERATE])
def test_the_other_default_capabilities_are_unchanged(capability) -> None:
    assert AppSettings(_env_file=None).capability_policy(capability).enabled is True


def test_the_shipped_example_config_lists_agent_core_enabled() -> None:
    from pathlib import Path

    import yaml

    example = Path(__file__).resolve().parents[2] / "config" / "config.example.yaml"
    shipped = yaml.safe_load(example.read_text(encoding="utf-8"))

    assert shipped["capabilities"]["agent.core"] == {
        "enabled": True, "scopes": [], "requires_approval": False, "max_risk_level": "low",
    }
    # And it loads.
    settings = AppSettings(_env_file=None, capabilities=shipped["capabilities"])
    assert settings.capability_policy(Capability.AGENT_CORE).enabled is True
