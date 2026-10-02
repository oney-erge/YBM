"""First-run model detection: pick from what is already on the machine, free.

Every case injects the environment and the Ollama probe, so none of these touch
the real network or depend on what the machine running them has installed.
"""

from __future__ import annotations

import yaml

from agent_control.config_sync import ConfigManager
from agent_control.llm import autodetect, catalog


def _env(**values: str):
    return lambda key: values.get(key)


def _no_ollama() -> list[str]:
    return []


def test_a_configured_localdeploy_wins_over_everything() -> None:
    found = autodetect.detect_model(
        read_env=_env(YBM_LOCALDEPLOY_ROOT="C:/LocalDeploy", OPENAI_API_KEY="sk-test-value"),
        ollama_models=lambda: ["qwen3:8b"],
    )

    assert found is not None
    assert found.source == "localdeploy"
    # The shipped profile already exists in config.example.yaml; nothing to write.
    assert found.profile is None
    assert found.profile_name == autodetect.LOCALDEPLOY_PROFILE_NAME


def test_a_running_ollama_beats_a_cloud_key_so_nothing_is_spent_by_default() -> None:
    found = autodetect.detect_model(
        read_env=_env(OPENAI_API_KEY="sk-test-value"),
        ollama_models=lambda: ["obscure:latest", "qwen3-vl:8b-instruct"],
    )

    assert found is not None
    assert found.source == "ollama"
    assert found.profile is not None
    assert found.profile["model"] == "qwen3-vl:8b-instruct"
    assert found.profile["api_key_env"] is None
    assert found.profile["base_url"] == "http://127.0.0.1:11434/v1"


def test_ollama_with_no_recognised_model_still_uses_the_first_installed_one() -> None:
    found = autodetect.detect_model(read_env=_env(), ollama_models=lambda: ["obscure-a:1", "obscure-b:1"])

    assert found is not None
    assert found.profile is not None
    assert found.profile["model"] == "obscure-a:1"


def test_a_cloud_key_in_the_environment_is_adopted_without_being_shown() -> None:
    secret = "sk-this-must-never-be-printed-123456"
    found = autodetect.detect_model(read_env=_env(OPENAI_API_KEY=secret), ollama_models=_no_ollama)

    assert found is not None
    assert found.source == "api_key"
    assert found.env_var == "OPENAI_API_KEY"
    assert found.profile is not None
    assert found.profile["api_key_env"] == "OPENAI_API_KEY"
    assert found.profile["base_url"] == "https://api.openai.com/v1"
    # Only the variable's name is ever recorded or shown, never its value.
    assert secret not in found.label
    assert secret not in str(found.profile)
    assert "OPENAI_API_KEY" in found.label


def test_anthropic_uses_the_native_provider_with_no_base_url() -> None:
    found = autodetect.detect_model(read_env=_env(ANTHROPIC_API_KEY="k"), ollama_models=_no_ollama)

    assert found is not None
    assert found.profile is not None
    assert found.profile["provider"] == "anthropic"
    assert found.profile["base_url"] is None


def test_provider_order_is_deterministic_when_several_keys_exist() -> None:
    found = autodetect.detect_model(
        read_env=_env(OPENAI_API_KEY="a", ANTHROPIC_API_KEY="b", GROQ_API_KEY="c"),
        ollama_models=_no_ollama,
    )

    assert found is not None
    assert found.env_var == "ANTHROPIC_API_KEY"


def test_every_cloud_provider_in_the_order_list_is_in_the_catalog_with_a_key_variable() -> None:
    for key in autodetect.CLOUD_PROVIDER_ORDER:
        spec = catalog.get(key)
        assert spec is not None, key
        assert spec.api_key_env, key


def test_nothing_found_returns_none_rather_than_guessing() -> None:
    assert autodetect.detect_model(read_env=_env(), ollama_models=_no_ollama) is None


def test_the_ollama_probe_returns_nothing_instead_of_raising_when_it_is_unreachable(monkeypatch) -> None:
    from urllib.error import URLError

    def refuse(*args, **kwargs):
        raise URLError("connection refused")

    monkeypatch.setattr(autodetect, "urlopen", refuse)

    assert autodetect._installed_ollama_models() == []


# ---- writing the result into config.yaml ---------------------------------


def _manager(tmp_path, config: dict) -> ConfigManager:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return ConfigManager(config_path=config_path, env_path=tmp_path / ".env")


def test_auto_configure_fills_in_an_empty_default_profile(tmp_path) -> None:
    manager = _manager(tmp_path, {"llm": {"default_profile": "", "profiles": {}}, "server": {"port": 8765}})

    detected = autodetect.auto_configure_llm(manager, read_env=_env(OPENAI_API_KEY="k"), ollama_models=_no_ollama)

    written = manager.read_config()
    assert detected is not None
    assert written["llm"]["default_profile"] == "onboard"
    assert written["llm"]["profiles"]["onboard"]["api_key_env"] == "OPENAI_API_KEY"
    # Unrelated settings survive.
    assert written["server"] == {"port": 8765}


def test_auto_configure_replaces_a_default_that_points_at_a_missing_profile(tmp_path) -> None:
    manager = _manager(tmp_path, {"llm": {"default_profile": "gone", "profiles": {"other": {"model": "m"}}}})

    autodetect.auto_configure_llm(manager, read_env=_env(GROQ_API_KEY="k"), ollama_models=_no_ollama)

    written = manager.read_config()
    assert written["llm"]["default_profile"] == "onboard"
    assert "other" in written["llm"]["profiles"]


def test_auto_configure_never_overwrites_a_model_the_person_already_chose(tmp_path) -> None:
    manager = _manager(
        tmp_path,
        {"llm": {"default_profile": "mine", "profiles": {"mine": {"provider": "openai_compatible", "model": "m"}}}},
    )
    before = manager.read_config()

    detected = autodetect.auto_configure_llm(manager, read_env=_env(OPENAI_API_KEY="k"), ollama_models=lambda: ["x"])

    assert detected is None
    assert manager.read_config() == before


def test_auto_configure_points_at_the_shipped_localdeploy_profile_without_rewriting_it(tmp_path) -> None:
    shipped = {"provider": "openai_compatible", "model": "qwen3vl_8b_ollama", "base_url": "http://127.0.0.1:8000/v1"}
    manager = _manager(
        tmp_path,
        {"llm": {"default_profile": "", "profiles": {autodetect.LOCALDEPLOY_PROFILE_NAME: shipped}}},
    )

    detected = autodetect.auto_configure_llm(manager, read_env=_env(YBM_LOCALDEPLOY_ROOT="C:/LD"), ollama_models=_no_ollama)

    written = manager.read_config()
    assert detected is not None and detected.source == "localdeploy"
    assert written["llm"]["default_profile"] == autodetect.LOCALDEPLOY_PROFILE_NAME
    assert written["llm"]["profiles"][autodetect.LOCALDEPLOY_PROFILE_NAME] == shipped


def test_auto_configure_leaves_a_hand_edited_config_alone_when_the_localdeploy_profile_is_gone(tmp_path) -> None:
    manager = _manager(tmp_path, {"llm": {"default_profile": "", "profiles": {}}})
    before = manager.read_config()

    detected = autodetect.auto_configure_llm(manager, read_env=_env(YBM_LOCALDEPLOY_ROOT="C:/LD"), ollama_models=_no_ollama)

    assert detected is None
    assert manager.read_config() == before


def test_auto_configure_with_nothing_available_writes_nothing(tmp_path) -> None:
    manager = _manager(tmp_path, {"llm": {"default_profile": "", "profiles": {}}})
    before = (tmp_path / "config.yaml").read_text(encoding="utf-8")

    assert autodetect.auto_configure_llm(manager, read_env=_env(), ollama_models=_no_ollama) is None

    assert (tmp_path / "config.yaml").read_text(encoding="utf-8") == before
