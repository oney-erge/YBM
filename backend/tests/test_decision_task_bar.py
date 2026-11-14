"""A message the decision model is sure is a task, though not sure which tool, no longer waits for the concierge (llm/decision.py)."""

from __future__ import annotations

import pytest

from agent_control.config import DecisionConfig
from agent_control.llm.decision import DecisionModelClassifier, DecisionOpinion, settles_as_a_task, task_probability
from agent_control.schemas import AuditEventType, ChannelType, InboundMessage, IntentRoute, MessageKind
from helpers import make_repos
from test_decision_classifier import _Client, _Concierge


def _opinion(probabilities: dict[str, float], ability: float | None = 0.05) -> DecisionOpinion:
    route = max(probabilities, key=probabilities.get)
    return DecisionOpinion(IntentRoute(route), probabilities[route], probabilities, 12.0, ability=ability)


def _config(**kwargs) -> DecisionConfig:
    return DecisionConfig(mode=kwargs.pop("mode", "cascade"), **kwargs)


SPREAD = {"filesystem.manage": 0.55, "document.manage": 0.25, "code.interpreter": 0.14, "conversation": 0.04, "status": 0.02}


def test_the_probability_of_a_task_is_the_sum_over_the_tool_routes():
    assert task_probability(_opinion(SPREAD)) == pytest.approx(0.94)
    assert task_probability(_opinion({"conversation": 0.9, "status": 0.1})) == 0


def test_a_task_the_model_is_sure_of_is_settled_though_no_single_tool_is():
    assert settles_as_a_task(_config(), _opinion(SPREAD))
    assert not settles_as_a_task(_config(task_bar=0.95), _opinion(SPREAD))
    assert not settles_as_a_task(_config(settle_tasks_alone=False), _opinion(SPREAD))
    assert not settles_as_a_task(_config(mode="shadow"), _opinion(SPREAD)) and not settles_as_a_task(_config(mode="off"), _opinion(SPREAD))


def test_chat_status_and_unsure_messages_are_not_settled_as_tasks():
    assert not settles_as_a_task(_config(), _opinion({"conversation": 0.7, "filesystem.manage": 0.3}))
    assert not settles_as_a_task(_config(), _opinion({"filesystem.manage": 0.5, "conversation": 0.3, "status": 0.2}))
    assert not settles_as_a_task(_config(), _opinion({"status": 0.6, "filesystem.manage": 0.4}))


def test_a_question_about_what_the_bot_can_do_stays_with_the_concierge():
    assert not settles_as_a_task(_config(), _opinion(SPREAD, ability=0.95))
    assert settles_as_a_task(_config(), _opinion(SPREAD, ability=None))


def test_requests_the_concierge_must_read_are_never_settled_this_way():
    for kept in ("configuration", "coding.agent", "desktop.observe"):
        assert not settles_as_a_task(_config(), _opinion({kept: 0.5, "filesystem.manage": 0.49, "conversation": 0.01})), kept
        # a little probability on them is enough to keep the concierge in
        assert not settles_as_a_task(_config(), _opinion({"filesystem.manage": 0.7, kept: 0.1, "browser.open": 0.19, "conversation": 0.01})), kept


def _message(text: str) -> InboundMessage:
    return InboundMessage(channel=ChannelType.TELEGRAM, kind=MessageKind.TEXT, sender_id="1", chat_id="1", text=text)


@pytest.mark.asyncio
async def test_a_settled_task_skips_the_concierge_leaves_the_route_open_and_is_recorded(tmp_path):
    from agent_control.storage import AuditLogger

    repos, _ = make_repos(tmp_path)
    audit = AuditLogger(repos.audit)
    concierge, client = _Concierge(), _Client(_opinion(SPREAD))
    classifier = DecisionModelClassifier(concierge, _config(), audit, client=client)

    result = await classifier.classify(_message("find the invoice from March and tell me the total"))

    assert concierge.calls == 0
    assert result.is_task and result.intent.route == IntentRoute.UNKNOWN
    assert result.normalized_objective == "find the invoice from March and tell me the total"
    assert "tool not settled" in result.reason and "filesystem.manage" in result.reason
    event = next(e for e in repos.audit.list_recent(5) if e.type == AuditEventType.INTAKE_DECISION)
    assert event.payload["used"] is True and event.payload["settled_by"] == "task_probability"
    assert event.payload["task_probability"] == pytest.approx(0.94)


@pytest.mark.asyncio
async def test_a_confident_single_route_is_decided_as_before_and_an_unsure_message_still_goes_to_the_concierge(tmp_path):
    from agent_control.storage import AuditLogger

    repos, _ = make_repos(tmp_path)
    audit = AuditLogger(repos.audit)
    sure = DecisionModelClassifier(_Concierge(), _config(), audit, client=_Client(_opinion({"filesystem.manage": 0.97, "conversation": 0.03})))
    decided = await sure.classify(_message("how many files are in downloads"))
    assert decided.intent.route == IntentRoute.FILESYSTEM_MANAGE
    event = next(e for e in repos.audit.list_recent(5) if e.type == AuditEventType.INTAKE_DECISION)
    assert event.payload["used"] is True and event.payload["settled_by"] is None

    concierge = _Concierge()
    unsure = DecisionModelClassifier(concierge, _config(), audit, client=_Client(_opinion({"filesystem.manage": 0.5, "conversation": 0.35, "status": 0.15})))
    await unsure.classify(_message("hmm what about that thing"))
    assert concierge.calls == 1


def test_cascade_keeps_to_its_list_of_routes_for_this_too_and_decision_mode_does_not():
    schedule = {"schedule.manage": 0.8, "filesystem.manage": 0.15, "conversation": 0.05}
    assert not settles_as_a_task(_config(mode="cascade"), _opinion(schedule))
    assert settles_as_a_task(_config(mode="decision"), _opinion(schedule))
