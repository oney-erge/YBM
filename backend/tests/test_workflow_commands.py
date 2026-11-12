"""Saved workflows from the chat."""

from __future__ import annotations

import pytest

from agent_control.channels.workflow_commands import workflow_command_reply
from agent_control.schemas import ChannelType, InboundMessage, MessageKind, TaskStatus, TaskWorkflow
from helpers import make_repos

HISTORY = [
    {"tool_name": "filesystem.manage", "input": {"operation": "inspect_folder", "root": "{{folder}}"}, "status": "succeeded"},
    {"tool_name": "document.manage", "input": {"operation": "summarize_pdf", "path": "C:/a.pdf"}, "status": "succeeded"},
    {"tool_name": "_fulfillment_check", "input": {}, "status": "passed"},
]


def _say(repos, audit, conversation_id, text):
    inbound = InboundMessage(channel=ChannelType.TELEGRAM, chat_id="42", sender_id="1", text=text, kind=MessageKind.TEXT)
    return workflow_command_reply(repos, audit, inbound, conversation_id)


def _finished(repos, conversation_id, objective="Summarize my weekly PDFs", history=None):
    task = repos.tasks.create(objective, conversation_id=conversation_id)
    return repos.tasks.update_metadata(task.id, {**task.metadata, "operator_history": history or HISTORY}, TaskStatus.COMPLETED)


@pytest.fixture
def chat(tmp_path):
    repos, audit = make_repos(tmp_path)
    return repos, audit, repos.conversations.get_or_create(ChannelType.TELEGRAM, "42")


def test_save_the_last_finished_task_under_a_name(chat):
    repos, audit, conv = chat
    _finished(repos, conv)
    reply = _say(repos, audit, conv, "save that as a workflow called weekly report")
    assert reply.text == 'Saved "weekly report" (2 steps). Say "run weekly report" any time to do it again.'
    workflow = repos.workflows.list_all()[0]
    assert workflow.name == "weekly report" and len(workflow.plan) == 2


def test_save_without_a_name_uses_the_request(chat):
    repos, audit, conv = chat
    _finished(repos, conv)
    reply = _say(repos, audit, conv, "save this as a workflow")
    assert 'Saved "Summarize my weekly PDFs"' in reply.text


def test_save_with_nothing_finished_says_so(chat):
    repos, audit, conv = chat
    assert "nothing finished to save" in _say(repos, audit, conv, "save that as a workflow called x").text


def test_a_name_already_in_use_is_not_overwritten(chat):
    repos, audit, conv = chat
    _finished(repos, conv)
    _say(repos, audit, conv, "save that as a workflow called weekly report")
    again = _say(repos, audit, conv, "save that as a workflow called Weekly Report")
    assert "already have a workflow called" in again.text
    assert len(repos.workflows.list_all()) == 1


def test_list_my_workflows(chat):
    repos, audit, conv = chat
    assert "no saved workflows" in _say(repos, audit, conv, "list my workflows").text
    _finished(repos, conv)
    _say(repos, audit, conv, "save that as a workflow called weekly report")
    listing = _say(repos, audit, conv, "my workflows").text
    assert "- weekly report" in listing and 'Say "run <name>"' in listing


def test_run_a_saved_workflow_creates_a_task_for_this_chat(chat):
    repos, audit, conv = chat
    _finished(repos, conv, history=[{"tool_name": "document.manage", "input": {"operation": "summarize_pdf", "path": "C:/a.pdf"}, "status": "succeeded"}])
    _say(repos, audit, conv, "save that as a workflow called weekly report")
    before = {t.id for t in repos.tasks.list_recent(50)}
    reply = _say(repos, audit, conv, "run weekly report")
    assert reply.text == 'Running "weekly report" now. I\'ll message you when it is done.'
    created = [t for t in repos.tasks.list_recent(50) if t.id not in before]
    assert len(created) == 1
    task = created[0]
    assert task.metadata["replay_plan"][0]["tool_name"] == "document.manage"
    assert task.metadata["source_channel"] == "telegram" and task.metadata["source_chat_id"] == "42"
    assert task.conversation_id == conv


def test_a_workflow_with_parameters_asks_for_them_and_then_runs(chat):
    repos, audit, conv = chat
    source = _finished(repos, conv)
    repos.workflows.create(TaskWorkflow(
        name="folder digest", source_task_id=source.id, objective_template="x",
        plan=[{"tool_name": "filesystem.manage", "tool_input": {"operation": "inspect_folder", "root": "{{folder}}"}}], parameters=["folder"],
    ))
    asked = _say(repos, audit, conv, "run folder digest")
    assert "needs folder" in asked.text and "run folder digest with folder=<value>" in asked.text
    ok = _say(repos, audit, conv, "run folder digest with folder=Downloads")
    assert ok.text.startswith('Running "folder digest" now')
    plan = [t for t in repos.tasks.list_recent(10) if t.metadata.get("workflow_id")][0].metadata["replay_plan"]
    assert plan[0]["tool_input"]["root"] == "Downloads"
    # A single value after "for" fills the only parameter.
    assert _say(repos, audit, conv, "run folder digest for Documents").text.startswith('Running "folder digest" now')


@pytest.mark.parametrize("text", [
    "run the tests", "do my laundry", "start a timer for 5 minutes", "run xylophone tests", "what is a workflow?", "save the file", "",
])
def test_other_messages_are_left_alone(chat, text):
    repos, audit, conv = chat
    _finished(repos, conv)
    _say(repos, audit, conv, "save that as a workflow called x")
    assert _say(repos, audit, conv, text) is None


def test_other_channels_are_left_alone(chat):
    repos, audit, conv = chat
    inbound = InboundMessage(channel=ChannelType.WHATSAPP, chat_id="42", sender_id="1", text="list my workflows", kind=MessageKind.TEXT)
    assert workflow_command_reply(repos, audit, inbound, conv) is None
