"""`ybm onboard` - an interactive first-run wizard on top of the existing
mechanical `run_setup()`/`run_doctor()` (bootstrap.py), which stay
non-interactive and reusable on their own (CI, scripting, `ybm setup`).

Two choices actually block a new user from getting any value out of YBM at
all, so the wizard asks exactly those two and defaults everything else:

1. Which LLM answers - detected automatically where possible (a local Ollama
   server, or an existing LocalDeploy checkout via YBM_LOCALDEPLOY_ROOT), a
   cloud API key as the fallback, or skip and fix it later.
2. How you talk to it - the local web chat (already built, zero extra setup)
   is the default; Telegram is opt-in and asks for a bot token only if
   chosen. See docs/archive/HISTORY.md Part 4 T2.8 for why the web chat exists.
"""

from __future__ import annotations

from agent_control.bootstrap import OLLAMA_TAGS_URL, _http_json, run_doctor, run_setup
from agent_control.config_sync import ConfigManager, read_env_value


def _prompt(question: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    answer = input(f"{question}{suffix}: ").strip()
    return answer or (default or "")


def _prompt_llm_choice() -> dict | None:
    """Returns a dict of llm.profiles.onboard fields to write, or None to
    leave the shipped default config.example.yaml profile untouched.

    Checks .env for an already-usable credential/endpoint before asking for
    anything - a user re-running onboarding (or installing into a directory
    that already has a real .env) should never be asked to re-paste a key
    that's already sitting right there."""
    print("\n-- LLM --")
    ollama = _http_json(OLLAMA_TAGS_URL)
    if ollama and ollama.get("models"):
        names = [m["name"] for m in ollama["models"]]
        print(f"Found a local Ollama server with {len(names)} model(s): {', '.join(names[:6])}"
              + (", ..." if len(names) > 6 else ""))
        model = _prompt("Which model should YBM use", default=names[0])
        return {
            "provider": "openai_compatible", "model": model,
            "base_url": "http://127.0.0.1:11434/v1", "api_key_env": None,
        }

    localdeploy_root = read_env_value("YBM_LOCALDEPLOY_ROOT")
    if localdeploy_root:
        print(f"YBM_LOCALDEPLOY_ROOT is set ({localdeploy_root}) - keeping the shipped LocalDeploy profile.")
        return None

    if read_env_value("OPENAI_API_KEY"):
        print("OPENAI_API_KEY is already set in .env - keeping the shipped cloud profile that uses it.")
        return None

    print("No local model server detected (checked Ollama at 127.0.0.1:11434), and no OPENAI_API_KEY in .env.")
    choice = _prompt("Use a cloud API instead? [y/N]", default="n").lower()
    if choice not in ("y", "yes"):
        print("Skipping - the default config points at a local profile that won't respond until you "
              "set one up. `ybm doctor` will flag this; re-run `ybm onboard` any time.")
        return None

    api_key = _prompt("Paste your OpenAI (or OpenAI-compatible) API key")
    if not api_key:
        print("No key entered - skipping LLM setup.")
        return None
    base_url = _prompt("API base URL", default="https://api.openai.com/v1")
    model = _prompt("Model name", default="gpt-4.1")
    ConfigManager().upsert_env({"OPENAI_API_KEY": api_key})
    return {
        "provider": "openai_compatible", "model": model,
        "base_url": base_url, "api_key_env": "OPENAI_API_KEY",
    }


def _apply_llm_choice(profile: dict | None) -> None:
    if profile is None:
        return
    manager = ConfigManager()
    config = manager.read_config()
    config.setdefault("llm", {}).setdefault("profiles", {})["onboard"] = profile
    config["llm"]["default_profile"] = "onboard"
    manager.write_config(config)
    print("Set llm.default_profile = 'onboard' in config/config.yaml.")


def _prompt_telegram_choice() -> str | None:
    print("\n-- How you'll talk to YBM --")
    existing_token = read_env_value("TELEGRAM_BOT_TOKEN")
    if existing_token:
        print("TELEGRAM_BOT_TOKEN is already set in .env - enabling Telegram with it.")
        return existing_token
    print("The local web chat in the admin console needs no setup and is enabled by default.")
    choice = _prompt("Also set up Telegram now? [y/N]", default="n").lower()
    if choice not in ("y", "yes"):
        return None
    token = _prompt("Paste your Telegram bot token (from @BotFather)")
    return token or None


def run_onboard() -> int:
    print("YBM onboarding")
    print("===============")
    print("A few questions, then you'll be running.\n")

    llm_profile = _prompt_llm_choice()
    telegram_token = _prompt_telegram_choice()

    print("\n-- Setting up --")
    setup_rc = run_setup(telegram_token=telegram_token)
    if setup_rc != 0:
        return setup_rc

    _apply_llm_choice(llm_profile)

    if telegram_token:
        from agent_control.config_sync import set_config_path
        set_config_path("channels.telegram.enabled", "true")

    print("\n-- Checking your setup --\n")
    run_doctor()

    start_choice = _prompt("\nStart YBM now?", default="Y").lower()
    if start_choice in ("", "y", "yes"):
        from agent_control.supervisor import start_all
        # open_browser=True is the one moment this is worth doing
        # automatically: the very end of first-run onboarding, where "open a
        # browser tab" (pre-authenticated via ?token=, see start_all) is
        # exactly the one-click experience being onboarded into. `ybm start`
        # on its own (a developer restarting the stack) deliberately does
        # NOT do this by default - it would be a nuisance popup on every
        # restart.
        return start_all(open_browser=True)

    print("\nWhen you're ready: `ybm start` (or `.\\scripts\\ybm.ps1 start` on Windows).")
    print("Then open http://127.0.0.1:8765/admin for the admin console and web chat.")
    return 0
