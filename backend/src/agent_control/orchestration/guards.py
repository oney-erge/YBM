"""What stands between the model's decision and the world: refusals, the ladder of next steps, the offers to finish or to hand over."""
from __future__ import annotations

import shutil
from typing import Any

from agent_control.llm.cloud_gate import CloudFallbackNeeded, is_yes
from agent_control.orchestration import call_policy, ladder
from agent_control.orchestration.asking import users_own_words
from agent_control.orchestration.check_entries import (
    CHECK_ENTRY_NAMES,
    CHECK_ENTRY_STRATEGY,
    CHECK_ENTRY_STUCK,
)
from agent_control.orchestration.finishing import operator_transition_error_message
from agent_control.orchestration.fulfillment import (
    validate_fulfillment,
)
from agent_control.orchestration.number_check import (
    inconsistent_sum,
    ungrounded_addresses,
    ungrounded_numbers,
)
from agent_control.orchestration.repeats import repeated_no_progress_call
from agent_control.orchestration.task_record import claude_code_decision
from agent_control.schemas import (
    AuditEventType,
    OperatorDecision,
    TaskRecord,
    TaskStatus,
    ToolResultStatus,
)


class GuardStage:
    def _transition_operator(
        self, task: TaskRecord, metadata: dict[str, Any], status: TaskStatus, reason: str
    ) -> TaskRecord:
        """Terminal-state transition for the operator loop only.

        Deliberately does NOT call _transition(): that method's COMPLETED path
        runs fulfillment-gap recovery which can attach a plan-based recovery
        plan, crossing back into the plan-once path from inside this one.
        """
        if status in {TaskStatus.FAILED, TaskStatus.BLOCKED}:
            metadata = {**metadata}
            metadata.setdefault("last_worker_error", operator_transition_error_message(reason, self.operator_max_steps))
        updated = self.repositories.tasks.update_metadata(task.id, metadata, status)
        self.audit.task_state_changed("worker", task.id, task.status, updated.status)
        if status in {TaskStatus.FAILED, TaskStatus.BLOCKED}:
            self.audit.append(
                AuditEventType.ERROR, actor="worker", task_id=task.id,
                payload={"error": reason, "status": status.value},
            )
        return updated

    def _answer_is_grounded_though_unconfirmed(
        self, task: TaskRecord, history: list[dict[str, Any]], answer: str | None, memory_context: str
    ) -> bool:
        """Whether an answer the Auditor would not confirm at least invents no address and no calculated number.

        Those are the two kinds of made-up value a script can check, so they stand in for the Auditor's second opinion once it
        has been asked twice. Off with ``operator.verify_numbers``, which owns the same checks.
        """
        if not self.verify_numbers or not answer or not answer.strip():
            return False
        request = " ".join(str(value or "") for value in (task.metadata.get("original_message_text"), task.objective))
        if ungrounded_addresses(request, answer, history, memory_context):
            return False
        return not ungrounded_numbers(request, answer, history)

    def _values_no_tool_produced(
        self, task: TaskRecord, history: list[dict[str, Any]], answer: str | None, copied_from_results: bool
    ) -> tuple[list[str], list[str]]:
        """Addresses, and numbers of a calculation, in the answer that nothing the task read or ran produced.

        Once per task. Numbers are only checked when code can be run to calculate them; addresses never need a tool.
        """
        if not self.verify_numbers or copied_from_results or not answer:
            return [], []
        if int(task.metadata.get("operator_number_gap_count", 0)) >= 1:
            return [], []
        request = " ".join(str(value or "") for value in (task.metadata.get("original_message_text"), task.objective))
        addresses = ungrounded_addresses(request, answer, history, str(task.metadata.get("memory_context") or ""))
        numbers: list[str] = []
        interpreter = self.executor.tool_definitions.get("code.interpreter")
        if interpreter is not None and interpreter.enabled:
            numbers = ungrounded_numbers(request, answer, history)
        return addresses, numbers

    def _audit_rewrite_adds_no_wrong_value(
        self, task: TaskRecord, history: list[dict[str, Any]], original: str | None, reworded: str | None
    ) -> bool:
        """The Auditor may reword an answer; it may not make a value in it wrong.

        The checks of answer_checks.py run on the Operator's answer, before the Auditor reads it. In a live run the model's answer was
        corrected to "15 lines (4 + 5 + 6)" and the Auditor's wording of it was "5 lines in total (4 + 5 + 6)", which nothing looked at. A
        reworded answer with a sum that does not add up, or an address or a calculated number that no tool produced, that the original
        did not have, is dropped, and the original goes on.
        """
        if not reworded or not original:
            return True
        if inconsistent_sum(reworded) and not inconsistent_sum(original):
            return False
        if not self.verify_numbers:
            return True
        request = " ".join(str(value or "") for value in (task.metadata.get("original_message_text"), task.objective))
        memory = str(task.metadata.get("memory_context") or "")
        if set(ungrounded_addresses(request, reworded, history, memory)) - set(ungrounded_addresses(request, original, history, memory)):
            return False
        interpreter = self.executor.tool_definitions.get("code.interpreter")
        if interpreter is not None and interpreter.enabled:
            return not (set(ungrounded_numbers(request, reworded, history)) - set(ungrounded_numbers(request, original, history)))
        return True

    def _offer_finish_or_block(
        self, task: TaskRecord, history: list[dict[str, Any]], *, called: str, blocked_message: str
    ) -> TaskRecord:
        """The same call keeps returning the same result. Once, tell the model to answer with what there is; then stop.

        Stopping at once left the user with "Blocked" although the model often had what the answer needed (a file it had
        read, a search that found nothing), or could say plainly that it found nothing. The first time, a check entry
        tells it so and, when the Operator's reply is held to a schema, the next reply can only finish or ask. If it
        still calls tools and gets the same result, the task stops as before.
        """
        if any(entry.get("tool_name") == CHECK_ENTRY_STUCK for entry in history if isinstance(entry, dict)):
            if task.metadata.get("claude_code_offer") is None and self._claude_code_ready():
                return self._offer_claude_code(task, history)
            return self._transition_operator(
                task,
                {**task.metadata, "operator_history": history, "last_worker_error": blocked_message},
                TaskStatus.BLOCKED,
                "operator_repeated_no_progress",
            )
        return self._offer_finish(task, history, called=called)

    def _offer_finish(self, task: TaskRecord, history: list[dict[str, Any]], *, called: str) -> TaskRecord:
        history.append({
            "tool_name": CHECK_ENTRY_STUCK,
            "input": None,
            "status": "stuck",
            "error": (
                f"{called} has returned the same result several times, so calling it again will not help. Answer now with "
                "what you have: say plainly what you found and what you could not do (action done), or ask the user one "
                "question (action ask_user). Do not call that tool again."
            ),
        })
        return self.repositories.tasks.update_metadata(task.id, {**task.metadata, "operator_history": history})

    def _refuse_simulated_delete(
        self, task: TaskRecord, history: list[dict[str, Any]], tool_name: str | None, tool_input: dict[str, Any], step_id: str
    ) -> TaskRecord:
        """Not run: the request is to delete files, there is no delete operation, and a move or rename is not a delete."""
        history.append({
            "tool_name": tool_name, "input": tool_input, "status": ToolResultStatus.DENIED.value,
            "error": (
                "Not run: this request is to delete files, YBM has no delete operation, and moving or renaming files to look "
                "like a deletion changes them without deleting them. Tell the user plainly that you cannot delete files, and "
                "offer to move them into a folder they name instead (action done, or action ask_user for that folder)."
            ),
            "step_id": step_id,
        })
        return self.repositories.tasks.update_metadata(task.id, {**task.metadata, "operator_history": history})

    def _refuse_call(self, task: TaskRecord, history: list[dict[str, Any]], vetted: call_policy.VettedCall, step_id: str) -> TaskRecord:
        """Record why a call was not run (call_policy.vet_call decided; this writes it down, because it owns the repositories)."""
        refusal = vetted.refusal
        assert refusal is not None
        if refusal.kind == "ungrounded_path":
            return self._refuse_ungrounded_path(
                task, history, vetted.requested_name, vetted.requested_input, list(refusal.paths), step_id,
            )
        if refusal.kind == "unrequested_coding_agent":
            return self._refuse_unrequested_coding_agent(task, history, vetted.tool_input, step_id)
        if refusal.kind == "simulated_delete":
            return self._refuse_simulated_delete(task, history, vetted.tool_name, vetted.tool_input, step_id)
        return self._refuse_blind_persona_replace(task, history, vetted.tool_input, step_id)

    def _refuse_ungrounded_path(
        self, task: TaskRecord, history: list[dict[str, Any]], tool_name: str | None, tool_input: dict[str, Any],
        paths: list[str], step_id: str,
    ) -> TaskRecord:
        """Not run, once per path: it came from an earlier turn, not from this request or anything this task found."""
        history.append({
            "tool_name": tool_name, "input": tool_input, "status": ToolResultStatus.DENIED.value,
            "error": ladder.UNGROUNDED_TEXT.format(paths=", ".join(paths[:2])), "step_id": step_id,
        })
        seen = [*(task.metadata.get("ungrounded_paths_seen") or []), *paths]
        return self.repositories.tasks.update_metadata(
            task.id, {**task.metadata, "operator_history": history, "ungrounded_paths_seen": seen},
        )

    def _refuse_blind_persona_replace(
        self, task: TaskRecord, history: list[dict[str, Any]], tool_input: dict[str, Any], step_id: str
    ) -> TaskRecord:
        """Not run: persona.manage update replaces the whole document, and this task has not read what is in it."""
        history.append({
            "tool_name": "persona.manage", "input": tool_input, "status": ToolResultStatus.DENIED.value,
            "error": (
                "Not run: persona.manage update replaces the whole persona document, and you have not read it in this task. "
                "To note a fact about the user (a name, a pet, a number, something they like), use memory.manage with operation "
                "remember: that adds to the facts YBM keeps and shows when the user asks what it knows. The persona is only for how "
                "YBM should talk and behave; to change it, read it first (persona.manage get) and send back the whole text with "
                "your change."
            ),
            "step_id": step_id,
        })
        return self.repositories.tasks.update_metadata(task.id, {**task.metadata, "operator_history": history})

    def _refuse_unrequested_coding_agent(
        self, task: TaskRecord, history: list[dict[str, Any]], tool_input: dict[str, Any], step_id: str
    ) -> TaskRecord:
        """Codex, Claude Code and Copilot run only when the user asked for one; the model chose it alone here."""
        history.append({
            "tool_name": "coding.agent", "input": tool_input, "status": ToolResultStatus.DENIED.value,
            "error": (
                "Not started: Codex, Claude Code and Copilot run only when the user asks for one. "
                "Do the work with the local tools, or ask the user whether they want a coding agent."
            ),
            "step_id": step_id,
        })
        if repeated_no_progress_call(history) is not None:
            return self._transition_operator(
                task,
                {
                    **task.metadata, "operator_history": history,
                    "last_worker_error": "Stopped: a coding agent was not requested, and the model kept asking to start one.",
                },
                TaskStatus.BLOCKED,
                "operator_repeated_no_progress",
            )
        return self.repositories.tasks.update_metadata(task.id, {**task.metadata, "operator_history": history})

    def _finish_with_read_result(
        self, task: TaskRecord, history: list[dict[str, Any]], entry: dict[str, Any]
    ) -> TaskRecord | None:
        """The model asked for the same read a third time: answer with what it read instead of stopping the task.

        Only for a request that needs nothing more (no file to write, nothing to send); otherwise the caller stops it.
        """
        candidate = task.model_copy(update={"metadata": {**task.metadata, "operator_history": history}})
        validation = validate_fulfillment(candidate)
        if validation.expected and not validation.ok:
            return None
        answer = " ".join(str(entry.get("output_summary") or "").split())[:1500]
        if not answer:
            return None
        metadata = {**task.metadata, "operator_history": history, "synthesized_answer": answer}
        return self._transition_operator(task, metadata, TaskStatus.COMPLETED, "operator_redundant_read_finished")

    def _claude_code_ready(self) -> bool:
        """Claude Code can be offered: its tool is on and the command is installed on this computer."""
        if self.executor is None:
            return False
        definition = self.executor.tool_definitions.get("coding.agent")
        return bool(definition is not None and definition.enabled and shutil.which("claude"))

    def _ladder_step(self, task: TaskRecord, history: list[dict[str, Any]], step_id: str) -> TaskRecord | OperatorDecision | None:
        """Before each model decision, act on what the task's own history proves (orchestration/ladder.py).

        Appends at most one check entry to `history`, which the caller persists with the decision's own entry. Returns a task
        when this tick is spent on something else (asking the user, or taking their answer), a decision when the user has said
        yes to handing the task to Claude Code, and None otherwise.
        """
        offer = task.metadata.get("claude_code_offer")
        if isinstance(offer, dict):
            if offer.get("state") == "pending" and task.metadata.get("answered_clarifying_question") == offer.get("question"):
                accepted = is_yes(task.metadata.get("clarification_answer"))
                task = self.repositories.tasks.update_metadata(
                    task.id, {**task.metadata, "claude_code_offer": {**offer, "state": "accepted" if accepted else "declined"}}
                )
                self.audit.append(
                    AuditEventType.TASK_STATE_CHANGED, actor="user", task_id=task.id,
                    payload={"reason": "claude_code_handoff_accepted" if accepted else "claude_code_handoff_declined"},
                )
                if not accepted:
                    history.append({
                        "tool_name": CHECK_ENTRY_STRATEGY, "input": None, "status": "strategy", "hint_id": "declined", "step_id": step_id,
                        "error": (
                            "The user did not agree to hand this to Claude Code, so do not offer it again. Answer now with what "
                            "you have (action done): what you found, and plainly what you could not do."
                        ),
                    })
                    return self.repositories.tasks.update_metadata(task.id, {**task.metadata, "operator_history": history}, TaskStatus.RUNNING)
            offer = task.metadata.get("claude_code_offer")
            if isinstance(offer, dict) and offer.get("state") == "accepted" and not any(
                isinstance(e, dict) and e.get("tool_name") == "coding.agent" for e in history
            ):
                return claude_code_decision(task, history)
            if offer.get("state") != "pending":
                return None
        steps = [e for e in history if isinstance(e, dict) and e.get("tool_name") not in CHECK_ENTRY_NAMES]
        if offer is None and ladder.exhausted(steps) and self._claude_code_ready():
            return self._offer_claude_code(task, history)
        given = {
            str(e.get("hint_id")) for e in history
            if isinstance(e, dict) and e.get("tool_name") == CHECK_ENTRY_STRATEGY and e.get("hint_id")
        }
        code = self.executor.tool_definitions.get(ladder.CODE_TOOL) if self.executor is not None else None
        inventory = self.executor.tool_definitions.get(ladder.INVENTORY_TOOL) if self.executor is not None else None
        hint = ladder.next_hint(
            users_own_words(task), steps, given,
            code_enabled=bool(code is not None and code.enabled), inventory_enabled=bool(inventory is not None and inventory.enabled),
        )
        if hint is not None:
            history.append({
                "tool_name": CHECK_ENTRY_STRATEGY, "input": None, "status": "strategy", "hint_id": hint.id,
                "error": hint.text, "step_id": step_id,
            })
        return None

    def _offer_claude_code(self, task: TaskRecord, history: list[dict[str, Any]]) -> TaskRecord:
        """The local model has run out of ways: ask the user whether to hand the task to Claude Code. Nothing is sent without a yes."""
        question = (
            "I tried several ways to do this on my own and could not finish it. Do you want me to hand it to Claude Code on this "
            "computer? It would see your request and what I tried, and it uses your Claude account. Reply yes to hand it over, "
            "or no and I will tell you what I found."
        )
        updated = self.repositories.tasks.update_metadata(
            task.id, {**task.metadata, "operator_history": history, "claude_code_offer": {"question": question, "state": "pending"}}
        )
        self.audit.append(
            AuditEventType.TASK_STATE_CHANGED, actor="worker", task_id=task.id, payload={"reason": "claude_code_handoff_offered"},
        )
        return self._operator_ask_user(updated, question)

    def _ask_hosted_permission(self, task: TaskRecord, history: list[dict[str, Any]], exc: CloudFallbackNeeded) -> TaskRecord:
        """The local model did not answer and the only fallback is a hosted one: nothing is sent, the user is asked."""
        question = (
            f"The local model is not answering ({exc.reason}). Nothing has been sent anywhere. "
            f"Do you want me to send this task to {exc.model} ({exc.profile}), a hosted model, instead? "
            "That would share your request and any file contents involved with them. Reply yes to send it, or no to stop."
        )
        metadata = {
            **task.metadata, "operator_history": history,
            "cloud_fallback_pending": {"profile": exc.profile, "model": exc.model, "question": question},
        }
        updated = self.repositories.tasks.update_metadata(task.id, metadata)
        self.audit.append(
            AuditEventType.TASK_STATE_CHANGED, actor="worker", task_id=task.id,
            payload={"reason": "hosted_model_approval_requested", "profile": exc.profile, "model": exc.model, "why": exc.reason},
        )
        return self._operator_ask_user(updated, question)

    def _operator_ask_user(self, task: TaskRecord, question: str, options: list[str] | None = None) -> TaskRecord:
        """The question is used verbatim - the operator already composed a
        specific question because it decided it needs one, so there's nothing
        to template from a failure reason. Two-question exhaustion limit,
        same as everywhere else in this file that can pause a task on the
        user (see clarify_count usage throughout).
        """
        latest = self.repositories.tasks.get(task.id) or task
        ask_count = int(latest.metadata.get("clarify_count", 0))
        if ask_count >= 2:
            return self._transition_operator(
                latest, latest.metadata, TaskStatus.FAILED, "clarification_attempts_exhausted"
            )
        metadata = {
            **latest.metadata,
            "clarify_count": ask_count + 1,
            "clarifying_question": question,
            "clarifying_reason": question[:400],
        }
        if options is not None:
            # The Operator's own question: its choices, or none, never the buttons of an earlier question. (None keeps what the caller set,
            # as the planner does for its question.)
            metadata["clarifying_options"] = options
        updated = self.repositories.tasks.update_metadata(latest.id, metadata, TaskStatus.CLARIFYING)
        self.audit.task_state_changed("worker", latest.id, latest.status, updated.status)
        self.audit.append(
            AuditEventType.TASK_STATE_CHANGED,
            actor="operator",
            task_id=latest.id,
            payload={"reason": "clarification_requested", "question": question, "clarify_count": ask_count + 1},
        )
        return updated
