"""The Operator's reply as one JSON schema in which every tool call is a real tool, a real operation and its real arguments.

The Operator used to be held to OperatorDecision's own shape, where ``tool_name`` is any text and ``tool_input`` is any
object. A small local model then wrote ``task_name`` where schedule.manage needs ``objective``, called a tool named
``filesystem``, or left the operation out, and the call was refused afterwards: half of the requests that failed on the
local Qwen3.5-9B since 10/25 had at least one such call. llama.cpp (and Ollama) can enforce a JSON schema while the model
writes, so here every tool operation becomes one branch of an ``anyOf`` and a wrong call cannot be written at all.

The branches come from the tool definitions, so they cannot drift from what ``ToolDefinition.validate_input`` checks.
This is a constraint on writing only: the reply is still validated as an OperatorDecision, and the tool's own validation
still runs.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from agent_control.schemas import OperatorAction, OperatorDecision
from agent_control.tools.spec import ToolDefinition

_MAX_REF_DEPTH = 8

# Actions that finish or hand control back, as opposed to running a tool.
FINISH_ACTIONS = (OperatorAction.DONE.value, OperatorAction.ASK_USER.value, OperatorAction.BLOCKED.value)


def inline_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """The schema with every local ``$ref`` replaced by the definition it names.

    The branches sit inside a larger schema, where a ``#/$defs/...`` reference would point at the wrong document. A
    definition that refers to itself is cut at ``_MAX_REF_DEPTH`` and left permissive there.
    """
    defs = schema.get("$defs") or {}

    def walk(node: Any, depth: int) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                if depth >= _MAX_REF_DEPTH:
                    return {}
                target = defs.get(str(node["$ref"]).rsplit("/", 1)[-1], {})
                merged = {**target, **{key: value for key, value in node.items() if key != "$ref"}}
                return walk(merged, depth + 1)
            return {key: walk(value, depth) for key, value in node.items() if key != "$defs"}
        if isinstance(node, list):
            return [walk(item, depth) for item in node]
        return node

    return walk(schema, 0)


def _fields(model: Any) -> dict[str, Any]:
    """A model's own fields as the properties and required list of an object schema."""
    described = inline_refs(model.model_json_schema())
    return {
        "type": "object",
        "properties": dict(described.get("properties") or {}),
        "required": list(described.get("required") or []),
        "additionalProperties": False,
    }


def _arguments_schema(model: Any, operation: str | None) -> dict[str, Any]:
    """The ``tool_input`` object for one operation: the model's fields, the operation fixed, nothing else allowed."""
    if model is None:
        # A tool whose arguments are not described (a bridged MCP tool, a tool made at run time): any object.
        return {"type": "object"}
    arguments = _fields(model)
    if operation is not None:
        arguments["properties"]["operation"] = {"const": operation}
        arguments["required"] = sorted(set(arguments["required"]) | {"operation"})
    return arguments


def _tool_branches(definition: ToolDefinition) -> list[dict[str, Any]]:
    """One branch per operation (or one for a tool that has none), each a complete ``call_tool`` decision."""
    if definition.operation_schemas:
        pairs: list[tuple[str | None, Any]] = list(definition.operation_schemas.items())
    else:
        pairs = [(None, definition.input_schema)]
    return [
        {
            "type": "object",
            "properties": {
                "action": {"const": OperatorAction.CALL_TOOL.value},
                "reasoning": {"type": "string"},
                "tool_name": {"const": definition.name},
                "tool_input": _arguments_schema(model, operation),
                "risk_level": _risk_levels(),
            },
            "required": ["action", "tool_name", "tool_input"],
            "additionalProperties": False,
        }
        for operation, model in pairs
    ]


def _risk_levels() -> dict[str, Any]:
    return inline_refs(OperatorDecision.model_json_schema())["properties"]["risk_level"]


def operator_response_schema(
    definitions: Iterable[ToolDefinition],
    *,
    tool_names: Iterable[str] | None = None,
    actions: Iterable[str] | None = None,
) -> dict[str, Any]:
    """The schema the Operator's reply must satisfy.

    ``tool_names`` narrows the callable tools (default: every enabled tool) and ``actions`` narrows the kinds of reply
    (default: all of OperatorAction), for the steps where only some may be taken. The shapes of
    ``call_tools_parallel`` and ``delegate`` stay loose: a tool name from the list, any arguments.
    """
    wanted = None if tool_names is None else set(tool_names)
    usable = [d for d in definitions if d.enabled and (wanted is None or d.name in wanted)]
    allowed = {action.value for action in OperatorAction} if actions is None else set(actions)
    names = sorted(d.name for d in usable)

    branches: list[dict[str, Any]] = []
    if OperatorAction.CALL_TOOL.value in allowed:
        for definition in usable:
            branches.extend(_tool_branches(definition))
    if OperatorAction.CALL_TOOLS_PARALLEL.value in allowed and names:
        branches.append(
            {
                "type": "object",
                "properties": {
                    "action": {"const": OperatorAction.CALL_TOOLS_PARALLEL.value},
                    "reasoning": {"type": "string"},
                    "parallel_calls": {
                        "type": "array",
                        "minItems": 2,
                        "items": {
                            "type": "object",
                            "properties": {"tool_name": {"enum": names}, "tool_input": {"type": "object"}},
                            "required": ["tool_name"],
                        },
                    },
                },
                "required": ["action", "parallel_calls"],
                "additionalProperties": False,
            }
        )
    if OperatorAction.DELEGATE.value in allowed and names:
        branches.append(
            {
                "type": "object",
                "properties": {
                    "action": {"const": OperatorAction.DELEGATE.value},
                    "reasoning": {"type": "string"},
                    "delegate_objective": {"type": "string", "minLength": 1},
                    "delegate_tools": {"type": "array", "items": {"enum": names}},
                },
                "required": ["action", "delegate_objective"],
                "additionalProperties": False,
            }
        )
    finishing = [action for action in FINISH_ACTIONS if action in allowed]
    if finishing:
        branches.append(
            {
                "type": "object",
                "properties": {
                    "action": {"enum": finishing},
                    "reasoning": {"type": "string"},
                    "final_answer": {"type": "string"},
                    "question": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["action"],
                "additionalProperties": False,
            }
        )
    return {"anyOf": branches}


class CachedOperatorSchema:
    """The schema for the registry's current tools, rebuilt only when a tool is added, removed, enabled or disabled.

    Building it walks every operation's model, so it is done once rather than on every step. A tool made at run time
    (adapter.factory promotes one) changes the key and gets its branch on the next step.
    """

    def __init__(self, registry: Any) -> None:
        self._registry = registry
        self._key: tuple | None = None
        self._schema: dict[str, Any] = {}
        self._finish_only = operator_response_schema([], actions=FINISH_ACTIONS)

    def __call__(self, *, finish_only: bool = False) -> dict[str, Any]:
        if finish_only:
            return self._finish_only
        definitions = tuple(self._registry.definitions)
        key = tuple((d.name, d.enabled, tuple(d.operations or ())) for d in definitions)
        if key != self._key:
            self._schema = operator_response_schema(definitions)
            self._key = key
        return self._schema
