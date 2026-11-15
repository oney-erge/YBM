"""After a task: what the assistant noticed it could keep (a preference, a skill), offered in the chat."""
from __future__ import annotations

import logging
from typing import Any

from agent_control.learning_cards import card as learning_card
from agent_control.plans.runner import routing_of
from agent_control.schemas import (
    TaskRecord,
    TaskStatus,
)

logger = logging.getLogger(__name__)

#: A task in one of these states is the user's work in progress: it is moving, or it is waiting for a tap.
_MOVING = frozenset({
    TaskStatus.RECEIVED, TaskStatus.INTERPRETING, TaskStatus.PLANNED, TaskStatus.RUNNING, TaskStatus.RETRYING, TaskStatus.AWAITING_APPROVAL,
})


class LearningStage:
    async def _maybe_learn_preference(self, task: TaskRecord) -> None:
        """Queue a durable preference from the operator's own words, if any.

        Runs after the task is finished and its result already sent, so a slow
        or failing extraction can never delay or break the actual answer -
        this is the least important thing happening on this tick.
        """
        if self.persona_config is None or self.operator is None:
            return
        if task.status not in {TaskStatus.COMPLETED, TaskStatus.FAILED}:
            return
        message = str(task.metadata.get("original_message_text") or task.objective or "")
        try:
            from agent_control.persona_learning import propose_from_message

            suggestion = await propose_from_message(
                getattr(self.operator, "provider", None),
                self.persona_config,
                message,
                task_id=task.id,
            )
            await self._offer_learning(task, suggestion)
        except Exception:  # noqa: BLE001 - never surface as a task failure
            logger.warning("persona learning pass failed", exc_info=True)

    async def _offer_learning(self, task: TaskRecord, suggestion: Any) -> None:
        """Show a new proposal (a skill, a preference) in the chat the task came from, with Keep and Drop buttons (learning_cards.py).

        Nothing is installed here: the proposal waits in its queue (and in the console) until someone answers. A chat that cannot be reached,
        or a sink that cannot send a line with buttons, leaves it there.
        """
        say = getattr(self.notification_sink, "say", None)
        routing = routing_of(task)
        if suggestion is None or say is None or not routing:
            return
        if self._chat_is_busy(task):
            return  # a question about keeping something does not belong in the middle of someone's work: it waits in the queue
        text, keyboard = learning_card(suggestion)
        await say(routing, text, keyboard)

    def _chat_is_busy(self, task: TaskRecord) -> bool:
        """The task is a step of a plan, or another task of this chat is still moving (or waiting for an approval tap)."""
        if task.metadata.get("plan_id"):
            return True
        if task.conversation_id is None:
            return False
        for other in self.repositories.tasks.list_for_conversation(task.conversation_id, limit=20):
            if other.id != task.id and other.status in _MOVING and not other.metadata.get("plan_id"):
                return True
        return False

    async def _maybe_learn_skill(self, task: TaskRecord) -> None:
        """Offer to save a successful multi-step run as a reusable procedure.

        After the answer has already gone out, like the persona pass - this is
        the least important work on the tick and must never delay or break a
        finished task.
        """
        if self.skills_config is None or self.operator is None:
            return
        if task.status != TaskStatus.COMPLETED:
            return
        try:
            from agent_control.skill_learning import propose_from_task, save_helper

            # The script that produced the answer is kept as is: no model call, nothing to review.
            save_helper(self.skills_config, task)
            suggestion = await propose_from_task(getattr(self.operator, "provider", None), self.skills_config, task)
            await self._offer_learning(task, suggestion)
        except Exception:  # noqa: BLE001 - never surface as a task failure
            logger.warning("skill learning pass failed", exc_info=True)
