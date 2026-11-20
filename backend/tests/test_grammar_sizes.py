"""Every field the model writes under a JSON grammar has a size (docs/DESIGN.md, D6).

Live 11/19: an open field is where the 9B writes on to the 4,096-token cap. "I live in Dallas." waited three and a half minutes behind a fact's invented `id`; a plan, made and
then revised, lost three minutes to each of two planner calls; the model server has one slot, so every other call waits too. Code is the one answer whose field cannot
have a size, so it is asked for without the grammar instead.
"""

from __future__ import annotations

import importlib
import inspect
from pathlib import Path

import pytest
from pydantic import BaseModel

SRC = Path(__file__).resolve().parents[1] / "src" / "agent_control"

#: Where a structured answer is asked for, and where its models are defined. A new caller fails `test_every_caller_is_listed` until it is added here.
CALLERS = {
    "channels/memory_learning.py": "agent_control.channels.memory_learning",
    "jobs/io.py": None,  # `think` passes on the models of the three job modules below
    "jobs/investigate.py": "agent_control.jobs.investigate",
    "jobs/research.py": "agent_control.jobs.research",
    "jobs/survey.py": "agent_control.jobs.survey",
    "llm/classifier.py": "agent_control.schemas",  # MessageClassification, run on every message
    "orchestration/job_start.py": "agent_control.orchestration.job_start",
    "orchestration/guards.py": "agent_control.orchestration.retry",  # the retry's one diagnosis call
    "orchestration/operator.py": None,  # held to the per-tool schema of tools/call_schema.py (D6 part 1), not to OperatorDecision's own
    "persona_learning.py": "agent_control.persona_learning",
    "plans/planner.py": "agent_control.plans.planner",
    "skill_learning.py": "agent_control.skill_learning",
    "tools/code_interpreter.py": "agent_control.tools.code_interpreter",
}
#: The classes in agent_control.schemas that a structured call asks for.
SCHEMAS_ASKED_FOR = {"MessageClassification"}
#: Fields asked for without the grammar (`grammar=False`), so no size is needed: long free text.
WITHOUT_GRAMMAR = {"GeneratedPythonScript.code"}
NOT_STRUCTURED = ("llm/providers.py", "llm/anthropic_provider.py", "testing/")


def _open_fields(schema: dict, defs: dict, path: str, seen: frozenset[str] = frozenset()) -> list[str]:
    if "$ref" in schema:
        name = schema["$ref"].split("/")[-1]
        return [] if name in seen else _open_fields(defs[name], defs, path, seen | {name})
    found: list[str] = []
    for key in ("anyOf", "oneOf", "allOf"):
        for part in schema.get(key, []):
            found += _open_fields(part, defs, path, seen)
    kind = schema.get("type")
    if kind == "string" and not ({"enum", "const", "maxLength", "format"} & set(schema)):
        found.append(path)
    if kind == "array":
        if "maxItems" not in schema:
            found.append(path + "[]")
        found += _open_fields(schema.get("items", {}), defs, path + "[]", seen)
    for name, sub in (schema.get("properties") or {}).items():
        found += _open_fields(sub, defs, f"{path}.{name}", seen)
    return found


def _models() -> list[type[BaseModel]]:
    models = []
    for module_name in sorted({name for name in CALLERS.values() if name}):
        module = importlib.import_module(module_name)
        for name, cls in inspect.getmembers(module, inspect.isclass):
            if issubclass(cls, BaseModel) and cls.__module__ == module_name and (module_name != "agent_control.schemas" or name in SCHEMAS_ASKED_FOR):
                models.append(cls)
    return models


def test_every_caller_is_listed() -> None:
    callers = set()
    for path in SRC.rglob("*.py"):
        relative = path.relative_to(SRC).as_posix()
        code = [line for line in path.read_text(encoding="utf-8").splitlines() if not line.lstrip().startswith("#")]
        if not relative.startswith(NOT_STRUCTURED) and any("generate_structured" in line or "io.think(" in line for line in code):
            callers.add(relative)
    assert callers <= set(CALLERS), f"a structured answer is asked for in {sorted(callers - set(CALLERS))}: list it in CALLERS and give its fields a size"


@pytest.mark.parametrize("model", _models(), ids=lambda model: model.__name__)
def test_every_field_the_model_writes_under_a_grammar_has_a_size(model: type[BaseModel]) -> None:
    schema = model.model_json_schema()
    found = [field for field in _open_fields(schema, schema.get("$defs", {}), model.__name__) if field not in WITHOUT_GRAMMAR]
    assert not found, f"open under the grammar (give each a maxLength or maxItems in json_schema_extra, docs/DESIGN.md D6): {found}"


def test_a_size_is_for_the_grammar_only_and_a_longer_answer_is_still_read() -> None:
    """A model with no grammar (a hosted fallback, the no-grammar path) may write more; that is cut where the code cuts it, not refused."""
    from agent_control.jobs.survey import _Cell
    from agent_control.schemas import MessageClassification

    assert len(_Cell(criterion="hours", value="v" * 500, quote="q" * 900).quote) == 900
    long = MessageClassification.model_validate({"is_task": True, "reason": "r" * 900, "goal_ref": "G" * 40, "intent": {"route": "conversation", "reasoning": "x" * 900}})
    assert len(long.reason) == 900 and len(long.goal_ref) == 40


def test_an_optional_text_field_carries_its_size_on_the_string() -> None:
    """Beside an anyOf (str | None) a size is not seen by the grammar; it has to sit on the string branch."""
    from agent_control.schemas import MessageClassification

    goal_ref = MessageClassification.model_json_schema()["properties"]["goal_ref"]
    assert {"type": "string", "maxLength": 12}.items() <= goal_ref["anyOf"][0].items()
