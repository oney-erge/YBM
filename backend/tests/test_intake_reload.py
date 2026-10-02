"""Telegram and WhatsApp intake follow a model change instead of a startup snapshot.

The task worker already follows config.yaml and .env (test_worker_reload.py). The
two intake processes built their classifier, responder and memory service once,
so a model chosen or changed later reached tasks but not the first-line chat
replies and task classification until everything was restarted. These pin that a
running intake now picks it up, and that a bad edit cannot take it down.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import yaml

from agent_control import cli
from agent_control.config_sync import config_fingerprint
from helpers import make_repos


class _Audit:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def append(self, event_type, actor=None, task_id=None, payload=None) -> None:
        self.events.append({"actor": actor, "payload": payload})


def _bump_mtime(path, seconds: int = 5) -> None:
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + seconds * 1_000_000_000))


def _service() -> SimpleNamespace:
    return SimpleNamespace(settings="old", classifier="old", responder="old", memory_service="old")


def _reloader(tmp_path, build, service=None):
    watched = (tmp_path / "config.yaml", tmp_path / ".env")
    audit = _Audit()
    service = service or _service()
    return cli.IntakeModelReloader(service, build, audit, actor="telegram_polling", watched=watched), service, audit, watched


# ---- the real thing: a different model chosen while the intake is running ----------------------


def _profile(model: str) -> dict:
    return {"llm": {"default_profile": "onboard", "profiles": {"onboard": {
        "provider": "openai_compatible", "model": model,
        "base_url": "https://api.openai.com/v1", "api_key_env": "OPENAI_API_KEY",
    }}}}


def test_a_model_chosen_after_startup_reaches_a_running_intake(monkeypatch, tmp_path) -> None:
    """Saving a key already worked (the provider reads .env on every call); choosing a *different
    model* did not, because the classifier and responder held the startup profile."""
    monkeypatch.chdir(tmp_path)
    for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "AGENT_CAPABILITIES"):
        monkeypatch.delenv(name, raising=False)
    config_path = tmp_path / "config" / "config.yaml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(yaml.safe_dump(_profile("gpt-4.1")), encoding="utf-8")
    repos, _ = make_repos(tmp_path)
    settings = cli.load_settings()
    classifier, responder, memory = cli._concierge_parts(settings, repos)
    assert classifier.provider.profile.model == "gpt-4.1"
    service = SimpleNamespace(settings=settings, classifier=classifier, responder=responder, memory_service=memory)
    reloader = cli.IntakeModelReloader(service, lambda: cli._intake_parts(repos), _Audit(), actor="whatsapp_polling")

    # What the console does when a different model is chosen.
    config_path.write_text(yaml.safe_dump(_profile("gpt-4.1-mini")), encoding="utf-8")
    _bump_mtime(config_path)

    assert reloader() is True
    assert service.classifier.provider.profile.model == "gpt-4.1-mini"
    assert service.responder.provider.profile.model == "gpt-4.1-mini"
    assert service.settings.llm.profiles["onboard"].model == "gpt-4.1-mini"
    assert service.memory_service is not memory


def test_the_concierge_parts_are_empty_without_any_model(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "AGENT_CAPABILITIES"):
        monkeypatch.delenv(name, raising=False)
    repos, _ = make_repos(tmp_path)

    classifier, responder, memory = cli._concierge_parts(cli.load_settings(), repos)

    assert classifier is None and responder is None
    assert memory is not None  # the plain-text path still needs conversation memory


# ---- the reloader itself --------------------------------------------------------------------


def test_nothing_is_rebuilt_while_the_files_are_unchanged(tmp_path) -> None:
    builds: list[int] = []
    reloader, service, _, _ = _reloader(tmp_path, lambda: builds.append(1) or ("s", "c", "r", "m"))

    assert reloader() is False
    assert reloader() is False

    assert builds == []
    assert service.classifier == "old"


def test_a_change_swaps_every_part_onto_the_live_service_once(tmp_path) -> None:
    builds: list[int] = []

    def build():
        builds.append(1)
        return ("settings-2", "classifier-2", "responder-2", "memory-2")

    reloader, service, _, (config, _) = _reloader(tmp_path, build)

    config.write_text("llm: {}\n", encoding="utf-8")
    assert reloader() is True
    assert (service.settings, service.classifier, service.responder, service.memory_service) == (
        "settings-2", "classifier-2", "responder-2", "memory-2",
    )

    assert reloader() is False  # the same change is not applied twice
    assert builds == [1]


def test_creating_the_env_file_counts_as_a_change(tmp_path) -> None:
    reloader, service, _, (_, env) = _reloader(tmp_path, lambda: ("s", "c", "r", "m"))

    env.write_text("OPENAI_API_KEY=x\n", encoding="utf-8")

    assert reloader() is True
    assert service.classifier == "c"


def test_a_broken_edit_is_reported_and_the_previous_parts_keep_working(tmp_path) -> None:
    state = {"fail": True}

    def build():
        if state["fail"]:
            raise ValueError("invalid config: unknown field")
        return ("s2", "c2", "r2", "m2")

    reloader, service, audit, (config, _) = _reloader(tmp_path, build)

    config.write_text("garbage: [", encoding="utf-8")
    assert reloader() is False

    assert service.classifier == "old"  # not replaced, and not cleared
    assert audit.events[-1]["actor"] == "telegram_polling"
    assert audit.events[-1]["payload"]["error"] == "config_reload_failed"
    assert "unknown field" in audit.events[-1]["payload"]["reason"]
    assert reloader() is False  # not retried every poll while the file is unchanged

    # Fixing the file recovers without a restart.
    state["fail"] = False
    config.write_text("ok: true\n", encoding="utf-8")
    _bump_mtime(config)
    assert reloader() is True
    assert service.classifier == "c2"


def test_the_fingerprint_tells_a_missing_file_from_an_empty_one(tmp_path) -> None:
    present = tmp_path / "a"
    present.write_text("", encoding="utf-8")
    missing = tmp_path / "b"

    before = config_fingerprint((present, missing))
    missing.write_text("", encoding="utf-8")

    assert config_fingerprint((present, missing)) != before
    assert config_fingerprint((present, tmp_path / "nope"))[1] is None
