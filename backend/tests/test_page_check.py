"""An app is only done when the launched page offers what the person said they would enter."""

from __future__ import annotations

import http.server
import threading

import pytest

from agent_control.orchestration.fulfillment import expected_postconditions, fulfillment_guidance, validate_fulfillment
from agent_control.schemas import PostconditionType, TaskRecord, TaskStatus
from agent_control.tools.page_check import describe, fetch_page, inspect_html, requirement_items, unmet_items

RECIPE = ("Build me a small web app that scales a recipe: I enter ingredient amounts and the number of servings, and it shows the "
          "amounts for a new number of servings. Run it on my computer and send me the link.")

SERVINGS_ONLY = """<html><head><title>Recipe Scaler</title></head><body><h1>Recipe</h1>
<div><label for="o">Original Servings:</label><input type="number" id="o" value="4"></div>
<label>Target Servings <input type="number" id="t"></label><button onclick="go()">Scale Recipe</button><script>function go(){}</script></body></html>"""

WITH_INGREDIENTS = """<html><body><h1>Recipe</h1>
<label>Ingredient amounts <input name="amounts"></label><label>Number of servings <input name="servings"></label>
<input type="submit" value="Scale"></body></html>"""


def test_the_requirement_is_read_from_the_users_words():
    assert requirement_items(RECIPE) == ["ingredient amounts", "the number of servings"]
    assert requirement_items("A page where the user can type a city name and see the weather") == ["a city name"]
    assert requirement_items("Convert miles to kilometers in both directions") == []
    assert requirement_items("") == []


def test_the_controls_are_read_back_by_the_words_a_person_sees():
    page = inspect_html(SERVINGS_ONLY)
    assert [c.label for c in page.controls] == ["Original Servings:", "Target Servings"]
    assert page.buttons == ["Scale Recipe"] and page.title == "Recipe Scaler" and page.has_script


def test_a_name_or_id_is_used_only_when_the_page_has_no_words():
    page = inspect_html('<label>Full name <input name="full_name"></label><input name="github_user"><input type="hidden" name="x">')
    assert [c.label for c in page.controls] == ["Full name", "github user"]


def test_a_page_missing_what_was_asked_for_is_named():
    assert unmet_items(RECIPE, inspect_html(SERVINGS_ONLY)) == ["ingredient amounts"]
    assert unmet_items(RECIPE, inspect_html(WITH_INGREDIENTS)) == []


def test_free_text_is_never_faulted_for_a_missing_field():
    assert unmet_items(RECIPE, inspect_html("<textarea></textarea><button>Go</button>")) == []


def test_a_request_that_states_nothing_to_enter_is_not_judged():
    assert unmet_items("Build a page that converts miles to kilometers", inspect_html(SERVINGS_ONLY)) == []


def test_the_description_says_what_is_missing_in_words():
    page = inspect_html(SERVINGS_ONLY)
    text = describe(page, unmet_items(RECIPE, page))
    assert "2 field(s) (Original Servings:, Target Servings) and 1 button(s) (Scale Recipe)" in text
    assert "enters ingredient amounts, but no field on the page is named for it" in text


def _task(controls: dict | None) -> TaskRecord:
    metadata = {"original_message_text": RECIPE}
    if controls is not None:
        metadata["page_controls"] = controls
    return TaskRecord(objective=RECIPE, status=TaskStatus.RUNNING, metadata=metadata)


def test_a_launched_page_without_the_field_is_a_gap_until_it_is_fixed():
    kinds = {item.type for item in expected_postconditions(_task(None))}
    assert PostconditionType.PAGE_MATCHES_REQUEST in kinds
    assert PostconditionType.PAGE_MATCHES_REQUEST in validate_fulfillment(_task(inspect_html(SERVINGS_ONLY).as_dict())).missing
    assert PostconditionType.PAGE_MATCHES_REQUEST not in validate_fulfillment(_task(inspect_html(WITH_INGREDIENTS).as_dict())).missing


def test_with_no_record_of_a_page_there_is_nothing_to_compare():
    assert PostconditionType.PAGE_MATCHES_REQUEST not in validate_fulfillment(_task(None)).missing


def test_it_survives_a_classifier_declaration_and_other_requests_are_untouched():
    task = _task(None)
    task.metadata["expected_postconditions"] = ["preview_url"]
    assert PostconditionType.PAGE_MATCHES_REQUEST in {item.type for item in expected_postconditions(task)}
    plain = TaskRecord(objective="Build a page that converts miles to kilometers", status=TaskStatus.RUNNING, metadata={})
    assert PostconditionType.PAGE_MATCHES_REQUEST not in {item.type for item in expected_postconditions(plain)}


def test_the_gap_tells_the_model_how_to_fix_the_page():
    text = fulfillment_guidance("expected_page_matches_request_missing")
    assert "Page check" in text and "overwrite=true" in text


@pytest.fixture
def served_page():
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = SERVINGS_ONLY.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            return None

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/"
    server.shutdown()


def test_a_real_page_is_fetched_and_read(served_page):
    html = fetch_page(served_page)
    assert html is not None and unmet_items(RECIPE, inspect_html(html)) == ["ingredient amounts"]


def test_an_unreachable_page_is_none_not_an_error():
    assert fetch_page("http://127.0.0.1:9/", timeout=0.5) is None


def test_a_launched_server_is_recorded_on_its_task_so_it_can_be_found_and_stopped(tmp_path):
    from agent_control.orchestration import TaskWorker
    from agent_control.schemas import ToolCallResult, ToolResultStatus
    from helpers import make_repos
    from test_worker_operator_loop import QueueOperator, _executor, _settings

    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("Build the recipe scaler and run it")
    worker = TaskWorker(repos, audit, executor=_executor(_settings(), audit, repos), operator=QueueOperator([]))
    controls = inspect_html(SERVINGS_ONLY).as_dict()
    updated = worker._record_tool_result(task.id, "workspace.manage", ToolCallResult(
        request_id="r", status=ToolResultStatus.SUCCEEDED,
        output={"workspace_dir": str(tmp_path / "w"), "url": "http://127.0.0.1:8891/", "server_pid": 4242, "page_controls": controls},
    ))
    assert updated.metadata["preview_server"] == {"pid": 4242, "url": "http://127.0.0.1:8891/", "workspace_dir": str(tmp_path / "w")}
    assert updated.metadata["page_controls"] == controls
