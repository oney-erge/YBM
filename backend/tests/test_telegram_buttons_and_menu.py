"""A Stop button on the progress line, an Undo button under the answer to a move, /stop, /undo and the "/" menu."""

from __future__ import annotations

import pytest

from agent_control.channels.telegram import BOT_COMMANDS, TelegramBotApi
from agent_control.channels.telegram_notifications import TelegramTaskNotifier, _stop_keyboard, _undo_keyboard
from agent_control.config import TelegramConfig
from agent_control.schemas import ChannelType, TaskRecord, TaskStatus
from test_telegram import _FakeBotApiForCallbacks, _service


class _Client:
    def __init__(self) -> None:
        self.edits: list[tuple] = []
        self.sent: list[tuple] = []
        self.deleted: list = []

    async def edit_message_text(self, chat_id, message_id, text, reply_markup=None):
        self.edits.append((chat_id, message_id, text, reply_markup))
        return {"ok": True}

    async def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append((chat_id, text, reply_markup))
        return {"ok": True}

    async def delete_message(self, chat_id, message_id):
        self.deleted.append(message_id)
        return {"ok": True}


def _task(status: TaskStatus = TaskStatus.RUNNING, **metadata) -> TaskRecord:
    return TaskRecord(
        objective="Sort my photos", status=status,
        metadata={"source_channel": "telegram", "source_chat_id": "100", "progress_message_id": 55, **metadata},
    )


@pytest.mark.asyncio
async def test_every_progress_edit_carries_a_stop_button_or_telegram_would_remove_it():
    client = _Client()
    task = _task(operator_history=[{"tool_name": "filesystem.manage", "status": "succeeded"}])
    await TelegramTaskNotifier(client, clock=lambda: 1000.0).notify(task)  # type: ignore[arg-type]
    assert client.edits and client.edits[0][3] == _stop_keyboard(task.id)
    assert client.edits[0][3]["inline_keyboard"][0][0]["callback_data"] == f"task:{task.id}:cancel"


def test_undo_is_offered_under_the_answer_to_a_move_once():
    moved = _task(TaskStatus.COMPLETED, undo_moves={"root": "C:/p", "moves": [{"from": "C:/p/a/x.png", "to": "C:/p/x.png"}]})
    assert _undo_keyboard(moved)["inline_keyboard"][0][0]["callback_data"] == f"undo:{moved.id}"
    assert _undo_keyboard(_task(TaskStatus.COMPLETED)) is None  # nothing was moved
    assert _undo_keyboard(_task(TaskStatus.RUNNING, undo_moves={"moves": [1]})) is None  # not finished
    undone = _task(TaskStatus.COMPLETED, undo_moves={"moves": [1]}, undo_task_id="task_u")
    assert _undo_keyboard(undone) is None
    the_undo = _task(TaskStatus.COMPLETED, undo_moves={"moves": [1]}, undo_of_task_id="task_m")
    assert _undo_keyboard(the_undo) is None


@pytest.mark.asyncio
async def test_the_answer_to_a_move_is_sent_with_the_undo_button():
    client = _Client()
    task = _task(TaskStatus.COMPLETED, synthesized_answer="Moved 3 files.", undo_moves={"root": "C:/p", "moves": [{"from": "a", "to": "b"}]})
    await TelegramTaskNotifier(client).notify(task)  # type: ignore[arg-type]
    assert client.sent[0][2] == _undo_keyboard(task)


def _setup(tmp_path):
    bot = _FakeBotApiForCallbacks()
    service, repos = _service(tmp_path, TelegramConfig(enabled=True, allowed_user_ids=[42], allowed_chat_ids=[100]), bot_api=bot)
    return service, repos, bot


def _press(service, data: str, callback_id: str = "cbq1"):
    return service.handle_update({"update_id": 1, "callback_query": {"id": callback_id, "from": {"id": 42}, "message": {"chat": {"id": 100}}, "data": data}})


_message_ids = iter(range(100, 10_000))


def _say(service, text: str):
    message_id = next(_message_ids)
    return service.handle_update({"update_id": message_id, "message": {"message_id": message_id, "from": {"id": 42}, "chat": {"id": 100}, "text": text}})


def _running(repos):
    conversation_id = repos.conversations.get_or_create(ChannelType.TELEGRAM, "100")
    task = repos.tasks.create("Build the page", conversation_id=conversation_id, metadata={"source_chat_id": "100"})
    repos.tasks.update_metadata(task.id, task.metadata, TaskStatus.RUNNING)
    return task


def test_tapping_stop_cancels_the_task_and_says_so(tmp_path):
    service, repos, bot = _setup(tmp_path)
    task = _running(repos)
    _press(service, f"task:{task.id}:cancel")
    assert repos.tasks.get(task.id).status == TaskStatus.CANCELLED
    assert bot.answered == [("cbq1", "Stopping.")]


def test_slash_stop_stops_the_task_that_is_working_in_this_chat(tmp_path):
    service, repos, _ = _setup(tmp_path)
    task = _running(repos)
    reply = _say(service, "/stop")
    assert reply.outbound_message.text.startswith("Stopping: Build the page")
    assert repos.tasks.get(task.id).status == TaskStatus.CANCELLED
    assert _say(service, "/stop").outbound_message.text == "Nothing is running."


def _moved(repos, tmp_path):
    root = tmp_path / "photos"
    (root / "pictures").mkdir(parents=True)
    moved = root / "pictures" / "a.png"
    moved.write_text("x", encoding="utf-8")
    conversation_id = repos.conversations.get_or_create(ChannelType.TELEGRAM, "100")
    task = repos.tasks.create(
        "Move the pngs", conversation_id=conversation_id,
        metadata={"source_chat_id": "100", "undo_moves": {"root": str(root), "moves": [{"from": str(moved), "to": str(root / "a.png")}]}},
    )
    repos.tasks.update_metadata(task.id, task.metadata, TaskStatus.COMPLETED)
    return task


def test_tapping_undo_starts_the_undo_task_once(tmp_path):
    service, repos, bot = _setup(tmp_path)
    source = _moved(repos, tmp_path)
    _press(service, f"undo:{source.id}", "cbq1")
    started = repos.tasks.get(source.id)
    assert started.metadata.get("undo_task_id")
    undo = repos.tasks.get(started.metadata["undo_task_id"])
    assert undo.metadata["undo_of_task_id"] == source.id and undo.metadata["replay_plan"]
    assert bot.answered[0][1].startswith("Putting 1 file(s) back")
    _press(service, f"undo:{source.id}", "cbq2")
    assert bot.answered[1][1] == "I already put those back."


def test_an_undo_button_for_a_task_with_nothing_to_undo_says_so(tmp_path):
    service, repos, bot = _setup(tmp_path)
    _press(service, "undo:task_unknown")
    assert bot.answered == [("cbq1", "There is nothing to undo there any more.")]


def test_slash_undo_is_the_same_as_saying_undo_that(tmp_path):
    service, repos, _ = _setup(tmp_path)
    source = _moved(repos, tmp_path)
    reply = _say(service, "/undo")
    assert reply.outbound_message.text.startswith("Putting 1 file(s) back")
    assert repos.tasks.get(source.id).metadata.get("undo_task_id")


def test_the_menu_lists_the_commands_that_work():
    names = [name for name, _ in BOT_COMMANDS]
    assert names == ["status", "tasks", "stop", "undo", "passwords", "screenshot"]
    assert all(0 < len(text) <= 256 for _, text in BOT_COMMANDS)


@pytest.mark.asyncio
async def test_the_menu_and_a_keyboard_on_an_edit_are_sent_to_the_bot_api(monkeypatch):
    sent = []

    async def fake_post(self, method, payload):
        sent.append((method, payload))
        return {"ok": True, "result": {}}

    monkeypatch.setattr(TelegramBotApi, "_post", fake_post)
    api = TelegramBotApi("123:abc")
    await api.set_my_commands([("status", "What I am working on")])
    await api.edit_message_text("100", 5, "Working", reply_markup={"inline_keyboard": []})
    await api.edit_message_text("100", 5, "Done")
    assert sent[0] == ("setMyCommands", {"commands": [{"command": "status", "description": "What I am working on"}]})
    assert sent[1][1]["reply_markup"] == {"inline_keyboard": []} and "reply_markup" not in sent[2][1]
