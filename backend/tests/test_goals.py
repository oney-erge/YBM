"""Goals keep what the person wants across the attempts at it (goals.py, orchestration/goal_routing.py, channels/base.py).

The 2026-11-16 chain, as tests: a task that ends without the result leaves its goal open; "plan for it", "you choose" and "no, you do it" belong to that goal and are
not read as new work; a declined hand-over is not offered again; several open goals make the person choose once, with a number.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from agent_control.channels.base import classify_and_spawn_task
from agent_control.goals import Goal, GoalStatus, attempt_entry, goal_notes_for_operator, goals_context
from agent_control.orchestration import goal_routing
from agent_control.schemas import ChannelType, InboundMessage, MessageClassification, MessageKind, TaskStatus, TaskType, utc_now
from helpers import make_repos


def _inbound(text: str) -> InboundMessage:
    return InboundMessage(channel=ChannelType.TELEGRAM, kind=MessageKind.TEXT, sender_id="7", chat_id="42", text=text)


class _Classifier:
    """Answers the way the Concierge would, and keeps the context it was given so a test can see the goals in it."""

    def __init__(self, **fields) -> None:
        self.fields, self.contexts = fields, []

    async def classify(self, message, context=None) -> MessageClassification:
        self.contexts.append(context or "")
        return MessageClassification(is_task=True, task_type=TaskType.OTHER, reason="test", **self.fields)


async def _no_progress(chat_id: str, text: str) -> None:
    return None


def _tv_goal(repos, conversation_id: str, **changes) -> Goal:
    goal = Goal(
        conversation_id=conversation_id, objective="Get the Samsung TV in the living room to take casts from this computer", words="connect to my TV and cast to it",
        status=GoalStatus.BLOCKED, attempts=[attempt_entry("task", "task_1", outcome="blocked", note="no device answered")], declined=["claude_code"], **changes,
    )
    return repos.goals.create(goal)


def test_the_repository_keeps_goals_and_lists_only_the_open_fresh_ones(tmp_path) -> None:
    repos, _audit = make_repos(tmp_path)
    open_goal = repos.goals.create(Goal(conversation_id="c1", objective="a"))
    repos.goals.create(Goal(conversation_id="c1", objective="done", status=GoalStatus.DONE))
    repos.goals.create(Goal(conversation_id="c2", objective="elsewhere"))
    old = repos.goals.create(Goal(conversation_id="c1", objective="stale", updated_at=utc_now() - timedelta(days=30)))
    assert [goal.objective for goal in repos.goals.list_open("c1")] == ["a"] and old.id != open_goal.id

    def add(goal: Goal) -> Goal:
        goal.declined = ["claude_code", "claude_code"]
        goal.attempts = [*goal.attempts, attempt_entry("task", "t1")]
        return goal

    saved = repos.goals.update(open_goal.id, add)
    assert saved.declined == ["claude_code"] and len(saved.attempts) == 1 and saved.revision == 1
    assert repos.goals.update("goal_missing", add) is None


def test_two_writers_of_one_goal_do_not_overwrite_each_other(tmp_path) -> None:
    repos, _audit = make_repos(tmp_path)
    goal = repos.goals.create(Goal(conversation_id="c1", objective="a"))
    seen = []

    def first(current: Goal) -> Goal:
        if not seen:  # someone else saves between this read and this write
            seen.append(1)
            repos.goals.update(goal.id, lambda other: other.model_copy(update={"evidence": ["from the other writer"]}))
        current.declined = ["claude_code"]
        return current

    saved = repos.goals.update(goal.id, first)
    assert saved.declined == ["claude_code"] and saved.evidence == ["from the other writer"]  # tried again on the newer copy: both changes are there


def test_the_open_goals_block_is_empty_without_goals_and_names_what_matters_with_them(tmp_path) -> None:
    repos, _audit = make_repos(tmp_path)
    assert goals_context([]) == ("", {})
    goal = _tv_goal(repos, "c1", waiting={"task_id": "task_9", "question": "Which casting library should I use?"})
    text, labels = goals_context([goal])
    assert list(labels) == ["G1"] and "G1: Get the Samsung TV" in text
    assert "waiting for the person to answer: Which casting library" in text and "tried: task blocked (no device answered)" in text and "declined: claude_code" in text
    assert "goal_ref" in text and "delegate" in text
    notes = goal_notes_for_operator(goal)
    assert "Do not repeat a method that already failed" in notes and "claude_code" in notes and "no device answered" in notes


def test_a_follow_up_belongs_to_the_only_open_goal_asks_when_there_are_several_and_is_new_when_there_are_none(tmp_path) -> None:
    repos, _audit = make_repos(tmp_path)
    tv = _tv_goal(repos, "c1")
    one = {"G1": tv}
    plan = MessageClassification(is_task=True, reason="t", act="plan")
    assert goal_routing.route_of(plan, one).goal is tv  # no label, one goal: that one
    assert goal_routing.route_of(MessageClassification(is_task=True, reason="t", goal_ref="g1", act="continue"), one).goal is tv  # a label in any case
    printer = repos.goals.create(Goal(conversation_id="c1", objective="Fix the label printer"))
    assert goal_routing.route_of(plan, {"G1": tv, "G2": printer}).ambiguous
    assert goal_routing.route_of(plan, {}).act == "new" and goal_routing.route_of(MessageClassification(is_task=True, reason="t"), one).act == "new"  # no label, no act: a request on its own
    question = goal_routing.ambiguity_question({"G1": tv, "G2": printer})
    assert "1. Get the Samsung TV" in question and "2. Fix the label printer" in question and "3. Something new" in question


@pytest.mark.asyncio
async def test_plan_for_it_after_a_blocked_attempt_makes_a_new_attempt_of_the_same_goal(tmp_path) -> None:
    """The first failure of 11/16: "Ok give me write me an optimum plan for it" became a plan titled with those words."""
    repos, audit = make_repos(tmp_path)
    conversation_id = repos.conversations.get_or_create(ChannelType.TELEGRAM, "42")
    tv = _tv_goal(repos, conversation_id)
    classifier = _Classifier(goal_ref="G1", act="plan", normalized_objective="Write a plan for getting the TV to take casts")

    result = await classify_and_spawn_task(
        _inbound("Ok give me write me an optimum plan for it"), conversation_id, repositories=repos, audit=audit, classifier=classifier, send_progress=_no_progress,
    )

    assert "Open goals in this chat" in classifier.contexts[0] and "G1: Get the Samsung TV" in classifier.contexts[0]
    task = result.task
    assert task is not None and task.metadata["goal_id"] == tv.id and task.metadata["goal_act"] == "plan"
    assert "Samsung TV" in task.objective  # the goal's own words, not only the follow-up's
    assert task.metadata["declined_methods"] == ["claude_code"] and "Do not repeat a method that already failed" in task.metadata["goal_notes"]
    saved = repos.goals.get(tv.id)
    assert saved.status == GoalStatus.OPEN and [attempt["id"] for attempt in saved.attempts] == ["task_1", task.id]
    assert len(repos.goals.list_open(conversation_id)) == 1  # no goal was made for the follow-up


@pytest.mark.asyncio
async def test_a_new_request_gets_a_goal_of_its_own_and_chat_does_not(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    conversation_id = repos.conversations.get_or_create(ChannelType.TELEGRAM, "42")
    classifier = _Classifier(normalized_objective="Count the words in notes.txt")
    result = await classify_and_spawn_task(_inbound("count the words in notes.txt"), conversation_id, repositories=repos, audit=audit, classifier=classifier, send_progress=_no_progress)
    goals = repos.goals.list_open(conversation_id)
    assert len(goals) == 1 and goals[0].objective == "Count the words in notes.txt" and result.task.metadata["goal_id"] == goals[0].id
    assert goals[0].attempts[0]["id"] == result.task.id and "Open goals" not in classifier.contexts[0]  # the first message of a chat sees no goals


@pytest.mark.asyncio
async def test_you_choose_answers_the_question_the_goal_waits_on_instead_of_starting_something(tmp_path) -> None:
    """The second failure of 11/16: "You choose" after "which library?" started an unrelated data-analysis plan."""
    repos, audit = make_repos(tmp_path)
    conversation_id = repos.conversations.get_or_create(ChannelType.TELEGRAM, "42")
    asking = repos.tasks.create("Find the TV and cast to it", conversation_id=conversation_id, metadata={"source_chat_id": "42", "clarifying_question": "Which casting library should I use?"})
    repos.tasks.update_metadata(asking.id, asking.metadata, TaskStatus.CLARIFYING)
    repos.goals.create(Goal(conversation_id=conversation_id, objective="Find the TV and cast to it", status=GoalStatus.WAITING, waiting={"task_id": asking.id, "question": "Which casting library should I use?"}))
    classifier = _Classifier(goal_ref="G1", act="delegate")

    result = await classify_and_spawn_task(_inbound("You choose"), conversation_id, repositories=repos, audit=audit, classifier=classifier, send_progress=_no_progress)

    assert result.task is None and "choice" in result.outbound_message.text  # no new task
    resumed = repos.tasks.get(asking.id)
    assert resumed.status == TaskStatus.RECEIVED and resumed.metadata["clarification_answer"] == goal_routing.DELEGATED_ANSWER  # the waiting task goes on with the canonical delegation
    assert [task.id for task in repos.tasks.list_by_statuses([TaskStatus.RECEIVED], limit=10)] == [asking.id]


@pytest.mark.asyncio
async def test_no_do_it_yourself_to_a_pending_handover_declines_the_method_and_keeps_the_goal(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    conversation_id = repos.conversations.get_or_create(ChannelType.TELEGRAM, "42")
    question = "Do you want me to hand it to Claude Code on this computer?"
    asking = repos.tasks.create("Cast to the TV", conversation_id=conversation_id, metadata={
        "source_chat_id": "42", "clarifying_question": question, "claude_code_offer": {"question": question, "state": "pending"},
    })
    repos.tasks.update_metadata(asking.id, asking.metadata, TaskStatus.CLARIFYING)
    goal = repos.goals.create(Goal(conversation_id=conversation_id, objective="Cast to the TV", status=GoalStatus.WAITING, waiting={"task_id": asking.id, "question": question}))

    result = await classify_and_spawn_task(
        _inbound("No, I want you to do it"), conversation_id, repositories=repos, audit=audit,
        classifier=_Classifier(goal_ref="G1", act="decline"), send_progress=_no_progress,
    )

    assert result.task is None and repos.tasks.get(asking.id).metadata["clarification_answer"] == "no"
    assert repos.goals.get(goal.id).declined == ["claude_code"] and repos.goals.get(goal.id).is_open


@pytest.mark.asyncio
async def test_with_two_open_goals_the_person_is_asked_once_and_a_number_places_the_message(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    conversation_id = repos.conversations.get_or_create(ChannelType.TELEGRAM, "42")
    tv = _tv_goal(repos, conversation_id)
    printer = repos.goals.create(Goal(conversation_id=conversation_id, objective="Fix the label printer", status=GoalStatus.BLOCKED))
    classifier = _Classifier(act="plan")  # a plan for something, and no label

    asked = await classify_and_spawn_task(_inbound("Ok, make me a plan for it"), conversation_id, repositories=repos, audit=audit, classifier=classifier, send_progress=_no_progress)
    assert asked.task is None and "Which one do you mean?" in asked.outbound_message.text and "Something new" in asked.outbound_message.text

    # "1" is the TV (the goal touched most recently is listed first, so the order is the order of the question)
    order = [goal.id for goal in repos.goals.list_open(conversation_id)]
    first_is_tv = order[0] == tv.id
    answered = await classify_and_spawn_task(_inbound("1" if first_is_tv else "2"), conversation_id, repositories=repos, audit=audit, classifier=_Classifier(), send_progress=_no_progress)
    assert answered.task is not None and answered.task.metadata["goal_id"] == tv.id and answered.task.metadata["goal_act"] == "plan"
    assert answered.task.metadata["original_message_text"] == "Ok, make me a plan for it"  # the message that could not be placed, not the "1"
    assert repos.goals.get(printer.id).attempts == []
    assert goal_routing.take_choice(repos, conversation_id, "2") is None  # a choice is used once


def test_a_task_ending_updates_its_goal_and_a_declined_handover_is_remembered(tmp_path) -> None:
    repos, _audit = make_repos(tmp_path)
    goal = repos.goals.create(Goal(conversation_id="c1", objective="Cast to the TV"))
    task = repos.tasks.create("Cast to the TV", metadata={"goal_id": goal.id, "claude_code_offer": {"question": "q", "state": "declined"}})
    for status, expected in ((TaskStatus.CLARIFYING, GoalStatus.WAITING), (TaskStatus.BLOCKED, GoalStatus.BLOCKED), (TaskStatus.CANCELLED, GoalStatus.BLOCKED), (TaskStatus.COMPLETED, GoalStatus.DONE)):
        goal_routing.sync_from_task(repos, task.model_copy(update={"status": status, "metadata": {**task.metadata, "clarifying_question": "Which one?", "synthesized_answer": "done or not"}}))
        assert repos.goals.get(goal.id).status == expected
    saved = repos.goals.get(goal.id)
    assert saved.declined == ["claude_code"] and [(item["id"], item["outcome"]) for item in saved.attempts] == [(task.id, "completed")]  # one entry per attempt, updated in place
    goal_routing.sync_from_task(repos, repos.tasks.create("unrelated"))  # a task with no goal is left alone


class _LeavesTheActOutOnce:
    def __init__(self) -> None:
        self.calls = 0

    async def classify(self, message, context=None) -> MessageClassification:
        self.calls += 1
        if self.calls == 1:
            return MessageClassification(is_task=True, task_type=TaskType.OTHER, reason="test")  # nothing said about the goals
        return MessageClassification(is_task=True, task_type=TaskType.OTHER, reason="test", goal_ref="G1", act="continue")


@pytest.mark.asyncio
async def test_a_reply_that_says_nothing_about_the_open_goals_is_asked_for_again_once(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    conversation_id = repos.conversations.get_or_create(ChannelType.TELEGRAM, "42")
    tv = _tv_goal(repos, conversation_id)
    classifier = _LeavesTheActOutOnce()
    result = await classify_and_spawn_task(_inbound("try again but use a different method"), conversation_id, repositories=repos, audit=audit, classifier=classifier, send_progress=_no_progress)
    assert classifier.calls == 2 and result.task.metadata["goal_id"] == tv.id and result.task.metadata["goal_act"] == "continue"
    # with no goal open the call is made once: nothing to say about
    other = repos.conversations.get_or_create(ChannelType.TELEGRAM, "99")
    fresh = _LeavesTheActOutOnce()
    await classify_and_spawn_task(_inbound("count the words in notes.txt"), other, repositories=repos, audit=audit, classifier=fresh, send_progress=_no_progress)
    assert fresh.calls == 1


@pytest.mark.asyncio
async def test_to_a_pending_handover_offer_the_persons_own_yes_is_the_answer_even_if_the_model_calls_it_a_delegation(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    conversation_id = repos.conversations.get_or_create(ChannelType.TELEGRAM, "42")
    question = "Do you want me to hand it to Claude Code on this computer?"
    asking = repos.tasks.create("Cast to the TV", conversation_id=conversation_id, metadata={
        "source_chat_id": "42", "clarifying_question": question, "claude_code_offer": {"question": question, "state": "pending"},
    })
    repos.tasks.update_metadata(asking.id, asking.metadata, TaskStatus.CLARIFYING)
    repos.goals.create(Goal(conversation_id=conversation_id, objective="Cast to the TV", status=GoalStatus.WAITING, waiting={"task_id": asking.id, "question": question}))
    await classify_and_spawn_task(
        _inbound("Yes, please hand it over"), conversation_id, repositories=repos, audit=audit,
        classifier=_Classifier(goal_ref="G1", act="delegate"), send_progress=_no_progress,
    )
    assert repos.tasks.get(asking.id).metadata["clarification_answer"] == "Yes, please hand it over"  # not "You choose."


@pytest.mark.asyncio
async def test_after_an_investigation_a_pick_or_a_you_choose_tells_the_next_attempt_which_option_to_carry_out(tmp_path) -> None:
    """Live 11/18: "You choose." after the TV investigation's options started an attempt that looked at the graphics card and did none of them."""
    repos, audit = make_repos(tmp_path)
    conversation_id = repos.conversations.get_or_create(ChannelType.TELEGRAM, "42")
    tv = repos.goals.create(Goal(
        conversation_id=conversation_id, objective="Get the TV to take casts", status=GoalStatus.WAITING, waiting={"question": "Which way forward should I try?"},
        evidence=["Found: Ethernet is on a Public network", "Option 1: Set the network to Private - Settings, Network"],
    ))
    result = await classify_and_spawn_task(
        _inbound("You choose."), conversation_id, repositories=repos, audit=audit, classifier=_Classifier(goal_ref="G1", act="delegate"), send_progress=_no_progress,
    )
    notes = result.task.metadata["goal_notes"]
    assert result.task.metadata["goal_id"] == tv.id and "Option 1: Set the network to Private" in notes
    assert 'answered: "You choose."' in notes and "carry out Option 1" in notes and "computer.use can open Windows Settings" in notes
    # a goal that waits on nothing of the kind gets no such instruction
    plain = repos.goals.create(Goal(conversation_id=conversation_id, objective="Fix the printer", status=GoalStatus.BLOCKED, attempts=[attempt_entry("task", "t1", outcome="blocked")]))
    other = await classify_and_spawn_task(_inbound("keep trying"), conversation_id, repositories=repos, audit=audit, classifier=_Classifier(goal_ref="G2", act="continue"), send_progress=_no_progress)
    assert "carry out Option 1" not in other.task.metadata["goal_notes"] and plain.id
