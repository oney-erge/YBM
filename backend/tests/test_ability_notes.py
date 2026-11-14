"""What bears on a request and is not in the Operator's tool list: found by words, confirmed by a question, shown once (abilities/context.py)."""

from __future__ import annotations

import httpx
import pytest

from agent_control.abilities.context import CLEAR_SCORE, SHORTLIST_SCORE, relevant, render
from agent_control.abilities.index import AbilityIndex
from agent_control.abilities.model import Ability, AbilityKind, AbilityState
from agent_control.abilities.sources import build_index
from agent_control.config import AppSettings, DecisionConfig
from agent_control.llm.decision import DecisionModelClient, DecisionModelError
from agent_control.orchestration import TaskWorker
from agent_control.tools.registry import build_tool_registry
from helpers import make_repos


def _index(tmp_path) -> AbilityIndex:
    settings = AppSettings(_env_file=None, abilities={"enabled": True}, adapters={"skills": {"root_dir": str(tmp_path / "none")}})
    return build_index(build_tool_registry(settings, "http://127.0.0.1:8765"), settings)


class _Confirmer:
    """Answers each question with a fixed probability and remembers what it was asked."""

    def __init__(self, answers: dict[str, float] | Exception) -> None:
        self.answers, self.asked = answers, []

    async def __call__(self, request: str, questions: dict[str, str]) -> dict[str, float]:
        self.asked.append((request, dict(questions)))
        if isinstance(self.answers, Exception):
            raise self.answers
        return {name: p for name, p in self.answers.items() if name in questions}


@pytest.mark.asyncio
async def test_a_strong_word_match_is_shown_without_asking_anyone(tmp_path):
    confirmer = _Confirmer({})
    shown = await relevant(_index(tmp_path), "Order me a large pizza from the shop on the corner.", confirm=confirmer)
    assert [a.id for a in shown] == ["gap:commerce.order"] and confirmer.asked == []


@pytest.mark.asyncio
async def test_a_weak_match_is_shown_only_when_the_question_is_answered_yes(tmp_path):
    index = _index(tmp_path)
    request = "pay my electricity bill"  # one shared word: a shortlist, not a match
    assert await relevant(index, request, confirm=None) == []
    yes = _Confirmer({"gap:commerce.order": 0.7})
    assert [a.id for a in await relevant(index, request, confirm=yes)] == ["gap:commerce.order"]
    assert yes.asked == [(request, {"gap:commerce.order": index.get("gap:commerce.order").confirm})]
    assert await relevant(index, request, confirm=_Confirmer({"gap:commerce.order": 0.1})) == []


@pytest.mark.asyncio
async def test_an_unreachable_decision_model_leaves_only_the_strong_matches_and_never_raises(tmp_path):
    index = _index(tmp_path)
    down = _Confirmer(DecisionModelError("decision model unreachable"))
    assert await relevant(index, "pay my electricity bill", confirm=down) == []
    assert [a.name for a in await relevant(index, "buy me a new laptop on amazon", confirm=down)] == ["order and pay"]


@pytest.mark.asyncio
async def test_what_the_prompt_already_lists_is_not_repeated_and_ordinary_requests_add_nothing(tmp_path):
    index = _index(tmp_path)
    for request in ("Move the .png files into a subfolder called pictures.", "How many Steam games are installed?", "What is on https://example.com ?"):
        assert await relevant(index, request, confirm=_Confirmer({})) == []
    assert render([]) == ""


@pytest.mark.asyncio
async def test_a_record_between_the_bars_is_shown_on_a_yes_and_only_the_strong_ones_without_a_decision_model():
    filler = [
        Ability(id=f"tool:t{i}", kind=AbilityKind.TOOL, name=f"t{i}", summary=f"does thing number {i} with files and folders") for i in range(8)
    ]
    zorb = Ability(id="gap:z", kind=AbilityKind.GAP, name="zorb", summary="ride a zorb ball", state=AbilityState.UNAVAILABLE,
                   provides=("zorb", "zorbing", "inflatable"), confirm="Is it about riding a zorb?", confirm_at=0.5)
    index = AbilityIndex([*filler, zorb])
    weak, strong, nothing = "ride zorb", "zorb inflatable ball ride zorbing", "zorb"
    assert SHORTLIST_SCORE <= index.search(weak)[0][1] < CLEAR_SCORE <= index.search(strong)[0][1]
    assert index.search(nothing)[0][1] < SHORTLIST_SCORE
    assert [a.id for a in await relevant(index, weak, confirm=_Confirmer({"gap:z": 0.6}))] == ["gap:z"]
    assert await relevant(index, weak, confirm=_Confirmer({"gap:z": 0.4})) == []
    assert await relevant(index, weak, confirm=_Confirmer({})) == []  # no answer is not a yes
    assert await relevant(index, weak, confirm=None) == []
    assert [a.id for a in await relevant(index, strong, confirm=None)] == ["gap:z"]  # strong enough alone
    asked = _Confirmer({"gap:z": 0.9})
    assert await relevant(index, nothing, confirm=asked) == [] and asked.asked == []  # below the shortlist: never asked


def test_the_block_says_what_exists_and_what_it_would_take():
    gap = Ability(id="gap:x", kind=AbilityKind.GAP, name="order and pay", summary="buy things", state=AbilityState.UNAVAILABLE,
                  guidance="Say plainly it cannot pay.")
    model = Ability(id="model:v", kind=AbilityKind.MODEL, name="vision model", summary="read photos", state=AbilityState.INSTALLABLE,
                    call={"tool": "local.models", "input": {"operation": "load"}})
    text = render([gap, model])
    assert text.startswith("\n\n## What bears on this request")
    assert "order and pay (cannot be done by this assistant): buy things" in text and "Say plainly it cannot pay." in text
    assert "vision model (can be loaded or installed with the user's approval)" in text and "use: {'tool': 'local.models'" in text


# --- the decision model client ----------------------------------------------------------------------------------------------------------------


def _client(handler) -> DecisionModelClient:
    return DecisionModelClient(DecisionConfig(mode="decision"), transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_yes_no_questions_go_in_one_call_and_come_back_as_probabilities():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"answers": {"a": {"noul": 0.9}, "b": {"noul": "0.2"}, "c": {"choice": "x"}}})

    client = _client(handler)
    answers = await client.yes_no("hello", {"a": "Is it a?", "b": "Is it b?", "c": "Is it c?"})
    assert answers == {"a": 0.9, "b": 0.2}  # c was not a yes/no answer: absent, not "no"
    assert len(seen) == 1 and seen[0]["questions"]["a"] == {"type": "noul", "instructions": "Is it a?"} and seen[0]["state"] == "hello"
    assert await client.yes_no("hello", {}) == {}


@pytest.mark.asyncio
async def test_yes_no_reports_an_unreachable_or_broken_model_as_a_decision_model_error():
    def broken(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    with pytest.raises(DecisionModelError):
        await _client(broken).yes_no("hello", {"a": "Is it a?"})

    def odd(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"answers": ["not", "an", "object"]})

    with pytest.raises(DecisionModelError):
        await _client(odd).yes_no("hello", {"a": "Is it a?"})


# --- the worker -------------------------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_worker_works_out_the_notes_once_and_keeps_them_with_the_task(tmp_path):
    repos, audit = make_repos(tmp_path)
    calls = []

    async def notes(request: str) -> str:
        calls.append(request)
        return "\n\n## What bears on this request\n- order and pay"

    worker = TaskWorker(repos, audit, ability_notes=notes)
    task = repos.tasks.create("Order me a pizza.")
    task = await worker._with_ability_notes(task)
    assert task.metadata["ability_notes"].startswith("\n\n## What bears") and calls == ["Order me a pizza."]
    again = await worker._with_ability_notes(task)
    assert again.metadata["ability_notes"] == task.metadata["ability_notes"] and len(calls) == 1


@pytest.mark.asyncio
async def test_notes_that_fail_leave_the_task_without_them_and_a_worker_without_the_option_is_unchanged(tmp_path):
    repos, audit = make_repos(tmp_path)

    async def broken(request: str) -> str:
        raise RuntimeError("index unreadable")

    task = repos.tasks.create("Anything.")
    failed = await TaskWorker(repos, audit, ability_notes=broken)._with_ability_notes(task)
    assert failed.metadata["ability_notes"] == ""
    plain = await TaskWorker(repos, audit)._with_ability_notes(task)
    assert "ability_notes" not in plain.metadata
