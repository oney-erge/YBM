from __future__ import annotations

import pytest
from pydantic import ValidationError

from agent_control.config import AppSettings, ConfigValidationError, is_loopback_host, load_settings
from agent_control.schemas import Capability


def test_autonomy_level_supports_nested_environment_config_and_reads_the_earlier_setting_only_when_it_is_absent(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGENT_OPERATOR__INITIATIVE_LEVEL", "4")
    assert AppSettings(_env_file=None).operator.effective_autonomy == 3  # the earlier 4 was "proactive": the autonomous level
    monkeypatch.setenv("AGENT_OPERATOR__INITIATIVE_LEVEL", "3")
    assert AppSettings(_env_file=None).operator.effective_autonomy == 2  # the earlier 3 was balanced
    monkeypatch.setenv("AGENT_OPERATOR__AUTONOMY_LEVEL", "1")
    assert AppSettings(_env_file=None).operator.effective_autonomy == 1  # the new setting wins


def test_default_invasive_capabilities_are_disabled(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    settings = AppSettings(_env_file=None)

    invasive = [
        Capability.TERMINAL_RUN,
        Capability.FILESYSTEM_READ,
        Capability.FILESYSTEM_WRITE,
        Capability.VSCODE_READ_STATE,
        Capability.VSCODE_WRITE_FILES,
        Capability.DESKTOP_SCREENSHOT,
        Capability.DESKTOP_CONTROL,
        Capability.BROWSER_OPEN,
        Capability.BROWSER_CONTROL,
        Capability.NETWORK_HTTP,
        Capability.SCHEDULE_MANAGE,
        Capability.GITHUB_PUSH,
        Capability.DEPENDENCIES_INSTALL,
    ]

    for capability in invasive:
        assert settings.capabilities[capability].enabled is False


def test_unknown_top_level_config_key_fails() -> None:
    with pytest.raises(ValidationError):
        AppSettings(_env_file=None, unknown_section={})


def test_invalid_capability_name_fails() -> None:
    with pytest.raises(ValidationError):
        AppSettings(
            _env_file=None,
            capabilities={"not.a.capability": {"enabled": True}},
        )


def test_load_settings_error_never_echoes_the_offending_value(tmp_path) -> None:
    """Settings come from `.env`, so pydantic's ``input_value=`` in a failure is
    the API key / bot token / vault key itself - and `ybm doctor` prints that
    text verbatim. The failure must stay debuggable without carrying the value."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text("server:\n  host: 127.0.0.1\n", encoding="utf-8")
    canary = "sk-live-CONFIGCANARY-4471"

    with pytest.raises(ConfigValidationError) as caught:
        load_settings(config_path=config_file, _env_file=None, server={"port": canary})

    rendered = f"{caught.value}\n{repr(caught.value)}"
    assert canary not in rendered
    # Still points at the exact field and reason, or it is useless in doctor.
    assert "server.port" in rendered
    # A chained cause would put the unredacted ValidationError back into any
    # rendered traceback.
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_config_validation_error_stays_catchable_as_value_error(tmp_path) -> None:
    """pydantic's ValidationError is a ValueError; callers that already caught
    that must keep working."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text("server:\n  host: 127.0.0.1\n", encoding="utf-8")

    with pytest.raises(ValueError):
        load_settings(config_path=config_file, _env_file=None, unknown_section={})


def test_doctor_config_check_reports_redacted_failure(tmp_path, monkeypatch) -> None:
    """The concrete leak path: doctor formats the raised exception into output
    users routinely paste into bug reports."""
    from agent_control import bootstrap

    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "config.yaml").write_text("server:\n  port: not-a-port\n", encoding="utf-8")

    check, settings = bootstrap._load_settings_checked()

    assert settings is None
    assert check.status == "fail"
    assert "not-a-port" not in check.detail
    assert "server.port" in check.detail


def test_minimal_llm_profile_validates() -> None:
    settings = AppSettings(
        _env_file=None,
        llm={"profiles": {"default": {"model": "local-model"}}},
    )

    assert settings.llm.profiles["default"].model == "local-model"


def test_safe_summary_redacts_secret_values() -> None:
    settings = AppSettings(
        _env_file=None,
        channels={"telegram": {"token": "super-secret-token"}},
        llm={"profiles": {"default": {"model": "x", "api_key": "secret-key"}}},
    )

    summary = settings.safe_summary()

    assert summary["channels"]["telegram"]["token"] == "***"
    assert summary["llm"]["profiles"]["default"]["api_key"] == "***"


def test_safe_summary_reports_whatsapp_count_not_raw_numbers() -> None:
    settings = AppSettings(
        _env_file=None,
        channels={"whatsapp": {"enabled": True, "allowed_numbers": ["15551234567", "19998887777"]}},
    )

    summary = settings.safe_summary()

    whatsapp = summary["channels"]["whatsapp"]
    assert whatsapp["enabled"] is True
    assert whatsapp["allowed_number_count"] == 2
    assert "allowed_numbers" not in whatsapp
    assert "15551234567" not in str(whatsapp)


def test_safe_summary_strips_mcp_server_env_values() -> None:
    settings = AppSettings(
        _env_file=None,
        mcp={
            "servers": {
                "github": {
                    "command": "npx",
                    "env": {"GITHUB_TOKEN": "ghp_super_secret"},
                }
            }
        },
    )

    summary = settings.safe_summary()
    server = summary["mcp"]["servers"]["github"]

    assert server["env_keys"] == ["GITHUB_TOKEN"]
    assert "env" not in server
    assert "ghp_super_secret" not in str(summary)


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",
        "127.0.0.2",  # regression: only the exact 127.0.0.1 literal used to count
        "127.255.255.255",  # full 127.0.0.0/8 range
        "localhost",
        "::1",
        "0:0:0:0:0:0:0:1",
        "::ffff:127.0.0.1",  # regression: IPv4-mapped IPv6 loopback used to count as non-loopback
    ],
)
def test_is_loopback_host_covers_full_loopback_range(host: str) -> None:
    assert is_loopback_host(host) is True


@pytest.mark.parametrize(
    "host",
    [
        "0.0.0.0",
        "10.0.0.5",
        "192.168.1.1",
        # An IPv4-mapped *public* address must not inherit loopback just
        # because it is unwrapped - the mapping is followed, not assumed safe.
        "::ffff:8.8.8.8",
        "example.com",
        "",
        "not-an-ip",
    ],
)
def test_is_loopback_host_rejects_non_loopback(host: str) -> None:
    assert is_loopback_host(host) is False


def test_settings_nothing_read_are_removed_and_an_old_config_that_still_sets_them_loads(tmp_path, caplog) -> None:
    """docs/PLAN.md C3: eleven settings that no code read (and a whole terminal adapter block) are gone; a config.yaml written before still loads, with a warning, not an error."""
    import yaml

    from agent_control.config import REMOVED_SETTINGS, AppSettings, load_settings

    old = {
        "identity": {"instance_name": "ybm-old", "owner_label": "me"},
        "approval_policy": {"approval_token_ttl_seconds": 60},
        "limits": {"tool_timeout_seconds": 5, "max_log_chars": 500},
        "adapters": {"terminal": {"enabled": True, "blocked_command_patterns": ["x"]}, "code_interpreter": {"jupyter": {"enabled": True}, "session_ttl_seconds": 60}, "desktop": {"screenshot_interval_seconds": 3}},
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(old), encoding="utf-8")
    with caplog.at_level("WARNING", logger="agent_control.config"):
        settings = load_settings(path)
    assert settings.identity.instance_name == "ybm-old"
    assert sum("was removed" in record.getMessage() for record in caplog.records) == len(REMOVED_SETTINGS)
    for gone in ("owner_label",):
        assert not hasattr(settings.identity, gone)
    assert not hasattr(settings.adapters, "terminal") and not hasattr(settings.adapters.code_interpreter, "jupyter")
    with pytest.raises(ValidationError):
        AppSettings(_env_file=None, identity={"not_a_setting": 1})  # a key that was never a setting is still refused
