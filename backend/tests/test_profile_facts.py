"""What a person says about themselves is kept one fact at a time."""

from __future__ import annotations

import pytest

from agent_control.channels.base import remember_standing_instruction
from agent_control.channels.profile_facts import describe_pairs, profile_pairs, store_profile_facts
from agent_control.schemas import ChannelType, InboundMessage, MessageKind
from helpers import make_repos

SENTENCE = (
    "Remember these about me: my GitHub username is oney-erge, my favorite programming language is Python, "
    "and my t-shirt size is L."
)


def test_a_statement_about_oneself_splits_into_one_pair_per_clause():
    assert profile_pairs(SENTENCE) == [
        ("GitHub username", "oney-erge"),
        ("favorite programming language", "Python"),
        ("t-shirt size", "L"),
    ]


def test_a_single_clause_with_a_called_phrase():
    assert profile_pairs("remember that my dog is called Biscuit.") == [("dog", "Biscuit")]


@pytest.mark.parametrize("text", [
    "remember that I prefer short answers",
    "remember that whenever you summarize a PDF, my boss is the audience and she hates long emails",
    "",
    "my name is",
])
def test_rules_and_other_text_are_not_profile_facts(text):
    assert profile_pairs(text) == []


def test_pairs_are_described_in_plain_words():
    assert describe_pairs([("t-shirt size", "L")]) == "your t-shirt size (L)"
    assert describe_pairs(profile_pairs(SENTENCE)) == (
        "your GitHub username (oney-erge), your favorite programming language (Python) and your t-shirt size (L)"
    )


def test_each_pair_becomes_its_own_fact_and_saying_it_again_updates_it(tmp_path):
    repos, _ = make_repos(tmp_path)
    store_profile_facts(repos, profile_pairs(SENTENCE))
    facts = {f.content for f in repos.memory_facts.list_all(category="profile")}
    assert facts == {"GitHub username: oney-erge", "Favorite programming language: Python", "T-shirt size: L"}

    store_profile_facts(repos, [("t-shirt size", "XL")])
    after = {f.content for f in repos.memory_facts.list_all(category="profile")}
    assert "T-shirt size: XL" in after and "T-shirt size: L" not in after
    assert len(after) == 3


def test_the_chat_remember_path_stores_profile_facts_and_says_what_it_kept(tmp_path):
    repos, audit = make_repos(tmp_path)
    inbound = InboundMessage(channel=ChannelType.TELEGRAM, chat_id="42", sender_id="1", text=SENTENCE, kind=MessageKind.TEXT)
    remembered = remember_standing_instruction(repos, audit, inbound, "telegram:user:1")
    assert remembered == (
        "your GitHub username (oney-erge), your favorite programming language (Python) and your t-shirt size (L)"
    )
    assert len(repos.memory_facts.list_all(category="profile")) == 3
    assert repos.memory_facts.list_all(category="preference") == []


def test_a_rule_still_takes_the_old_path(tmp_path):
    repos, audit = make_repos(tmp_path)
    inbound = InboundMessage(channel=ChannelType.TELEGRAM, chat_id="42", sender_id="1",
                             text="From now on always answer in metric units", kind=MessageKind.TEXT)
    assert remember_standing_instruction(repos, audit, inbound, "telegram:user:1") is not None
    assert repos.memory_facts.list_all(category="profile") == []
    assert len(repos.memory_facts.list_all(category="preference")) == 1
