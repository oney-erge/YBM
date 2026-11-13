"""The approval prompt is the message a person reads most carefully: it says what will happen, in words, and
never shows a secret."""

from __future__ import annotations

from agent_control.channels.task_notify import format_task_message
from agent_control.human_text import describe_action
from agent_control.schemas import TaskRecord, TaskStatus


def _task(**metadata) -> TaskRecord:
    return TaskRecord(id="task_1", objective="sign me up", status=TaskStatus.AWAITING_APPROVAL, metadata=metadata)


def _batch(calls):
    return _task(operator_pending_batch={"calls": calls}, pending_approval_preview="- raw preview")


def test_each_step_is_described_in_words():
    text = format_task_message(_batch([
        {"tool_name": "browser.control", "tool_input": {"operation": "fill_form_step", "field": "Full name", "value": "Oney Erge"}},
        {"tool_name": "browser.control", "tool_input": {"operation": "fill_form_step", "fields": {"T-shirt size": "M"}}},
        {"tool_name": "browser.control", "tool_input": {"operation": "click", "text": "Sign me up"}},
    ]))
    assert text.startswith("I need your OK before I go on:")
    assert '- Type "Oney Erge" into the "Full name" field' in text
    assert '- Type "M" into the "T-shirt size" field' in text
    assert '- Click "Sign me up"' in text
    assert "browser.control" not in text and "{" not in text and "raw preview" not in text
    assert "approve" in text.lower() and "cancel" in text.lower()


def test_a_secret_is_never_shown():
    text = format_task_message(_batch([
        {"tool_name": "browser.control", "tool_input": {"operation": "fill_form", "fields": {"Password": "hunter2", "Email": "a@b.co"}}},
    ]))
    assert "hunter2" not in text
    # The value is withheld by the wording, and the message-wide redactor may mask it a second time.
    assert '"Password" = ' in text and '"Email" = "a@b.co"' in text


def test_a_single_pending_call_is_described_too():
    task = _task(operator_pending_call={
        "tool_name": "computer.use", "tool_input": {"operation": "act", "action": {"type": "run_goal", "goal": "Open Steam"}},
    })
    assert "- Control your computer: Open Steam" in format_task_message(task)


def test_without_a_pending_call_the_old_preview_is_still_shown():
    text = format_task_message(_task(pending_approval_preview="- Run cleanup script (risk: high)"))
    assert "Run cleanup script" in text and "approve" in text.lower()


def test_other_actions_read_as_sentences():
    assert describe_action("browser.open", {"url": "http://127.0.0.1:8899/"}) == "Open http://127.0.0.1:8899/"
    assert describe_action("code.interpreter", {"code": "print(1)"}) == "Run a short Python script"
    assert describe_action("coding.agent", {"provider": "claude_code", "prompt": "write primes"}) == "Start Claude Code on: write primes"
    assert describe_action("schedule.manage", {"operation": "create", "objective": "check the inbox"}) == "Set up a reminder: check the inbox"
    assert describe_action("filesystem.manage", {"operation": "apply_manifest", "moves": [1, 2, 3]}) == "Move or rename 3 file(s)"
    assert describe_action("system.inventory", {"operation": "steam_games"}) == "Checking what is on this computer"


def test_moving_around_a_page_is_described_not_called_using_the_web_page():
    assert describe_action("browser.control", {"operation": "navigate", "url": "http://127.0.0.1:8899/"}) == "Go to http://127.0.0.1:8899/"
    assert describe_action("browser.control", {"operation": "navigate"}) == "Go to another page"
    assert describe_action("browser.control", {"operation": "extract_page_state"}) == "Read what is on the page"
    assert describe_action("browser.control", {"operation": "chain", "steps": []}) == "Do several things on the web page, one after another"
    assert describe_action("browser.control", {"operation": "something_new"}) == "Use the web page"


def test_a_manifest_says_where_the_files_are_and_where_they_go():
    """A phone approval read "Move or rename 24 file(s)", with no folder named."""
    folder = "C:\\Users\\sam\\Downloads"
    sorted_by_type = [
        {"operation": "move", "source": f"{folder}\\{name}", "destination": f"{folder}\\{kind}\\{name}"}
        for name, kind in (("q3.pdf", "documents"), ("cat.jpg", "images"), ("song.mp3", "audio"), ("notes.txt", "documents"))
    ]
    assert describe_action("filesystem.manage", {"operation": "apply_manifest", "manifest": sorted_by_type}) == (
        "Move 4 files in Downloads into 3 folders: audio, documents, images"
    )
    one_folder = [{"operation": "move", "source": "/home/sam/Desktop/a.pdf", "destination": "/home/sam/Desktop/old/a.pdf"}]
    assert describe_action("filesystem.manage", {"operation": "apply_manifest", "manifest": one_folder}) == (
        "Move 1 file in Desktop into old"
    )
    renames = [
        {"operation": "rename", "source": "/home/sam/Desktop/IMG_1.jpg", "destination": "/home/sam/Desktop/beach.jpg"},
        {"operation": "move", "source": "/home/sam/Desktop/b.pdf", "destination": "/home/sam/Desktop/papers/b.pdf"},
    ]
    assert describe_action("filesystem.manage", {"operation": "apply_manifest", "manifest": renames}) == (
        "Move 1 file in Desktop into papers and rename 1 file"
    )


def test_the_console_approval_for_a_manifest_says_the_same():
    from agent_control.config import AppSettings
    from agent_control.tools.registry import build_tool_registry

    registry = build_tool_registry(AppSettings(), "http://127.0.0.1:1")
    manage = next(d for d in registry.definitions if d.name == "filesystem.manage")
    moves = [{"operation": "move", "source": "/home/sam/Downloads/a.pdf", "destination": "/home/sam/Downloads/documents/a.pdf"}]
    assert manage.approval_reason({"operation": "apply_manifest", "manifest": moves}) == "Move 1 file in Downloads into documents."
    assert manage.approval_reason({"operation": "write_text_file", "path": "x.txt"}) is None


def test_a_manifest_names_up_to_seven_folders():
    def moves(count):
        return [{"operation": "move", "source": f"/d/Downloads/f{n}.x", "destination": f"/d/Downloads/k{n}/f{n}.x"} for n in range(count)]

    seven = describe_action("filesystem.manage", {"operation": "apply_manifest", "manifest": moves(7)})
    assert seven.endswith("k0, k1, k2, k3, k4, k5, k6") and "more" not in seven
    nine = describe_action("filesystem.manage", {"operation": "apply_manifest", "manifest": moves(9)})
    assert nine.endswith("k0, k1, k2, k3, k4, k5 and 3 more")
