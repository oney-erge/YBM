"""What the hands suite found on 11/19 (the first measurement of the assistant using the computer and the web): spellings a model uses for the same request, a button named
in one word, fields outside a form, and a goal worked without a vision model."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agent_control.config import ComputerUseAdapterConfig
from agent_control.schemas import Capability, RiskLevel, ToolCallRequest
from agent_control.tools.browser import _click_script, _summary_script
from agent_control.tools.computer_use import ComputerUseAdapter, _named_click
from agent_control.tools.contracts import BrowserFillFormInput, BrowserFillFormStepInput, CodeInterpreterRunPythonInput, ComputerRunGoalInput


def test_a_field_is_a_field_whatever_the_model_calls_it() -> None:
    expected = {"Name": "Ada Lovelace"}
    assert BrowserFillFormStepInput(operation="fill_form_step", field_name="Name", value="Ada Lovelace", url="http://x/").fields == expected
    assert BrowserFillFormStepInput(operation="fill_form_step", field="Name", value="Ada Lovelace").fields == expected
    assert BrowserFillFormStepInput(operation="fill_form_step", name="Name", text="Ada Lovelace").fields == expected
    pairs = BrowserFillFormInput(operation="fill_form", fields=[{"name": "quantity", "value": "1"}, {"field": "color", "text": "red"}])
    assert pairs.fields == {"quantity": "1", "color": "red"}
    assert BrowserFillFormInput(operation="fill_form", fields={"Name": "Ada"}).fields == {"Name": "Ada"}  # the proper form is untouched
    with pytest.raises(ValueError):
        BrowserFillFormStepInput(operation="fill_form_step", url="http://x/")  # nothing to fill is still refused


def test_a_goal_is_an_objective_and_a_script_is_the_code() -> None:
    assert ComputerRunGoalInput(operation="run_goal", goal="Open Calculator and work out 17 times 23").objective.startswith("Open Calculator")
    assert ComputerRunGoalInput(operation="run_goal", objective="x").require_vision is False
    assert CodeInterpreterRunPythonInput(operation="run_python", script="print(17 * 23)").code == "print(17 * 23)"
    assert CodeInterpreterRunPythonInput(operation="run_python", code="print(1)", script="print(2)").code == "print(1)"


def test_the_click_script_takes_a_one_word_button_and_the_playwright_spelling_as_visible_text() -> None:
    script = _click_script("text=Checkout", None)
    assert "prefixed" in script and "text\s*=" in script  # text=Checkout is the words of the button
    assert "+)*$" in script  # one word is enough: "Checkout"


def test_the_page_state_lists_inputs_outside_forms_and_the_buttons() -> None:
    script = _summary_script(5000)
    assert "not_in_a_form" in script and "buttons" in script and "input[type=\"submit\"]" in script


class _Backend:
    def __init__(self) -> None:
        self.actions: list[dict[str, Any]] = []

    def observe(self, screenshot_path: Path, *, include_ui_tree: bool, max_ui_elements: int) -> dict[str, Any]:
        screenshot_path.parent.mkdir(parents=True, exist_ok=True)
        screenshot_path.write_bytes(b"x")
        return {
            "screenshot_path": str(screenshot_path), "active_window": {"title": "Calculator"}, "visible_windows": [{"title": "Calculator"}],
            "ui_tree": [{"name": "Seven", "control_type": "Button", "rectangle": {"left": 10, "top": 20, "right": 50, "bottom": 60}}],
        }

    def execute_action(self, action: dict[str, Any], config: ComputerUseAdapterConfig) -> dict[str, Any]:
        self.actions.append(action)
        return {"ok": True, "summary": "done"}


class _NoVisionProvider:
    """A model that has the method for pictures and refuses them (the local vision model is not running), and answers text."""

    def __init__(self) -> None:
        self.texts: list[str] = []
        self.answers = [{"completed": False, "summary": "press seven", "action": {"type": "click", "name": "Seven"}}, {"completed": True, "summary": "17 times 23 is 391"}]

    async def generate_multimodal_text(self, system_prompt: str, user_prompt: str, image_paths: list[str]) -> str:
        raise RuntimeError("hosted fallback is not permitted")

    async def generate_text(self, system_prompt: str, user_prompt: str) -> str:
        self.texts.append(system_prompt)
        return json.dumps(self.answers.pop(0))


@pytest.mark.asyncio
async def test_a_goal_is_worked_from_the_ui_tree_when_the_model_that_sees_will_not(tmp_path) -> None:
    backend, provider = _Backend(), _NoVisionProvider()
    adapter = ComputerUseAdapter(ComputerUseAdapterConfig(enabled=True, screenshot_dir=str(tmp_path / "shots"), step_delay_seconds=0.0), provider=provider, backend=backend)

    result = await adapter.execute(ToolCallRequest(
        task_id="t", tool_name="computer.use", capability=Capability.DESKTOP_CONTROL, risk_level=RiskLevel.CRITICAL,
        input={"operation": "run_goal", "objective": "Open Calculator and work out 17 times 23"},
    ))

    assert result.status.value == "succeeded" and result.output["completed"] is True and "391" in result.output["final_summary"]
    assert backend.actions == [{"type": "click", "x": 30, "y": 40, "named": "seven"}]  # the named element became the middle of its box
    assert provider.texts and "cannot see the screen" in provider.texts[0]


def test_a_click_that_names_nothing_on_the_screen_waits_instead_of_clicking_somewhere() -> None:
    observation = {"ui_tree": [{"name": "Seven", "rectangle": {"left": 0, "top": 0, "right": 10, "bottom": 10}}]}
    assert _named_click({"type": "click", "name": "Equals"}, observation) == {"type": "wait", "seconds": 1}
    assert _named_click({"type": "click", "x": 5, "y": 6, "name": "Seven"}, observation) == {"type": "click", "x": 5, "y": 6, "name": "Seven"}
    assert _named_click({"type": "hotkey", "keys": ["ctrl", "c"]}, observation) == {"type": "hotkey", "keys": ["ctrl", "c"]}


def test_a_click_names_its_target_in_whatever_word_the_model_uses() -> None:
    from agent_control.tools.contracts import BrowserClickInput

    assert BrowserClickInput(operation="click", target="Continue").text == "Continue"
    assert BrowserClickInput(operation="click", element="Add to cart").text == "Add to cart"
    assert BrowserClickInput(operation="click", button="#go").selector == "#go"
    assert BrowserClickInput(operation="click", target="button.primary").selector == "button.primary"
    assert BrowserClickInput(operation="click", target="text=Checkout").text == "text=Checkout"  # the click script reads the prefix
    assert BrowserClickInput(operation="click", selector="#a", target="b").selector == "#a"  # what was given properly wins
    with pytest.raises(ValueError):
        BrowserClickInput(operation="click")


def test_a_payment_that_was_asked_for_approved_and_carried_out_backs_the_words_that_say_so() -> None:
    from agent_control.orchestration import claims

    paid = [{"tool_name": "browser.control", "status": "succeeded", "input": {"operation": "click", "text": "Place your order"}}]
    looked = [{"tool_name": "browser.control", "status": "succeeded", "input": {"operation": "click", "text": "Add to cart"}}]
    failed = [{"tool_name": "browser.control", "status": "failed", "input": {"operation": "click", "text": "Place your order"}}]
    said = "The red mug has been purchased. Your order was placed, order number ORD-2290."
    assert claims.unbacked_claims(said, paid, {}) == []
    assert any(c.kind == "paid" for c in claims.unbacked_claims(said, looked, {}))  # nothing paid, and it says it was
    assert any(c.kind == "paid" for c in claims.unbacked_claims("I ordered the mug for you.", failed, {}))


@pytest.mark.asyncio
async def test_what_a_script_wrote_in_a_folder_it_names_is_a_recorded_change(tmp_path) -> None:
    from agent_control.config import CodeInterpreterAdapterConfig
    from agent_control.tools.code_interpreter import CodeInterpreterAdapter

    target = tmp_path / "work" / "out"
    adapter = CodeInterpreterAdapter(CodeInterpreterAdapterConfig(enabled=True, workspace_root=str(tmp_path / "ws"), blocked_imports=[]), provider=None)
    code = f"import os\nfolder = r'{target}'\nos.makedirs(folder, exist_ok=True)\nopen(os.path.join(folder, 'hands-note.txt'), 'w').write('hello from hands')\nprint('saved')\n"
    result = await adapter.execute(ToolCallRequest(
        task_id="task_x", tool_name="code.interpreter", capability=Capability.TERMINAL_RUN, risk_level=RiskLevel.HIGH,
        input={"operation": "run_python", "code": code, "execution_profile": "trusted"},
    ))
    assert result.status.value == "succeeded", result.error_message
    assert result.output["changed_paths"] == [str(target / "hands-note.txt")] and "outside the workspace" in result.output["summary"]
