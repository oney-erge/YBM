"""Doing more than one thing at once: independent calls in parallel, and a sub-task in its own inner loop."""
from __future__ import annotations

import asyncio
import json
from typing import Any
from uuid import uuid4

from agent_control.orchestration import ladder
from agent_control.orchestration.call_rewrites import (
    ALREADY_DONE_STATUS,
    _canonical_operator_tool_call,
)
from agent_control.orchestration.check_entries import (
    CHECK_ENTRY_STRATEGY,
)
from agent_control.orchestration.recovery import (
    in_flight_is_ambiguous,
    uncertain_consequential_result,
)
from agent_control.orchestration.repeats import (
    already_done_summary,
    succeeded_side_effect,
)
from agent_control.orchestration.task_record import (
    effective_operator_risk,
    is_background_external_tool_result,
    normalization_history_fields,
    tool_output_text,
)
from agent_control.schemas import (
    OperatorAction,
    OperatorDecision,
    ParallelToolCall,
    TaskRecord,
    ToolCallRequest,
    ToolResultStatus,
    new_id,
    utc_now,
)


class DelegationStage:
    def _mark_in_flight(
        self, task_id: str, request: ToolCallRequest, *, step_id: str, allow_awaiting_approval: bool = False
    ) -> tuple[TaskRecord, bool]:
        """Reserve this exact call unless a control or another call won first."""
        return self.repositories.tasks.admit_operator_call(task_id, {
                "tool_name": request.tool_name,
                "capability": request.capability.value,
                "risk_level": request.risk_level.value,
                "input": request.input,
                "step_id": step_id,
                "dispatched_at": utc_now().isoformat(),
        }, allow_awaiting_approval=allow_awaiting_approval)

    def _mark_parallel_in_flight(
        self, task_id: str, calls: list[ParallelToolCall], *, step_id: str,
        allow_awaiting_approval: bool = False,
    ) -> tuple[TaskRecord, bool]:
        """Reserve a batch and retain enough identity to review uncertain effects."""
        assert self.executor is not None
        children = []
        for call in calls:
            name, tool_input = _canonical_operator_tool_call(
                call.tool_name, call.tool_input, self.executor.tool_definitions
            )
            definition = self.executor.tool_definitions.get(name)
            children.append({
                "tool_name": name, "input": tool_input,
                "capability": definition.capability_for(tool_input).value if definition else "",
                "risk_level": effective_operator_risk(definition, tool_input, call.risk_level).value
                if definition else call.risk_level.value,
            })
        return self.repositories.tasks.admit_operator_call(task_id, {
            "tool_name": "call_tools_parallel", "risk_level": "low", "parallel_calls": children,
            "step_id": step_id, "dispatched_at": utc_now().isoformat(),
        }, allow_awaiting_approval=allow_awaiting_approval)

    async def _run_parallel_calls(
        self,
        task_id: str,
        calls: list[ParallelToolCall],
        *,
        origin_prefix: str = "",
        step_id: str,
        approvals_by_call: dict[tuple[str, str], str] | None = None,
    ) -> list[dict[str, Any]]:
        """Execute independent tool calls concurrently (docs/archive/HISTORY.md Part 3
        T1.1) and return one history-entry dict per call, same shape as a
        normal call_tool entry plus ``"parallel": True``.

        Every call in one batch shares a single ``parallel_batch:<id>``
        ``ToolCallRequest.origin`` (docs/archive/UI_REWRITE_PLAN.md §7/§9 Phase 0.6),
        so a trace UI can render them as siblings that ran at once instead of
        indistinguishable sequential calls. ``origin_prefix`` lets
        ``_run_delegate`` nest this correctly when a sub-task itself fans
        out - the batch's origin becomes ``subagent:<id>/parallel_batch:<id>``.

        Deliberately narrow, not a general CALL_TOOL replacement: skips the
        approval/retry/background-wait machinery entirely, because none of
        it generalizes cleanly to N calls at once (which one pauses the
        whole task for approval? what does "retry" mean when 2 of 5
        succeeded?). A call that needs approval or starts a background
        session fails with a message telling the model to reissue it alone
        via call_tool - a real, disclosed limitation. Intended for
        independent reads/lookups only; operator_system.md tells the model
        so directly.

        Safe to run concurrently against the same task's metadata: each
        call gets its own ToolCallRequest (its own id, so
        tool_invocations.create() is an independent insert per call, not a
        shared row), and _record_tool_result - a plain synchronous method
        with no ``await`` inside it - runs atomically with respect to the
        single-threaded asyncio event loop, so concurrent calls can't
        interleave a read and a write against each other. Which call's
        result ends up as last_tool_name/last_tool_output_text is simply
        whichever finishes last - there is no single well-defined "last"
        call when N ran at once, and that's an acceptable, disclosed
        property of a parallel batch, not a bug.
        """
        batch_origin = f"{origin_prefix}parallel_batch:{uuid4().hex[:12]}"
        task = self.repositories.tasks.get(task_id)
        prior_history = list((task.metadata.get("operator_history") or []) if task is not None else [])

        async def _execute_one(call: ParallelToolCall) -> dict[str, Any]:
            tool_name, tool_input = _canonical_operator_tool_call(
                call.tool_name,
                call.tool_input,
                self.executor.tool_definitions,
            )
            normalization = normalization_history_fields(
                call.tool_name, call.tool_input, tool_name, tool_input
            )
            earlier = succeeded_side_effect(prior_history, tool_name, tool_input)
            if earlier is not None:
                return {
                    "tool_name": tool_name, "input": tool_input,
                    "status": ALREADY_DONE_STATUS, "output_summary": already_done_summary(*earlier),
                    "parallel": True, "origin": batch_origin, "step_id": step_id,
                    **normalization,
                }
            tool_def = self.executor.tool_definitions.get(tool_name)
            if tool_def is None:
                return {
                    "tool_name": tool_name, "input": tool_input,
                    "status": "failed", "error": f"unregistered tool: {tool_name}",
                    "parallel": True, "origin": batch_origin, "step_id": step_id,
                    **normalization,
                }
            request = ToolCallRequest(
                task_id=task_id, tool_name=tool_name, capability=tool_def.capability_for(tool_input),
                risk_level=effective_operator_risk(tool_def, tool_input, call.risk_level),
                input=tool_input, origin=batch_origin, parent_step_id=step_id,
            )
            # On a resumed batch each call carries the approval minted for it,
            # keyed by exactly what was shown to the human.
            # Keyed by the call as it was bound when the person approved it (see _batch_requests).
            approval_id = (approvals_by_call or {}).get(
                (tool_name, json.dumps(self.executor.normalized_request(request).input, sort_keys=True))
            )
            result = await self.executor.execute(request, approval_id=approval_id)
            if result.status == ToolResultStatus.NEEDS_APPROVAL:
                return {
                    "tool_name": tool_name, "input": tool_input,
                    "status": "failed",
                    "error": (
                        "this call still needs approval after the batch was decided - "
                        "it was not one of the calls the batch listed"
                    ),
                    "parallel": True, "origin": batch_origin, "step_id": step_id,
                    **normalization,
                }
            if is_background_external_tool_result(tool_name, result):
                return {
                    "tool_name": tool_name, "input": tool_input,
                    "status": "running", "outcome_uncertain": True,
                    "session_id": str(result.output.get("session_id") or ""),
                    "error": (
                        "this call started a background session, which call_tools_parallel does not "
                        "monitor - check the session and ensure it has stopped before repeating"
                    ),
                    "parallel": True, "origin": batch_origin, "step_id": step_id,
                    "request_id": result.request_id,
                    **normalization,
                }
            self._record_tool_result(task_id, tool_name, result)
            output_text = tool_output_text(result) if result.status == ToolResultStatus.SUCCEEDED else None
            entry = {
                "tool_name": tool_name, "input": tool_input,
                "status": result.status.value,
                "output_summary": output_text[:2000] if output_text else None,
                "error": result.error_message,
                "parallel": True, "origin": batch_origin,
                "request_id": result.request_id,
                "step_id": step_id,
                **normalization,
            }
            if uncertain_consequential_result(request, result):
                entry["outcome_uncertain"] = True
            return entry

        in_batch: dict[tuple[str, str], asyncio.Task[dict[str, Any]]] = {}

        async def _one(call: ParallelToolCall) -> dict[str, Any]:
            name, tool_input = _canonical_operator_tool_call(
                call.tool_name, call.tool_input, self.executor.tool_definitions
            )
            definition = self.executor.tool_definitions.get(name)
            if definition is None:
                return await _execute_one(call)
            risk = effective_operator_risk(definition, tool_input, call.risk_level)
            capability = definition.capability_for(tool_input)
            if not in_flight_is_ambiguous({"risk_level": risk.value, "capability": capability.value}):
                return await _execute_one(call)
            # The executor validates inputs and fills schema defaults before
            # dispatch. Compare that same effective input so two spellings of
            # one consequential call do not run the effect twice.
            request = ToolCallRequest(
                task_id=task_id, tool_name=name, capability=capability,
                risk_level=risk, input=tool_input, parent_step_id=step_id,
            )
            effective_input = self.executor.normalized_request(request).input
            key = (name, json.dumps(effective_input, sort_keys=True, default=str))
            pending = in_batch.get(key)
            if pending is None:
                pending = asyncio.create_task(_execute_one(call))
                in_batch[key] = pending
                return await pending
            original = await asyncio.shield(pending)
            duplicate = {**original, "duplicate_in_batch": True}
            if original.get("status") == ToolResultStatus.SUCCEEDED.value:
                duplicate["status"] = ALREADY_DONE_STATUS
                duplicate["output_summary"] = "The identical call in this batch already succeeded; it was not dispatched again."
            return duplicate

        return list(await asyncio.gather(*[_one(call) for call in calls]))

    async def _run_delegate(
        self, task: TaskRecord, decision: OperatorDecision, *, step_id: str
    ) -> tuple[TaskRecord, dict[str, Any]]:
        """Run a delegated sub-task in an isolated context (docs/archive/HISTORY.md
        Part 3 T1.2): its own operator loop, its own history starting from
        nothing, its own fixed step budget - only a compact summary crosses
        back into the parent's history. That's the entire value: a
        long/exploratory sub-task doesn't bloat the parent's context the way
        inlining the same steps via plain call_tool would.

        The parent task's own metadata (last_tool_name, last_tool_output_text,
        etc.) IS updated by the sub-task's tool calls, deliberately, not as a
        leak: it's what lets the audit gate ground a `done` that immediately
        follows a delegate step in the sub-agent's real last tool output
        (see CONTENT_TOOLS in orchestration/auditor.py, which lists
        "delegate" for exactly this). Only the step-by-step history stays
        isolated - the whole point of the isolation is a smaller prompt for
        the parent's next decide() call, not hiding what happened.

        Deliberately narrow, same reasoning as _run_parallel_calls: a
        sub-task cannot pause for approval, wait on a background session,
        ask the user, or delegate further - each needs task-level state a
        synchronous, in-process sub-loop doesn't have. Hitting one of those
        fails the sub-task with a clear reason instead of hanging; the
        parent sees the reason in its own history and can handle that step
        directly with a normal call_tool instead of delegating it.

        `step_id` is the parent tick's step - stamped onto the single
        "delegate" summary entry returned to the parent's history. Each
        sub-decision inside the loop below is its own step, with its own
        freshly generated step_id, the same way the parent's own tick loop
        works (docs/archive/UI_UX_AUDIT.md Phase 14e).
        """
        delegate_origin = f"subagent:{uuid4().hex[:12]}"
        objective = decision.delegate_objective or ""
        allowed_tools = (
            {
                _canonical_operator_tool_call(name, {}, self.executor.tool_definitions)[0]
                for name in decision.delegate_tools
            }
            if decision.delegate_tools
            else None
        )
        extra_context = self._project_planner_context(task.metadata)
        if allowed_tools:
            extra_context += (
                "\n\nFor this delegated sub-task, you may ONLY use these tools: "
                f"{', '.join(sorted(allowed_tools))}. Any other tool_name will be refused."
            )
        # What a part returns is quoted by the parent, so it must be what the tools said: a title shortened to "Python.org" from
        # "Welcome to Python.org" became the answer to a request for the exact title.
        extra_context += (
            "\n\nWhen your answer includes a title, name, number or other value that a tool returned, copy it exactly as the tool "
            "gave it. Do not shorten, reword or tidy it."
        )
        summary_input = {"objective": objective, "delegate_tools": decision.delegate_tools}
        sub_history: list[dict[str, Any]] = []

        def interrupted(current: TaskRecord) -> tuple[TaskRecord, dict[str, Any]]:
            return current, {
                "tool_name": "delegate", "input": summary_input, "status": "interrupted",
                "error": "delegated work stopped before another call; review any completed work before repeating",
                "origin": delegate_origin, "step_id": step_id,
            }

        for _step in range(self.DELEGATE_MAX_STEPS):
            sub_step_id = new_id("step")
            try:
                sub_decision = await self.operator.decide(
                    objective, self._planner_context(extra_context), sub_history, memory_context=""
                )
            except Exception as exc:
                return task, {
                    "tool_name": "delegate", "input": summary_input, "status": "failed",
                    "output_summary": None, "error": f"sub-task decide() failed: {exc}",
                    "origin": delegate_origin, "step_id": step_id,
                }
            task = self._record_llm_usage(
                task, "subagent", getattr(self.operator, "last_usage", None),
                fallback_used=getattr(self.operator, "last_fallback_used", False),
            )
            self._record_llm_call(task.id, "subagent", len(sub_history), self.operator, step_id=sub_step_id)

            if sub_decision.action == OperatorAction.DONE:
                invented = ladder.ungrounded_quotes(sub_decision.final_answer, objective, sub_history)
                if invented and not any(isinstance(e, dict) and e.get("hint_id") == "quotes" for e in sub_history):
                    # One more look, inside the part: a title or name in quotation marks has to be what a tool returned.
                    sub_history.append({
                        "tool_name": CHECK_ENTRY_STRATEGY, "input": None, "status": "strategy", "hint_id": "quotes",
                        "error": ladder.UNGROUNDED_QUOTES_TEXT.format(quotes=", ".join(f'"{q}"' for q in invented[:3])),
                        "step_id": sub_step_id,
                    })
                    continue
                return task, {
                    "tool_name": "delegate", "input": summary_input, "status": "succeeded",
                    "output_summary": (sub_decision.final_answer or "")[:2000], "error": None,
                    "origin": delegate_origin, "step_id": step_id,
                }
            if sub_decision.action == OperatorAction.BLOCKED:
                return task, {
                    "tool_name": "delegate", "input": summary_input, "status": "failed",
                    "output_summary": None,
                    "error": f"sub-task blocked: {sub_decision.reason or 'no reason given'}",
                    "origin": delegate_origin, "step_id": step_id,
                }
            if sub_decision.action == OperatorAction.ASK_USER:
                return task, {
                    "tool_name": "delegate", "input": summary_input, "status": "failed",
                    "output_summary": None,
                    "error": (
                        "sub-task needs user input, which delegation does not support: "
                        f"{sub_decision.question or ''}"
                    ),
                    "origin": delegate_origin, "step_id": step_id,
                }
            if sub_decision.action == OperatorAction.DELEGATE:
                sub_history.append({
                    "tool_name": "delegate", "input": None, "status": "failed",
                    "error": "delegation is not available inside a delegated sub-task",
                    "step_id": sub_step_id,
                })
                continue
            if sub_decision.action == OperatorAction.CALL_TOOLS_PARALLEL:
                calls = sub_decision.parallel_calls
                if allowed_tools and any(
                    _canonical_operator_tool_call(call.tool_name, call.tool_input, self.executor.tool_definitions)[0]
                    not in allowed_tools
                    for call in calls
                ):
                    sub_history.append({
                        "tool_name": "call_tools_parallel", "input": None, "status": "failed",
                        "error": f"one or more tools are not in this sub-task's allowed set: {sorted(allowed_tools)}",
                        "step_id": sub_step_id,
                    })
                    continue
                current, permitted = self.repositories.tasks.confirm_reserved_dispatch(task.id, step_id)
                if not permitted:
                    return interrupted(current)
                entries = await self._run_parallel_calls(
                    task.id, calls, origin_prefix=f"{delegate_origin}/", step_id=sub_step_id
                )
                sub_history.extend(entries)
                uncertain = [entry for entry in entries if entry.get("outcome_uncertain")]
                if uncertain:
                    current = self.repositories.tasks.get(task.id) or current
                    current, report = interrupted(current)
                    report["uncertain_calls"] = uncertain
                    report["interrupted_batch_results"] = entries
                    return current, report
                continue

            # CALL_TOOL
            requested_tool_name = sub_decision.tool_name
            requested_tool_input = sub_decision.tool_input
            tool_name, tool_input = _canonical_operator_tool_call(
                requested_tool_name,
                requested_tool_input,
                self.executor.tool_definitions,
            )
            normalization = normalization_history_fields(
                requested_tool_name, requested_tool_input, tool_name, tool_input
            )
            if allowed_tools and tool_name not in allowed_tools:
                sub_history.append({
                    "tool_name": tool_name, "input": tool_input, "status": "failed",
                    "error": f"tool not in this sub-task's allowed set: {sorted(allowed_tools)}",
                    "step_id": sub_step_id,
                    **normalization,
                })
                continue
            tool_def = self.executor.tool_definitions.get(tool_name)
            if tool_def is None:
                sub_history.append({
                    "tool_name": tool_name, "input": tool_input, "status": "failed",
                    "error": f"unregistered tool: {tool_name}",
                    "step_id": sub_step_id,
                    **normalization,
                })
                continue
            request = ToolCallRequest(
                task_id=task.id, tool_name=tool_name, capability=tool_def.capability_for(tool_input),
                risk_level=effective_operator_risk(tool_def, tool_input, sub_decision.risk_level),
                input=tool_input, origin=delegate_origin, parent_step_id=sub_step_id,
            )
            current, permitted = self.repositories.tasks.confirm_reserved_dispatch(task.id, step_id)
            if not permitted:
                return interrupted(current)
            result = await self.executor.execute(request)
            if result.status == ToolResultStatus.NEEDS_APPROVAL:
                sub_history.append({
                    "tool_name": tool_name, "input": tool_input, "status": "failed",
                    "error": "this call needs approval, which delegation does not support",
                    "step_id": sub_step_id,
                    **normalization,
                })
                continue
            if is_background_external_tool_result(tool_name, result):
                current, report = interrupted(task)
                report["uncertain_calls"] = [{
                    "tool_name": tool_name, "input": tool_input, "status": "running",
                    "session_id": str(result.output.get("session_id") or ""),
                    "request_id": result.request_id, "step_id": sub_step_id,
                    **normalization,
                }]
                return current, report
            task = self._record_tool_result(task.id, tool_name, result)
            if uncertain_consequential_result(request, result):
                current, report = interrupted(task)
                report["uncertain_calls"] = [{
                    "tool_name": tool_name, "input": tool_input, "status": result.status.value,
                    "request_id": result.request_id, "step_id": sub_step_id,
                }]
                return current, report
            output_text = tool_output_text(result) if result.status == ToolResultStatus.SUCCEEDED else None
            sub_history.append({
                "tool_name": tool_name, "input": tool_input,
                "status": result.status.value,
                "output_summary": output_text[:2000] if output_text else None,
                "error": result.error_message,
                "step_id": sub_step_id,
                **normalization,
            })

        return task, {
            "tool_name": "delegate", "input": summary_input, "status": "failed",
            "output_summary": None,
            "error": f"sub-task step budget ({self.DELEGATE_MAX_STEPS}) exhausted without finishing",
            "origin": delegate_origin, "step_id": step_id,
        }
