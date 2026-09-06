"""Fulfillment checks for the Operator loop.

Expectations come from objective text only - the plan-derived path
(`plan.postconditions` and tool-name inference) went away with the plan-once
execution path, since nothing creates a PlanModel anymore (docs/HISTORY.md §1.1).

Tests are split accordingly:
- `validate_fulfillment()` for the end-to-end path, driven by objective wording.
- `_postcondition_satisfied()` directly for satisfaction rules whose
  postcondition objective-text inference doesn't produce on its own. Those
  rules are still live and load-bearing (a `run_goal` that stopped early or a
  coding agent still running must NOT count as done) - previously they were
  only reachable in tests via a hand-built plan, which no longer exists.
"""

from __future__ import annotations

from agent_control.orchestration.fulfillment import _postcondition_satisfied, expected_postconditions, validate_fulfillment
from agent_control.schemas import PostconditionType, TaskRecord


def test_browser_state_postcondition_satisfied_from_objective_text() -> None:
    task = TaskRecord(
        objective="Open the browser, search docs, and tell me what you see",
        metadata={
            "last_tool_result": {
                "output": {
                    "browser_url": "https://example.test",
                    "page_title": "Example",
                }
            }
        },
    )

    validation = validate_fulfillment(task)

    assert validation.ok


def test_external_command_postcondition_requires_successful_exit() -> None:
    task = TaskRecord(
        objective="Run a terminal command",
        metadata={
            "last_tool_result": {
                "output": {
                    "terminal_output": [
                        {
                            "content": "failed",
                            "is_final": True,
                            "exit_code": 1,
                        }
                    ]
                }
            }
        },
    )

    validation = validate_fulfillment(task)

    assert not validation.ok
    assert validation.first_gap == "expected_external_command_missing"


def test_desktop_observation_inferred_from_objective_is_unsatisfied_without_evidence() -> None:
    task = TaskRecord(objective="take a screenshot of my desktop", metadata={})

    validation = validate_fulfillment(task)

    assert not validation.ok
    assert validation.first_gap == "expected_desktop_observation_missing"


def test_requested_screenshot_requires_the_image_itself_to_be_delivered() -> None:
    task = TaskRecord(
        objective="Open the page and send me a screenshot",
        metadata={
            "browser_url": "https://example.test",
            "screenshot_path": "/tmp/page.png",
            "artifact_delivery": {
                "delivered": True,
                "operation": "send_latest",
                "path": "/tmp/latest-output.txt",
                "delivery_method": "telegram.sendDocument",
            },
        },
    )

    validation = validate_fulfillment(task)

    assert PostconditionType.SCREENSHOT_DELIVERED in {item.type for item in validation.expected}
    assert PostconditionType.SCREENSHOT_DELIVERED in validation.missing


def test_send_screenshot_satisfies_requested_screenshot_delivery() -> None:
    task = TaskRecord(
        objective="Open the page and send me a screenshot",
        metadata={
            "browser_url": "https://example.test",
            "screenshot_path": "/tmp/page.png",
            "artifact_delivery": {
                "delivered": True,
                "operation": "send_screenshot",
                "path": "/tmp/page.png",
                "delivery_method": "telegram.sendPhoto",
            },
        },
    )

    assert validate_fulfillment(task).ok


def test_run_goal_that_stopped_early_does_not_satisfy_desktop_observation() -> None:
    """A run_goal is a multi-step action loop with a real objective; a
    screenshot existing is not evidence the goal was reached. Only an explicit
    completed=True counts - max_steps exhaustion reports False."""
    task = TaskRecord(
        objective="Use computer to open a folder",
        metadata={
            "last_tool_result": {
                "output": {
                    "operation": "run_goal",
                    "completed": False,
                    "screenshot_path": "C:/tmp/screen.png",
                    "final_summary": "Stopped after max steps.",
                }
            }
        },
    )

    assert not _postcondition_satisfied(task, PostconditionType.DESKTOP_OBSERVATION)


def test_run_goal_missing_completed_field_is_not_satisfied() -> None:
    task = TaskRecord(
        objective="Use computer to open a folder",
        metadata={
            "last_tool_result": {
                "output": {
                    "operation": "run_goal",
                    "screenshot_path": "C:/tmp/screen.png",
                }
            }
        },
    )

    assert not _postcondition_satisfied(task, PostconditionType.DESKTOP_OBSERVATION)


def test_completed_run_goal_satisfies_desktop_observation() -> None:
    task = TaskRecord(
        objective="Use computer to open a folder",
        metadata={
            "last_tool_result": {
                "output": {
                    "operation": "run_goal",
                    "completed": True,
                    "final_summary": "Folder opened.",
                }
            }
        },
    )

    assert _postcondition_satisfied(task, PostconditionType.DESKTOP_OBSERVATION)


def test_running_coding_agent_session_does_not_satisfy_postcondition() -> None:
    task = TaskRecord(
        objective="Use Codex to fix tests",
        metadata={
            "last_tool_result": {
                "output": {
                    "provider": "codex",
                    "status": "running",
                    "session_id": "codex_abc",
                    "returncode": None,
                }
            }
        },
    )

    assert not _postcondition_satisfied(task, PostconditionType.CODING_AGENT_STEP)


def test_completed_coding_agent_session_satisfies_postcondition() -> None:
    task = TaskRecord(
        objective="Use Codex to fix tests",
        metadata={
            "last_tool_result": {
                "output": {
                    "provider": "codex",
                    "status": "completed",
                    "session_id": "codex_abc",
                    "returncode": 0,
                }
            }
        },
    )

    assert _postcondition_satisfied(task, PostconditionType.CODING_AGENT_STEP)


def test_inflected_verb_still_infers_the_postcondition() -> None:
    """The classifier paraphrases the request into `objective`, and exact-token
    matching meant "creating" did not count as "create" - the WORKSPACE_DIR
    obligation silently vanished and a run that wrote nothing while claiming
    otherwise completed unchallenged (docs/E2E_FINDINGS.md P0-2)."""
    paraphrased = TaskRecord(
        objective=(
            "Scaffold a minimal VS Code extension called ybm-dog-facts, creating package.json, "
            "extension.js, and README, and list exactly what was created."
        ),
        metadata={},
    )

    expected = {item.type for item in validate_fulfillment(paraphrased).expected}

    assert PostconditionType.WORKSPACE_DIR in expected
    assert PostconditionType.WORKSPACE_FILES in expected


def test_original_message_keeps_an_obligation_the_paraphrase_dropped() -> None:
    """A paraphrase may add an obligation it makes explicit, but must not be
    able to drop one the user's own words already established."""
    task = TaskRecord(
        objective="Summarize what the extension should contain.",
        metadata={"original_message_text": "Create the real app files in that folder."},
    )

    expected = {item.type for item in validate_fulfillment(task).expected}

    assert PostconditionType.WORKSPACE_DIR in expected
    assert PostconditionType.WORKSPACE_FILES in expected


def test_empty_prepared_workspace_does_not_satisfy_project_creation() -> None:
    task = TaskRecord(
        objective="Scaffold a VS Code extension",
        metadata={"workspace_dir": "C:/work/task", "files": ["C:/work/task/TASK.md"]},
    )

    validation = validate_fulfillment(task)

    assert PostconditionType.WORKSPACE_FILES in validation.missing
    assert validation.first_gap == "expected_workspace_files_missing"


def test_real_changed_file_satisfies_project_file_evidence() -> None:
    task = TaskRecord(
        objective="Scaffold a VS Code extension",
        metadata={
            "workspace_dir": "C:/work/task",
            "changed_files": ["package.json", "extension.js"],
        },
    )

    assert validate_fulfillment(task).ok


def test_starting_a_coding_project_does_not_imply_a_running_preview() -> None:
    task = TaskRecord(
        objective="Use Codex to start a small accessible dog-adoption web app",
        metadata={},
    )

    expected = {item.type for item in validate_fulfillment(task).expected}

    assert PostconditionType.PREVIEW_URL not in expected
    assert PostconditionType.WORKSPACE_FILES in expected


def test_explicitly_launching_a_web_app_still_requires_a_preview_url() -> None:
    task = TaskRecord(objective="Launch the dog web app and give me its URL", metadata={})

    expected = {item.type for item in validate_fulfillment(task).expected}

    assert PostconditionType.PREVIEW_URL in expected


def test_code_interpreter_created_files_satisfy_project_file_evidence() -> None:
    task = TaskRecord(
        objective="Write and run a small local Python script that creates report.json",
        metadata={
            "workspace_dir": "C:/work/task",
            "last_tool_result": {
                "output": {"files_created": ["report.json", "script.py"], "returncode": 0}
            },
        },
    )

    assert validate_fulfillment(task).ok


def test_explicit_project_folder_writes_do_not_require_separate_workspace_metadata() -> None:
    task = TaskRecord(
        objective="Scaffold a VS Code extension in C:/projects/dog-helper",
        metadata={"changed_paths": ["C:/projects/dog-helper/package.json"]},
    )

    assert validate_fulfillment(task).ok


def test_named_coding_provider_requires_coding_agent_evidence_even_after_another_tool() -> None:
    task = TaskRecord(
        objective="Ask Codex to build a VS Code extension",
        metadata={
            "workspace_dir": "C:/work/task",
            "changed_files": ["package.json", "extension.js"],
            "coding_agent_session": {
                "provider": "codex",
                "status": "completed",
                "returncode": 0,
            },
            # A later inspection/preview result must not erase provider proof.
            "last_tool_result": {"output": {"operation": "inspect_folder"}},
        },
    )

    expected = {item.type for item in validate_fulfillment(task).expected}

    assert PostconditionType.CODING_AGENT_STEP in expected
    assert validate_fulfillment(task).ok


def test_inflection_expansion_does_not_match_unrelated_words() -> None:
    """Inflections are generated from the known trigger vocabulary, never by
    stemming arbitrary input - a false positive invents a postcondition nothing
    can satisfy, which makes a finished task loop on an unclosable gap."""
    task = TaskRecord(objective="Tell me the creation date of that spreadsheet.", metadata={})

    assert validate_fulfillment(task).expected == ()


def test_embedded_filesystem_path_does_not_fabricate_a_postcondition() -> None:
    """Regression guard (docs/HISTORY.md): objectives routinely embed a literal
    path whose last segment contains a trigger word. A folder named
    '..._search' must not infer a BROWSER_STATE expectation nothing can
    satisfy - that produced a gap the loop could never close."""
    task = TaskRecord(
        objective=r"look in the folder C:\Users\me\AppData\Local\Temp\operator_loop_filesystem_search and read the file",
        metadata={},
    )

    validation = validate_fulfillment(task)

    assert PostconditionType.BROWSER_STATE not in {item.type for item in validation.expected}


def test_natural_filesystem_search_does_not_require_browser_state() -> None:
    task = TaskRecord(
        objective="Search under my documents for the renamed career file, read it, and tell me its marker.",
        metadata={},
    )

    expected = {item.type for item in validate_fulfillment(task).expected}

    assert PostconditionType.SOURCE_CONTENT in expected
    assert PostconditionType.BROWSER_STATE not in expected


def test_explicit_online_search_still_requires_browser_state() -> None:
    task = TaskRecord(objective="Search online for the official installer page.", metadata={})

    expected = {item.type for item in validate_fulfillment(task).expected}

    assert PostconditionType.BROWSER_STATE in expected


def test_embedded_posix_path_does_not_fabricate_a_postcondition() -> None:
    task = TaskRecord(
        objective=(
            "look in the folder "
            "/tmp/ybm_scenario_scratch/operator_loop_filesystem_search "
            "and read the file"
        ),
        metadata={},
    )

    validation = validate_fulfillment(task)

    assert PostconditionType.BROWSER_STATE not in {
        item.type for item in validation.expected
    }


def test_daily_change_in_adapter_request_does_not_fabricate_schedule_postcondition() -> None:
    task = TaskRecord(
        objective=(
            "Create an adapter for a stock API that fetches the latest price, "
            "daily change, and basic quote information."
        ),
        metadata={"adapter_dir": "/tmp/adapters/stock_quotes"},
    )

    validation = validate_fulfillment(task)

    assert PostconditionType.SCHEDULE_CREATED not in {item.type for item in validation.expected}
    assert validation.ok


def test_daily_schedule_still_requires_created_schedule() -> None:
    task = TaskRecord(objective="Create a daily schedule to check the stock price", metadata={})

    validation = validate_fulfillment(task)

    assert PostconditionType.SCHEDULE_CREATED in {item.type for item in validation.expected}
    assert validation.first_gap == "expected_schedule_created_missing"


def test_adapter_proposal_does_not_also_require_generic_workspace() -> None:
    task = TaskRecord(
        objective="Create an adapter proposal with code, manifest, and tests in the adapter cache",
        metadata={"adapter_dir": "C:/cache/example_adapter"},
    )

    validation = validate_fulfillment(task)

    assert validation.ok
    assert {item.type for item in validation.expected} == {PostconditionType.ADAPTER_PROPOSAL}


def test_adapter_review_boundary_does_not_invent_source_reading() -> None:
    task = TaskRecord(
        objective="Create the adapter proposal.",
        metadata={
            "original_message_text": (
                "Compare a local career evidence pack with a profile export. Create a reusable "
                "adapter proposal named linkedin_evidence_compare, then tell me exactly what "
                "review is still required."
            ),
            "adapter_dir": "C:/cache/linkedin_evidence_compare",
        },
    )

    validation = validate_fulfillment(task)

    assert validation.ok
    assert PostconditionType.SOURCE_CONTENT not in {
        item.type for item in validation.expected
    }


def test_writing_a_named_file_does_not_require_a_code_workspace() -> None:
    task = TaskRecord(
        objective="Write a grounded LinkedIn brief to report.md and send the file",
        metadata={"changed_paths": ["C:/output/report.md"], "artifact_delivery": {"delivered": True}},
    )

    validation = validate_fulfillment(task)

    assert PostconditionType.WORKSPACE_DIR not in {item.type for item in validation.expected}


def test_request_to_send_a_file_requires_delivery_evidence() -> None:
    task = TaskRecord(
        objective="Write a grounded LinkedIn brief to report.md and send me that exact file",
        metadata={"changed_paths": ["C:/output/report.md"]},
    )

    validation = validate_fulfillment(task)

    assert PostconditionType.ARTIFACT_DELIVERED in validation.missing
    assert validation.first_gap == "expected_artifact_delivery_missing"


def test_request_to_send_an_update_does_not_require_file_delivery() -> None:
    task = TaskRecord(objective="Send me an update every five minutes", metadata={})

    assert PostconditionType.ARTIFACT_DELIVERED not in {
        item.type for item in validate_fulfillment(task).expected
    }


def test_inspecting_every_evidence_file_requires_content_not_just_a_listing() -> None:
    task = TaskRecord(
        objective="Inspect every career-evidence file and write a grounded brief",
        metadata={
            "operator_history": [
                {
                    "tool_name": "filesystem.manage",
                    "status": "succeeded",
                    "input": {"operation": "inspect_folder", "root": "C:/career"},
                    "output_summary": "Entries:\n- [file] one.md\n- [file] two.csv\n- [file] three.txt",
                }
            ]
        },
    )

    validation = validate_fulfillment(task)

    assert PostconditionType.SOURCE_CONTENT in validation.missing
    assert validation.first_gap == "expected_source_content_missing"


def test_reading_every_listed_file_satisfies_source_content_evidence() -> None:
    history = [
        {
            "tool_name": "filesystem.manage",
            "status": "succeeded",
            "input": {"operation": "inspect_folder", "root": "C:/career"},
            "output_summary": "Entries:\n- [file] one.md\n- [file] two.csv\n- [file] three.txt",
        }
    ]
    history.extend(
        {
            "tool_name": "filesystem.manage",
            "status": "succeeded",
            "input": {"operation": "read_file", "path": f"C:/career/{name}"},
            "output_summary": f"Read {name}.",
        }
        for name in ("one.md", "two.csv", "three.txt")
    )
    task = TaskRecord(
        objective="Inspect every career-evidence file",
        metadata={"operator_history": history},
    )

    assert validate_fulfillment(task).ok


# ---- expected_postconditions: a classifier's declared list (docs/ROADMAP.md
# "Finish the Proof") beats keyword-inferred guesses when present ----------

def test_expected_postconditions_prefers_a_declared_list_over_keyword_inference() -> None:
    """The objective's own wording would infer nothing here ("summarize
    this" has no construction/delivery verb the keyword matcher looks
    for) - a declared postcondition is the only reason this task has any
    obligation at all."""
    task = TaskRecord(
        objective="summarize this",
        metadata={"expected_postconditions": ["document_summary"]},
    )

    expected = expected_postconditions(task)

    assert [item.type for item in expected] == [PostconditionType.DOCUMENT_SUMMARY]


def test_expected_postconditions_falls_back_to_keyword_inference_when_nothing_declared() -> None:
    """Every classifier today leaves expected_postconditions empty
    (schemas.py) - this is the path every real task still takes."""
    task = TaskRecord(objective="Create a new script called report.py", metadata={})

    expected = expected_postconditions(task)

    assert PostconditionType.WORKSPACE_FILES in [item.type for item in expected]


def test_expected_postconditions_ignores_a_garbage_declared_value_and_falls_back() -> None:
    """A hallucinated postcondition type must not silently produce zero
    obligations for a task whose wording would otherwise infer one - the
    per-item filter (schemas.py's own validator already drops these before
    they reach here, this is the second, defensive layer) falls through to
    the keyword guess rather than leaving the task with no safety net."""
    task = TaskRecord(
        objective="Create a new script called report.py",
        metadata={"expected_postconditions": ["not_a_real_type"]},
    )

    expected = expected_postconditions(task)

    assert PostconditionType.WORKSPACE_FILES in [item.type for item in expected]


def test_expected_postconditions_declared_list_is_checked_the_same_way_as_inferred() -> None:
    """A declared postcondition is a real PlanPostcondition, not a special
    case - validate_fulfillment doesn't need to know where it came from."""
    task = TaskRecord(
        objective="build me a slide deck",
        metadata={
            "expected_postconditions": ["presentation_file"],
            "last_tool_result": {"output": {"path": "C:/out/deck.pptx"}},
        },
    )

    assert validate_fulfillment(task).ok
