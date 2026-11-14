"""What the assistant learned, offered in the chat where the person says yes or no (learning_cards.py)."""

from __future__ import annotations

import pytest

from agent_control.config import AppSettings, TelegramConfig
from agent_control.learning_cards import PERSONA, SKILL, card, decide
from agent_control.orchestration import TaskWorker
from agent_control.persona_learning import SuggestionStore
from agent_control.schemas import TaskStatus
from agent_control.skill_learning import SkillSuggestionStore
from agent_control.tools.skills import _load_skills
from helpers import make_repos
from test_plan_buttons import _Bot, _tap
from test_telegram import _service

ROUTING = {"source_channel": "telegram", "source_chat_id": "100", "source_sender_id": "42"}


def _settings(tmp_path) -> AppSettings:
    return AppSettings(
        _env_file=None,
        adapters={
            "skills": {"enabled": True, "root_dir": str(tmp_path / "skills"), "learning_enabled": True},
            "persona": {"enabled": True, "path": str(tmp_path / "persona.md"), "learning_enabled": True},
        },
    )


def _skill(settings: AppSettings):
    task = type("T", (), {"id": "task_1", "objective": "Total the invoices"})()
    return SkillSuggestionStore.load(settings.adapters.skills).add("Invoice totals", "add up the totals on invoices", "1. read each\n2. sum", task)


def _persona(settings: AppSettings):
    return SuggestionStore.load(settings.adapters.persona).add("Keep answers under five bullet points.", "please keep it short", "task_1")


def test_a_card_says_what_was_learned_and_has_two_short_buttons(tmp_path):
    settings = _settings(tmp_path)
    skill, persona = _skill(settings), _persona(settings)
    text, keyboard = card(skill)
    assert "Invoice totals" in text and "add up the totals on invoices" in text and text.endswith("Keep it as a skill?")
    keep, drop = keyboard["inline_keyboard"][0]
    assert (keep["text"], drop["text"]) == ("Keep it", "Not now")
    assert keep["callback_data"] == f"learn:s:{skill.id}:k" and drop["callback_data"] == f"learn:s:{skill.id}:d"
    text, keyboard = card(persona)
    assert '"Keep answers under five bullet points."' in text and [b["text"] for b in keyboard["inline_keyboard"][0]] == ["Remember", "No"]
    for entry in (skill, persona):
        assert all(len(b["callback_data"]) <= 64 for b in card(entry)[1]["inline_keyboard"][0])  # Telegram's limit


def test_keeping_a_skill_installs_it_and_dropping_does_not_and_neither_can_be_answered_twice(tmp_path):
    settings = _settings(tmp_path)
    kept, dropped = _skill(settings), SkillSuggestionStore.load(settings.adapters.skills).add(
        "Tidy desktop", "sort the desktop", "1. look\n2. move", type("T", (), {"id": "task_2", "objective": "Tidy"})()
    )
    assert decide(settings, SKILL, kept.id, True) == "Kept: Invoice totals. I will use it on similar requests."
    assert [s["name"] for s in _load_skills(settings.adapters.skills.root_dir)] == ["Invoice totals"]
    assert decide(settings, SKILL, dropped.id, False) == "Okay, I will not keep that."
    assert [s["name"] for s in _load_skills(settings.adapters.skills.root_dir)] == ["Invoice totals"]
    assert decide(settings, SKILL, kept.id, True) is None and decide(settings, SKILL, "skill_sugg_nope", True) is None
    store = SkillSuggestionStore.load(settings.adapters.skills)
    assert {s.name: s.status for s in store.suggestions} == {"Invoice totals": "accepted", "Tidy desktop": "rejected"}


def test_remembering_a_preference_writes_it_with_where_it_came_from(tmp_path):
    settings = _settings(tmp_path)
    suggestion = _persona(settings)
    assert decide(settings, PERSONA, suggestion.id, True) == "Remembered: Keep answers under five bullet points."
    written = (tmp_path / "persona.md").read_text(encoding="utf-8")
    assert "Keep answers under five bullet points." in written and "please keep it short" in written
    assert decide(settings, PERSONA, suggestion.id, False) is None and decide(settings, "x", suggestion.id, True) is None


# --- the tap ----------------------------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tapping_keep_installs_the_skill_and_the_card_becomes_the_answer(tmp_path):
    settings = _settings(tmp_path)
    skill = _skill(settings)
    bot = _Bot()
    service, repos = _service(tmp_path, TelegramConfig(enabled=True, allowed_user_ids=[42], allowed_chat_ids=[100]), bot_api=bot)
    service.settings = settings
    await _tap(service, f"learn:s:{skill.id}:k")
    assert bot.answered == [("cbq1", "Kept: Invoice totals. I will use it on similar requests.")]
    assert bot.edits[0][:3] == ("100", 7, "Kept: Invoice totals. I will use it on similar requests.") and bot.edits[0][3] is None
    assert [s["name"] for s in _load_skills(settings.adapters.skills.root_dir)] == ["Invoice totals"]
    await _tap(service, f"learn:s:{skill.id}:k", "again")
    assert bot.answered[-1] == ("again", "That was already answered.")
    recorded = repos.audit.list_matching_payload_value("suggestion_id", skill.id)
    assert [e.payload["via"] for e in recorded] == ["telegram_button"] and recorded[0].payload["decision"] == "accepted"


# --- the worker -------------------------------------------------------------------------------------------------------------------------------


class _Sink:
    def __init__(self) -> None:
        self.said = []

    async def say(self, routing, text, reply_markup=None):
        self.said.append((routing, text, reply_markup))


@pytest.mark.asyncio
async def test_the_worker_shows_a_new_proposal_in_the_chat_the_task_came_from(tmp_path):
    repos, audit = make_repos(tmp_path)
    settings = _settings(tmp_path)
    sink = _Sink()
    worker = TaskWorker(repos, audit, notification_sink=sink)
    task = repos.tasks.create("Total the invoices", metadata=dict(ROUTING))
    task = repos.tasks.update_status(task.id, TaskStatus.COMPLETED)
    await worker._offer_learning(task, _skill(settings))
    routing, text, keyboard = sink.said[0]
    assert routing == ROUTING and "Invoice totals" in text and keyboard["inline_keyboard"][0][0]["text"] == "Keep it"
    # Nothing to offer, no way to say it, or no chat to say it in: nothing is sent and nothing fails.
    await worker._offer_learning(task, None)
    await worker._offer_learning(repos.tasks.create("no chat"), _skill(settings))
    await TaskWorker(repos, audit)._offer_learning(task, _skill(settings))
    assert len(sink.said) == 1
