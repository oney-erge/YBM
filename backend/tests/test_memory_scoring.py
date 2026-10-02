"""score_facts (docs/archive/UI_UX_AUDIT.md Phase 15) - deterministic relevance
selection replacing "inject every fact into every task," which is fine at
five facts and actively harmful at a thousand.
"""

from __future__ import annotations

from datetime import timedelta

from agent_control.channels.memory import score_facts
from agent_control.schemas import MemoryFact, MemorySource, utc_now


def _fact(content: str, *, category: str = "general", task_id: str | None = "task_1", age_days: float = 0) -> MemoryFact:
    now = utc_now()
    return MemoryFact(
        category=category,
        content=content,
        source=MemorySource.TASK_DERIVED,
        task_id=task_id,
        created_at=now - timedelta(days=age_days),
        updated_at=now - timedelta(days=age_days),
    )


def test_facts_with_no_task_id_are_always_included_unscored():
    """A fact with no task_id is a durable, global preference (set via the
    Memory page or a "remember that ..." message), not something tied to
    one task's now-irrelevant context - it must survive regardless of the
    objective, the same way Phase 4's untrimmed-facts guarantee already
    works for these."""
    pinned = _fact("Always use metric units", task_id=None)
    irrelevant_scoped = _fact("The invoice total was $250", task_id="task_1")

    result = score_facts([pinned, irrelevant_scoped], "completely unrelated objective about weather", limit=0)

    assert pinned in result
    assert irrelevant_scoped not in result


def test_scoped_facts_are_ranked_by_relevance_to_the_objective():
    relevant = _fact("Prefers Python over Java for scripting", category="preference")
    irrelevant = _fact("The invoice total was $250", category="finance")

    result = score_facts([irrelevant, relevant], "write me a Python script to rename files")

    assert result[0] is relevant


def test_category_match_boosts_relevance():
    matching_category = _fact("Something unrelated in content", category="coding_style")
    no_category_match = _fact("Also unrelated content", category="misc")

    result = score_facts([no_category_match, matching_category], "what is my coding_style preference?")

    assert result[0] is matching_category


def test_more_recent_facts_rank_higher_when_otherwise_tied():
    old = _fact("Some old fact about nothing objective-relevant", age_days=200)
    recent = _fact("Some old fact about nothing objective-relevant", age_days=0)

    result = score_facts([old, recent], "totally unrelated objective")

    assert result[0] is recent


def test_limit_caps_scoped_facts_but_not_pinned_ones():
    pinned = [_fact(f"pinned {i}", task_id=None) for i in range(3)]
    scoped = [_fact(f"scoped {i}", task_id="task_1") for i in range(10)]

    result = score_facts(pinned + scoped, "objective", limit=2)

    assert len(result) == 3 + 2
    assert all(fact in result for fact in pinned)


def test_empty_fact_list_returns_empty():
    assert score_facts([], "any objective") == []
