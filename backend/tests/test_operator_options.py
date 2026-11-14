"""A question the Operator asks can offer its choices as buttons (schemas.OperatorDecision.options, guards._operator_ask_user)."""

from __future__ import annotations

from agent_control.channels.quick_answers import MAX_OPTIONS, clarifying_options
from agent_control.orchestration import TaskWorker
from agent_control.schemas import OperatorAction, OperatorDecision, TaskStatus
from agent_control.tools.call_schema import operator_response_schema
from helpers import make_repos


def test_a_decision_may_carry_the_choices_and_does_not_have_to() -> None:
    asking = OperatorDecision(action=OperatorAction.ASK_USER, question="Which one?", options=["resume.docx", "resume.pdf"])
    assert asking.options == ["resume.docx", "resume.pdf"]
    assert OperatorDecision(action=OperatorAction.ASK_USER, question="Which one?").options == []
    assert OperatorDecision.model_validate({"action": "done", "final_answer": "x", "options": ["ignored by done"]}).final_answer == "x"


def test_the_strict_schema_lets_a_question_carry_a_few_short_choices() -> None:
    schema = operator_response_schema([], actions=("done", "ask_user", "blocked"))
    finishing = next(branch for branch in schema["anyOf"] if "options" in branch["properties"])
    options = finishing["properties"]["options"]
    assert options["type"] == "array" and options["maxItems"] == MAX_OPTIONS and options["items"]["maxLength"] == 40


def test_buttons_follow_the_question_that_was_asked(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    worker = TaskWorker(repos, audit)
    task = repos.tasks.create("Find my resume")
    asked = worker._operator_ask_user(task, "Which resume?", ["resume.docx", "  resume.pdf ", "resume.docx", "x" * 80, ""])
    assert asked.status == TaskStatus.CLARIFYING and asked.metadata["clarifying_question"] == "Which resume?"
    assert clarifying_options(asked) == ["resume.docx", "resume.pdf"]  # cleaned where they are read: trimmed, distinct, short
    # a second question without choices is not given the first one's buttons
    again = worker._operator_ask_user(repos.tasks.update_metadata(asked.id, asked.metadata, TaskStatus.RUNNING), "Anything else?", [])
    assert clarifying_options(again) == []


def test_a_caller_that_sets_its_own_buttons_keeps_them(tmp_path) -> None:
    """The planner puts the options of its one question in the task's metadata, then asks through the same method."""
    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("Build a robot", metadata={"clarifying_options": ["Desk robot", "Drone", "Rover"]})
    asked = TaskWorker(repos, audit)._operator_ask_user(task, "What kind of robot?")
    assert clarifying_options(asked) == ["Desk robot", "Drone", "Rover"]
