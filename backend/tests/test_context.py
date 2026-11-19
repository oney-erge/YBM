"""What the model sees besides the tool list (orchestration/context.py): the notes about this person and this moment keep their place, and the catalog is cut to fit."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from agent_control.orchestration import context
from agent_control.orchestration.operator import _MAX_CONFIG_PROMPT_CHARS, OperatorLoopService
from agent_control.schemas import ChannelType
from helpers import make_repos

PLAN_NOTE = (
    "\n\n## A plan is open in this chat\nGoal: a miles to kilometres page (the plan is proposed)\n"
    "1. Sketch the page - to do\n2. Write the conversion - to do\n"
    "If the user is talking about this plan, use plan.manage to show it."
)


def _catalog(tools: int) -> str:
    lines = [
        f"- tool{i}.manage (capability.x): does the thing number {i} and a fairly long description of what it does, so the line is long enough to matter; "
        "operations=a,b,c,d,e,f; operations=a,b,c,d,e,f"
        for i in range(tools)
    ]
    return "Decision preference (Autonomous): Do not ask the person. Work out what is meant, choose the tools yourself, and say what you assumed.\n" + "\n".join(lines)


def test_parts_that_fit_are_joined_as_they_are():
    assert context.fit([(2, "a\n"), (1, "b\n"), (3, "c\n")], budget=100) == "a\nb\nc\n"
    assert context.fit([], budget=10) == ""


def test_the_least_important_part_is_shortened_first_and_the_order_is_kept():
    abilities = "\n".join(f"- ability {i}: a line about something that bears on the request" for i in range(60))
    parts = [(context.PROJECT, "Project: pool\n"), (context.ABILITIES, abilities), (context.GOAL, "This continues a goal.\n"), (context.TODAY, "Today is Friday.\n")]
    out = context.fit(parts, budget=900)
    assert len(out) <= 900
    assert out.startswith("Project: pool\n") and "This continues a goal." in out and "Today is Friday." in out  # the important parts are whole
    assert out.index("Project: pool") < out.index("ability 0") < out.index("This continues a goal.") < out.index("Today is Friday.")
    assert "left out to fit" in out and "ability 59" not in out


def test_a_shortened_part_ends_at_a_line_and_not_in_the_middle_of_a_sentence():
    text = "\n".join(f"line {i:03d} with a few more words in it" for i in range(100))
    cut = context.fit([(3, text)], budget=600)
    body = cut.split("\n(more of this")[0]
    assert all(line.startswith("line ") and line.endswith("in it") for line in body.splitlines())


def test_a_part_with_no_room_is_dropped_not_shown_as_a_stub():
    out = context.fit([(1, "x" * 400), (3, "y" * 300)], budget=450)
    assert out == "x" * 400  # 50 characters of room is not worth a stub of the other part


def test_the_open_plan_survives_a_catalog_that_does_not_fit_and_the_catalog_gives_way():
    catalog = _catalog(70)
    assert len(catalog) > _MAX_CONFIG_PROMPT_CHARS
    situation = PLAN_NOTE + "\n\n## What bears on this request\n" + "- a skill line that bears on it\n" * 20
    prompt = OperatorLoopService._prompt("Revise the plan", catalog, [], "", situation)
    assert "## A plan is open in this chat" in prompt and "1. Sketch the page - to do" in prompt
    assert "Do not ask the person. Work out what is meant, choose the tools yourself, and say what you assumed." in prompt  # the preference is not cut in the middle
    assert "tool69.manage" in prompt  # every tool is still named


def test_with_no_notes_the_prompt_is_what_it_was():
    catalog = _catalog(20)
    assert OperatorLoopService._prompt("Do a thing", catalog, [], "") == OperatorLoopService._prompt("Do a thing", catalog, [], "", "")
    assert catalog in OperatorLoopService._prompt("Do a thing", catalog, [], "")


def test_what_was_said_just_before_is_word_for_word_and_skips_the_message_being_answered(tmp_path):
    repos, _ = make_repos(tmp_path)
    conversation = repos.conversations.get_or_create(ChannelType.WEB, "local")
    repos.episodes.add("said", "Give me a plan for a miles to kilometres page.", conversation_id=conversation)
    repos.episodes.add("answer", "Plan: 1. Sketch the page. 2. Write the conversion. Start it when you are ready.", conversation_id=conversation)
    repos.episodes.add("said", "Revise it and add a short animation.", conversation_id=conversation)
    note = context.last_exchange(repos.episodes, conversation, "Revise it and add a short animation.")
    assert "They said: Give me a plan for a miles to kilometres page." in note
    assert "You answered: Plan: 1. Sketch the page. 2. Write the conversion. Start it when you are ready." in note
    assert "Revise it and add" not in note.split("You answered")[0]
    assert "not at a file" in note


def test_nothing_recent_and_nothing_answered_add_nothing(tmp_path):
    repos, _ = make_repos(tmp_path)
    conversation = repos.conversations.get_or_create(ChannelType.WEB, "local")
    assert context.last_exchange(repos.episodes, conversation, "Hello") == ""
    assert context.last_exchange(repos.episodes, None, "Hello") == ""
    repos.episodes.add("answer", "Something answered long ago.", conversation_id=conversation)
    later = datetime.now(timezone.utc) + timedelta(hours=7)
    assert context.last_exchange(repos.episodes, conversation, "Hello", now=later) == ""


def test_the_last_answer_is_cut_at_a_length_that_keeps_the_prompt_small(tmp_path):
    repos, _ = make_repos(tmp_path)
    conversation = repos.conversations.get_or_create(ChannelType.WEB, "local")
    repos.episodes.add("answer", "word " * 2000, conversation_id=conversation)
    note = context.last_exchange(repos.episodes, conversation, "next")
    assert len(note) < 2_000 and note.count("word") < 400 and "..." in note
