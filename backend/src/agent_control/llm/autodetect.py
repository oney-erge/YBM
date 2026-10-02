"""Choose a usable model from what is already on this machine, without asking.

A fresh install used to ship a default model that only existed on the author's
machine (a LocalDeploy profile) with a paid OpenAI profile behind it as a
silent fallback. Everyone else started with a console that said "no model
configured" and a wizard asking questions the machine could already answer.

Detection is deliberately free and read-only: it looks at environment
variables and probes a local port, and it never calls a paid API. The order is
chosen so that nothing is spent unless the person has already provided a key,
and so that a local, private model beats a cloud one when both exist:

1. ``YBM_LOCALDEPLOY_ROOT`` is set - the person runs their own LocalDeploy.
2. A local Ollama server is running with at least one model pulled.
3. A provider API key is present in the environment or ``.env``.
4. Nothing found - the caller leaves the model unset and says so honestly.

The result is shown to the user (the launcher prints one line and the console
names the model), so "it picked my OpenAI key" is never a surprise.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable
from urllib.error import URLError
from urllib.request import urlopen

from agent_control.config_sync import ConfigManager, read_env_value
from agent_control.llm import catalog

#: The profile name the console's provider picker and ``ybm onboard`` also
#: write, so a detected model and a hand-picked one look the same afterwards.
PROFILE_NAME = "onboard"

#: Shipped profile (config.example.yaml) used when the person runs LocalDeploy.
LOCALDEPLOY_PROFILE_NAME = "localdeploy_qwen3vl_8b"

OLLAMA_TAGS_URL = "http://127.0.0.1:11434/api/tags"

#: Cloud providers in the order a detected key wins. Anthropic and OpenAI first
#: because they are what a developer's shell most often already exports.
CLOUD_PROVIDER_ORDER: tuple[str, ...] = (
    "anthropic", "openai", "openrouter", "google", "groq", "deepseek", "mistral", "xai", "together",
)

# Ollama tags to prefer when several are installed, best first. Matched as a
# prefix against the tag ("qwen3-vl:8b-instruct" matches "qwen3-vl"), so a
# specific quantisation still counts. Vision-capable and instruction-tuned
# models come first because the operator loop asks for structured output and
# the desktop tools pass screenshots.
PREFERRED_OLLAMA_MODELS = (
    "qwen3-vl", "qwen3vl", "qwen3", "qwen2.5", "gemma3", "llama3.1", "mistral",
)


@dataclass(frozen=True)
class DetectedModel:
    #: "localdeploy", "ollama" or "api_key".
    source: str
    profile_name: str
    #: The profile to write, or None when the profile already exists in the
    #: shipped config (the LocalDeploy one).
    profile: dict[str, Any] | None
    #: A sentence a person can read: what was picked and why.
    label: str
    #: The environment variable that supplied a key, if any. Never its value.
    env_var: str | None = None


def recommended_ollama_model(models: list[str]) -> str | None:
    """The model to preselect, or None when it should not guess.

    A single installed model is the answer whatever it is - the user has
    already made the choice by pulling it. With several, prefer the ones this
    project is actually tuned against rather than whichever sorts first.
    """
    if not models:
        return None
    if len(models) == 1:
        return models[0]
    for preferred in PREFERRED_OLLAMA_MODELS:
        for model in models:
            if model.casefold().startswith(preferred):
                return model
    return None


def _installed_ollama_models(timeout: float = 2.0) -> list[str]:
    try:
        with urlopen(OLLAMA_TAGS_URL, timeout=timeout) as resp:  # noqa: S310 - fixed loopback URL
            if not 200 <= resp.status < 300:
                return []
            payload = json.loads(resp.read().decode("utf-8"))
    except (URLError, OSError, ValueError):
        return []
    return [str(m.get("name")) for m in payload.get("models", []) if m.get("name")]


def _cloud_profile(spec: catalog.ProviderSpec) -> dict[str, Any]:
    return {
        "provider": spec.kind,
        "model": spec.default_model,
        "base_url": spec.base_url,
        "api_key_env": spec.api_key_env,
        "timeout_seconds": 120,
        "max_tokens": 4096,
        "temperature": 0.2,
    }


def detect_model(
    *,
    read_env: Callable[[str], str | None] | None = None,
    ollama_models: Callable[[], list[str]] | None = None,
) -> DetectedModel | None:
    """Return the best model already available here, or None.

    ``read_env`` and ``ollama_models`` default to the real environment and a
    real probe of the local Ollama port; they are looked up when called (not
    bound as defaults) so a test can substitute either one.
    """
    read_env = read_env or read_env_value
    ollama_models = ollama_models or _installed_ollama_models
    if read_env("YBM_LOCALDEPLOY_ROOT"):
        return DetectedModel(
            source="localdeploy",
            profile_name=LOCALDEPLOY_PROFILE_NAME,
            profile=None,
            label="your LocalDeploy model (YBM_LOCALDEPLOY_ROOT is set)",
        )

    models = ollama_models()
    if models:
        chosen = recommended_ollama_model(models) or models[0]
        local = catalog.get("ollama")
        assert local is not None  # the catalog ships this provider
        return DetectedModel(
            source="ollama",
            profile_name=PROFILE_NAME,
            profile={
                "provider": local.kind,
                "model": chosen,
                "base_url": local.base_url,
                "api_key_env": None,
                "timeout_seconds": 300,
                "max_tokens": 4096,
                "temperature": 0.2,
            },
            label=f"{chosen} on your local Ollama server (free, private)",
        )

    for key in CLOUD_PROVIDER_ORDER:
        spec = catalog.get(key)
        if spec is None or not spec.api_key_env:
            continue
        if read_env(spec.api_key_env):
            return DetectedModel(
                source="api_key",
                profile_name=PROFILE_NAME,
                profile=_cloud_profile(spec),
                label=f"{spec.label} ({spec.default_model}), using {spec.api_key_env} from your environment",
                env_var=spec.api_key_env,
            )
    return None


def _has_usable_default(config: dict[str, Any]) -> bool:
    llm = config.get("llm") or {}
    default = llm.get("default_profile")
    return bool(default) and default in (llm.get("profiles") or {})


def auto_configure_llm(
    manager: ConfigManager | None = None,
    *,
    read_env: Callable[[str], str | None] | None = None,
    ollama_models: Callable[[], list[str]] | None = None,
) -> DetectedModel | None:
    """Write the detected model into config.yaml when none is configured.

    Does nothing, and returns None, when a default profile is already set and
    defined - a person's own choice is never overwritten. Returns what it
    configured so the caller can tell the person.
    """
    manager = manager or ConfigManager()
    config = manager.read_config()
    if _has_usable_default(config):
        return None

    detected = detect_model(read_env=read_env, ollama_models=ollama_models)
    if detected is None:
        return None

    llm = config.setdefault("llm", {})
    profiles = llm.setdefault("profiles", {})
    if detected.profile is not None:
        profiles[detected.profile_name] = detected.profile
    elif detected.profile_name not in profiles:
        # The shipped LocalDeploy profile is missing from a hand-edited config;
        # there is nothing to point the default at.
        return None
    llm["default_profile"] = detected.profile_name
    manager.write_config(config)
    return detected
