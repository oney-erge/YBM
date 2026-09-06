from __future__ import annotations

import pytest

from agent_control.orchestration.workflows import (
    instantiate_plan,
    parameterize_plan,
    replay_plan_gaps,
)


# ---- replay_plan_gaps -------------------------------------------------

def test_replay_plan_gaps_is_empty_for_a_fully_capturable_history() -> None:
    history = [
        {"tool_name": "filesystem.manage", "status": "succeeded", "input": {"path": "a"}},
        {"tool_name": "filesystem.manage", "status": "succeeded", "input": {"path": "b"}},
    ]

    assert replay_plan_gaps(history) == []


def test_replay_plan_gaps_names_a_delegated_sub_task() -> None:
    history = [
        {"tool_name": "filesystem.manage", "status": "succeeded", "input": {"path": "a"}},
        {"tool_name": "delegate", "status": "succeeded", "input": {"objective": "find the invoice total"}},
    ]

    gaps = replay_plan_gaps(history)

    assert len(gaps) == 1
    assert "find the invoice total" in gaps[0]


def test_replay_plan_gaps_names_a_parallel_batch_member() -> None:
    history = [
        {"tool_name": "http.request", "status": "succeeded", "input": {}, "origin": "batch_1"},
    ]

    gaps = replay_plan_gaps(history)

    assert len(gaps) == 1
    assert "http.request" in gaps[0]
    assert "parallel batch" in gaps[0]


def test_replay_plan_gaps_ignores_failed_and_pseudo_check_entries() -> None:
    history = [
        {"tool_name": "delegate", "status": "failed", "input": {"objective": "should not count"}},
        {"tool_name": "_fulfillment_check", "status": "succeeded", "origin": "batch_1"},
    ]

    assert replay_plan_gaps(history) == []


# ---- parameterize_plan / instantiate_plan are inverses -----------------

def test_parameterize_plan_replaces_matching_literal_values() -> None:
    plan = [
        {"tool_name": "filesystem.manage", "tool_input": {"operation": "inspect_folder", "root": "C:/Users/sam/Downloads"}},
        {"tool_name": "filesystem.manage", "tool_input": {"operation": "apply_manifest", "manifest": [
            {"source": "C:/Users/sam/Downloads/a.pdf", "destination": "C:/Users/sam/Documents/a.pdf"},
        ]}},
    ]

    parameterized, parameters = parameterize_plan(
        plan, {"C:/Users/sam/Downloads": "folder", "C:/Users/sam/Documents": "destination"}
    )

    assert parameters == ["folder"]  # "destination" never matched a value present verbatim in the plan
    assert parameterized[0]["tool_input"]["root"] == "{{folder}}"
    # A substring match inside a longer path must not fire - only an exact
    # value match is replaced, so "C:/Users/sam/Downloads/a.pdf" (the
    # source) stays untouched even though it starts with the folder value.
    assert parameterized[1]["tool_input"]["manifest"][0]["source"] == "C:/Users/sam/Downloads/a.pdf"


def test_parameterize_plan_reports_only_parameters_actually_used() -> None:
    plan = [{"tool_name": "web.search", "tool_input": {"query": "ferrets"}}]

    _parameterized, parameters = parameterize_plan(plan, {"never present": "unused"})

    assert parameters == []


def test_instantiate_plan_substitutes_every_placeholder() -> None:
    template = [
        {"tool_name": "filesystem.manage", "tool_input": {"operation": "inspect_folder", "root": "{{folder}}"}},
    ]

    plan = instantiate_plan(template, {"folder": "C:/Users/sam/Desktop"})

    assert plan == [
        {"tool_name": "filesystem.manage", "tool_input": {"operation": "inspect_folder", "root": "C:/Users/sam/Desktop"}}
    ]


def test_instantiate_plan_raises_naming_every_missing_parameter() -> None:
    template = [
        {"tool_name": "filesystem.manage", "tool_input": {"root": "{{folder}}", "pattern": "{{pattern}}"}},
    ]

    with pytest.raises(ValueError, match="folder"):
        instantiate_plan(template, {})


def test_instantiate_plan_raises_all_missing_names_at_once() -> None:
    template = [
        {"tool_name": "filesystem.manage", "tool_input": {"root": "{{folder}}", "pattern": "{{pattern}}"}},
    ]

    try:
        instantiate_plan(template, {})
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "folder" in str(exc)
        assert "pattern" in str(exc)


def test_instantiate_plan_leaves_non_placeholder_text_untouched() -> None:
    """A field that happens to contain literal curly braces but isn't
    exactly `{{name}}` must not be mistaken for a placeholder."""
    template = [{"tool_name": "document.manage", "tool_input": {"content": "Use {{curly}} braces in the doc."}}]

    plan = instantiate_plan(template, {})

    assert plan[0]["tool_input"]["content"] == "Use {{curly}} braces in the doc."


def test_parameterize_then_instantiate_round_trips_to_a_different_value() -> None:
    original_plan = [
        {"tool_name": "filesystem.manage", "tool_input": {"operation": "inspect_folder", "root": "C:/Users/sam/Downloads"}},
    ]

    template, parameters = parameterize_plan(original_plan, {"C:/Users/sam/Downloads": "folder"})
    assert parameters == ["folder"]

    rerun_plan = instantiate_plan(template, {"folder": "C:/Users/sam/Desktop"})

    assert rerun_plan[0]["tool_input"]["root"] == "C:/Users/sam/Desktop"
