"""The demo's replay server answers only in the situation each response was recorded in.

The README GIF replays a real model run (docs/screenshots/demo-run.json): YBM and its tools run
live, the model's responses come from the recording. A replay that answered whatever came next
could show a run that never happened, so each response is tied to its role and the steps taken
before it.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "replay_model_server.py"
RECORDING = ROOT / "docs" / "screenshots" / "demo-run.json"


@pytest.fixture(scope="module")
def replay():
    spec = importlib.util.spec_from_file_location("replay_model_server", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _ask(system: str, user: str) -> dict:
    return {"messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}


def test_a_history_reads_as_the_steps_taken(replay):
    prompt = (
        "## History\n1. filesystem.manage (succeeded)\n   input: {\"operation\": \"inspect_folder\", \"root\": \"downloads\"}\n"
        "   output: 24 entries\n2. filesystem.manage (failed)\n   input: {\"operation\": \"organize_plan\"}\n"
    )
    assert replay.situation(prompt) == [
        ["filesystem.manage", "inspect_folder", "succeeded"],
        ["filesystem.manage", "organize_plan", "failed"],
    ]
    assert replay.situation("Decide the next action. No steps yet.") == []


def test_a_response_is_served_only_in_its_recorded_situation(replay):
    recording = {
        "objective": "Organize my Downloads folder by type",
        "model": "m",
        "calls": [
            {"role": "operator", "situation": [], "response": "first", "latency_ms": 0, "usage": {}},
            {"role": "operator", "situation": [["filesystem.manage", "inspect_folder", "succeeded"]],
             "response": "second", "latency_ms": 0, "usage": {}},
        ],
    }
    server = replay.Replay(recording, speed=1.0)
    operator = "You are the operator loop for a local agent-control bot."
    status, body = server.answer(_ask(operator, "No steps yet."))
    assert status == 200 and body["choices"][0]["message"]["content"] == "first"
    # The run went another way (the listing failed): the recorded answer does not fit, so none is given.
    diverged = "1. filesystem.manage (failed)\n   input: {\"operation\": \"inspect_folder\"}\n"
    status, body = server.answer(_ask(operator, diverged))
    assert status == 409 and "different situation" in body["error"]["message"]
    status, _ = server.answer(_ask("You are someone else.", "hi"))
    assert status == 409


def test_the_committed_recording_is_a_whole_run(replay):
    recording = json.loads(RECORDING.read_text(encoding="utf-8"))
    roles = [call["role"] for call in recording["calls"]]
    assert roles[0] == "concierge" and roles[-1] == "auditor" and "operator" in roles
    # Each operator call knows one more step than the one before it.
    steps = [len(call["situation"]) for call in recording["calls"] if call["role"] == "operator"]
    assert steps == list(range(len(steps)))
    assert all(call["latency_ms"] > 0 for call in recording["calls"])
