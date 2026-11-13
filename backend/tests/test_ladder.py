"""The ladder: what a task has proven, and what to try next (orchestration/ladder.py), and how the worker uses it."""

from __future__ import annotations

import pytest

from agent_control.config import AppSettings, CapabilityPolicy
from agent_control.orchestration import StaticToolAdapter, TaskWorker, ToolExecutor
from agent_control.orchestration.ladder import (
    CODE_SEARCH_TEXT,
    FEASIBILITY_TEXT,
    exhausted,
    move_scope_gap,
    needs_feasibility_check,
    next_hint,
    proven_absent,
    write_receipt_gap,
)
from agent_control.orchestration.worker import CHECK_ENTRY_STRATEGY, _claude_code_decision
from agent_control.policy import PolicyEngine
from agent_control.schemas import Capability, OperatorAction, OperatorDecision, RiskLevel, TaskStatus
from agent_control.tools.spec import ToolDefinition
from agent_control.tools.system_inventory import NOT_ANYWHERE_MARK
from helpers import make_repos
from test_worker_operator_loop import QueueOperator

PATH = r"C:\for fun\YBM\.agent_control\e2e_fixtures\heldout3\no-such-report.txt"
REQUEST = f"Read {PATH} and tell me what it says."


def _read_failed(path: str = PATH, error: str = "filesystem operation failed: file does not exist: x") -> dict:
    return {"tool_name": "filesystem.manage", "input": {"operation": "read_file", "path": path}, "status": "failed", "error": error}


def _find(summary: str, query: str = "annual budget") -> dict:
    return {"tool_name": "system.inventory", "input": {"operation": "find_files", "query": query}, "status": "succeeded",
            "output_summary": summary}


def _empty(query: str = "annual budget") -> dict:
    return _find(f"No file matching '{query}' was found.", query)


COMPLETE = f"No file matching 'annual budget' was found. I looked through all 9,000 folders under C:\\, {NOT_ANYWHERE_MARK} (it may be elsewhere)."
INCOMPLETE = "No file matching 'annual budget' was found. I only had time to look through 40 folders under C:\\, so this does not prove it is not there."


# --- what is proven absent ---------------------------------------------------------------------------------------------


def test_a_failed_read_of_the_path_the_request_names_proves_it_is_not_there_even_with_spaces_in_the_path():
    assert proven_absent(REQUEST, [_read_failed()]) == f"{PATH} does not exist."


def test_a_failed_read_of_some_other_path_proves_nothing_about_the_one_asked_for():
    assert proven_absent(REQUEST, [_read_failed(r"C:\somewhere\else.txt")]) is None


def test_a_read_that_failed_for_another_reason_proves_nothing():
    assert proven_absent(REQUEST, [_read_failed(error="Access is denied")]) is None


def test_a_finished_search_for_words_the_request_uses_proves_it_is_not_there():
    request = "Where is my annual budget spreadsheet?"
    assert proven_absent(request, [_find(COMPLETE)]) == COMPLETE[COMPLETE.index(" I looked through"):].strip()


def test_a_search_that_ran_out_of_time_proves_nothing():
    assert proven_absent("Where is my annual budget?", [_find(INCOMPLETE)]) is None


def test_a_finished_search_for_something_the_request_did_not_ask_about_proves_nothing():
    assert proven_absent("Where is my tax return?", [_find(COMPLETE)]) is None


# --- the result is where the request said -------------------------------------------------------------------------------------


def test_a_file_written_outside_the_place_the_request_names_is_a_gap():
    request = r"Write my cat's name into cat.txt in C:\for fun\YBM\fixtures\work."
    gap = write_receipt_gap(request, [r"C:\for fun\YBM\cat.txt"])
    assert gap and r"C:\for fun\YBM\cat.txt" in gap and "do not claim" in gap


def test_a_file_written_inside_the_named_place_is_fine():
    request = r"Write my cat's name into cat.txt in C:\for fun\YBM\fixtures\work."
    assert write_receipt_gap(request, [r"C:\for fun\YBM\fixtures\work\cat.txt"]) is None


def test_a_request_that_names_no_place_is_not_checked():
    assert write_receipt_gap("Write my cat's name into cat.txt", [r"C:\for fun\YBM\cat.txt"]) is None
    assert write_receipt_gap(r"Write to C:\a\b.txt", []) is None


def test_the_named_file_itself_counts_as_the_named_place():
    assert write_receipt_gap(r"Save it as C:\notes\todo.txt", [r"C:\notes\todo.txt"]) is None


def test_moving_files_of_a_kind_the_request_did_not_name_is_a_gap_that_says_how_to_put_them_back():
    undo = {"root": "C:/r", "moves": [
        {"from": r"C:\r\pictures\a.png", "to": r"C:\r\a.png"}, {"from": r"C:\r\txt\keep.txt", "to": r"C:\r\keep.txt"}]}
    gap = move_scope_gap("Move the .png files in C:\\r into pictures. Leave everything else where it is.", undo)
    assert gap and r"C:\r\txt\keep.txt back to C:\r\keep.txt" in gap and "a.png" not in gap


def test_moving_only_the_named_kind_is_fine_and_a_request_naming_no_kind_is_not_checked():
    undo = {"root": "C:/r", "moves": [{"from": r"C:\r\pictures\a.png", "to": r"C:\r\a.png"}]}
    assert move_scope_gap("Move the .png files into pictures", undo) is None
    assert move_scope_gap("Sort my downloads", {"root": "C:/r", "moves": [{"from": r"C:\r\txt\k.txt", "to": r"C:\r\k.txt"}]}) is None
    assert move_scope_gap("Move the .png files", None) is None


# --- what to try next ------------------------------------------------------------------------------------------------------------


def test_a_proven_absence_is_told_once_and_ends_the_searching():
    hint = next_hint(REQUEST, [_read_failed()], set(), code_enabled=True)
    assert hint.id == "absent" and "Do not search for it again" in hint.text
    assert next_hint(REQUEST, [_read_failed()], {"absent"}, code_enabled=True) is None


def test_a_finished_empty_search_is_not_repeated_with_another_spelling():
    # The search was for words the request does not use (the model chose its own query), so it is not proof about the request,
    # but it is still a finished search and running it again with another spelling will not change it.
    hint = next_hint("Where is my budget file?", [_find(COMPLETE)], set(), code_enabled=True)
    assert hint.id == "complete" and "Do not run it again" in hint.text
    assert next_hint("Where is my annual budget?", [_find(COMPLETE)], set(), code_enabled=True).id == "absent"


def test_two_empty_lookups_point_at_writing_a_script_once():
    steps = [_empty("budget"), _empty("annual")]
    hint = next_hint("find my annual thing", steps, set(), code_enabled=True)
    assert hint.id == "code_search" and hint.text == CODE_SEARCH_TEXT and "os.walk" in hint.text and "run_python" in hint.text
    assert next_hint("find my annual thing", steps, {"code_search"}, code_enabled=True) is None


def test_no_script_is_suggested_when_the_code_tool_is_off_or_already_used():
    steps = [_empty("budget"), _empty("annual")]
    assert next_hint("find my annual thing", steps, set(), code_enabled=False) is None
    used = steps + [{"tool_name": "code.interpreter", "input": {"operation": "run_python"}, "status": "succeeded", "output_summary": "x"}]
    assert next_hint("find my annual thing", used, set(), code_enabled=True) is None


def test_a_locked_folder_is_said_to_be_skipped():
    steps = [{"tool_name": "filesystem.manage", "input": {"operation": "search"}, "status": "failed",
              "error": "filesystem operation failed: [WinError 5] Access is denied: 'C:\\\\Users\\\\x\\\\My Music'"}]
    assert next_hint("find a thing", steps, set(), code_enabled=False).id == "locked"


def test_three_failures_in_a_row_suggest_a_script():
    failing = {"tool_name": "browser.open", "input": {}, "status": "failed", "error": "boom"}
    assert next_hint("do it", [failing, failing, failing], set(), code_enabled=True).id == "try_code"


def test_nothing_is_said_when_nothing_has_happened():
    assert next_hint("hello", [], set(), code_enabled=True) is None


def test_the_ladder_is_exhausted_after_the_script_also_failed_twice_or_many_steps_came_to_nothing():
    code_failed = {"tool_name": "code.interpreter", "input": {"operation": "run_python"}, "status": "failed", "error": "x"}
    assert exhausted([code_failed, code_failed]) is True
    assert exhausted([_empty(), _empty()]) is False
    assert exhausted([_empty() for _ in range(8)]) is True
    assert exhausted([]) is False


# --- how the worker uses it ---------------------------------------------------------------------------------------------------------


class _Seen(QueueOperator):
    def __init__(self, decisions):
        super().__init__(decisions)
        self.histories: list[list[dict]] = []

    async def decide(self, objective, config_context, history, **kwargs):
        self.histories.append([dict(entry) for entry in history])
        return await super().decide(objective, config_context, history, **kwargs)


class _NoAuditor:
    async def audit(self, *args, **kwargs):  # pragma: no cover - the test fails if this is ever called
        raise AssertionError("the auditor must not be asked about a proven absence")


def _settings() -> AppSettings:
    return AppSettings(
        _env_file=None,
        capabilities={
            Capability.FILESYSTEM_READ: CapabilityPolicy(enabled=True, requires_approval=False, max_risk_level=RiskLevel.LOW),
            Capability.TERMINAL_RUN: CapabilityPolicy(enabled=True, requires_approval=False, max_risk_level=RiskLevel.HIGH),
        },
    )


def _worker(repos, audit, operator, *, auditor=None, with_code=True):
    settings = _settings()
    definitions = {
        "filesystem.manage": ToolDefinition(name="filesystem.manage", capability=Capability.FILESYSTEM_READ, enabled=True, description="files"),
    }
    adapters = {"filesystem.manage": StaticToolAdapter({"summary": "ok"})}
    if with_code:
        definitions["code.interpreter"] = ToolDefinition(
            name="code.interpreter", capability=Capability.TERMINAL_RUN, enabled=True, description="python")
        adapters["code.interpreter"] = StaticToolAdapter({"summary": "ok"})
    executor = ToolExecutor(PolicyEngine(settings, audit), repos, audit, adapters=adapters, tool_definitions=definitions)
    return TaskWorker(repos, audit, executor=executor, operator=operator, auditor=auditor)


def _task_with_history(repos, message, history, **metadata):
    task = repos.tasks.create(message, metadata={"original_message_text": message, **metadata})
    return repos.tasks.update_metadata(
        task.id, {**task.metadata, "operator_loop": True, "operator_history": history}, TaskStatus.RUNNING)


@pytest.mark.asyncio
async def test_a_done_after_a_proven_absence_is_accepted_without_asking_the_auditor(tmp_path):
    repos, audit = make_repos(tmp_path)
    history = [
        {"tool_name": "filesystem.manage", "input": {"operation": "inspect_folder", "path": "C:/x"}, "status": "succeeded",
         "output_summary": "Found 2 item(s)."},
        _read_failed(),
    ]
    task = _task_with_history(repos, REQUEST, history)
    operator = _Seen([OperatorDecision(action=OperatorAction.DONE, final_answer="That file does not exist, so there is nothing to read.")])
    worker = _worker(repos, audit, operator, auditor=_NoAuditor())
    done = await worker.process_task(task.id)
    assert done.status == TaskStatus.COMPLETED
    assert "does not exist" in done.metadata["synthesized_answer"]
    # The model was told it is settled before it decided.
    assert any(e["tool_name"] == CHECK_ENTRY_STRATEGY and e["hint_id"] == "absent" for e in operator.histories[0])


@pytest.mark.asyncio
async def test_two_empty_searches_make_the_next_decision_see_how_to_write_a_script(tmp_path):
    repos, audit = make_repos(tmp_path)
    task = _task_with_history(repos, "Find my annual thing", [_empty("budget"), _empty("annual")])
    operator = _Seen([OperatorDecision(action=OperatorAction.DONE, final_answer="I could not find it.")])
    await _worker(repos, audit, operator).process_task(task.id)
    hints = [e for e in operator.histories[0] if e["tool_name"] == CHECK_ENTRY_STRATEGY]
    assert [h["hint_id"] for h in hints] == ["code_search"] and "run_python" in hints[0]["error"]


@pytest.mark.asyncio
async def test_no_script_is_suggested_when_the_code_tool_is_not_available(tmp_path):
    repos, audit = make_repos(tmp_path)
    task = _task_with_history(repos, "Find my annual thing", [_empty("budget"), _empty("annual")])
    operator = _Seen([OperatorDecision(action=OperatorAction.DONE, final_answer="I could not find it.")])
    await _worker(repos, audit, operator, with_code=False).process_task(task.id)
    assert not [e for e in operator.histories[0] if e["tool_name"] == CHECK_ENTRY_STRATEGY]


@pytest.mark.asyncio
async def test_a_file_written_somewhere_the_request_did_not_say_sends_the_model_back_twice_then_lets_it_report(tmp_path):
    repos, audit = make_repos(tmp_path)
    request = r"Write my cat's name into cat.txt in C:\for fun\YBM\fixtures\work."
    history = [{"tool_name": "filesystem.manage", "input": {"operation": "write_text_file"}, "status": "succeeded",
                "output_summary": r"Wrote text file C:\for fun\YBM\cat.txt."}]
    task = _task_with_history(repos, request, history, changed_paths=[r"C:\for fun\YBM\cat.txt"])
    answer = OperatorDecision(action=OperatorAction.DONE, final_answer=r"I put it in C:\for fun\YBM\cat.txt, not in the folder you named.")
    operator = _Seen([answer, answer, answer])
    worker = _worker(repos, audit, operator)
    first = await worker.process_task(task.id)
    assert first.status == TaskStatus.RUNNING
    assert first.metadata["operator_history"][-1]["hint_id"] == "receipt1" and "elsewhere" in first.metadata["operator_history"][-1]["error"]
    second = await worker.process_task(first.id)
    assert second.status == TaskStatus.RUNNING and second.metadata["operator_history"][-1]["hint_id"] == "receipt2"
    third = await worker.process_task(second.id)
    assert third.status == TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_when_the_local_model_has_run_out_of_ways_the_user_is_offered_claude_code_and_nothing_is_run(tmp_path, monkeypatch):
    repos, audit = make_repos(tmp_path)
    code_failed = {"tool_name": "code.interpreter", "input": {"operation": "run_python"}, "status": "failed", "error": "boom"}
    task = _task_with_history(repos, "Find my annual thing", [code_failed, code_failed])
    operator = _Seen([])
    worker = _worker(repos, audit, operator)
    monkeypatch.setattr(worker, "_claude_code_ready", lambda: True)
    asked = await worker.process_task(task.id)
    assert asked.status == TaskStatus.CLARIFYING and operator.calls == 0
    assert asked.metadata["claude_code_offer"]["state"] == "pending"
    assert "hand it to Claude Code" in asked.metadata["clarifying_question"]


@pytest.mark.asyncio
async def test_the_offer_is_not_made_when_claude_code_is_not_installed(tmp_path, monkeypatch):
    repos, audit = make_repos(tmp_path)
    code_failed = {"tool_name": "code.interpreter", "input": {"operation": "run_python"}, "status": "failed", "error": "boom"}
    task = _task_with_history(repos, "Find my annual thing", [code_failed, code_failed])
    operator = _Seen([OperatorDecision(action=OperatorAction.DONE, final_answer="I could not do it.")])
    worker = _worker(repos, audit, operator)
    monkeypatch.setattr(worker, "_claude_code_ready", lambda: False)
    result = await worker.process_task(task.id)
    assert result.status != TaskStatus.CLARIFYING and operator.calls == 1


def _answered(repos, task, answer, *, state="pending"):
    question = "Do you want me to hand it to Claude Code?"
    return repos.tasks.update_metadata(task.id, {
        **task.metadata, "claude_code_offer": {"question": question, "state": state},
        "answered_clarifying_question": question, "clarification_answer": answer,
    }, TaskStatus.RUNNING)


@pytest.mark.asyncio
async def test_a_yes_becomes_a_claude_code_call_that_carries_the_request_and_what_was_tried(tmp_path):
    repos, audit = make_repos(tmp_path)
    task = _task_with_history(repos, "Find my annual thing", [_empty("budget"), _empty("annual")])
    task = _answered(repos, task, "yes")
    worker = _worker(repos, audit, _Seen([]))
    decision = worker._ladder_step(task, list(task.metadata["operator_history"]), "step_1")
    assert isinstance(decision, OperatorDecision) and decision.tool_name == "coding.agent"
    assert decision.tool_input["provider"] == "claude_code" and decision.tool_input["operation"] == "run_goal"
    objective = decision.tool_input["objective"]
    assert "Find my annual thing" in objective and "system.inventory:find_files" in objective and "No file matching" in objective


@pytest.mark.asyncio
async def test_a_no_stops_the_offer_for_good_and_tells_the_model_to_answer_with_what_it_has(tmp_path):
    repos, audit = make_repos(tmp_path)
    task = _answered(repos, _task_with_history(repos, "Find my annual thing", [_empty("budget")]), "no")
    worker = _worker(repos, audit, _Seen([]))
    result = worker._ladder_step(task, list(task.metadata["operator_history"]), "step_1")
    assert result.status == TaskStatus.RUNNING and result.metadata["claude_code_offer"]["state"] == "declined"
    assert result.metadata["operator_history"][-1]["hint_id"] == "declined"


@pytest.mark.asyncio
async def test_only_a_plain_yes_hands_the_task_over(tmp_path):
    repos, audit = make_repos(tmp_path)
    task = _answered(repos, _task_with_history(repos, "Find my annual thing", []), "yes, but keep my files private")
    result = _worker(repos, audit, _Seen([]))._ladder_step(task, [], "step_1")
    assert not isinstance(result, OperatorDecision) and result.metadata["claude_code_offer"]["state"] == "declined"


def test_the_hand_off_text_is_bounded_and_plain():
    from agent_control.schemas import TaskRecord

    task = TaskRecord(objective="x", metadata={"original_message_text": "Find my annual thing"})
    history = [{"tool_name": "system.inventory", "input": {"operation": "find_files"}, "status": "succeeded", "output_summary": "n" * 1000}
               for _ in range(30)]
    objective = _claude_code_decision(task, history).tool_input["objective"]
    assert objective.count("- system.inventory:find_files") == 10 and len(objective) < 3000


def test_a_longer_folder_name_that_merely_starts_like_the_changed_ones_parent_is_not_a_match():
    assert write_receipt_gap(r"Save it in C:\a\bcd", [r"C:\a\b\x.txt"]) is not None
    assert write_receipt_gap(r"Save it in C:\a\b.", [r"C:\a\b\x.txt"]) is None


def test_one_search_that_stopped_early_already_points_at_a_script():
    from agent_control.tools.filesystem_manage import SEARCH_STOPPED_MARK

    step = {"tool_name": "filesystem.manage", "input": {"operation": "search"}, "status": "succeeded",
            "output_summary": f"Found 0 result(s) for 'x' under D:/. {SEARCH_STOPPED_MARK} 30 seconds, after 90,000 files and folders."}
    hint = next_hint("find x", [step], set(), code_enabled=True)
    assert hint.id == "code_search"


def test_a_search_that_ran_out_of_time_inside_one_folder_first_points_at_the_everywhere_search_then_at_a_script():
    from agent_control.tools.filesystem_manage import SEARCH_STOPPED_MARK

    stopped = {"tool_name": "filesystem.manage", "input": {"operation": "search"}, "status": "succeeded",
               "output_summary": f"Found 0 result(s) for 'x' under C:/. {SEARCH_STOPPED_MARK} 20 seconds, after 90,000 files and folders."}
    first = next_hint("find x", [stopped], set(), code_enabled=True, inventory_enabled=True)
    assert first.id == "find_files" and "system.inventory" in first.text
    inventory_empty = _find("No file matching 'x' was found. I only had time to look through 40 folders under C:\\, so this does not prove it is not there.", "x")
    second = next_hint("find x", [stopped, inventory_empty], {"find_files"}, code_enabled=True, inventory_enabled=True)
    assert second.id == "code_search"
    # With the inventory tool off, the script is the next rung straight away.
    assert next_hint("find x", [stopped], set(), code_enabled=True, inventory_enabled=False).id == "code_search"


def test_the_script_the_ladder_suggests_looks_at_other_drives_before_c_and_stops_itself():
    assert "d != 'C:" in CODE_SEARCH_TEXT and "time.time() - start > 120" in CODE_SEARCH_TEXT and "looked through" in CODE_SEARCH_TEXT


# --- a question that is really "I cannot do that" ---------------------------------------------------------------------------


def test_the_first_question_on_a_request_no_tool_matches_is_checked_for_feasibility_once():
    assert needs_feasibility_check(set(), route="unknown", questions_asked=0) is True
    assert needs_feasibility_check({"feasibility"}, route="unknown", questions_asked=0) is False
    assert needs_feasibility_check(set(), route="unknown", questions_asked=1) is False
    assert needs_feasibility_check(set(), route="filesystem.manage", questions_asked=0) is False


@pytest.mark.asyncio
async def test_a_first_question_about_something_no_tool_can_do_sends_the_model_back_to_check_then_lets_it_say_so(tmp_path):
    repos, audit = make_repos(tmp_path)
    task = _task_with_history(
        repos, "Order me a pizza from the place on the corner.", [],
        orchestration_intent={"route": "unknown", "reasoning": "needs an external action"},
    )
    operator = _Seen([
        OperatorDecision(action=OperatorAction.ASK_USER, question="Which restaurant on the corner?"),
        OperatorDecision(action=OperatorAction.DONE, final_answer="I cannot order food. I can look up a menu if you give me the web address."),
    ])
    worker = _worker(repos, audit, operator)
    first = await worker.process_task(task.id)
    assert first.status == TaskStatus.RUNNING and first.metadata["operator_history"][-1]["hint_id"] == "feasibility"
    assert first.metadata["operator_history"][-1]["error"] == FEASIBILITY_TEXT
    second = await worker.process_task(first.id)
    assert second.status == TaskStatus.COMPLETED and "cannot order food" in second.metadata["synthesized_answer"]


@pytest.mark.asyncio
async def test_a_question_the_model_still_asks_after_the_check_goes_to_the_user(tmp_path):
    repos, audit = make_repos(tmp_path)
    task = _task_with_history(
        repos, "Clean up that folder", [],
        orchestration_intent={"route": "unknown", "reasoning": "x"},
    )
    ask = OperatorDecision(action=OperatorAction.ASK_USER, question="Which folder do you mean?")
    worker = _worker(repos, audit, _Seen([ask, ask]))
    first = await worker.process_task(task.id)
    second = await worker.process_task(first.id)
    assert second.status == TaskStatus.CLARIFYING


@pytest.mark.asyncio
async def test_a_task_the_classifier_matched_to_a_tool_is_not_second_guessed(tmp_path):
    repos, audit = make_repos(tmp_path)
    task = _task_with_history(
        repos, "Move my pdfs", [], orchestration_intent={"route": "filesystem.manage", "reasoning": "x"},
    )
    worker = _worker(repos, audit, _Seen([OperatorDecision(action=OperatorAction.ASK_USER, question="Which folder?")]))
    assert (await worker.process_task(task.id)).status == TaskStatus.CLARIFYING


@pytest.mark.asyncio
async def test_a_question_asked_after_a_web_search_that_found_nothing_usable_is_checked_too(tmp_path):
    repos, audit = make_repos(tmp_path)
    searched = [{"tool_name": "web.search", "input": {"operation": "search", "query": "pizza"}, "status": "succeeded",
                 "output_summary": "Found 5 results."}]
    task = _task_with_history(repos, "Order me a pizza from the place on the corner.", searched,
                              orchestration_intent={"route": "unknown", "reasoning": "x"})
    ask = OperatorDecision(action=OperatorAction.ASK_USER, question="Which pizza place?")
    first = await _worker(repos, audit, _Seen([ask])).process_task(task.id)
    assert first.status == TaskStatus.RUNNING and first.metadata["operator_history"][-1]["hint_id"] == "feasibility"


# --- a persona update replaces the whole document: not before it has been read ---------------------------------------------------

from agent_control.orchestration.worker import _coding_agent_requested, _is_blind_persona_replace  # noqa: E402


def test_a_persona_update_without_reading_the_persona_first_is_refused():
    assert _is_blind_persona_replace("persona.manage", {"operation": "update", "content": "x"}, []) is True
    read = [{"tool_name": "persona.manage", "input": {"operation": "get"}, "status": "succeeded"}]
    assert _is_blind_persona_replace("persona.manage", {"operation": "update", "content": "x"}, read) is False
    failed_read = [{"tool_name": "persona.manage", "input": {"operation": "get"}, "status": "failed"}]
    assert _is_blind_persona_replace("persona.manage", {"operation": "update", "content": "x"}, failed_read) is True
    assert _is_blind_persona_replace("persona.manage", {"operation": "get"}, []) is False
    assert _is_blind_persona_replace("memory.manage", {"operation": "remember"}, []) is False


@pytest.mark.asyncio
async def test_a_refused_persona_update_tells_the_model_to_use_memory_for_a_fact_about_the_user(tmp_path):
    repos, audit = make_repos(tmp_path)
    task = _task_with_history(repos, "Remember that my cat is called Pixel", [])
    update = OperatorDecision(action=OperatorAction.CALL_TOOL, tool_name="persona.manage",
                              tool_input={"operation": "update", "content": "cat is Pixel"}, risk_level=RiskLevel.LOW)
    refused = await _worker(repos, audit, _Seen([update])).process_task(task.id)
    last = refused.metadata["operator_history"][-1]
    assert last["status"] == "denied" and "memory.manage with operation remember" in last["error"]


def test_a_hand_off_the_user_agreed_to_is_a_requested_coding_agent():
    from agent_control.schemas import TaskRecord

    accepted = TaskRecord(objective="x", metadata={"claude_code_offer": {"state": "accepted", "question": "q"}})
    call = {"operation": "start", "provider": "claude_code", "objective": "do it"}
    assert _coding_agent_requested(accepted, call) is True
    pending = TaskRecord(objective="x", metadata={"claude_code_offer": {"state": "pending", "question": "q"}})
    assert _coding_agent_requested(pending, call) is False


# --- a path that came from an earlier turn ----------------------------------------------------------------------------------------------------

from agent_control.orchestration.ladder import UNGROUNDED_TEXT, facts_block, ungrounded_paths  # noqa: E402

STALE = r"D:\old\no-file-here.txt"
RESUME = r"C:\Users\o\Downloads\resume.docx"
MEMORY = (
    "Remembered facts:\n- [profile] Name: Oney\n- [note] my resume is " + RESUME + "\n"
    "User Oney. Earlier: read " + STALE + "\nRecent turns:\n- user: read " + STALE
)


def test_the_facts_block_is_only_the_remembered_facts_not_the_conversation_summary():
    block = facts_block(MEMORY)
    assert "resume.docx" in block and "no-file-here" not in block and "Recent turns" not in block
    assert facts_block(None) == "" and facts_block("just a summary") == ""


def test_a_path_from_an_earlier_turn_is_not_grounded_but_the_request_a_result_or_a_remembered_fact_is():
    stale = {"operation": "read_file", "path": STALE}
    assert ungrounded_paths("Remember that my cat is Miso", stale, [], MEMORY) == [STALE]
    assert ungrounded_paths("Read " + STALE, stale, [], MEMORY) == []
    found = [{"tool_name": "system.inventory", "output_summary": "Found: " + STALE, "input": {}}]
    assert ungrounded_paths("find my notes", stale, found, MEMORY) == []
    assert ungrounded_paths("open my resume", {"operation": "read_file", "path": RESUME}, [], MEMORY) == []


def test_a_folder_the_request_names_grounds_a_file_inside_it_and_an_alias_is_never_checked():
    assert ungrounded_paths(r"Tidy C:\work\reports", {"operation": "read_file", "path": r"C:\work\reports\q1.txt"}, [], "") == []
    assert ungrounded_paths(r"Tidy C:\work\reports\q1", {"operation": "inspect_folder", "path": r"C:\work\reports"}, [], "") == []
    assert ungrounded_paths("find my resume", {"operation": "search", "root": "desktop", "query": "resume"}, [], MEMORY) == []
    manifest = {"operation": "apply_manifest", "manifest": [{"source": r"E:\a\b.txt", "destination": r"E:\a\c"}]}
    assert ungrounded_paths("tidy it", manifest, [], "") != []


@pytest.mark.asyncio
async def test_a_call_on_a_path_from_an_earlier_turn_is_refused_once_then_allowed_if_the_model_insists(tmp_path):
    repos, audit = make_repos(tmp_path)
    task = _task_with_history(repos, "Remember that my cat is Miso", [], memory_context=MEMORY,
                              orchestration_intent={"route": "configuration", "reasoning": "x"})
    read = OperatorDecision(action=OperatorAction.CALL_TOOL, tool_name="filesystem.manage",
                            tool_input={"operation": "read_file", "path": STALE}, risk_level=RiskLevel.LOW)
    worker = _worker(repos, audit, _Seen([read, read]))
    first = await worker.process_task(task.id)
    last = first.metadata["operator_history"][-1]
    assert last["status"] == "denied" and last["error"] == UNGROUNDED_TEXT.format(paths=STALE)
    second = await worker.process_task(first.id)
    assert second.metadata["operator_history"][-1]["status"] != "denied"


@pytest.mark.asyncio
async def test_a_file_request_is_never_second_guessed_about_its_paths(tmp_path):
    repos, audit = make_repos(tmp_path)
    task = _task_with_history(repos, "Organize my Downloads", [], memory_context=MEMORY,
                              orchestration_intent={"route": "filesystem.manage", "reasoning": "x"})
    read = OperatorDecision(action=OperatorAction.CALL_TOOL, tool_name="filesystem.manage",
                            tool_input={"operation": "inspect_folder", "root": RESUME.rsplit("\\", 1)[0]}, risk_level=RiskLevel.LOW)
    first = await _worker(repos, audit, _Seen([read])).process_task(task.id)
    assert first.metadata["operator_history"][-1]["status"] != "denied"


# --- a quoted value no tool returned ---------------------------------------------------------------------------------------------------------------

from agent_control.orchestration.ladder import UNGROUNDED_QUOTES_TEXT, ungrounded_quotes  # noqa: E402

PAGE = [{"tool_name": "http.request", "input": {"url": "https://www.mozilla.org"}, "status": "succeeded",
         "output_summary": 'HTTP GET returned 200. The page title is exactly: "Internet for people, not profit - Mozilla Global".'}]


def test_a_quoted_title_that_is_in_a_tool_result_is_fine_whatever_its_case_or_spacing():
    assert ungrounded_quotes('The title is "Internet for people, not profit - Mozilla Global".', "title?", PAGE) == []
    assert ungrounded_quotes('It is "internet for  people, not profit - mozilla global"', "title?", PAGE) == []
    assert ungrounded_quotes("The title is Mozilla Firefox (no quotation marks)", "title?", PAGE) == []


def test_a_quoted_title_no_tool_returned_is_caught_once_per_distinct_quote():
    assert ungrounded_quotes('The title is "Mozilla Firefox" and again "mozilla firefox".', "title?", PAGE) == ["Mozilla Firefox"]


def test_a_quote_from_the_request_or_a_remembered_fact_is_grounded_and_a_tiny_quote_is_ignored():
    assert ungrounded_quotes('Wrote "hello world" into it.', 'Write "hello world" into a.txt', []) == []
    assert ungrounded_quotes('Your nickname is "Captain Oney".', "what is my nickname", [], "Remembered facts:\n- [note] nickname: Captain Oney") == []
    assert ungrounded_quotes('Just "ok" then.', "hi", []) == []
    assert ungrounded_quotes(None, "hi", []) == [] and ungrounded_quotes("", "hi", []) == []


def test_curly_quotation_marks_count_too():
    assert ungrounded_quotes("The title is \u201cMozilla Firefox\u201d", "title?", PAGE) == ["Mozilla Firefox"]


@pytest.mark.asyncio
async def test_a_final_answer_quoting_a_title_no_tool_returned_is_sent_back_once_then_allowed(tmp_path):
    repos, audit = make_repos(tmp_path)
    task = _task_with_history(repos, "What is the page title of mozilla.org?", PAGE)
    wrong = OperatorDecision(action=OperatorAction.DONE, final_answer='The title is "Mozilla Firefox".')
    worker = _worker(repos, audit, _Seen([wrong, wrong]))
    first = await worker.process_task(task.id)
    assert first.status == TaskStatus.RUNNING
    last = first.metadata["operator_history"][-1]
    assert last["hint_id"] == "quotes" and last["error"] == UNGROUNDED_QUOTES_TEXT.format(quotes='"Mozilla Firefox"')
    second = await worker.process_task(first.id)
    assert second.status == TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_a_part_that_quotes_a_title_no_tool_returned_looks_again_inside_the_part(tmp_path):
    from agent_control.orchestration.planner import Part  # noqa: F401  (the parts run through delegate, which is what is checked)

    repos, audit = make_repos(tmp_path)
    task = _task_with_history(repos, "Titles please", [])
    delegate = OperatorDecision(action=OperatorAction.DELEGATE, delegate_objective="Get the title of https://www.mozilla.org")
    # The part's first answer quotes a title nothing returned; the second is what it was told to say.
    sub_wrong = OperatorDecision(action=OperatorAction.DONE, final_answer='"Mozilla Firefox"')
    sub_honest = OperatorDecision(action=OperatorAction.DONE, final_answer="I could not read the title.")
    operator = _Seen([delegate, sub_wrong, sub_honest])
    result = await _worker(repos, audit, operator).process_task(task.id)
    entry = [e for e in result.metadata["operator_history"] if e.get("tool_name") == "delegate"][-1]
    assert entry["status"] == "succeeded" and entry["output_summary"] == "I could not read the title."
    assert operator.calls == 3


@pytest.mark.asyncio
async def test_a_final_answer_that_writes_out_a_wrong_total_is_sent_back_once_with_the_right_one(tmp_path):
    repos, audit = make_repos(tmp_path)
    task = _task_with_history(repos, "How many lines in all the txt files?", [])
    wrong = OperatorDecision(
        action=OperatorAction.DONE, final_answer="5 lines in total (4 lines in a.txt + 5 lines in b.txt + 6 lines in c.txt).",
    )
    right = OperatorDecision(action=OperatorAction.DONE, final_answer="15 lines in total (4 + 5 + 6).")
    operator = _Seen([wrong, right])
    worker = _worker(repos, audit, operator)
    first = await worker.process_task(task.id)
    assert first.status == TaskStatus.RUNNING
    last = first.metadata["operator_history"][-1]
    assert last["tool_name"] == "_number_check" and last["hint_id"] == "sum" and "which is 15" in last["error"]
    second = await worker.process_task(first.id)
    assert second.status == TaskStatus.COMPLETED and second.metadata["synthesized_answer"].startswith("15 lines")
