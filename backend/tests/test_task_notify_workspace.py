"""The folder a task worked in is mentioned only when it holds something the user asked to be made."""

from __future__ import annotations

from agent_control.channels.task_notify import format_task_message
from agent_control.schemas import TaskRecord, TaskStatus

WORKSPACE = r"C:\agent\code_interpreter\task_task_effb904908444de48d2e4a2da794778b"


def _done(answer: str, files_created: list[str], *, tool: str = "code.interpreter") -> TaskRecord:
    return TaskRecord(
        objective="count the lines",
        status=TaskStatus.COMPLETED,
        metadata={
            "synthesized_answer": answer,
            "workspace_dir": WORKSPACE,
            "last_tool_name": tool,
            "last_tool_result": {
                "tool_name": tool,
                "output": {"operation": "run_python", "stdout": "15", "files_created": files_created, "workspace_dir": WORKSPACE},
            },
        },
    )


def test_a_script_that_only_printed_its_answer_leaves_no_workspace_line():
    # a live run answered "15 lines" and then added "Workspace: task_task_..." because the script's own file counted as created
    message = format_task_message(_done("There are 15 lines in total.", ["script.py"]))
    assert message == "There are 15 lines in total."


def test_the_workspace_is_named_when_the_script_made_something_for_the_user():
    message = format_task_message(_done("I made the report.", ["script.py", "report.csv"]))
    assert "Workspace:" in message


def test_the_answer_printed_by_a_script_lists_only_what_it_made_for_the_user():
    task = TaskRecord(
        objective="make a report",
        status=TaskStatus.COMPLETED,
        metadata={
            "last_tool_name": "code.interpreter",
            "last_tool_result": {"output": {
                "operation": "run_python", "stdout": "done", "files_created": ["script.py", "report.csv"], "workspace_dir": WORKSPACE,
            }},
        },
    )
    message = format_task_message(task)
    assert "- report.csv" in message and "- script.py" not in message and "Workspace:" in message


def test_a_script_that_made_nothing_but_its_own_file_shows_no_created_files_or_workspace():
    task = TaskRecord(
        objective="count",
        status=TaskStatus.COMPLETED,
        metadata={
            "last_tool_name": "code.interpreter",
            "last_tool_result": {"output": {"operation": "run_python", "stdout": "15", "files_created": ["script.py"], "workspace_dir": WORKSPACE}},
        },
    )
    message = format_task_message(task)
    assert message == "15"


def test_other_tools_keep_naming_their_workspace():
    message = format_task_message(_done("The site is ready.", [], tool="workspace.manage"))
    assert "Workspace:" in message
