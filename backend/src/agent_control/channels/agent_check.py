"""Are Codex and Claude Code ready? A check that costs no quota.

A live request to "use Codex" failed after the work was queued because the user's own `~/.codex/config.toml` named a model their plan cannot
use. Nothing short of a real call proves a model works, and a real call spends quota, so this checks what can be checked for free: whether
each command line tool is installed, which version, and which model Codex is configured to use, so the answer to "can you use Codex?" is
known before a task is queued on it. It is a deterministic command ("are my coding agents working?"), not a model call.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tomllib
from pathlib import Path

from agent_control.schemas import ChannelType, InboundMessage, OutboundMessage

_ASK = re.compile(
    r"^\s*(?:(?:are|is|do)\s+(?:my\s+|the\s+|both\s+)?(?:coding\s+agents?|codex(?:\s+and\s+claude(?:\s+code)?)?|claude(?:\s+code)?(?:\s+and\s+codex)?)"
    r"\s+(?:working|ready|set\s*up|installed|available|ok(?:ay)?)"
    r"|(?:check|test)\s+(?:my\s+|the\s+)?(?:coding\s+agents?|codex(?:\s+and\s+claude(?:\s+code)?)?|claude\s+code)"
    r"|can\s+you\s+(?:use|reach)\s+(?:codex|claude\s+code)\s*\??)\s*\??\s*[.!]*\s*$",
    re.IGNORECASE,
)


def _version(executable: str) -> str | None:
    try:
        done = subprocess.run([executable, "--version"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    text = (done.stdout or done.stderr or "").strip().splitlines()
    return text[0].strip() if done.returncode == 0 and text else None


def codex_configured_model(config_path: Path | None = None) -> str | None:
    """The model named in the user's Codex config, if any."""
    path = config_path or Path.home() / ".codex" / "config.toml"
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    model = data.get("model")
    return str(model) if isinstance(model, str) and model.strip() else None


def check_coding_agents(*, which=shutil.which, version=_version, codex_model=codex_configured_model) -> list[str]:
    """One plain line per tool."""
    lines = []
    for label, command in (("Codex", "codex"), ("Claude Code", "claude")):
        executable = which(command)
        if not executable:
            lines.append(f"- {label}: not installed (I could not find `{command}` on this computer).")
            continue
        found = version(executable)
        if found is None:
            lines.append(f"- {label}: found, but it did not answer when asked for its version, so I would not rely on it.")
            continue
        line = f"- {label}: installed ({found})."
        if command == "codex":
            model = codex_model()
            line += f" Its config uses the model {model}; if your plan cannot use that model a run stops straight away." if model else " No model is set in its config."
        lines.append(line)
    return lines


def agent_check_reply(inbound: InboundMessage) -> OutboundMessage | None:
    text = (inbound.text or "").strip()
    if not text or len(text) > 80 or inbound.channel != ChannelType.TELEGRAM or not _ASK.match(text):
        return None
    body = "\n".join(check_coding_agents())
    note = "\n\nThis checks what is installed and configured; it does not run them, so it cannot tell whether your plan allows the model. Say \"use Claude Code\" or \"use Codex\" and I will."
    return OutboundMessage(channel=inbound.channel, chat_id=inbound.chat_id, text="Coding agents on this computer:\n" + body + note)
