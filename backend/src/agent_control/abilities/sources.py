"""Read every place the assistant keeps a capability and turn each into an Ability.

Each function reads one source as it is now (nothing is cached or copied here), and `build_index` puts them in order, most specific
first, so a skill written for a job is offered before the general tool it uses. A source that cannot be read gives nothing instead of
an error: the index of what is known must not fail because one list is unreachable.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable

import structlog

from agent_control.abilities.gaps import KNOWN_GAPS
from agent_control.abilities.index import AbilityIndex
from agent_control.abilities.model import (
    NEED_ENABLE,
    Ability,
    AbilityKind,
    AbilityState,
)
from agent_control.config import AppSettings
from agent_control.skill_learning import HELPER_PREFIX
from agent_control.tools.mcp_client import load_mcp_catalog
from agent_control.tools.skills import _load_skills, slugify
from agent_control.tools.spec import ToolDefinition, ToolRegistry

logger = structlog.get_logger(__name__)

_FIRST_CLAUSE = re.compile(r"^(.{20,140}?)(?:[;:]|\.\s|$)", re.DOTALL)

#: Coding agents the work can be handed to, with what each is known for. The command-line tools are found on the machine, not assumed.
_AGENTS = {
    "claude_code": ("Claude Code", "hand a coding job to Claude Code in a task workspace: build or change a program, fix a bug, write tests"),
    "codex": ("Codex", "hand a coding job to Codex in a task workspace: build or change a program, fix a bug, write tests"),
    "github_copilot": ("GitHub Copilot CLI", "hand a coding job to the GitHub Copilot CLI in a task workspace"),
}


def _first_clause(text: str) -> str:
    text = " ".join((text or "").split())
    match = _FIRST_CLAUSE.match(text)
    return (match.group(1) if match else text[:140]).strip()


def tool_abilities(definitions: Iterable[ToolDefinition]) -> list[Ability]:
    """Every tool the registry declares, ready if it is switched on and disabled (the user can enable it) if not."""
    abilities = []
    for definition in definitions:
        operations = ", ".join(definition.operations)
        examples = " ".join(str(example) for example in definition.examples)
        ready = definition.enabled
        abilities.append(
            Ability(
                id=f"tool:{definition.name}",
                kind=AbilityKind.TOOL,
                name=definition.name,
                summary=_first_clause(definition.description),
                group=definition.group,
                state=AbilityState.READY if ready else AbilityState.DISABLED,
                provides=definition.provides,
                detail=f"{definition.description} {operations} {examples}".strip(),
                call={"tool": definition.name} if ready else {},
                needs=() if ready else (NEED_ENABLE,),
                guidance="" if ready else f"{definition.name} is switched off. The user can turn it on in Access.",
                source="registry",
            )
        )
    return abilities


def mcp_abilities(settings: AppSettings) -> list[Ability]:
    """The tools of the configured MCP servers, from the catalog the last `list_tools` wrote."""
    if not settings.mcp.enabled:
        return []
    try:
        catalog = load_mcp_catalog(settings.mcp)
    except Exception:  # noqa: BLE001 - an unreadable catalog is an empty source, not a failure of the index
        logger.warning("abilities_mcp_catalog_unreadable", exc_info=True)
        return []
    abilities = []
    for tool in catalog.get("tools", []):
        if not isinstance(tool, dict):
            continue
        name = str(tool.get("name") or tool.get("tool") or "").strip()
        server = str(tool.get("server") or "").strip()
        if not name or not server:
            continue
        disabled = bool(tool.get("disabled"))
        description = str(tool.get("description") or "").strip()
        abilities.append(
            Ability(
                id=f"mcp:{server}/{name}",
                kind=AbilityKind.MCP_TOOL,
                name=f"{server}/{name}",
                summary=_first_clause(description) or f"the {name} tool of the {server} MCP server",
                group="integrations",
                state=AbilityState.DISABLED if disabled else AbilityState.READY,
                provides=tuple(re.split(r"[_\-.\s]+", name.lower())),
                detail=description,
                call={} if disabled else {"tool": "mcp.client", "input": {"operation": "call_tool", "server": server, "tool": name}},
                needs=(NEED_ENABLE,) if disabled else (),
                source="mcp catalog",
            )
        )
    return abilities


def skill_abilities(settings: AppSettings) -> list[Ability]:
    """Skills and the scripts kept from earlier tasks (helpers), read fresh from the skills folder."""
    if not settings.adapters.skills.enabled:
        return []
    abilities = []
    for skill in _load_skills(settings.adapters.skills.root_dir):
        name = str(skill["name"])
        helper = name.startswith(HELPER_PREFIX)
        abilities.append(
            Ability(
                id=f"{'helper' if helper else 'skill'}:{slugify(name)}",
                kind=AbilityKind.HELPER if helper else AbilityKind.SKILL,
                name=name,
                summary=str(skill["description"]),
                group="skills",
                detail=str(skill.get("body") or "")[:600],
                call={"tool": "skills.use", "input": {"operation": "read", "name": name}},
                source="skills folder",
            )
        )
    return abilities


def workflow_abilities(workflows: Iterable[object]) -> list[Ability]:
    """Saved workflows: a finished task's own steps, kept under a name so they can run again."""
    abilities = []
    for workflow in workflows:
        name = str(getattr(workflow, "name", "") or "").strip()
        if not name:
            continue
        parameters = ", ".join(getattr(workflow, "parameters", None) or [])
        abilities.append(
            Ability(
                id=f"workflow:{slugify(name)}",
                kind=AbilityKind.WORKFLOW,
                name=name,
                summary=str(getattr(workflow, "objective_template", "") or "")[:140] or f"the saved workflow {name}",
                group="workflows",
                detail=f"inputs: {parameters}" if parameters else "",
                guidance=f'The user runs it by saying "run {name}"' + (f' with values for: {parameters}.' if parameters else "."),
                source="workflows",
            )
        )
    return abilities


def agent_abilities(providers_available: Iterable[str], enabled: bool) -> list[Ability]:
    """The coding agents installed on this machine. One that is not installed is not a record: it cannot be fetched from here."""
    available = set(providers_available)
    abilities = []
    for provider, (name, summary) in _AGENTS.items():
        if provider not in available:
            continue
        abilities.append(
            Ability(
                id=f"agent:{provider}",
                kind=AbilityKind.AGENT,
                name=name,
                summary=summary,
                group="code",
                state=AbilityState.READY if enabled else AbilityState.DISABLED,
                provides=("code", "program", "app", "website", "refactor", "bug", "tests", "repository", "develop", "software"),
                call={"tool": "coding.agent", "input": {"operation": "start", "provider": provider}} if enabled else {},
                needs=() if enabled else (NEED_ENABLE,),
                source="coding agent",
            )
        )
    return abilities


def build_index(
    registry: ToolRegistry,
    settings: AppSettings,
    *,
    workflows: Callable[[], Iterable[object]] | None = None,
    extra: Iterable[Ability] = (),
) -> AbilityIndex:
    """Everything known right now, most specific kinds first (skills and workflows before the tools they use, gaps last)."""
    coding = registry.adapters.get("coding.agent")
    providers = coding.providers_available() if hasattr(coding, "providers_available") else ()
    coding_definition = (registry.definition_index or {}).get("coding.agent")
    abilities: list[Ability] = []
    abilities.extend(skill_abilities(settings))
    try:
        abilities.extend(workflow_abilities(workflows() if workflows is not None else ()))
    except Exception:  # noqa: BLE001 - see module docstring
        logger.warning("abilities_workflows_unreadable", exc_info=True)
    abilities.extend(extra)
    abilities.extend(agent_abilities(providers, bool(coding_definition and coding_definition.enabled)))
    abilities.extend(mcp_abilities(settings))
    abilities.extend(tool_abilities(registry.definitions))
    abilities.extend(KNOWN_GAPS)
    return AbilityIndex(abilities)
