"""Say what access a blocked task was missing, instead of "no tool can do this".

A fresh install starts with every high-impact capability off, which is the right
default and also the reason the first real request - "organize my Downloads" -
ends in a task the agent could not attempt. The Operator reports that as
"blocked", and the person is left to work out which of a dozen settings it meant.

This turns what the runtime already knows (which tools exist but are switched
off, and which calls were refused because of it) into a sentence and a small
structured record, so the console can name the access that is off and offer to
turn it on. It is deliberately evidence-based, never keyword-based: it looks at
tools and denied calls, not at the words of the request (docs/ROADMAP.md, "Note
on surfacing").
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from agent_control.policy.access_modes import ACCESS_GROUPS, AccessGroup
from agent_control.schemas import Capability, CapabilityAccessMode

#: Shown first, because they are what "do something on my computer" usually needs.
_LEADING_GROUPS = ("filesystem", "browser", "terminal", "desktop_control")

#: Groups whose switches are not about doing work on the machine, so a blocked
#: request is never plausibly waiting on them.
_IGNORED_GROUPS = frozenset({"scheduler", "dependencies", "github", "vscode", "desktop_screenshot"})


def group_for_capability(capability: Capability) -> AccessGroup | None:
    for group in ACCESS_GROUPS.values():
        if capability in group.read_capabilities or capability in group.write_capabilities:
            return group
    return None


def _recommended_mode(group: AccessGroup) -> str:
    """The gentlest setting that lets the work happen: ask before changing things."""
    if CapabilityAccessMode.WRITE_ACCESS in group.options:
        return CapabilityAccessMode.WRITE_ACCESS.value
    if CapabilityAccessMode.READ_ONLY in group.options:
        return CapabilityAccessMode.READ_ONLY.value
    return group.options[-1].value


def _record(group: AccessGroup) -> dict[str, str]:
    return {"group": group.name, "label": group.label, "recommended_mode": _recommended_mode(group)}


def _ordered(groups: dict[str, AccessGroup]) -> list[AccessGroup]:
    leading = [groups[name] for name in _LEADING_GROUPS if name in groups]
    rest = sorted((g for name, g in groups.items() if name not in _LEADING_GROUPS), key=lambda g: g.label)
    return [*leading, *rest]


def _join(labels: list[str]) -> str:
    if len(labels) <= 1:
        return "".join(labels)
    return ", ".join(labels[:-1]) + " and " + labels[-1]


def blocked_access_hint(
    tool_definitions: Mapping[str, Any],
    history: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """What to tell the person when a task could not proceed, or None.

    ``evidence`` is ``"denied"`` when a call was actually refused because its
    capability is off (certain), and ``"off"`` when the agent gave up with tools
    switched off and nothing attempted (probable, so worded as such).
    """
    denied: dict[str, AccessGroup] = {}
    for entry in history:
        if entry.get("status") != "denied" or entry.get("error") != "capability_disabled":
            continue
        definition = tool_definitions.get(str(entry.get("tool_name") or ""))
        if definition is None:
            continue
        step_input = entry.get("input") if isinstance(entry.get("input"), dict) else {}
        group = group_for_capability(definition.capability_for(step_input))
        if group is not None and group.name not in _IGNORED_GROUPS:
            denied[group.name] = group
    if denied:
        groups = _ordered(denied)
        labels = [g.label for g in groups]
        return {
            "evidence": "denied",
            "groups": [_record(g) for g in groups],
            "summary": f"YBM tried to use {_join(labels)}, but that access is turned off. "
                       "Turn it on in Access, then ask again.",
        }

    off: dict[str, AccessGroup] = {}
    for definition in tool_definitions.values():
        if getattr(definition, "enabled", True):
            continue
        group = group_for_capability(definition.capability)
        if group is not None and group.name not in _IGNORED_GROUPS:
            off[group.name] = group
    if not off:
        return None
    groups = _ordered(off)
    shown = [g.label for g in groups[:3]]
    more = len(groups) - len(shown)
    named = _join(shown) + (f" and {more} more" if more > 0 else "")
    return {
        "evidence": "off",
        "groups": [_record(g) for g in groups],
        "summary": f"Access is off for {named}, so YBM had nothing it could use for this. "
                   "Turn on what you need in Access, then ask again.",
    }
