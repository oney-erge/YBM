"""Ask what could do something: the Operator's way into the ability index (abilities/).

The Operator's catalog lists the tools that are on. It cannot list a skill written last night, an MCP server's forty tools, a coding agent
that could take the job, a tool that is switched off, a local model that could be loaded, or the plain fact that something cannot be done
here, and it should not: most requests need none of them. This tool is the other half: when nothing in front of the model fits, it
searches everything and says, for each match, whether it is ready, what it would take to make it ready, and how to use it.

It only reads. Making something ready (turning a tool on, loading a model, installing a server) is done with the tools that already do
those things, each behind its own approval; the record says which.
"""

from __future__ import annotations

import json
from textwrap import shorten
from typing import Any

from agent_control.abilities.catalog import AbilityCatalog
from agent_control.abilities.model import Ability, AbilityKind, AbilityState
from agent_control.schemas import Capability, ToolCallRequest, ToolCallResult, ToolResultStatus
from agent_control.tools.contracts import AbilitiesInput, AbilitiesOutput
from agent_control.tools.spec import Adapters, Definitions, RegistryDeps, ToolDefinition, failed_result, same_output_schema

_STATE_WORDS = {
    AbilityState.READY: "ready",
    AbilityState.DISABLED: "switched off (the user can turn it on in Access)",
    AbilityState.INSTALLABLE: "not here yet (can be loaded or installed with the user's approval)",
    AbilityState.NEEDS_ACCESS: "needs an account, key or login from the user",
    AbilityState.UNAVAILABLE: "cannot be done by this assistant",
}


def describe_ability(ability: Ability, *, detail: bool = False) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "name": ability.name,
        "kind": ability.kind.value,
        "state": ability.state.value,
        "what": ability.summary,
    }
    if ability.state != AbilityState.READY:
        entry["status"] = _STATE_WORDS[ability.state]
    if ability.needs:
        entry["needs"] = list(ability.needs)
    if ability.guidance:
        entry["guidance"] = ability.guidance
    if ability.call:
        entry["how_to_use"] = ability.call
    if detail and ability.detail:
        entry["detail"] = ability.detail[:1200]
    return entry


def _text(entries: list[dict[str, Any]], heading: str) -> str:
    if not entries:
        return heading
    lines = [heading]
    for position, entry in enumerate(entries, 1):
        status = f" [{entry['status']}]" if "status" in entry else ""
        lines.append(f"{position}. {entry['name']} ({entry['kind']}){status}: {entry['what']}")
        if "how_to_use" in entry:
            lines.append(f"   how to use: {json.dumps(entry['how_to_use'], ensure_ascii=False)}")
        if "guidance" in entry:
            lines.append(f"   {entry['guidance']}")
    return "\n".join(lines)


#: What an overview lists, in the order a person asks: what cannot be done, what could be set up, what needs an account, what is switched
#: off, then what is ready. The cap is per section: the question was what the assistant can and cannot do, not for its whole catalog.
_OVERVIEW_SECTIONS = (
    (AbilityState.UNAVAILABLE, "Cannot be done by this assistant, and what to do instead", 15),
    (AbilityState.INSTALLABLE, "Could be set up, with the user's approval", 12),
    (AbilityState.NEEDS_ACCESS, "Would work with an account, key or login from the user", 12),
    (AbilityState.DISABLED, "Switched off (the user can turn them on in Access)", 12),
    (AbilityState.READY, "Ready now", 25),
)
_OVERVIEW_ANSWER = (
    "Answer from this list only. For what cannot be done, say each plainly with the nearest thing that can be done. For what is needed or "
    "missing, name the concrete things above, say in a few words what each would unlock, recommend the two or three that would help "
    "most and offer to set them up. This list has no dates, so do not say what is new since a day. Do not name tool ids."
)


def _overview(abilities: tuple[Ability, ...]) -> tuple[str, dict[str, Any]]:
    """The assistant's own abilities by state, for "what can't you do?", "what do you need from me?" and "what can you do?".

    The chat reply to those questions is written without the index, and it invented: "execute terminal commands without needing explicit
    approval", and a list of what it can do for "what can't you do".
    """
    lines: list[str] = []
    shown: list[dict[str, Any]] = []
    for state, heading, cap in _OVERVIEW_SECTIONS:
        in_state = [ability for ability in abilities if ability.state == state]
        # The tools of an MCP server are a catalog of their own (dozens, named like "filesystem/list_directory"): counted, not listed.
        members = sorted((ability for ability in in_state if ability.kind != AbilityKind.MCP_TOOL), key=lambda ability: ability.name)
        from_servers = len(in_state) - len(members)
        if not in_state:
            continue
        lines.append(f"{heading}:")
        for ability in members[:cap]:
            note = ability.guidance if state == AbilityState.UNAVAILABLE else ability.detail if state == AbilityState.INSTALLABLE else ""
            what = shorten(" ".join(ability.summary.split()), 110, placeholder="…")
            lines.append(f"- {ability.name}: {what}" + (f" ({shorten(' '.join(note.split()), 220, placeholder='…')})" if note else ""))
            shown.append(describe_ability(ability))
        if len(members) > cap:
            lines.append(f"- ...and {len(members) - cap} more.")
        if from_servers:
            lines.append(f"- {from_servers} more tools from connected MCP servers.")
        lines.append("")
    body = "\n".join(lines).rstrip() or "Nothing is listed."
    text = f"{body}\n\n{_OVERVIEW_ANSWER}"
    return text, {"summary": f"{len(shown)} abilities by state. {_OVERVIEW_ANSWER}", "abilities": shown}


class AbilitiesAdapter:
    """Search and describe abilities. The catalog is bound after the registry exists, because the index reads the registry's tools."""

    def __init__(self) -> None:
        self._catalog: AbilityCatalog | None = None

    def bind(self, catalog: AbilityCatalog) -> None:
        self._catalog = catalog

    async def execute(self, request: ToolCallRequest) -> ToolCallResult:
        if self._catalog is None:
            return failed_result(request, "the ability index is not available in this process")
        operation = str(request.input.get("operation") or "search")
        index = self._catalog.index()
        if operation == "search":
            query = str(request.input.get("query") or "")
            limit = int(request.input.get("limit") or 6)
            matches = [describe_ability(ability) for ability, _score in index.search(query, limit)]
            heading = (
                f"{len(matches)} ability(ies) match: {query[:120]}"
                if matches
                else f"Nothing in the ability index matches: {query[:120]}. If a script could do it, use code.interpreter; "
                "otherwise say plainly that this assistant cannot do it and what the user could do."
            )
            output: dict[str, Any] = {"summary": heading, "abilities": matches}
            text = _text(matches, heading)
        elif operation == "overview":
            text, output = _overview(index.all())
        elif operation == "describe":
            name = str(request.input.get("name") or "")
            ability = index.find_by_name(name)
            if ability is None:
                return failed_result(request, f"no ability is named {name!r}; search first and use a name exactly as it was shown")
            entry = describe_ability(ability, detail=True)
            output = {"summary": f"{ability.name}: {ability.summary}", "abilities": [entry]}
            text = _text([entry], output["summary"]) + (f"\n{entry['detail']}" if entry.get("detail") else "")
        else:
            return failed_result(request, f"unsupported abilities operation: {operation}")
        output["operation"] = operation
        output["terminal_output"] = [{"content": text, "exit_code": 0, "is_final": True}]
        return ToolCallResult(request_id=request.id, status=ToolResultStatus.SUCCEEDED, output=output)


def register(deps: RegistryDeps, definitions: Definitions, adapters: Adapters) -> None:
    enabled = deps.settings.abilities.enabled
    definitions.append(
        ToolDefinition(
            name="ability.search",
            group="assistant",
            provides=("ability", "capability", "tool", "skill", "can", "able", "possible", "support", "available"),
            capability=Capability.AGENT_CORE,
            enabled=enabled,
            # Not named while it is off: the catalog a recorded scenario was made against stays the same text.
            named_when_disabled=False,
            description=(
                "find out what could do something, across every tool, skill, MCP tool, coding agent and saved workflow, "
                "including ones that are switched off or not installed, and what cannot be done at all and what to do instead. "
                "When the question is about the assistant itself (what can you do, what can't you do, what are you missing, what do you "
                "need from me) use operation overview and answer from it, never from memory"
            ),
            operations=("search", "describe", "overview"),
            input_schema=AbilitiesInput,
            output_schema=AbilitiesOutput,
            operation_output_schemas=same_output_schema(("search", "describe", "overview"), AbilitiesOutput),
            default_operation="search",
            examples=(
                {"operation": "search", "query": "what could turn a photo into text"},
                {"operation": "describe", "name": "{{ability_name}}"},
                {"operation": "overview"},
            ),
        )
    )
    adapters["ability.search"] = AbilitiesAdapter()
