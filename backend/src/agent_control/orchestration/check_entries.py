"""Names of the pseudo-entries a check appends to `operator_history`, so the next decide() call can see why `done` was turned down.

They are observations, not work: `_tool_call_count()` leaves them out of the step budget. They live here, not in the worker, so the
modules that write them (answer_checks.py) and the worker that reads them do not import each other.
"""

from __future__ import annotations

CHECK_ENTRY_FULFILLMENT = "_fulfillment_check"
CHECK_ENTRY_AUDIT = "_audit_check"
CHECK_ENTRY_CLARIFICATION = "_clarification_check"
# Offered once, when the same call keeps returning the same result: answer with what there is instead of stopping.
CHECK_ENTRY_STUCK = "_stuck_check"
# A calculated number in the answer that no tool result produced (orchestration/number_check.py).
CHECK_ENTRY_NUMBER = "_number_check"
# What the task's own evidence says to do next (orchestration/ladder.py): a search that is settled, a script to write, a result
# that is not where the request said.
CHECK_ENTRY_STRATEGY = "_strategy_check"
CHECK_ENTRY_NAMES = frozenset(
    {CHECK_ENTRY_FULFILLMENT, CHECK_ENTRY_AUDIT, CHECK_ENTRY_CLARIFICATION, CHECK_ENTRY_STUCK, CHECK_ENTRY_NUMBER,
     CHECK_ENTRY_STRATEGY}
)
