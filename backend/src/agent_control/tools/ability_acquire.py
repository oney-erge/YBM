"""Get a capability the assistant does not have yet, with the user's approval when it matters: a voice, speech recognition, ffmpeg, a vision model.

The index (abilities/) says a capability is "installable" and which recipe gets it (abilities/recipes.py); this tool carries the recipe out
(abilities/acquire.py) and then carries the request forward: the worker picked its tools before the install, so the request that wanted the
capability is started again as a new task that sees it. Everything here is the recipe's: the address, the hash, the command and the
configuration are data in this repository, never taken from a request.

Whether to ask is the model's judgment, with floors it cannot lower. `ask_first` is the model saying "the user should be asked"; a recipe
that is large, or installs a program on this computer, is always put to the user. Both become the same approval card, worded here, so a
person sees what is about to be fetched, how big it is and where from.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import structlog

from agent_control.abilities.acquire import Acquirer, AcquireError
from agent_control.abilities.recipes import ASK_ALWAYS_MB, RECIPES, Recipe, recipe_by_id
from agent_control.config_sync import ConfigManager
from agent_control.schemas import Capability, RiskLevel, ToolCallRequest, ToolCallResult, ToolResultStatus
from agent_control.tools.contracts import AbilityAcquireInput, AbilityAcquireOutput
from agent_control.tools.spec import Adapters, Definitions, RegistryDeps, ToolDefinition, capability_enabled, failed_result, same_output_schema

logger = structlog.get_logger(__name__)

#: What a task keeps from the request that started it, when it is started again after an install: where the answer goes and what was asked.
_CARRIED_METADATA = (
    "source_message_id", "source_channel", "source_chat_id", "source_sender_id", "task_type", "classification_confidence",
    "classification_reason", "orchestration_intent", "expected_postconditions", "original_message_text", "memory_context",
)


def needs_approval(value: dict) -> bool:
    """The model said to ask, or the recipe is one that is always asked about, or the recipe is not one this assistant has."""
    recipe = recipe_by_id(str(value.get("recipe") or ""))
    return recipe is None or bool(value.get("ask_first")) or recipe.must_ask


def risk_of(value: dict) -> RiskLevel:
    """Low for a small curated file in this assistant's own folder, so that the approval policy's floor does not ask about it and the model's
    own judgment (`ask_first`) decides; high for what must always be asked, which the floor then asks about whatever the policy says."""
    recipe = recipe_by_id(str(value.get("recipe") or ""))
    return RiskLevel.HIGH if recipe is None or recipe.must_ask else RiskLevel.LOW


def approval_sentence(value: dict) -> str:
    recipe = recipe_by_id(str(value.get("recipe") or ""))
    if recipe is None:
        return "Set up something this assistant does not have a recipe for."
    reason = str(value.get("reason") or "").strip()
    where = "It installs a program on this computer. " if recipe.changes_system else "It stays in this assistant's own folder. "
    return (
        f"Set up {recipe.title} (about {recipe.size_mb} MB, from {recipe.source})"
        + (f" so I can {reason.rstrip('.')}" if reason else "")
        + f". {where}Nothing about your files leaves this computer."
    )


def _is_ready(recipe: Recipe, settings: Any) -> bool:
    try:
        return bool(recipe.ready(settings))
    except Exception:  # noqa: BLE001 - a check that cannot run is not "ready"
        return False


def _plan_text(recipe: Recipe) -> str:
    place = "and installs a program on this computer" if recipe.changes_system else "into this assistant's own folder"
    asking = "The user is always asked first." if recipe.must_ask else "Ask the user first (ask_first true) if you think they would want to be asked."
    return f"{recipe.title.capitalize()} is not set up. Setting it up fetches about {recipe.size_mb} MB from {recipe.source} {place}. {asking}"


class AbilityAcquireAdapter:
    def __init__(self, deps: RegistryDeps, acquirer: Acquirer | None = None) -> None:
        self.settings = deps.settings
        self.repositories = deps.repositories
        config = ConfigManager()
        self.acquirer = acquirer or Acquirer(
            Path(deps.settings.abilities.models_dir).expanduser(), read_config=config.read_config, write_config=config.write_config
        )

    async def execute(self, request: ToolCallRequest) -> ToolCallResult:
        recipe = recipe_by_id(str(request.input.get("recipe") or ""))
        if recipe is None:
            known = ", ".join(r.id for r in RECIPES)
            return failed_result(request, f"there is no recipe called {request.input.get('recipe')!r}. The ones this assistant has: {known}")
        operation = str(request.input.get("operation") or "install")
        ready = _is_ready(recipe, self.settings)
        if operation == "plan" or ready:
            text = f"{recipe.title.capitalize()} is already set up." if ready else _plan_text(recipe)
            return self._answer(request, recipe, operation, ready=ready, installed=False, text=text)
        try:
            done = await self.acquirer.install(recipe)
        except AcquireError as exc:
            return failed_result(request, f"could not set up {recipe.title}: {exc}")
        except Exception as exc:  # noqa: BLE001 - say what failed, in words
            logger.warning("acquire_failed", recipe=recipe.id, exc_info=True)
            return failed_result(request, f"could not set up {recipe.title}: {type(exc).__name__}: {str(exc)[:200]}")
        retry = self._start_again(request) if request.input.get("resume", True) else None
        text = f"{recipe.title.capitalize()} is set up ({len(done.files)} file(s) fetched, {len(done.configured)} setting(s) switched on). {recipe.after}"
        if retry:
            text += " The request that needed it has been started again with it."
        return self._answer(request, recipe, operation, ready=True, installed=True, text=text, retry=retry)

    def _answer(
        self, request: ToolCallRequest, recipe: Recipe, operation: str, *, ready: bool, installed: bool, text: str, retry: str | None = None
    ) -> ToolCallResult:
        output: dict[str, Any] = {
            "operation": operation, "summary": text, "recipe": recipe.id, "ready": ready, "installed": installed,
            "size_mb": recipe.size_mb, "source": recipe.source, "retry_task_id": retry,
            "terminal_output": [{"content": text, "exit_code": 0, "is_final": True}],
        }
        return ToolCallResult(request_id=request.id, status=ToolResultStatus.SUCCEEDED, output=output)

    def _start_again(self, request: ToolCallRequest) -> str | None:
        """The request that needed the capability, as a new task that sees it. Only once: a task that is itself a retry does not start another."""
        tasks = getattr(self.repositories, "tasks", None)
        if tasks is None:
            return None
        task = tasks.get(request.task_id)
        if task is None or task.metadata.get("retry_after_install"):
            return None
        metadata = {key: task.metadata[key] for key in _CARRIED_METADATA if key in task.metadata}
        metadata["retry_after_install"] = str(request.input.get("recipe"))
        metadata["retry_of"] = task.id
        return tasks.create(task.objective, conversation_id=task.conversation_id, metadata=metadata).id


def register(deps: RegistryDeps, definitions: Definitions, adapters: Adapters) -> None:
    settings = deps.settings
    enabled = settings.abilities.acquire and capability_enabled(settings, Capability.DEPENDENCIES_INSTALL)
    shown = "; ".join(f"{recipe.id} ({recipe.title})" for recipe in RECIPES)
    definitions.append(
        ToolDefinition(
            name="ability.acquire",
            group="setup",
            provides=("install", "download", "setup", "get", "voice", "ffmpeg", "model", "enable", "acquire"),
            capability=Capability.DEPENDENCIES_INSTALL,
            enabled=enabled,
            named_when_disabled=False,
            description=(
                "get a capability this assistant does not have yet from a recipe it knows, then carry the request on with it: "
                f"{shown}. Set ask_first to true when the user should be asked (a large download, a program installed on their computer, "
                "anything they may not expect); a small file kept in this assistant's own folder does not need a question. Always asked: "
                f"anything of {ASK_ALWAYS_MB} MB or more, and any program installed on this computer"
            ),
            operations=("plan", "install"),
            input_schema=AbilityAcquireInput,
            output_schema=AbilityAcquireOutput,
            operation_output_schemas=same_output_schema(("plan", "install"), AbilityAcquireOutput),
            default_operation="install",
            risk_resolver=risk_of,
            approval_resolver=lambda value: str(value.get("operation") or "install") == "install" and needs_approval(value),
            approval_reasons={"install": approval_sentence},
            examples=(
                {"operation": "install", "recipe": "voice.kokoro", "reason": "read the answer aloud", "ask_first": False},
                {"operation": "install", "recipe": "video.ffmpeg", "reason": "make the slideshow video", "ask_first": True},
            ),
        )
    )
    if enabled:
        adapters["ability.acquire"] = AbilityAcquireAdapter(deps)
