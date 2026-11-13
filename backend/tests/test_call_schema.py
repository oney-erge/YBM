"""The Operator's reply schema: every tool call is a real tool, a real operation and its real arguments."""

from __future__ import annotations

import json
from dataclasses import replace

import httpx
import pytest

from agent_control.config import AppSettings, LLMProfileConfig
from agent_control.llm.providers import OpenAICompatibleProvider
from agent_control.orchestration.operator import OperatorLoopService
from agent_control.schemas import OperatorAction, OperatorDecision
from agent_control.tools.call_schema import CachedOperatorSchema, inline_refs, operator_response_schema
from agent_control.tools.registry import build_tool_registry
from helpers import make_repos

jsonschema = pytest.importorskip("jsonschema")


@pytest.fixture(scope="module")
def registry(tmp_path_factory):
    """Every tool the product has, switched on (a fresh install leaves most off), so every operation is covered."""
    repos, audit = make_repos(tmp_path_factory.mktemp("call-schema"))
    built = build_tool_registry(
        AppSettings(_env_file=None), "http://127.0.0.1:8765", repositories=repos, task_repository=repos.tasks,
        artifact_repository=repos.artifacts, audit_logger=audit,
    )
    built.definitions = tuple(replace(definition, enabled=True) for definition in built.definitions)
    built.definition_index = {definition.name: definition for definition in built.definitions}
    return built


@pytest.fixture(scope="module")
def schema(registry):
    return operator_response_schema(registry.definitions)


def accepts(schema: dict, reply: dict) -> bool:
    return jsonschema.Draft202012Validator(schema).is_valid(reply)


def call(tool: str, **arguments) -> dict:
    return {"action": "call_tool", "tool_name": tool, "tool_input": arguments}


def test_every_enabled_operation_has_exactly_one_branch(registry, schema) -> None:
    branches: dict[str, list[dict]] = {}
    for branch in schema["anyOf"]:
        if branch["properties"]["action"].get("const") == "call_tool":
            branches.setdefault(branch["properties"]["tool_name"]["const"], []).append(branch)
    assert set(branches) == {d.name for d in registry.definitions if d.enabled}
    for definition in registry.definitions:
        found = branches[definition.name]
        if definition.operation_schemas:
            operations = [b["properties"]["tool_input"]["properties"]["operation"]["const"] for b in found]
            assert sorted(operations) == sorted(definition.operation_schemas), definition.name
        else:
            assert len(found) == 1, definition.name  # a tool without per-operation models is one branch


def test_the_schema_stands_alone(schema) -> None:
    text = json.dumps(schema)
    assert "$ref" not in text and "$defs" not in text


def test_each_branch_takes_exactly_the_fields_the_operations_model_has(registry, schema) -> None:
    for definition in registry.definitions:
        if not definition.enabled or not definition.operation_schemas:
            continue
        for operation, model in definition.operation_schemas.items():
            branch = next(
                b for b in schema["anyOf"]
                if b["properties"]["tool_name"].get("const") == definition.name
                and b["properties"]["tool_input"].get("properties", {}).get("operation", {}).get("const") == operation
            )
            fields = set(model.model_json_schema().get("properties", {})) | {"operation"}
            arguments = branch["properties"]["tool_input"]
            assert set(arguments["properties"]) == fields, (definition.name, operation)
            assert set(model.model_json_schema().get("required", [])) <= set(arguments["required"])
            assert arguments["additionalProperties"] is False


def test_the_calls_that_failed_live_cannot_be_written(schema) -> None:
    # Each of these is a real call the 9B made and the tool refused.
    assert not accepts(schema, call("schedule.manage", operation="create", task_name="Daily check", cron_expression="0 9 * * *"))
    assert not accepts(schema, call("filesystem", operation="read_text_file", path="C:/notes.txt"))
    assert not accepts(schema, call("web.search", query="new episodes"))  # no operation named
    assert not accepts(schema, call("filesystem.manage", operation="list_directory", path="C:/notes"))
    assert not accepts(schema, call("code.interpreter", operation="run_python", script="print(1)"))  # the field is `code`
    assert not accepts(schema, call("done", final_answer="The total is 611."))  # a finish is not a tool


def test_correct_calls_are_accepted(schema) -> None:
    assert accepts(schema, call("web.search", operation="search", query="new episodes"))
    assert accepts(schema, call("filesystem.manage", operation="read_file", path="C:/notes/a.txt"))
    assert accepts(schema, call("code.interpreter", operation="run_python", code="print(1)"))
    assert accepts(schema, {"action": "done", "final_answer": "The total is 611.", "reasoning": "from the cells"})
    assert accepts(schema, {"action": "ask_user", "question": "Which folder?"})
    assert accepts(schema, {"action": "blocked", "reason": "no access"})


def test_a_reply_that_is_valid_here_is_a_valid_decision(registry, schema) -> None:
    reply = call("filesystem.manage", operation="search", root="C:/notes", query="plan")
    assert accepts(schema, reply)
    decision = OperatorDecision.model_validate(reply)
    definition = registry.definition_index[decision.tool_name]
    assert definition.validate_input(decision.tool_input)["operation"] == "search"


def test_an_unknown_action_or_extra_field_is_refused(schema) -> None:
    assert not accepts(schema, {"action": "wander"})
    assert not accepts(schema, {"action": "done", "tool_name": "filesystem.manage"})
    assert not accepts(schema, call("filesystem.manage", operation="read_file", path="C:/a.txt", mode="fast"))


def test_the_tools_and_actions_can_be_narrowed(registry) -> None:
    narrow = operator_response_schema(registry.definitions, tool_names=["filesystem.manage"], actions=["done", "ask_user"])
    assert accepts(narrow, {"action": "done", "final_answer": "ok"})
    assert not accepts(narrow, call("filesystem.manage", operation="read_file", path="C:/a.txt"))  # a call is not allowed here
    only_files = operator_response_schema(registry.definitions, tool_names=["filesystem.manage"])
    assert accepts(only_files, call("filesystem.manage", operation="read_file", path="C:/a.txt"))
    assert not accepts(only_files, call("web.search", operation="search", query="x"))


def test_a_tool_whose_arguments_are_not_described_takes_any_object(registry) -> None:
    from agent_control.tools.spec import ToolDefinition

    definition = ToolDefinition(name="mcp.thing", capability=next(d.capability for d in registry.definitions), enabled=True, description="x")
    open_schema = operator_response_schema([definition])
    assert accepts(open_schema, call("mcp.thing", anything=1))
    assert not accepts(open_schema, call("mcp.other", anything=1))


def test_a_definition_that_refers_to_itself_is_cut_not_followed_forever() -> None:
    looping = {"$defs": {"Node": {"type": "object", "properties": {"child": {"$ref": "#/$defs/Node"}}}}, "$ref": "#/$defs/Node"}
    flat = inline_refs(looping)
    assert "$ref" not in json.dumps(flat)


def test_the_cached_schema_follows_the_registry(registry) -> None:
    cached = CachedOperatorSchema(registry)
    first = cached()
    assert cached() is first  # not rebuilt while nothing changed
    original = registry.definitions
    try:
        registry.definitions = tuple(d for d in original if d.name != "web.search")
        assert cached() is not first
        assert not accepts(cached(), call("web.search", operation="search", query="x"))
    finally:
        registry.definitions = original


# ---- how the providers and the Operator use it ------------------------------------------------------------------


class _Reply:
    def __init__(self, status: int, content: str = "") -> None:
        self.status_code = status
        self.content = content
        self.request = httpx.Request("POST", "http://127.0.0.1:8000/v1/chat/completions")

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            body = httpx.Response(self.status_code, json={"error": {"message": "Prompt is too large"}}, request=self.request)
            raise httpx.HTTPStatusError("bad request", request=self.request, response=body)

    def json(self) -> dict:
        return {"choices": [{"message": {"content": self.content}}]}


def _provider(monkeypatch, replies: list[_Reply], base_url: str = "http://127.0.0.1:8000/v1"):
    sent: list[dict] = []

    class Client:
        def __init__(self, timeout) -> None: ...
        async def __aenter__(self): return self
        async def __aexit__(self, *exc) -> None: return None

        async def post(self, url, *, headers, json):
            sent.append(json)
            return replies[len(sent) - 1]

    monkeypatch.setattr("agent_control.llm.providers.httpx.AsyncClient", Client)
    return OpenAICompatibleProvider(LLMProfileConfig(model="local", base_url=base_url, max_tokens=64)), sent


STRICT = {"anyOf": [{"type": "object", "properties": {"action": {"const": "done"}}, "required": ["action"]}]}
DONE = json.dumps({"action": "done", "final_answer": "ok"})


@pytest.mark.asyncio
async def test_a_local_server_gets_the_strict_schema(monkeypatch) -> None:
    provider, sent = _provider(monkeypatch, [_Reply(200, DONE)])
    decision = await provider.generate_structured("s", "u", OperatorDecision, response_schema=STRICT)
    assert decision.action == OperatorAction.DONE
    assert sent[0]["response_format"]["json_schema"]["schema"] == STRICT


@pytest.mark.asyncio
async def test_a_hosted_server_keeps_the_plain_schema(monkeypatch) -> None:
    provider, sent = _provider(monkeypatch, [_Reply(200, DONE)], base_url="https://api.openai.com/v1")
    await provider.generate_structured("s", "u", OperatorDecision, response_schema=STRICT)
    assert sent[0]["response_format"]["json_schema"]["schema"] == OperatorDecision.model_json_schema()


@pytest.mark.asyncio
async def test_a_server_that_refuses_the_strict_schema_gets_the_plain_one(monkeypatch) -> None:
    provider, sent = _provider(monkeypatch, [_Reply(400), _Reply(200, DONE)])
    decision = await provider.generate_structured("s", "u", OperatorDecision, response_schema=STRICT)
    assert decision.action == OperatorAction.DONE
    assert sent[0]["response_format"]["json_schema"]["schema"] == STRICT
    assert sent[1]["response_format"]["json_schema"]["schema"] == OperatorDecision.model_json_schema()


@pytest.mark.asyncio
async def test_no_strict_schema_changes_nothing(monkeypatch) -> None:
    provider, sent = _provider(monkeypatch, [_Reply(200, DONE)])
    await provider.generate_structured("s", "u", OperatorDecision)
    assert sent[0]["response_format"]["json_schema"]["schema"] == OperatorDecision.model_json_schema()


class _Recorder:
    def __init__(self) -> None:
        self.kwargs: list[dict] = []

    async def generate_text(self, system_prompt, user_prompt): raise NotImplementedError
    async def generate_multimodal_text(self, system_prompt, user_prompt, image_paths): raise NotImplementedError

    async def generate_structured(self, system_prompt, user_prompt, output_model, **kwargs):
        self.kwargs.append(kwargs)
        return OperatorDecision(action=OperatorAction.DONE, final_answer="ok")


@pytest.mark.asyncio
async def test_the_operator_passes_the_schema_only_when_it_has_one() -> None:
    plain, strict = _Recorder(), _Recorder()
    await OperatorLoopService(plain).decide("do it", "tools", [])
    await OperatorLoopService(strict, response_schema=lambda finish_only=False: STRICT).decide("do it", "tools", [])
    assert "response_schema" not in plain.kwargs[0]
    assert strict.kwargs[0]["response_schema"] == STRICT and strict.kwargs[0]["temperature"] == 0.1


@pytest.mark.asyncio
async def test_a_finish_only_step_gets_the_finishing_schema(registry) -> None:
    recorder = _Recorder()
    cached = CachedOperatorSchema(registry)
    operator = OperatorLoopService(recorder, response_schema=cached)
    await operator.decide("do it", "tools", [])
    await operator.decide("do it", "tools", [], finish_only=True)

    ordinary, finishing = (kw["response_schema"] for kw in recorder.kwargs)
    assert accepts(ordinary, call("filesystem.manage", operation="read_file", path="C:/a.txt"))
    assert not accepts(finishing, call("filesystem.manage", operation="read_file", path="C:/a.txt"))
    for reply in ({"action": "done", "final_answer": "Found nothing."}, {"action": "ask_user", "question": "Which one?"},
                  {"action": "blocked", "reason": "no access"}):
        assert accepts(finishing, reply)
    assert not accepts(finishing, {"action": "call_tools_parallel", "parallel_calls": []})
