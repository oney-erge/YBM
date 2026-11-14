"""Turn a task that worked into a reusable procedure - after review.

Skills are now visible to the Operator, but the catalog only ever contains
what a human wrote by hand. Everything needed to grow it is already recorded:
`operator_history` holds each tool, its input, and (since the progress work)
the model's own reason for choosing it. That is a procedure, written down, by
the run that succeeded.

Same discipline as persona learning, for the same reason: a skill is
instructions the Operator will follow on future tasks, so a system that
writes its own skills unattended can change how it behaves tomorrow with no
one having agreed to it. Proposals queue; a human installs them.

Deliberately conservative about what is even worth proposing - a catalog full
of near-duplicate machine-written skills is worse than a small hand-written
one, because the index goes into every Operator prompt.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
import re
import textwrap
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from agent_control.config import SkillsAdapterConfig
from agent_control.schemas import TaskRecord, TaskStatus, new_id, utc_now
from agent_control.tools.skills import _load_skills, write_skill_file


# Below this, the task was too trivial to be a procedure worth writing down.
MIN_STEPS_FOR_A_SKILL = 3

SKILL_SYSTEM_PROMPT = """You write short reusable procedures from a task that already succeeded.

- name: 3-5 words, the KIND of job, never this one instance
  (good "Invoice Total Extraction" / bad "Extract total from march-invoice.pdf")
- description: one line on when to use it
- body: numbered steps, naming the tools and operations that worked, written
  so they apply to the next job of this kind - no paths, dates or values from
  this specific run
- worth_keeping: false when this was a one-off, trivial, or too specific to generalise.
  Prefer false: a catalog of near-duplicates is worse than a small one."""


class _Proposal(BaseModel):
    """A schema, because a small model asked for JSON in free text answered in sentences or wrapped it in prose."""

    worth_keeping: bool = Field(description="true only when the steps are a procedure that will apply to the next job of this kind")
    name: str = Field(default="", description="3 to 5 words, the kind of job")
    description: str = Field(default="", description="one line on when to use it")
    body: str = Field(default="", description="numbered steps, naming the tools and operations that worked")


@dataclass
class SkillSuggestion:
    id: str
    name: str
    description: str
    body: str
    task_id: str | None
    objective: str
    created_at: str
    status: str = "pending"  # pending | accepted | rejected
    decided_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SkillSuggestionStore:
    path: Path
    suggestions: list[SkillSuggestion] = field(default_factory=list)

    @classmethod
    def load(cls, config: SkillsAdapterConfig) -> "SkillSuggestionStore":
        path = Path(config.root_dir).expanduser() / "_suggestions.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls(path=path, suggestions=[])
        return cls(path=path, suggestions=[SkillSuggestion(**item) for item in raw if isinstance(item, dict)])

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps([s.to_dict() for s in self.suggestions], indent=2), encoding="utf-8")

    def pending(self) -> list[SkillSuggestion]:
        return [s for s in self.suggestions if s.status == "pending"]

    def add(self, name: str, description: str, body: str, task: TaskRecord) -> SkillSuggestion | None:
        normalized = name.strip().lower()
        for existing in self.suggestions:
            if existing.status in ("pending", "accepted") and existing.name.strip().lower() == normalized:
                return None
        suggestion = SkillSuggestion(
            id=new_id("skill_sugg"),
            name=name.strip(),
            description=description.strip(),
            body=body.strip(),
            task_id=task.id,
            objective=task.objective[:300],
            created_at=utc_now().isoformat(),
        )
        self.suggestions.append(suggestion)
        self.save()
        return suggestion

    def decide(self, suggestion_id: str, accept: bool) -> SkillSuggestion | None:
        for suggestion in self.suggestions:
            if suggestion.id == suggestion_id and suggestion.status == "pending":
                suggestion.status = "accepted" if accept else "rejected"
                suggestion.decided_at = utc_now().isoformat()
                self.save()
                return suggestion
        return None


def install(config: SkillsAdapterConfig, suggestion: SkillSuggestion) -> dict[str, Any]:
    return write_skill_file(
        config.root_dir,
        suggestion.name,
        suggestion.description,
        f"{suggestion.body.strip()}\n\n_Learned from a completed task on "
        f"{suggestion.created_at[:10]}._",
    )


def _successful_steps(task: TaskRecord) -> list[dict[str, Any]]:
    history = task.metadata.get("operator_history")
    if not isinstance(history, list):
        return []
    return [
        step for step in history
        if isinstance(step, dict)
        and step.get("status") == "succeeded"
        and not str(step.get("tool_name") or "").startswith("_")
    ]


def worth_proposing(task: TaskRecord, config: SkillsAdapterConfig) -> bool:
    """Cheap checks before spending an LLM call.

    A two-step task is not a procedure, and a job the catalog already covers
    does not need a second entry - the index is injected into every Operator
    prompt, so every skill has a permanent cost.
    """
    if not config.enabled or not config.learning_enabled:
        return False
    steps = _successful_steps(task)
    if len(steps) < MIN_STEPS_FOR_A_SKILL:
        return False
    return len(_load_skills(config.root_dir)) < config.max_skills_listed


async def propose_from_task(provider: Any, config: SkillsAdapterConfig, task: TaskRecord) -> SkillSuggestion | None:
    if provider is None or not worth_proposing(task, config):
        return None
    steps = _successful_steps(task)
    rendered = "\n".join(
        f"{index}. {step.get('tool_name')} "
        f"{(step.get('input') or {}).get('operation', '')} - {step.get('reasoning') or ''}".strip()
        for index, step in enumerate(steps, 1)
    )
    prompt = f"Objective: {task.objective}\n\nSteps that worked:\n{rendered}"
    try:
        proposal = await provider.generate_structured(SKILL_SYSTEM_PROMPT, prompt[:4000], _Proposal, temperature=0.0)
    except Exception:  # noqa: BLE001 - never fail a finished task over this
        return None
    name, description, body = proposal.name.strip(), proposal.description.strip(), proposal.body.strip()
    if not (proposal.worth_keeping and name and description and body):
        return None
    return SkillSuggestionStore.load(config).add(name, description, body, task)


# --- helpers: the script that worked, kept -----------------------------------------------------------------------------------------------------------------

HELPER_PREFIX = "helper-"
MAX_HELPERS = 30
_NOT_WORDS = frozenset({
    "the", "a", "an", "of", "in", "on", "to", "for", "and", "or", "my", "me", "i", "is", "are", "was", "all", "how", "many", "what", "which",
    "where", "that", "this", "it", "with", "from", "under", "inside", "please", "can", "you", "do", "does", "there", "total", "each", "every",
    "any", "be", "by", "at", "as", "if", "so", "than", "then", "tell", "find", "show", "give", "get",
})


def helper_script(task: TaskRecord) -> str | None:
    """The Python the task's last successful script step ran, if a script is what produced its answer."""
    history = task.metadata.get("operator_history")
    if not isinstance(history, list):
        return None
    for step in reversed(history):
        if not isinstance(step, dict) or step.get("tool_name") != "code.interpreter" or step.get("status") != "succeeded":
            continue
        tool_input = step.get("input") if isinstance(step.get("input"), dict) else {}
        code = str(tool_input.get("code") or "").strip()
        if tool_input.get("operation") == "run_python" and code and str(step.get("output_summary") or "").strip():
            return code
    return None


def helper_name(request: str) -> str:
    """A plain, stable name from the request's own words: the same kind of request lands on the same helper."""
    words = [w for w in re.findall(r"[a-z0-9]+", request.casefold()) if w not in _NOT_WORDS and len(w) > 1]
    return HELPER_PREFIX + "-".join(words[:5] or ["script"])


def save_helper(config: SkillsAdapterConfig, task: TaskRecord) -> dict[str, Any] | None:
    """File the script that settled a finished task as a helper skill, unless one by that name is already there.

    The first script that worked is kept, not the latest: a later run of the same request that took a worse route should not
    overwrite it. The Operator's prompt lists skills by name and description, and tells it to read one that matches the objective
    before improvising, so the next request of this kind starts from a script that ran.
    """
    if not config.enabled or not config.helpers_enabled or task.status != TaskStatus.COMPLETED:
        return None
    code = helper_script(task)
    if code is None:
        return None
    request = str(task.metadata.get("original_message_text") or task.objective or "").strip()
    if not request:
        return None
    existing = _load_skills(config.root_dir)
    name = helper_name(request)
    if any(skill["name"] == name for skill in existing):
        return None
    if sum(1 for skill in existing if str(skill["name"]).startswith(HELPER_PREFIX)) >= MAX_HELPERS:
        return None
    body = (
        f"A Python script that worked for a request like: {request[:240]}\n\n"
        "Change the values at the top (folders, names, words) to fit the new request, then run it with code.interpreter "
        "(operation run_python, the program in `code`) and answer from what it prints.\n\n"
        f"```python\n{code}\n```"
    )
    return write_skill_file(
        config.root_dir, name, f"Python script for: {textwrap.shorten(request, width=110, placeholder='...')}", body, tools=["code.interpreter"],
    )
