"""A monitor: a script the assistant wrote and saved, tested once, run on a schedule by a watch, speaking only when what it prints changes (jobs/monitors.py, jobs/watch.py)."""

from __future__ import annotations

import sys

import pytest

from agent_control.config import AppSettings
from agent_control.jobs import monitors, watch
from agent_control.jobs.research import ResearchIO
from agent_control.schemas import ToolCallRequest, ToolResultStatus, Capability
from agent_control.tools.job_manage import JobManageAdapter
from helpers import make_repos

GOOD = "import sys\nprint('192.168.1.1 router')\nprint('192.168.1.20 tv')\n"


def test_a_script_is_saved_run_once_and_a_failing_or_silent_one_is_not_kept(tmp_path) -> None:
    folder = tmp_path / "monitors"
    path, first = monitors.save_and_test(folder, "Network Devices!", GOOD)
    assert path.name == "network-devices.py" and first.splitlines() == ["192.168.1.1 router", "192.168.1.20 tv"]
    with pytest.raises(RuntimeError, match="printed nothing"):
        monitors.save_and_test(folder, "quiet", "x = 1\n")
    with pytest.raises(RuntimeError, match="failed"):
        monitors.save_and_test(folder, "broken", "raise SystemExit(3)\n")
    assert not (folder / "quiet.py").exists() and not (folder / "broken.py").exists()
    with pytest.raises(RuntimeError, match="failed"):
        monitors.save_and_test(folder, "network-devices", "import sys; sys.exit(1)\n")
    assert (folder / "network-devices.py").read_text(encoding="utf-8") == GOOD  # a bad update does not replace the good script
    with pytest.raises(ValueError, match="too long"):
        monitors.save_and_test(folder, "huge", "print(1)\n" * 5000)
    with pytest.raises(ValueError, match="plain name"):
        monitors.script_path(folder, "!!!")


def test_a_monitors_folder_given_as_a_relative_path_still_runs_its_scripts(tmp_path, monkeypatch) -> None:
    """Live 11/19 (acceptance suite): the setting is `.agent_control/monitors`, relative; the script is run with its own folder as the working directory, and the relative path then pointed nowhere."""
    monkeypatch.chdir(tmp_path)
    path, first = monitors.save_and_test(".agent_control/monitors", "devices", GOOD)
    assert first.splitlines()[0] == "192.168.1.1 router" and path.name == "devices.py"
    assert monitors.run_script(".agent_control/monitors", "devices").splitlines()[1] == "192.168.1.20 tv"


def test_a_script_outside_the_folder_is_never_run_and_a_slow_one_is_stopped(tmp_path) -> None:
    folder = tmp_path / "monitors"
    folder.mkdir()
    (tmp_path / "outside.py").write_text("print('x')\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="does not exist"):
        monitors.run_script(folder, "../outside")  # the name is cleaned to a plain one: it looks for outside.py inside the folder
    (folder / "slow.py").write_text("import time\ntime.sleep(30)\nprint('late')\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="longer than 1 seconds"):
        monitors.run_script(folder, "slow", seconds=1)


@pytest.mark.asyncio
async def test_a_watch_of_a_script_records_the_first_look_and_speaks_once_about_each_change() -> None:
    outputs = iter(["a router\nb tv\n", "a router\nb tv\n", "a router\nb tv\nc phone\n", "a router\nc phone\n"])

    async def run_monitor(name):
        assert name == "devices"
        return next(outputs)

    io = ResearchIO(think=None, write=None, search=None, fetch=None, run_monitor=run_monitor)
    state = watch.new_state("script:devices", 5)
    assert state["target_kind"] == "script"
    said = []
    for _ in range(4):
        unit = await watch.advance("tell me about new devices", state, io)
        state = unit.state
        said.append(unit.say)
    assert said[0] is None and said[1] is None  # the first look only records; nothing changed
    assert "New: c phone." in said[2] and "Gone" not in said[2]
    assert "Gone: b tv." in said[3]


@pytest.mark.asyncio
async def test_start_watch_with_a_script_saves_tests_and_starts_it_and_refuses_a_script_that_fails(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    task = repos.tasks.create("watch my network", metadata={"source_chat_id": "42", "source_channel": "telegram", "source_sender_id": "7"})
    settings = AppSettings(_env_file=None, jobs={"enabled": True, "monitors_dir": str(tmp_path / "monitors")})
    adapter = JobManageAdapter(repos, audit, settings)

    def request(**fields):
        return ToolCallRequest(task_id=task.id, tool_name="job.manage", capability=Capability.AGENT_CORE, input={"operation": "start_watch", **fields})

    bad = await adapter.execute(request(script="print()\n", name="devices", goal="Tell me about new devices"))
    assert bad.status == ToolResultStatus.FAILED and "did not pass its test run" in (bad.error_message or "")
    assert repos.jobs.list(limit=5) == []

    good = await adapter.execute(request(script=GOOD, name="Devices", goal="Tell me about new devices on my network", every_minutes=10))
    assert good.status == ToolResultStatus.SUCCEEDED and "printed 2 line(s)" in good.output["summary"]
    job = repos.jobs.list(limit=5)[0]
    assert job.kind == "watch" and job.state["target"] == "script:devices" and job.state["target_kind"] == "script" and job.state["every_minutes"] == 10
    assert (tmp_path / "monitors" / "devices.py").is_file()


def test_the_python_that_runs_a_monitor_is_this_one() -> None:
    assert monitors.sys.executable == sys.executable
