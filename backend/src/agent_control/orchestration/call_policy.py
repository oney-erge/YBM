"""What happens to a tool call between the model's decision and the tool: one list, in order.

Before a call runs it is checked and, where it shows one of the local model's known habits, corrected. That used to be thirty lines of
the worker, an `if` then a rewrite then an `if` then fourteen rewrites in a row, and the order mattered without saying so: a refusal has
to come before any rewrite can hide what was asked for, and the rewrites read each other's output. This module is the order, written
once.

    1. REFUSALS on the call as the model wrote it. A call that must not run at all becomes a `Refusal`, and nothing else is looked at.
    2. The call is put into canonical form (aliases for tools and operations, folder aliases).
    3. REFUSALS on the canonical call.
    4. REWRITES, in the order of `REWRITES` below. Each one returns the tool and input to use.

The rules themselves are in `orchestration/call_rewrites.py`. The worker turns a `Refusal` into a history entry (it owns the repositories);
nothing here touches storage, runs a tool, or calls a model.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from agent_control.orchestration import ladder
from agent_control.orchestration.call_rewrites import (
    _apply_manifest_from_plan,
    _canonical_operator_tool_call,
    _coding_agent_input_with_explicit_workspace,
    _coding_agent_input_with_task_defaults,
    _coding_agent_requested,
    _document_summary_call,
    _file_in_named_folder,
    _filesystem_append_when_asked,
    _filesystem_listing_input_with_dates_intent,
    _filesystem_overwrite_when_asked_to_change,
    _filesystem_path_relative_to_a_listed_root,
    _filesystem_search_input_with_content_intent,
    _is_blind_persona_replace,
    _is_simulated_delete,
    _ordered_artifact_delivery_call,
    _required_named_coding_agent_call,
    _schedule_id_from_this_task,
    _schedule_objective_with_request_urls,
    _stale_read_recovery_call,
)
from agent_control.orchestration.request_facts import own_words
from agent_control.schemas import TaskRecord

#: Tools whose calls name files and folders by absolute path.
PATH_GROUNDED_TOOLS = frozenset({"filesystem.manage", "document.manage", "artifact.deliver"})


@dataclass(frozen=True)
class Refusal:
    """A call that must not run. `kind` says why; the worker records the reason in words."""

    kind: str  # ungrounded_path | unrequested_coding_agent | simulated_delete | blind_persona_replace
    paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class VettedCall:
    tool_name: str | None
    tool_input: dict[str, Any]
    refusal: Refusal | None = None
    #: What the model asked for, before canonical form and rewrites (the history says when the two differ).
    requested_name: str | None = None
    requested_input: dict[str, Any] = field(default_factory=dict)


# A rewrite is (task, tool_name, tool_input, history, definitions) -> (tool_name, tool_input). The rules have different signatures,
# so each is adapted here once, and the order below is the order they run in.
Rewrite = Callable[[TaskRecord, str | None, dict[str, Any], list[dict[str, Any]], dict[str, Any]], tuple[str | None, dict[str, Any]]]


def _only_input(rule: Callable[..., dict[str, Any]], *, with_history: bool = False) -> Rewrite:
    def apply(task, name, tool_input, history, definitions):
        return name, (rule(name, tool_input, history) if with_history else rule(task, name, tool_input))

    return apply


REWRITES: tuple[tuple[str, Rewrite], ...] = (
    ("artifact delivery in order", lambda task, name, i, history, defs: _ordered_artifact_delivery_call(task, name, i, history, defs)),
    ("search by content when asked", _only_input(_filesystem_search_input_with_content_intent)),
    ("dates in a listing when asked for newest or oldest", _only_input(_filesystem_listing_input_with_dates_intent)),
    ("path relative to a listed folder", lambda task, name, i, history, defs: (name, _filesystem_path_relative_to_a_listed_root(name, i, history))),
    ("add to the end of a file when asked", _only_input(_filesystem_append_when_asked)),
    ("overwrite a file when asked to change it", _only_input(_filesystem_overwrite_when_asked_to_change)),
    ("apply the manifest the plan made", lambda task, name, i, history, defs: (name, _apply_manifest_from_plan(task, name, i, history))),
    ("summarize a PDF when asked what it says", lambda task, name, i, history, defs: _document_summary_call(task, name, i, defs)),
    ("schedule id from this task", lambda task, name, i, history, defs: (name, _schedule_id_from_this_task(name, i, history))),
    ("keep the user's URLs in a schedule", _only_input(_schedule_objective_with_request_urls)),
    ("the one file in the folder the user named", _only_input(_file_in_named_folder)),
    ("a read of a file changed since it was read", lambda task, name, i, history, defs: _stale_read_recovery_call(task, name, i, history)),
    ("coding agent defaults for this task", _only_input(_coding_agent_input_with_task_defaults)),
    ("coding agent in the workspace the user named", _only_input(_coding_agent_input_with_explicit_workspace)),
)


def vet_call(
    task: TaskRecord,
    tool_name: str | None,
    tool_input: dict[str, Any],
    history: list[dict[str, Any]],
    definitions: dict[str, Any],
) -> VettedCall:
    """The call to run, or why it must not run."""
    requested_name, requested_input = tool_name, tool_input

    # 1. a path the call names that only an earlier turn named (a request to remember or change a setting reads no files)
    intent = task.metadata.get("orchestration_intent")
    if tool_name in PATH_GROUNDED_TOOLS and isinstance(intent, dict) and intent.get("route") == "configuration":
        already_refused = {str(seen).casefold() for seen in task.metadata.get("ungrounded_paths_seen") or []}
        ungrounded = [
            path for path in ladder.ungrounded_paths(own_words(task), tool_input, history, str(task.metadata.get("memory_context") or ""))
            if path.casefold() not in already_refused
        ]
        if ungrounded:
            return VettedCall(tool_name, tool_input, Refusal("ungrounded_path", tuple(ungrounded)), requested_name, requested_input)

    # 2. canonical form
    tool_name, tool_input = _canonical_operator_tool_call(tool_name, tool_input, definitions)
    tool_name, tool_input = _required_named_coding_agent_call(task, tool_name, tool_input, definitions)

    # 3. refusals on the canonical call
    refusal: Refusal | None = None
    if tool_name == "coding.agent" and not _coding_agent_requested(task, tool_input):
        refusal = Refusal("unrequested_coding_agent")
    elif _is_simulated_delete(task, tool_name, tool_input):
        refusal = Refusal("simulated_delete")
    elif _is_blind_persona_replace(tool_name, tool_input, history):
        refusal = Refusal("blind_persona_replace")
    if refusal is not None:
        return VettedCall(tool_name, tool_input, refusal, requested_name, requested_input)

    # 4. rewrites, in order
    for _label, rewrite in REWRITES:
        tool_name, tool_input = rewrite(task, tool_name, tool_input, history, definitions)
    return VettedCall(tool_name, tool_input, None, requested_name, requested_input)
