from __future__ import annotations

import pytest
from pydantic import ValidationError

from agent_control.schemas import (
    Capability,
    ChannelType,
    InboundMessage,
    MessageClassification,
    MessageKind,
    OperatorAction,
    OperatorDecision,
    OrchestrationIntent,
    ParallelToolCall,
    PlanPostcondition,
    PostconditionType,
    RiskLevel,
    ToolCallRequest,
)


def test_plan_postcondition_validates() -> None:
    # PlanPostcondition is the Operator loop's own deterministic postcondition
    # record now (orchestration/fulfillment.py), not part of a PlanModel -
    # that class was deleted with the plan-based path.
    postcondition = PlanPostcondition(
        type=PostconditionType.PREVIEW_URL,
        description="A local preview URL is reported.",
    )

    assert postcondition.type == PostconditionType.PREVIEW_URL
    assert postcondition.required is True


def test_inbound_message_forbids_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        InboundMessage(
            channel=ChannelType.TELEGRAM,
            kind=MessageKind.TEXT,
            sender_id="123",
            chat_id="456",
            text="hello",
            unexpected=True,
        )


def test_orchestration_intent_ignores_harmless_llm_extra_fields() -> None:
    intent = OrchestrationIntent.model_validate(
        {
            "route": "desktop.observe",
            "operation": "screenshot",
            "objective": "Capture VS Code and file directory.",
            "reasoning": "The user asked for a screenshot.",
            "screenshot": True,
        }
    )

    assert intent.route.value == "desktop.observe"
    assert not hasattr(intent, "screenshot")


def test_tool_call_requires_valid_capability() -> None:
    request = ToolCallRequest(
        task_id="task_1",
        tool_name="vscode",
        capability=Capability.VSCODE_READ_STATE,
        risk_level=RiskLevel.LOW,
    )

    assert request.capability == Capability.VSCODE_READ_STATE


def test_operator_decision_call_tool_requires_tool_name() -> None:
    with pytest.raises(ValidationError):
        OperatorDecision(action=OperatorAction.CALL_TOOL)


def test_operator_decision_call_tool_accepts_tool_name() -> None:
    decision = OperatorDecision(
        action=OperatorAction.CALL_TOOL,
        tool_name="filesystem.manage",
        tool_input={"operation": "search", "query": "resume"},
    )

    assert decision.tool_name == "filesystem.manage"
    assert decision.tool_input["query"] == "resume"


def test_operator_decision_done_does_not_require_tool_name() -> None:
    decision = OperatorDecision(action=OperatorAction.DONE, final_answer="The answer is 42.")

    assert decision.tool_name is None
    assert decision.final_answer == "The answer is 42."


def test_operator_decision_rejects_unknown_action() -> None:
    with pytest.raises(ValidationError):
        OperatorDecision(action="not_a_real_action")


def test_operator_decision_call_tools_parallel_requires_at_least_two_calls() -> None:
    with pytest.raises(ValidationError):
        OperatorDecision(
            action=OperatorAction.CALL_TOOLS_PARALLEL,
            parallel_calls=[ParallelToolCall(tool_name="site_a")],
        )


def test_operator_decision_call_tools_parallel_accepts_two_or_more_calls() -> None:
    decision = OperatorDecision(
        action=OperatorAction.CALL_TOOLS_PARALLEL,
        parallel_calls=[
            ParallelToolCall(tool_name="site_a", tool_input={"url": "a"}),
            ParallelToolCall(tool_name="site_b", tool_input={"url": "b"}, risk_level=RiskLevel.LOW),
        ],
    )

    assert len(decision.parallel_calls) == 2
    assert decision.parallel_calls[0].tool_name == "site_a"


def test_operator_decision_delegate_requires_objective() -> None:
    with pytest.raises(ValidationError):
        OperatorDecision(action=OperatorAction.DELEGATE)


def test_operator_decision_delegate_accepts_objective_and_optional_tools() -> None:
    decision = OperatorDecision(
        action=OperatorAction.DELEGATE,
        delegate_objective="find and summarize the file",
        delegate_tools=["filesystem.manage"],
    )

    assert decision.delegate_objective == "find and summarize the file"
    assert decision.delegate_tools == ["filesystem.manage"]


def test_operator_decision_delegate_tools_defaults_to_unrestricted() -> None:
    decision = OperatorDecision(action=OperatorAction.DELEGATE, delegate_objective="anything")

    assert decision.delegate_tools is None


def test_message_classification_expected_postconditions_defaults_empty() -> None:
    classification = MessageClassification(is_task=True, reason="ordinary task")

    assert classification.expected_postconditions == []


def test_message_classification_accepts_a_declared_postcondition() -> None:
    classification = MessageClassification(
        is_task=True, reason="wants a slide deck", expected_postconditions=["presentation_file"]
    )

    assert classification.expected_postconditions == [PostconditionType.PRESENTATION_FILE]


def test_message_classification_silently_drops_an_unrecognized_postcondition_type() -> None:
    """One hallucinated value must not fail the whole classification -
    same defensive posture as task_type_accepts_route_aliases."""
    classification = MessageClassification(
        is_task=True,
        reason="wants a slide deck",
        expected_postconditions=["presentation_file", "made_up_type", 42],
    )

    assert classification.expected_postconditions == [PostconditionType.PRESENTATION_FILE]
