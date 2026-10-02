from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import yaml

CONFIG_FILE_PATH = Path("config/config.yaml")
ENV_FILE_PATH = Path(".env")


def config_fingerprint(watched: tuple[Path, ...] = (CONFIG_FILE_PATH, ENV_FILE_PATH)) -> tuple[tuple[int, int] | None, ...]:
    """A cheap "did either file change" token: modification time and size, per file.

    Used by every long-running process that follows config.yaml and .env while
    it runs (the task worker and the Telegram and WhatsApp intake loops). A
    missing file is its own distinct state, so creating or deleting one counts.
    """
    state: list[tuple[int, int] | None] = []
    for path in watched:
        try:
            stat = path.stat()
        except OSError:
            state.append(None)
        else:
            state.append((stat.st_mtime_ns, stat.st_size))
    return tuple(state)


class ConfigManager:
    def __init__(self, config_path: Path = CONFIG_FILE_PATH, env_path: Path = ENV_FILE_PATH) -> None:
        self.config_path = config_path
        self.env_path = env_path

    def read_config(self) -> dict[str, Any]:
        if not self.config_path.exists():
            return {}
        loaded = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        if loaded is None:
            return {}
        if not isinstance(loaded, dict):
            raise ValueError(f"{self.config_path} must contain a YAML object")
        return loaded

    def write_config(self, config: dict[str, Any]) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        # newline="" keeps output LF-only; the default None lets Windows text
        # mode translate to CRLF, mixing line endings across repeated edits.
        self.config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8", newline="")

    def upsert_env(self, values: dict[str, str | None]) -> None:
        current_lines = self.env_path.read_text(encoding="utf-8").splitlines() if self.env_path.exists() else []
        replacements = {key: value for key, value in values.items() if value is not None}
        seen: set[str] = set()
        next_lines: list[str] = []

        for line in current_lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in line:
                next_lines.append(line)
                continue
            key = line.split("=", 1)[0].strip()
            if key in replacements:
                next_lines.append(f"{key}={_env_value(replacements[key])}")
                seen.add(key)
            else:
                next_lines.append(line)

        for key, value in replacements.items():
            if key not in seen:
                next_lines.append(f"{key}={_env_value(value)}")

        self.env_path.write_text("\n".join(next_lines).rstrip() + "\n", encoding="utf-8")

    def remove_env_keys(self, keys: list[str]) -> None:
        if not self.env_path.exists():
            return
        targets = set(keys)
        current_lines = self.env_path.read_text(encoding="utf-8").splitlines()
        next_lines: list[str] = []
        for line in current_lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in line:
                next_lines.append(line)
                continue
            key = line.split("=", 1)[0].strip()
            if key in targets:
                continue
            next_lines.append(line)
        self.env_path.write_text("\n".join(next_lines).rstrip() + "\n", encoding="utf-8")


def read_env_file(env_path: Path = ENV_FILE_PATH) -> dict[str, str]:
    if not env_path.exists():
        return {}
    values: dict[str, str] = {}
    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        key = key.strip()
        value = raw_value.strip()
        if value.startswith('"') and value.endswith('"'):
            try:
                value = str(json.loads(value))
            except json.JSONDecodeError:
                value = value[1:-1]
        values[key] = value
    return values


def read_env_value(key: str, env_path: Path = ENV_FILE_PATH) -> str | None:
    return os.getenv(key) or read_env_file(env_path).get(key)


def _env_value(value: str) -> str:
    if any(char.isspace() for char in value) or "#" in value:
        return json.dumps(value)
    return value


def parse_scalar(value: str) -> Any:
    if value.lower() in ("true", "false"):
        return value.lower() == "true"
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    return value


def set_config_path(path: str, value: str, manager: ConfigManager | None = None) -> tuple[bool, str]:
    """Set a dotted config path (e.g. ``capabilities.filesystem.write.enabled``).

    Writes, then validates by loading ``AppSettings`` from the result; reverts
    and reports failure rather than leaving an unloadable config.yaml behind.
    Returns ``(ok, message)``.
    """
    from agent_control.config import load_settings  # local import: avoids a config<->config_sync cycle

    manager = manager or ConfigManager()
    original = manager.read_config()
    config = json.loads(json.dumps(original)) if original else {}
    keys = [key for key in path.split(".") if key]
    if not keys:
        return False, "config path must be non-empty, e.g. server.port"

    node = config
    for key in keys[:-1]:
        if not isinstance(node.get(key), dict):
            node[key] = {}
        node = node[key]
    parsed = parse_scalar(value)
    node[keys[-1]] = parsed

    manager.write_config(config)
    try:
        load_settings()
    except Exception as exc:  # noqa: BLE001 - reporting, not handling
        manager.write_config(original)
        return False, f"{path}={value!r} produced an invalid config; reverted. {exc}"
    return True, f"set {path} = {parsed!r} in config/config.yaml"
