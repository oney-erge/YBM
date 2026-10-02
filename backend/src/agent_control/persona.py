"""A single stable identity/preference document (docs/archive/HISTORY.md Part 3
T2.5): "answer concisely", "my timezone is CST", "never ask for confirmation
on read-only filesystem searches" - things true across every task and every
conversation, not scoped to one chat thread.

Deliberately NOT channels/memory.py's ConversationMemoryService: that is
per-conversation short-term recall (what was just discussed, summarized and
decayed over the conversation), rebuilt fresh per conversation_id. This is
one global file, read fresh on every Operator step (so an edit takes effect
immediately) and updatable by the model itself via the persona.manage tool
so learned preferences persist without the user having to edit a file by
hand.
"""

from __future__ import annotations

from pathlib import Path

from agent_control.config import PersonaAdapterConfig

DEFAULT_PERSONA = "(no persona/preferences recorded yet)"


def read_persona(config: PersonaAdapterConfig) -> str:
    """Current persona content, or DEFAULT_PERSONA if nothing has been
    written yet. Never raises - a missing or unreadable file is simply "no
    persona yet", not a startup or per-step failure."""
    if not config.enabled:
        return DEFAULT_PERSONA
    path = Path(config.path).expanduser()
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return DEFAULT_PERSONA
    if not text:
        return DEFAULT_PERSONA
    return text if len(text) <= config.max_chars else f"{text[: config.max_chars - 3]}..."


def write_persona(config: PersonaAdapterConfig, content: str) -> str:
    """Replace the persona file's full content. The caller (persona.manage's
    `update` operation) is expected to have read the current content first
    via `get` and sent back the complete, edited version - same
    read-then-write-the-whole-thing model as a normal file edit, not an
    append or merge, so there is exactly one place the current state lives
    and no ambiguity about ordering.
    """
    text = content.strip()
    if len(text) > config.max_chars:
        raise ValueError(f"persona content exceeds max_chars ({len(text)} > {config.max_chars})")
    path = Path(config.path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return text


def persona_prompt_section(config: PersonaAdapterConfig) -> str:
    """Rendered block for injection into the Operator's config_context -
    empty string when disabled or nothing has been recorded, so an unused
    persona feature costs nothing in prompt size."""
    if not config.enabled:
        return ""
    content = read_persona(config)
    if content == DEFAULT_PERSONA:
        return ""
    return f"## User persona and preferences (apply these unless the current objective says otherwise)\n{content}"
