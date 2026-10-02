"""The first-run experience: nothing to ask, nothing noisy, nothing misleading.

These pin the behaviours the launchers and the console rely on so a fresh
install picks a model, signs the browser in, and prints only what a person
needs - none of which a unit test of one function would catch breaking.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import yaml

from agent_control import bootstrap
from agent_control.config import AppSettings
from agent_control.config_sync import read_env_value
from agent_control.llm import autodetect, catalog

REPO_EXAMPLE_CONFIG = Path(__file__).resolve().parents[2] / "config" / "config.example.yaml"


def _fresh_install(monkeypatch, tmp_path) -> None:
    """A directory with only config.example.yaml, and a machine with no keys."""
    monkeypatch.chdir(tmp_path)
    for name in ("AGENT_ADMIN_TOKEN", "AGENT_SECRET_VAULT_KEY", "TELEGRAM_BOT_TOKEN", "YBM_LOCALDEPLOY_ROOT"):
        monkeypatch.delenv(name, raising=False)
    for spec in catalog.PROVIDERS:
        if spec.api_key_env:
            monkeypatch.delenv(spec.api_key_env, raising=False)
    monkeypatch.setattr(autodetect, "_installed_ollama_models", lambda timeout=2.0: [])
    (tmp_path / "config").mkdir()
    shutil.copy(REPO_EXAMPLE_CONFIG, tmp_path / "config" / "config.example.yaml")


def _config(tmp_path) -> dict:
    return yaml.safe_load((tmp_path / "config" / "config.yaml").read_text(encoding="utf-8"))


# ---- the shipped config no longer points at the author's machine -----------


def test_the_shipped_config_names_no_default_model_and_no_paid_fallback() -> None:
    shipped = yaml.safe_load(REPO_EXAMPLE_CONFIG.read_text(encoding="utf-8"))

    assert shipped["llm"]["default_profile"] == ""
    assert shipped["llm"]["fallback_profile"] is None
    assert shipped["llm"]["major_profile"] is None


def test_the_shipped_config_still_loads_and_reports_no_model(monkeypatch, tmp_path) -> None:
    _fresh_install(monkeypatch, tmp_path)
    bootstrap.run_setup(quiet=True)

    settings = bootstrap.load_settings()

    assert settings.llm.default_profile == ""
    assert bootstrap.check_llm_configured(settings) is False


# ---- setup picks a model, and says so ---------------------------------------


def test_setup_adopts_a_provider_key_already_in_the_environment(monkeypatch, tmp_path, capsys) -> None:
    _fresh_install(monkeypatch, tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake-for-test")

    assert bootstrap.run_setup(quiet=True) == 0

    llm = _config(tmp_path)["llm"]
    assert llm["default_profile"] == "onboard"
    assert llm["profiles"]["onboard"]["api_key_env"] == "OPENAI_API_KEY"
    assert bootstrap.check_llm_configured(bootstrap.load_settings()) is True
    out = capsys.readouterr().out
    # The decision made on the person's behalf is always reported ...
    assert "model: using OpenAI" in out
    assert "OPENAI_API_KEY" in out
    # ... but the key itself never is.
    assert "sk-fake-for-test" not in out


def test_setup_keeps_a_model_the_person_already_chose(monkeypatch, tmp_path) -> None:
    _fresh_install(monkeypatch, tmp_path)
    bootstrap.run_setup(quiet=True)
    config = _config(tmp_path)
    config["llm"]["default_profile"] = "mine"
    config["llm"]["profiles"]["mine"] = {"provider": "openai_compatible", "model": "m", "base_url": "http://x/v1"}
    (tmp_path / "config" / "config.yaml").write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake-for-test")

    bootstrap.run_setup(quiet=True)

    assert _config(tmp_path)["llm"]["default_profile"] == "mine"


def test_setup_with_no_model_available_says_so_plainly_unless_quiet(monkeypatch, tmp_path, capsys) -> None:
    _fresh_install(monkeypatch, tmp_path)

    bootstrap.run_setup()

    assert "model: none found yet" in capsys.readouterr().out


# ---- quiet mode: only decisions, no developer hints ----------------------------


def test_quiet_setup_prints_no_developer_hints(monkeypatch, tmp_path, capsys) -> None:
    _fresh_install(monkeypatch, tmp_path)

    assert bootstrap.run_setup(quiet=True) == 0

    out = capsys.readouterr().out
    for noise in ("YBM setup", "Next:", "TELEGRAM_BOT_TOKEN", "YBM_LOCALDEPLOY_ROOT", "generated AGENT", "database ready"):
        assert noise not in out, noise
    # The secrets are still generated, just not narrated.
    assert read_env_value("AGENT_ADMIN_TOKEN")
    assert read_env_value("AGENT_SECRET_VAULT_KEY")


def test_plain_setup_still_narrates_for_a_developer(monkeypatch, tmp_path, capsys) -> None:
    _fresh_install(monkeypatch, tmp_path)

    bootstrap.run_setup()

    out = capsys.readouterr().out
    assert "generated AGENT_ADMIN_TOKEN" in out
    assert "Next:" in out


# ---- node / whatsapp are only touched when something needs them -------------


def test_setup_does_not_install_whatsapp_dependencies_when_whatsapp_is_off(monkeypatch, tmp_path) -> None:
    _fresh_install(monkeypatch, tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(bootstrap, "_install_whatsapp_bridge_deps", lambda: calls.append("installed"))

    bootstrap.run_setup(quiet=True)

    assert calls == []


def test_setup_installs_whatsapp_dependencies_when_whatsapp_is_already_on(monkeypatch, tmp_path) -> None:
    _fresh_install(monkeypatch, tmp_path)
    example = yaml.safe_load(REPO_EXAMPLE_CONFIG.read_text(encoding="utf-8"))
    example["channels"]["whatsapp"]["enabled"] = True
    (tmp_path / "config" / "config.yaml").write_text(yaml.safe_dump(example, sort_keys=False), encoding="utf-8")
    calls: list[str] = []
    monkeypatch.setattr(bootstrap, "_install_whatsapp_bridge_deps", lambda: calls.append("installed"))

    bootstrap.run_setup(quiet=True)

    assert calls == ["installed"]


def test_a_prebuilt_console_is_never_rebuilt_and_never_complains_about_node(monkeypatch, tmp_path, capsys) -> None:
    """Release archives and the container ship the console prebuilt, with no
    fingerprint file. Previously setup tried to build it anyway and told people
    their Node.js was too old for something they did not need to do."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "frontend" / "src").mkdir(parents=True)
    (tmp_path / "frontend" / "src" / "App.tsx").write_text("x", encoding="utf-8")
    static = tmp_path / "backend" / "src" / "agent_control" / "static" / "admin"
    static.mkdir(parents=True)
    (static / "index.html").write_text("<html></html>", encoding="utf-8")
    ran: list[list[str]] = []
    monkeypatch.setattr(bootstrap.subprocess, "run", lambda cmd, **kw: ran.append(cmd))

    bootstrap._build_admin_console()

    assert ran == []
    assert capsys.readouterr().out == ""


def test_node_is_not_reported_when_nothing_needs_it(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    static = tmp_path / "backend" / "src" / "agent_control" / "static" / "admin"
    static.mkdir(parents=True)
    (static / "index.html").write_text("<html></html>", encoding="utf-8")
    monkeypatch.setattr(bootstrap.shutil, "which", lambda name: None)

    check = bootstrap._check_node(AppSettings(_env_file=None), assume_needed=False)

    assert check.status == "ok"
    assert "not needed" in check.detail


def test_node_is_still_reported_when_the_console_must_be_built(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(bootstrap.shutil, "which", lambda name: None)

    check = bootstrap._check_node(AppSettings(_env_file=None), assume_needed=False)

    assert check.status == "warn"


def test_node_is_still_reported_when_whatsapp_is_on(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    static = tmp_path / "backend" / "src" / "agent_control" / "static" / "admin"
    static.mkdir(parents=True)
    (static / "index.html").write_text("<html></html>", encoding="utf-8")
    monkeypatch.setattr(bootstrap.shutil, "which", lambda name: None)
    settings = AppSettings(_env_file=None, channels={"whatsapp": {"enabled": True}})

    assert bootstrap._check_node(settings, assume_needed=False).status == "warn"


# ---- ports: free is the normal state, not a warning ---------------------------


def test_a_free_port_is_not_a_warning(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("YBM_LOCALDEPLOY_ROOT", raising=False)
    monkeypatch.setattr(bootstrap, "_port_listening", lambda port, **kw: False)

    checks = bootstrap._check_ports(AppSettings(_env_file=None))

    assert [c.status for c in checks] == ["ok"]
    assert [c.name for c in checks] == ["Port 8765 (Backend)"]


def test_the_backend_port_checked_is_the_configured_one(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("YBM_LOCALDEPLOY_ROOT", raising=False)
    probed: list[int] = []
    monkeypatch.setattr(bootstrap, "_port_listening", lambda port, **kw: probed.append(port) or False)

    checks = bootstrap._check_ports(AppSettings(_env_file=None, server={"port": 9123}))

    assert [c.name for c in checks] == ["Port 9123 (Backend)"]
    assert probed == [9123]


def test_the_localdeploy_port_is_only_listed_for_installs_that_use_it(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(bootstrap, "_port_listening", lambda port, **kw: False)

    monkeypatch.setenv("YBM_LOCALDEPLOY_ROOT", "C:/LocalDeploy")
    assert "Port 8000 (LocalDeploy)" in [c.name for c in bootstrap._check_ports(AppSettings(_env_file=None))]

    monkeypatch.delenv("YBM_LOCALDEPLOY_ROOT")
    settings = AppSettings(
        _env_file=None,
        llm={
            "default_profile": "ld",
            "profiles": {"ld": {"model": "m", "base_url": "http://127.0.0.1:8000/v1"}},
        },
    )
    assert "Port 8000 (LocalDeploy)" in [c.name for c in bootstrap._check_ports(settings)]


# ---- doctor --quiet --------------------------------------------------------------


def test_quiet_doctor_says_one_line_when_everything_is_fine(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        bootstrap, "collect_checks",
        lambda: [bootstrap.Check("A", "ok"), bootstrap.Check("B", "ok", "fine"), bootstrap.Check("C", "ok")],
    )

    assert bootstrap.run_doctor(quiet=True) == 0

    assert capsys.readouterr().out.strip() == "All 3 checks passed."


def test_quiet_doctor_shows_only_what_needs_attention(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        bootstrap, "collect_checks",
        lambda: [bootstrap.Check("Fine", "ok"), bootstrap.Check("Meh", "warn", "look at me"), bootstrap.Check("Fine2", "ok")],
    )

    assert bootstrap.run_doctor(quiet=True) == 0

    out = capsys.readouterr().out
    assert "look at me" in out
    assert "Fine" not in out
    assert "2 ok, 1 warning(s), 0 failure(s)" in out


def test_quiet_doctor_still_fails_on_a_failure(monkeypatch, capsys) -> None:
    monkeypatch.setattr(bootstrap, "collect_checks", lambda: [bootstrap.Check("Broken", "fail", "nope")])

    assert bootstrap.run_doctor(quiet=True) == 1

    assert "nope" in capsys.readouterr().out


def test_full_doctor_still_lists_every_check(monkeypatch, capsys) -> None:
    monkeypatch.setattr(bootstrap, "collect_checks", lambda: [bootstrap.Check("Fine", "ok"), bootstrap.Check("Meh", "warn")])

    bootstrap.run_doctor()

    out = capsys.readouterr().out
    assert "Fine" in out and "Meh" in out


# ---- the console address everything opens ------------------------------------------


def _settings(**server) -> AppSettings:
    return AppSettings(_env_file=None, server=server)


def test_the_console_url_carries_the_generated_token_so_nobody_has_to_paste_it(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGENT_ADMIN_TOKEN", "tok_with+odd/chars=")

    url = bootstrap.admin_console_url(_settings())

    parsed = urlparse(url)
    assert (parsed.scheme, parsed.netloc, parsed.path) == ("http", "127.0.0.1:8765", "/admin")
    assert parse_qs(parsed.query)["token"] == ["tok_with+odd/chars="]


def test_the_console_url_has_no_query_when_there_is_no_token(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AGENT_ADMIN_TOKEN", raising=False)

    assert bootstrap.admin_console_url(_settings()) == "http://127.0.0.1:8765/admin"


def test_the_console_url_can_be_printed_without_the_token(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGENT_ADMIN_TOKEN", "secret-token")

    assert "secret-token" not in bootstrap.admin_console_url(_settings(), with_token=False)


def test_the_console_url_follows_a_custom_port_and_rewrites_a_wildcard_bind(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AGENT_ADMIN_TOKEN", raising=False)

    assert bootstrap.admin_console_url(_settings(port=9001)) == "http://127.0.0.1:9001/admin"
    assert bootstrap.admin_console_url(_settings(host="0.0.0.0", port=9001)) == "http://127.0.0.1:9001/admin"


# ---- a correct fresh install does not warn about itself ----------------------


def test_a_default_install_reports_no_configuration_warnings() -> None:
    """Every capability starts off and no model is chosen yet; neither is a
    misconfiguration, and the console already says "no model" where it matters.
    The scheduler line used to flag the shipped default as broken."""
    from agent_control import admin as admin_module

    assert admin_module._config_warnings(AppSettings(_env_file=None)) == []


def test_enabling_telegram_with_no_model_is_still_worth_a_warning() -> None:
    from agent_control import admin as admin_module

    settings = AppSettings(
        _env_file=None,
        channels={"telegram": {"enabled": True, "allowed_user_ids": [1]}},
    )

    warnings = admin_module._config_warnings(settings)

    assert any("no model is configured" in w for w in warnings)
