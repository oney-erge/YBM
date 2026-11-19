"""Lets the agent itself save a durable fact mid-task (docs/archive/UI_UX_AUDIT.md
Phase 4), not just an operator typing into the Memory page.

Every fact this tool creates is stamped MemorySource.TASK_DERIVED and
carries the originating task_id - the provenance distinction the Memory
page needs to tell "I told it this" (operator_admin, via admin.py) apart
from "it decided this was worth keeping while working" (this tool). The
model cannot claim a different source; there is no source field on the
input at all.
"""

from __future__ import annotations

import re
from pathlib import Path

from agent_control import projects_memory
from agent_control.schemas import Capability, MemoryFact, MemorySource, RiskLevel, ToolCallRequest, ToolCallResult, ToolResultStatus
from agent_control.storage.repositories import Repositories
from agent_control.tools.contracts import MemoryManageInput, MemoryManageOutput
from agent_control.tools.spec import (
    Adapters,
    Definitions,
    RegistryDeps,
    ToolDefinition,
    capability_enabled,
    failed_result,
    same_output_schema,
)


class MemoryManageAdapter:
    def __init__(self, repositories: Repositories, memory_dir: Path | None = None) -> None:
        self.repositories = repositories
        # Where the readable copy of what is remembered is written (projects_memory.write_files).
        self.memory_dir = memory_dir or projects_memory.MEMORY_DIR

    async def execute(self, request: ToolCallRequest) -> ToolCallResult:
        operation = str(request.input.get("operation") or "remember")
        try:
            if operation == "remember":
                output = self._remember(request)
            elif operation == "list":
                output = self._list(request)
            elif operation == "forget":
                output = self._forget(request)
            elif operation == "search":
                output = self._search(request)
            elif operation == "projects":
                output = self._projects()
            elif operation == "project":
                output = self._project(request)
            elif operation == "set_project":
                output = self._set_project(request)
            else:
                return failed_result(request, f"unsupported memory operation: {operation}")
        except Exception as exc:
            return failed_result(request, f"memory operation failed: {exc}")
        output["operation"] = operation
        output["terminal_output"] = [_terminal_output(operation, output)]
        return ToolCallResult(request_id=request.id, status=ToolResultStatus.SUCCEEDED, output=output)

    def _remember(self, request: ToolCallRequest) -> dict:
        category = str(request.input["category"]).strip()
        content = str(request.input["content"]).strip()
        fact = self.repositories.memory_facts.create(
            MemoryFact(category=category, content=content, source=MemorySource.TASK_DERIVED, task_id=request.task_id)
        )
        return {
            "fact_id": fact.id,
            "facts": [fact.model_dump(mode="json")],
            "summary": f"Remembered [{category}]: {content}",
        }

    def _list(self, request: ToolCallRequest) -> dict:
        query = request.input.get("query")
        facts = self.repositories.memory_facts.list_all(query=str(query) if query else None)
        # The text is what the model reads: a bare count left "what do you remember about me?" with nothing to answer from.
        lines = [f"- [{fact.category}] {fact.content}" for fact in facts[:40]]
        if len(facts) > 40:
            lines.append(f"- ... {len(facts) - 40} more")
        return {
            "facts": [f.model_dump(mode="json") for f in facts],
            "summary": f"Found {len(facts)} remembered fact(s)." + ("\n" + "\n".join(lines) if lines else ""),
        }

    def _search(self, request: ToolCallRequest) -> dict:
        """What is known about something and what was said about it in any chat, with the dates. Facts that stopped being true are shown with when."""
        query = str(request.input.get("query") or "").strip()
        words = [w.casefold() for w in re.findall(r"[A-Za-z0-9]+", query) if len(w) > 2]
        facts = [
            fact for fact in self.repositories.memory_facts.list_all(include_closed=True)
            if any(w in f"{fact.content} {fact.category} {fact.about or ''}".casefold() for w in words)
        ][:12]
        episodes = self.repositories.episodes.search(query, limit=8)
        lines = []
        for fact in facts:
            until = f" (until {fact.valid_to.date().isoformat()})" if fact.valid_to else ""
            lines.append(f"- known [{fact.about or fact.category}] {fact.content}{until}")
        for episode in episodes:
            who = {"said": "you said", "answer": "I answered", "note": "noted"}.get(episode["kind"], episode["kind"])
            lines.append(f"- {str(episode['at'])[:10]} {who}: {episode['text'][:240]}")
        return {
            "facts": [f.model_dump(mode="json") for f in facts],
            "summary": (f"Found {len(facts)} fact(s) and {len(episodes)} past message(s) about {query!r}:\n" + "\n".join(lines)) if lines else f"Nothing remembered or said about {query!r}.",
        }

    def _projects(self) -> dict:
        """Every project and where it stands: what "which projects are active, which are parked?" is answered from."""
        rows = projects_memory.listing(self.repositories)
        projects_memory.write_files(self.repositories, self.memory_dir)
        if not rows:
            return {"summary": "No projects are being held for this person yet."}
        now = projects_memory.utc_now()
        lines = []
        for state in ("active", "parked", "done", "dropped"):
            group = [(project, touched) for project, found, touched in rows if found == state]
            if group:
                lines.append(f"{state.capitalize()} ({len(group)}):")
                lines += [f"- {project.name}, last touched {projects_memory._ago(touched, now)}" + (f"; words: {', '.join(project.hooks[:5])}" if project.hooks else "") for project, touched in group]
        return {"summary": "\n".join(lines) + f"\nEach project's page is a file under {self.memory_dir}; read one with memory.manage project."}

    def _project(self, request: ToolCallRequest) -> dict:
        """One project's page: where it stands, what was decided and why, what is open, what was bought. Read this before saying where a project stands."""
        found = projects_memory.describe(self.repositories, str(request.input["name"]))
        if found is None:
            raise ValueError(f"no project matches {request.input['name']!r}; list them with memory.manage projects")
        project, page = found
        projects_memory.write_files(self.repositories, self.memory_dir)
        return {"summary": page, "project_id": project.id}

    def _set_project(self, request: ToolCallRequest) -> dict:
        """The person's word on a project: it is done, dropped or active again, or these words point to it."""
        found = projects_memory.describe(self.repositories, str(request.input["name"]))
        if found is None:
            raise ValueError(f"no project matches {request.input['name']!r}; list them with memory.manage projects")
        project, _ = found
        hooks = [" ".join(str(h).lower().split())[:30] for h in request.input.get("hooks") or [] if str(h).strip()]
        merged = list(dict.fromkeys([*project.hooks, *hooks]))[: projects_memory.MAX_HOOKS] if hooks else None
        self.repositories.projects.set_memory(project.id, hooks=merged, status=request.input.get("status"))
        changed = ([f"status {request.input['status']}"] if request.input.get("status") else []) + ([f"words {', '.join(hooks)}"] if hooks else [])
        return {"summary": f"{project.name}: {'; '.join(changed)}.", "project_id": project.id}

    def _forget(self, request: ToolCallRequest) -> dict:
        fact_id = str(request.input["fact_id"]).strip()
        deleted = self.repositories.memory_facts.delete(fact_id)
        if not deleted:
            raise ValueError(f"no remembered fact with id {fact_id!r}")
        return {"fact_id": fact_id, "summary": f"Forgot {fact_id}."}


def _terminal_output(operation: str, output: dict) -> dict:
    return {
        "content": output.get("summary") or f"memory.manage {operation} completed.",
        "is_final": True,
        "exit_code": 0,
    }


def register(deps: RegistryDeps, definitions: Definitions, adapters: Adapters) -> None:
    settings = deps.settings
    enabled = capability_enabled(settings, Capability.MEMORY_MANAGE)
    definitions.append(
        ToolDefinition(
            name="memory.manage",
            group="memory",
            provides=('remember', 'forget', 'fact', 'that', 'note', 'down',),
            capability=Capability.MEMORY_MANAGE,
            enabled=enabled,
            description=(
                "remember a durable fact about the user or their setup for future tasks, list "
                "previously remembered facts, search what was said and remembered about something in any chat, or forget one by id. Also the projects held for the person "
                "(a pool, a robot, a daycare search): projects lists them as active, parked and finished, project reads one's page (what was decided and why, what is open), set_project "
                "records that the person says it is done or dropped, or which words point to it"
            ),
            operations=("remember", "list", "forget", "search", "projects", "project", "set_project"),
            input_schema=MemoryManageInput,
            output_schema=MemoryManageOutput,
            operation_output_schemas=same_output_schema(("remember", "list", "forget", "search", "projects", "project", "set_project"), MemoryManageOutput),
            default_operation="remember",
            # docs/archive/UI_UX_AUDIT.md Phase 15: remember/list stay low-risk,
            # no-approval - forgetting a durable fact is the one operation
            # that erases something a human may have relied on the agent
            # to keep, so it gets a real gate, the same pattern
            # schedule.manage already uses for its own destructive ops.
            operation_risks={
                "remember": RiskLevel.LOW,
                "list": RiskLevel.LOW,
                "search": RiskLevel.LOW,
                "projects": RiskLevel.LOW,
                "project": RiskLevel.LOW,
                "set_project": RiskLevel.LOW,
                "forget": RiskLevel.MEDIUM,
            },
            approval_required_operations=("forget",),
            approval_reasons={
                "forget": "permanently deletes a remembered fact - the agent cannot silently erase something you asked it to remember",
            },
            examples=(
                {"operation": "remember", "category": "preference", "content": "Prefers metric units"},
                {"operation": "list", "query": "preference"},
                {"operation": "search", "query": "daycare"},
                {"operation": "projects"},
                {"operation": "project", "name": "the pool"},
                {"operation": "forget", "fact_id": "{{fact_id}}"},
            ),
        )
    )
    if enabled and deps.repositories is not None:
        adapters["memory.manage"] = MemoryManageAdapter(deps.repositories)  # type: ignore[arg-type]
