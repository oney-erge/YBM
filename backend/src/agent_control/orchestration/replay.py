"""Replaying a saved workflow: the plan built from a finished task's history, and the next decision when it runs again."""
from __future__ import annotations

from typing import Any

from agent_control.orchestration.check_entries import CHECK_ENTRY_NAMES
from agent_control.orchestration.recovery import in_flight_is_ambiguous
from agent_control.schemas import (
    OperatorAction,
    OperatorDecision,
    RiskLevel,
)

# A replay step gets this many attempts before a lingering TIMEOUT/
# RATE_LIMITED result gives up and blocks instead of reissuing the same
# call forever. Small and separate from RetryPolicy.max_retries (which
# governs the automatic RETRYING backoff a live task also gets) - this
# caps how many times next_replay_decision itself will re-propose the
# identical step across those backoff cycles.
_MAX_REPLAY_STEP_ATTEMPTS = 3


def _replay_step_is_ambiguous(last: dict[str, Any] | None, tool_definitions: dict[str, Any]) -> bool:
    """Whether a replay step that just timed out is a consequential write
    (in_flight_is_ambiguous's own test, fed from the step's ToolDefinition
    instead of a live in-flight record - there is no live call here, only
    the history entry that recorded the timeout). A tool no longer in the
    registry (removed or disabled since the source task ran) is treated as
    high-risk by default - the same fail-safe direction
    in_flight_is_ambiguous already takes for an unrecognized capability.
    """
    tool_name = str((last or {}).get("tool_name") or "")
    step_input = (last or {}).get("input")
    step_input = step_input if isinstance(step_input, dict) else {}
    definition = tool_definitions.get(tool_name)
    if definition is None:
        return in_flight_is_ambiguous({"risk_level": "high", "capability": ""})
    return in_flight_is_ambiguous(
        {"risk_level": definition.required_risk(step_input).value, "capability": definition.capability_for(step_input).value}
    )


def next_replay_decision(
    replay_plan: list[dict[str, Any]],
    history: list[dict[str, Any]],
    metadata: dict[str, Any],
    tool_definitions: dict[str, Any],
) -> tuple[OperatorDecision, dict[str, Any]]:
    """The replay counterpart to OperatorLoopService.decide() (docs/ROADMAP.md
    "reusable verified workflows"): walks a recorded plan deterministically
    instead of asking an LLM what to do next. Returns the same
    OperatorDecision shape a live decide() call would, so every downstream
    branch in _process_operator_loop - approval, retry, verification,
    fulfillment, DONE grounding - runs completely unchanged regardless of
    which produced it; only this one call site differs.

    FAILED/DENIED (never auto-retried - RetryPolicy.RETRYABLE_STATUSES is
    only TIMEOUT/RATE_LIMITED) stop the replay immediately: something about
    the target changed since the source run, and silently trying the next
    step anyway would mean skipping over a step nobody confirmed happened.

    RATE_LIMITED is a clean pre-execution rejection - the live path's own
    AMBIGUOUS_COMPLETION_ERRORS distinction: nothing ran yet, so reissuing
    is always safe. TIMEOUT is not: the call may have reached the far end
    before the connection was lost. The governing rule (docs/ROADMAP.md
    "Finish the Proof"): never repeat an uncertain consequential action
    unless YBM can establish that repeating it is safe. "Consequential" is
    in_flight_is_ambiguous's own definition (reused, not re-derived, from
    the step's ToolDefinition - reconcile_orphaned_tasks applies the exact
    same test to a crashed worker's in-flight call). For a read or another
    low-risk call, reissuing is fine either way, so both statuses share the
    same bounded reissue-attempt logic. For a timed-out consequential write,
    establishing safety would mean asking the tool's own verify() hook - but
    every hook today (filesystem.manage's apply_manifest included) reads the
    *completed* call's reported output to decide what to re-check, and a
    timeout never produced one. Until a verifier exists that can also work
    from the request alone, "establish it's safe" has no answer here, so the
    honest move is to stop and ask a human rather than reissue on a guess.
    """
    real_entries = [entry for entry in history if entry.get("tool_name") not in CHECK_ENTRY_NAMES]
    completed = len(real_entries)
    last = real_entries[-1] if real_entries else None
    last_status = str(last.get("status") or "") if last else ""

    if last_status in {"failed", "denied"}:
        tool_name = str((last or {}).get("tool_name") or "a step")
        error = str((last or {}).get("error") or "it did not succeed")
        return (
            OperatorDecision(
                action=OperatorAction.BLOCKED,
                reason=f"Replay step {completed}/{len(replay_plan)} ({tool_name}) failed: {error}",
            ),
            metadata,
        )

    if last_status == "timeout" and _replay_step_is_ambiguous(last, tool_definitions):
        tool_name = str((last or {}).get("tool_name") or "a step")
        return (
            OperatorDecision(
                action=OperatorAction.BLOCKED,
                reason=(
                    f"Replay step {completed}/{len(replay_plan)} ({tool_name}) timed out. This is a "
                    "consequential write, so it may have already taken effect before the connection "
                    "was lost, and nothing can mechanically confirm that either way - refusing to "
                    "reissue it automatically. Check the target for this step before retrying."
                ),
            ),
            metadata,
        )

    if last_status in {"timeout", "rate_limited"}:
        attempts = int(metadata.get("replay_step_attempts", 0)) + 1
        if attempts > _MAX_REPLAY_STEP_ATTEMPTS:
            tool_name = str((last or {}).get("tool_name") or "a step")
            error = str((last or {}).get("error") or last_status)
            return (
                OperatorDecision(
                    action=OperatorAction.BLOCKED,
                    reason=(
                        f"Replay step {completed}/{len(replay_plan)} ({tool_name}) did not succeed "
                        f"after {attempts - 1} attempt(s): {error}"
                    ),
                ),
                metadata,
            )
        # completed still counts this step (it produced a history entry) -
        # replay_plan[completed - 1] is the step that just came back
        # timed-out/rate-limited, reissued rather than advanced past.
        step = replay_plan[completed - 1]
        return _replay_call_decision(step, completed, len(replay_plan)), {
            **metadata,
            "replay_step_attempts": attempts,
        }

    if completed >= len(replay_plan):
        # What the last step found is the point of running it again ("how many Steam games" -> the count); saying only that
        # the steps ran sent a person who asked for a saved workflow to the dashboard to see the result.
        found = str((last or {}).get("output_summary") or "").strip()
        answer = f"Replayed {len(replay_plan)}/{len(replay_plan)} step(s) successfully."
        if found:
            answer += f"\n\n{found[:1500]}"
        return OperatorDecision(action=OperatorAction.DONE, final_answer=answer), metadata

    step = replay_plan[completed]
    decision = _replay_call_decision(step, completed + 1, len(replay_plan))
    if metadata.get("replay_step_attempts"):
        return decision, {**metadata, "replay_step_attempts": 0}
    return decision, metadata


def build_replay_plan(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The ordered list of real, succeeded, single-tool calls from a
    completed task's operator_history (docs/ROADMAP.md "reusable verified
    workflows") - what admin.py's replay endpoint hands to a new task's
    replay_plan. Only succeeded entries: a step that failed, got retried,
    and then succeeded left more than one history entry behind for the
    same conceptual step, and only the one that actually worked should be
    reissued. risk_level is deliberately not carried over -
    effective_operator_risk always re-derives it from the tool's own
    definition regardless of what a decision (live or replayed) declares,
    the same runtime-owned-risk guarantee a live task gets.

    Excludes "delegate" and anything with a non-empty `origin` (a
    parallel-batch or subagent entry, per ToolCallRequest.origin's own
    values) - both represent something other than one plain CALL_TOOL step
    (a whole sub-task, or one member of a batch dispatched together), and
    next_replay_decision only ever reissues a single CALL_TOOL. Replaying
    those correctly would need its own handling, left out of this pass.
    """
    return [
        {"tool_name": entry["tool_name"], "tool_input": entry.get("input") or {}}
        for entry in history
        if entry.get("status") == "succeeded"
        and entry.get("tool_name") not in CHECK_ENTRY_NAMES
        and entry.get("tool_name") != "delegate"
        and not entry.get("origin")
    ]


def _replay_call_decision(step: dict[str, Any], step_number: int, total: int) -> OperatorDecision:
    return OperatorDecision(
        action=OperatorAction.CALL_TOOL,
        tool_name=str(step.get("tool_name") or ""),
        tool_input=dict(step.get("tool_input") or {}),
        risk_level=RiskLevel(str(step.get("risk_level") or "low")),
        reasoning=f"Replaying step {step_number}/{total} from the source task.",
    )
