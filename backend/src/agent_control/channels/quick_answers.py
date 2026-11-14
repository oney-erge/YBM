"""The quick answers a question can offer, as buttons: short choices that answer it with a tap (typing the words still works)."""

from __future__ import annotations

from agent_control.schemas import TaskRecord, TaskStatus

#: The most buttons a question carries, and the longest label: a phone shows a few short choices, not a menu.
MAX_OPTIONS = 4
MAX_OPTION_CHARS = 40


def clarifying_options(task: TaskRecord) -> list[str]:
    """The quick answers a question offers, if it came with any: short, distinct, at most MAX_OPTIONS."""
    raw = task.metadata.get("clarifying_options")
    if task.status != TaskStatus.CLARIFYING or not isinstance(raw, list):
        return []
    options: list[str] = []
    for item in raw:
        text = " ".join(str(item).split())
        if text and len(text) <= MAX_OPTION_CHARS and text not in options:
            options.append(text)
    return options[:MAX_OPTIONS]
