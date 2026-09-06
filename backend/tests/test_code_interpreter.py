from __future__ import annotations

from pathlib import Path
from typing import TypeVar

import pytest
from pydantic import BaseModel

from agent_control.config import AppSettings, CapabilityPolicy, CodeInterpreterAdapterConfig
from agent_control.schemas import Capability, RiskLevel, ToolCallRequest, ToolCallResult, ToolResultStatus
from agent_control.tools import code_interpreter as code_interpreter_module
from agent_control.tools.code_interpreter import (
    CodeExecutionPlan,
    CodeExecutionResult,
    CodeInterpreterAdapter,
    DockerPythonBackend,
    ProcessExecutionResult,
    _verify_code_interpreter,
)
from agent_control.tools.registry import build_tool_registry

T = TypeVar("T", bound=BaseModel)

class FakeScriptProvider:
    async def generate_text(self, system_prompt: str, user_prompt: str) -> str:
        return ""

    async def generate_multimodal_text(self, system_prompt: str, user_prompt: str, image_paths: list[str]) -> str:
        return ""

    async def generate_structured(self, system_prompt: str, user_prompt: str, output_model: type[T], **_ignored_kwargs) -> T:
        return output_model.model_validate(
            {
                "summary": "Write a small result file.",
                "code": "from pathlib import Path\nPath('result.txt').write_text('ok', encoding='utf-8')\nprint('created result')\n",
                "expected_files": ["result.txt"],
            }
        )

class MalformedMarkdownProvider(FakeScriptProvider):
    async def generate_structured(self, system_prompt: str, user_prompt: str, output_model: type[T], **_ignored_kwargs) -> T:
        return output_model.model_validate(
            {
                "summary": "Malformed Markdown writer.",
                "code": 'Path("meeting-report.md").write_text("# Meeting\n\n- broken", encoding="utf-8")',
                "expected_files": ["meeting-report.md"],
            }
        )

class BrokenStructuredProvider(FakeScriptProvider):
    async def generate_structured(self, system_prompt: str, user_prompt: str, output_model: type[T], **_ignored_kwargs) -> T:
        raise ValueError("LLM structured output failed validation")

class FakeArtifactRepository:
    def __init__(self) -> None:
        self.created = []

    def create(self, artifact):
        self.created.append(artifact)
        return artifact

def _settings(tmp_path: Path) -> AppSettings:
    return AppSettings(
        _env_file=None,
        capabilities={Capability.TERMINAL_RUN: CapabilityPolicy(enabled=True, requires_approval=False, max_risk_level=RiskLevel.HIGH)},
        adapters={
            "code_interpreter": {"enabled": True, "workspace_root": str(tmp_path / "code")},
            "workspace": {"root_dir": str(tmp_path / "workspaces")},
        },
    )

def _request(tmp_path: Path, operation: str, **payload) -> ToolCallRequest:
    return ToolCallRequest(
        task_id="task_code",
        tool_name="code.interpreter",
        capability=Capability.TERMINAL_RUN,
        input={"operation": operation, "workspace_dir": str(tmp_path / "code" / "task_code"), **payload},
    )

def test_registry_exposes_code_interpreter_when_terminal_run_is_enabled(tmp_path) -> None:
    registry = build_tool_registry(_settings(tmp_path), backend_base_url="http://127.0.0.1:8765", provider=FakeScriptProvider())

    definitions = {definition.name: definition for definition in registry.definitions}
    assert definitions["code.interpreter"].enabled is True
    assert "generate_and_run" in definitions["code.interpreter"].operations
    assert "code.interpreter" in registry.adapters


def test_generate_and_run_contract_preserves_runtime_approval_marker(tmp_path) -> None:
    registry = build_tool_registry(
        _settings(tmp_path),
        backend_base_url="http://127.0.0.1:8765",
        provider=FakeScriptProvider(),
    )
    definition = next(item for item in registry.definitions if item.name == "code.interpreter")

    validated = definition.validate_input(
        {
            "operation": "generate_and_run",
            "objective": "Create a result file.",
            "approved": True,
        }
    )

    assert validated["approved"] is True

@pytest.mark.asyncio
async def test_code_interpreter_runs_python_in_managed_workspace(tmp_path) -> None:
    adapter = CodeInterpreterAdapter(_settings(tmp_path).adapters.code_interpreter)

    result = await adapter.execute(
        _request(
            tmp_path,
            "run_python",
            code="from pathlib import Path\nPath('answer.txt').write_text('42', encoding='utf-8')\nprint('done')\n",
        )
    )

    assert result.status.value == "succeeded"
    assert result.output["returncode"] == 0
    assert "answer.txt" in result.output["files_created"]
    assert (tmp_path / "code" / "task_code" / "answer.txt").read_text(encoding="utf-8") == "42"

@pytest.mark.asyncio
async def test_code_interpreter_generate_and_run_uses_local_llm_provider(tmp_path) -> None:
    adapter = CodeInterpreterAdapter(_settings(tmp_path).adapters.code_interpreter, provider=FakeScriptProvider())

    # approved=True: Docker isn't available in this test environment, so the
    # run would otherwise fall back to unsandboxed execution and need
    # approval first - see test_code_interpreter_generated_run_needs_approval_*
    # below for that gate itself.
    result = await adapter.execute(
        _request(tmp_path, "generate_and_run", objective="Create a result file.", approved=True)
    )

    assert result.status.value == "succeeded"
    assert result.output["generated"] is True
    assert result.output["generation_summary"] == "Write a small result file."
    assert "result.txt" in result.output["files_created"]

@pytest.mark.asyncio
async def test_code_interpreter_repairs_malformed_generated_markdown_script(tmp_path) -> None:
    adapter = CodeInterpreterAdapter(_settings(tmp_path).adapters.code_interpreter, provider=MalformedMarkdownProvider())

    result = await adapter.execute(
        _request(
            tmp_path,
            "generate_and_run",
            objective="Turn these notes into a Markdown report named meeting-report.md: desktop inspection passed, browser screenshot pending.",
            approved=True,
        )
    )

    assert result.status.value == "succeeded"
    assert result.output["generation_repaired"] is True
    assert "meeting-report.md" in result.output["files_created"]
    assert "desktop inspection passed" in (tmp_path / "code" / "task_code" / "meeting-report.md").read_text(encoding="utf-8")

@pytest.mark.asyncio
async def test_code_interpreter_falls_back_when_structured_generation_fails_for_simple_file(tmp_path) -> None:
    adapter = CodeInterpreterAdapter(_settings(tmp_path).adapters.code_interpreter, provider=BrokenStructuredProvider())

    result = await adapter.execute(
        _request(
            tmp_path,
            "generate_and_run",
            objective="Run a small local script that creates route-checklist.md: inspect desktop, search files, deliver artifact.",
            approved=True,
        )
    )

    assert result.status.value == "succeeded"
    assert result.output["generation_repaired"] is True
    assert "route-checklist.md" in result.output["files_created"]

@pytest.mark.asyncio
async def test_code_interpreter_blocks_dangerous_imports_by_default(tmp_path) -> None:
    adapter = CodeInterpreterAdapter(CodeInterpreterAdapterConfig(workspace_root=str(tmp_path / "code")))

    result = await adapter.execute(_request(tmp_path, "run_python", code="import subprocess\nprint('bad')\n"))

    assert result.status.value == "failed"
    assert "blocked import" in (result.error_message or "")

@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code",
    [
        "import importlib\nimportlib.import_module('os')\n",
        "from importlib import import_module\nimport_module('subprocess')\n",
        "import multiprocessing\n",
        "import winreg\n",
    ],
)
async def test_code_interpreter_blocks_import_bypass_modules_by_default(tmp_path, code) -> None:
    # importlib.import_module dynamically imports a module without a static
    # Import/ImportFrom AST node - _validate_python's blocked_imports check
    # wouldn't see "os"/"subprocess" being reached this way unless importlib
    # itself is blocked. multiprocessing (process spawning) and winreg
    # (Windows registry) are the same risk class as subprocess/os but were
    # missing from the original list (docs/HISTORY.md P5).
    adapter = CodeInterpreterAdapter(CodeInterpreterAdapterConfig(workspace_root=str(tmp_path / "code")))

    result = await adapter.execute(_request(tmp_path, "run_python", code=code))

    assert result.status.value == "failed"
    assert "blocked import" in (result.error_message or "")

@pytest.mark.asyncio
async def test_code_interpreter_output_includes_backend_metadata(tmp_path) -> None:
    adapter = CodeInterpreterAdapter(_settings(tmp_path).adapters.code_interpreter)

    result = await adapter.execute(_request(tmp_path, "run_python", code="print('metadata ok')\n"))

    assert result.status.value == "succeeded"
    assert result.output["backend"] == "local_subprocess"
    assert result.output["execution_profile"] == "trusted"
    assert result.output["sandboxed"] is False
    assert result.output["resource_limits"]["timeout_seconds"] == 60
    assert "duration_seconds" in result.output["resource_usage"]

@pytest.mark.asyncio
async def test_code_interpreter_can_block_configured_imports(tmp_path) -> None:
    config = _settings(tmp_path).adapters.code_interpreter.model_copy(update={"blocked_imports": ["subprocess"]})
    adapter = CodeInterpreterAdapter(config)

    result = await adapter.execute(_request(tmp_path, "run_python", code="import subprocess\nprint('bad')\n"))

    assert result.status.value == "failed"
    assert "blocked import" in (result.error_message or "")

@pytest.mark.asyncio
async def test_code_interpreter_untrusted_run_python_needs_approval(tmp_path) -> None:
    adapter = CodeInterpreterAdapter(_settings(tmp_path).adapters.code_interpreter)

    result = await adapter.execute(
        _request(tmp_path, "run_python", code="print('blocked until approved')\n", execution_profile="untrusted")
    )

    assert result.status.value == "needs_approval"
    assert "requires approval" in (result.error_message or "")

@pytest.mark.asyncio
async def test_code_interpreter_health_reports_backends(tmp_path) -> None:
    adapter = CodeInterpreterAdapter(_settings(tmp_path).adapters.code_interpreter)

    result = await adapter.execute(_request(tmp_path, "health"))

    assert result.status.value == "succeeded"
    assert result.output["health"]["default_backend"] == "local_subprocess"
    names = {item["name"] for item in result.output["health"]["backends"]}
    assert "local_subprocess" in names
    assert "docker_python" in names

@pytest.mark.asyncio
async def test_code_interpreter_health_warns_when_import_blocklist_empty(tmp_path) -> None:
    config = _settings(tmp_path).adapters.code_interpreter.model_copy(update={"blocked_imports": []})
    adapter = CodeInterpreterAdapter(config)

    result = await adapter.execute(_request(tmp_path, "health"))

    assert result.status.value == "succeeded"
    assert result.output["health"]["safety_warnings"]
    assert "blocklist is empty" in result.output["stdout"]

@pytest.mark.asyncio
async def test_code_interpreter_health_warns_when_untrusted_default_is_unavailable_docker(tmp_path) -> None:
    adapter = CodeInterpreterAdapter(_settings(tmp_path).adapters.code_interpreter)

    result = await adapter.execute(_request(tmp_path, "health"))

    assert result.status.value == "succeeded"
    warnings = result.output["health"]["safety_warnings"]
    assert any("untrusted_default_backend is docker_python" in item for item in warnings)
    assert "runs UNSANDBOXED on the host" in result.output["stdout"]

@pytest.mark.asyncio
async def test_code_interpreter_generated_run_needs_approval_on_silent_docker_fallback(tmp_path) -> None:
    # Generated code is normally exempt from approval (it's meant to be a
    # self-contained, automatic operation) - but not when it would silently
    # fall back from the configured sandboxed backend to unsandboxed
    # local_subprocess (Docker unavailable, the state in this test
    # environment): full process-privilege execution of LLM-authored code
    # with no human review is exactly the gap this gate closes.
    adapter = CodeInterpreterAdapter(_settings(tmp_path).adapters.code_interpreter, provider=FakeScriptProvider())

    result = await adapter.execute(_request(tmp_path, "generate_and_run", objective="Create a result file."))

    assert result.status.value == "needs_approval"
    assert "unsandboxed" in (result.error_message or "")
    assert "docker_python backend is unavailable" in (result.error_message or "")

@pytest.mark.asyncio
async def test_code_interpreter_generated_run_warns_on_silent_docker_fallback_once_approved(tmp_path) -> None:
    adapter = CodeInterpreterAdapter(_settings(tmp_path).adapters.code_interpreter, provider=FakeScriptProvider())

    result = await adapter.execute(
        _request(tmp_path, "generate_and_run", objective="Create a result file.", approved=True)
    )

    assert result.status.value == "succeeded"
    assert result.output["backend"] == "local_subprocess"
    assert result.output["backend_fallback_warning"] is not None
    assert "docker_python backend is unavailable" in result.output["backend_fallback_warning"]
    assert "Warning: docker_python backend is unavailable" in result.output["summary"]

@pytest.mark.asyncio
async def test_code_interpreter_inspect_state_lists_workspace_files(tmp_path) -> None:
    workspace = tmp_path / "code" / "task_code"
    workspace.mkdir(parents=True)
    (workspace / "existing.txt").write_text("ok", encoding="utf-8")
    adapter = CodeInterpreterAdapter(_settings(tmp_path).adapters.code_interpreter)

    result = await adapter.execute(_request(tmp_path, "inspect_state"))

    assert result.status.value == "succeeded"
    assert "existing.txt" in result.output["files_after"]

@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["solve_once", "build_temp_helper", "repair_script"])
async def test_code_interpreter_advertised_generation_operations_execute(tmp_path, operation) -> None:
    adapter = CodeInterpreterAdapter(_settings(tmp_path).adapters.code_interpreter, provider=FakeScriptProvider())
    payload = {"objective": "Create a result file.", "approved": True}
    if operation == "repair_script":
        payload.update({"failing_code": "print(unknown)", "error_text": "NameError"})

    result = await adapter.execute(_request(tmp_path, operation, **payload))

    assert result.status.value == "succeeded"
    assert result.output["operation"] == operation
    assert result.output["generated"] is True
    assert "result.txt" in result.output["files_created"]

@pytest.mark.asyncio
async def test_code_interpreter_registers_generated_artifacts_but_not_script(tmp_path) -> None:
    artifacts = FakeArtifactRepository()
    adapter = CodeInterpreterAdapter(_settings(tmp_path).adapters.code_interpreter, artifacts=artifacts)

    result = await adapter.execute(
        _request(
            tmp_path,
            "run_python",
            code=(
                "from pathlib import Path\n"
                "Path('report.md').write_text('# Report', encoding='utf-8')\n"
                "Path('data.csv').write_text('a,b\\n1,2\\n', encoding='utf-8')\n"
                "Path('workbook.xlsx').write_bytes(b'PK\\x03\\x04')\n"
                "print('created artifacts')\n"
            ),
        )
    )

    assert result.status.value == "succeeded"
    registered = {Path(item.uri).name for item in artifacts.created}
    assert registered == {"report.md", "data.csv", "workbook.xlsx"}
    assert "script.py" not in registered
    assert len(result.output["artifact_ids"]) == 3

@pytest.mark.asyncio
async def test_code_interpreter_can_select_docker_backend_when_configured(tmp_path, monkeypatch) -> None:
    base_config = _settings(tmp_path).adapters.code_interpreter
    config = base_config.model_copy(
        update={
            "backends": ["local_subprocess", "docker_python"],
            "docker": base_config.docker.model_copy(update={"enabled": True, "image": "python:3.12-slim", "pull_policy": "never"}),
        }
    )
    adapter = CodeInterpreterAdapter(config)

    monkeypatch.setattr(DockerPythonBackend, "available", lambda self: True)

    async def fake_execute(self, plan):
        plan.script_path.write_text(plan.code + "\nprint('docker fake')\n", encoding="utf-8")
        return CodeExecutionResult(
            returncode=0,
            stdout="docker fake\n",
            stderr="",
            backend="docker_python",
            sandboxed=True,
            resource_usage={"duration_seconds": 0.01},
            resource_limits={"memory": "512m"},
            network_enabled=plan.allow_network,
        )

    monkeypatch.setattr(DockerPythonBackend, "execute", fake_execute)

    result = await adapter.execute(
        _request(
            tmp_path,
            "run_python",
            code="print('hello')\n",
            backend="docker_python",
            approved=True,
            execution_profile="untrusted",
        )
    )

    assert result.status.value == "succeeded"
    assert result.output["backend"] == "docker_python"
    assert result.output["sandboxed"] is True

@pytest.mark.asyncio
async def test_docker_backend_runs_with_network_disabled_and_security_flags(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "code" / "task_code"
    workspace.mkdir(parents=True)
    script = workspace / "script.py"
    script.write_text("print('hello')\n", encoding="utf-8")
    base_config = CodeInterpreterAdapterConfig(workspace_root=str(tmp_path / "code"))
    config = base_config.model_copy(
        update={
            "docker": base_config.docker.model_copy(update={"enabled": True, "pull_policy": "never"}),
        }
    )
    backend = DockerPythonBackend(config)
    captured: list[list[str]] = []

    monkeypatch.setattr(code_interpreter_module.shutil, "which", lambda _name: "docker")

    async def fake_run_command(args, *, cwd, timeout):
        captured.append(args)
        return ProcessExecutionResult(returncode=0, stdout="hello\n", stderr="", duration_seconds=0.01, pid=123)

    monkeypatch.setattr(code_interpreter_module, "_run_command", fake_run_command)

    result = await backend.execute(
        CodeExecutionPlan(
            request_id="req",
            task_id="task_code",
            code="print('hello')\n",
            workspace=workspace,
            script_path=script,
            timeout_seconds=30,
            generated=False,
            backend="docker_python",
            execution_profile="untrusted",
            allow_network=False,
        )
    )

    assert result.backend == "docker_python"
    args = captured[0]
    assert "--network" in args
    assert "none" in args
    assert "--security-opt" in args
    assert "no-new-privileges" in args
    assert "--mount" in args
    assert any(str(workspace) in item for item in args)

@pytest.mark.asyncio
async def test_code_interpreter_fallback_can_create_and_load_excel_workbook(tmp_path) -> None:
    adapter = CodeInterpreterAdapter(_settings(tmp_path).adapters.code_interpreter, provider=BrokenStructuredProvider())

    result = await adapter.execute(
        _request(
            tmp_path,
            "generate_and_run",
            objective="Create a simple Python script to generate and load an Excel workbook.",
            approved=True,
        )
    )

    assert result.status.value == "succeeded"
    assert "workbook.xlsx" in result.output["files_created"]
    assert "created and loaded" in result.output["stdout"]

def test_import_validation_allows_openpyxl_and_third_party() -> None:
    from agent_control.tools.code_interpreter import _validate_python

    code = "import openpyxl\nimport pandas\nimport requests\nprint('ok')\n"
    # Should not raise - third-party imports are permitted by default
    _validate_python(code, allowed_imports=set(), blocked_imports=set())



def _auto_workspace_request(operation: str, task_id: str = "task_chain", **payload) -> ToolCallRequest:
    """Like _request() but deliberately omits workspace_dir, so the adapter's
    own _workspace() resolution is what's under test."""
    return ToolCallRequest(
        task_id=task_id,
        tool_name="code.interpreter",
        capability=Capability.TERMINAL_RUN,
        input={"operation": operation, **payload},
    )


@pytest.mark.asyncio
async def test_code_interpreter_reuses_one_workspace_across_calls_in_a_task(tmp_path) -> None:
    """Regression guard (docs/HISTORY.md Part 2 §4 item 7): _workspace() used
    to append uuid4().hex[:8], so every call got a fresh empty directory and
    no multi-step file workflow could ever work - step 2 could not see the
    file step 1 created. Task ids are already unique, so the suffix only ever
    separated calls within one task, which is precisely what must be shared.
    """
    adapter = CodeInterpreterAdapter(_settings(tmp_path).adapters.code_interpreter)

    first = await adapter.execute(
        _auto_workspace_request(
            "run_python",
            code="from pathlib import Path\nPath('step1.txt').write_text('from step one', encoding='utf-8')\nprint('wrote')\n",
        )
    )
    second = await adapter.execute(
        _auto_workspace_request(
            "run_python",
            code="from pathlib import Path\nprint(Path('step1.txt').read_text(encoding='utf-8'))\n",
        )
    )

    assert first.status.value == "succeeded"
    assert "step1.txt" in first.output["files_created"]
    # The whole point: the second call lands in the same directory and can
    # read what the first one wrote.
    assert second.status.value == "succeeded"
    assert "from step one" in second.output["stdout"]
    assert first.output["workspace_dir"] == second.output["workspace_dir"]


@pytest.mark.asyncio
async def test_code_interpreter_inspect_state_sees_files_from_an_earlier_call(tmp_path) -> None:
    """Same root cause, different symptom: inspect_state always got a brand
    new empty directory, so it reported zero files no matter what the task
    had just generated."""
    adapter = CodeInterpreterAdapter(_settings(tmp_path).adapters.code_interpreter)

    await adapter.execute(
        _auto_workspace_request(
            "run_python",
            code="from pathlib import Path\nPath('generated.txt').write_text('x', encoding='utf-8')\n",
        )
    )
    inspected = await adapter.execute(_auto_workspace_request("inspect_state"))

    assert inspected.status.value == "succeeded"
    assert "generated.txt" in inspected.output["files_after"]


@pytest.mark.asyncio
async def test_code_interpreter_keeps_separate_tasks_isolated(tmp_path) -> None:
    """The flip side of sharing per task: two different tasks must NOT see
    each other's files."""
    adapter = CodeInterpreterAdapter(_settings(tmp_path).adapters.code_interpreter)

    await adapter.execute(
        _auto_workspace_request(
            "run_python",
            task_id="task_alpha",
            code="from pathlib import Path\nPath('alpha.txt').write_text('a', encoding='utf-8')\n",
        )
    )
    other = await adapter.execute(_auto_workspace_request("inspect_state", task_id="task_beta"))

    assert other.status.value == "succeeded"
    assert "alpha.txt" not in other.output["files_after"]


class PromptCapturingProvider(FakeScriptProvider):
    """Records the prompts generate_and_run actually sends, so the
    relative-path instruction can be asserted on directly."""

    def __init__(self) -> None:
        self.system_prompts: list[str] = []
        self.user_prompts: list[str] = []

    async def generate_structured(self, system_prompt: str, user_prompt: str, output_model: type[T], **_ignored_kwargs) -> T:
        self.system_prompts.append(system_prompt)
        self.user_prompts.append(user_prompt)
        return await super().generate_structured(system_prompt, user_prompt, output_model)


@pytest.mark.asyncio
async def test_code_interpreter_generation_prompt_demands_relative_paths(tmp_path) -> None:
    """Generated scripts must never hardcode the workspace's absolute path.

    Two real consequences, both observed (docs/HISTORY.md Part 2 §4 item 9):
    the Docker backend bind-mounts the workspace at a DIFFERENT path inside
    the container (`workspace_mount_target`, default /workspace), so a script
    carrying an absolute host path fails outright under the sandbox backend -
    the one that is supposed to be the secure default; and a recorded
    scenario fixture becomes unreplayable, because the baked-in path contains
    the recording run's task id.

    Both backends already run the script with the workspace as cwd, so
    relative paths are always correct and always portable.
    """
    provider = PromptCapturingProvider()
    adapter = CodeInterpreterAdapter(_settings(tmp_path).adapters.code_interpreter, provider=provider)

    result = await adapter.execute(
        _request(tmp_path, "generate_and_run", objective="Create a result file.", approved=True)
    )

    assert result.status.value == "succeeded"
    assert provider.system_prompts, "generate_and_run should have called the provider"
    system_prompt = provider.system_prompts[0]
    user_prompt = provider.user_prompts[0]
    assert "relative path" in system_prompt.lower()
    # The instruction must say the script already runs in the workspace -
    # otherwise the absolute path shown for reference invites copying.
    assert "current working directory" in system_prompt.lower()
    assert "never" in system_prompt.lower()
    # The user prompt still shows the path (useful context) but must label it
    # as reference-only rather than as something to paste into the script.
    assert "reference only" in user_prompt.lower()


@pytest.mark.asyncio
async def test_code_interpreter_previews_created_file_content_for_grounding(tmp_path) -> None:
    """Regression guard (docs/HISTORY.md Part 3 W1): the Auditor grounds its
    "did this actually answer the objective" check in terminal_output text.
    Before this, that text listed only file NAMES plus stdout - a script
    that computed the wrong number and wrote it to a file passed every check
    because nothing downstream ever looked inside the file. A small text
    preview of created/modified files must appear in both the structured
    output and the rendered terminal_output content.
    """
    adapter = CodeInterpreterAdapter(_settings(tmp_path).adapters.code_interpreter)

    result = await adapter.execute(
        _request(
            tmp_path,
            "run_python",
            code=(
                "from pathlib import Path\n"
                "Path('answer.json').write_text('{\"total\": 42}', encoding='utf-8')\n"
                "print('done')\n"
            ),
        )
    )

    assert result.status.value == "succeeded"
    previews = {p["path"]: p["content"] for p in result.output["file_previews"]}
    assert previews.get("answer.json") == '{"total": 42}'
    terminal_content = result.output["terminal_output"][0]["content"]
    assert "Content of answer.json:" in terminal_content
    assert '"total": 42' in terminal_content


@pytest.mark.asyncio
async def test_code_interpreter_skips_binary_files_in_previews(tmp_path) -> None:
    adapter = CodeInterpreterAdapter(_settings(tmp_path).adapters.code_interpreter)

    result = await adapter.execute(
        _request(
            tmp_path,
            "run_python",
            code=(
                "from pathlib import Path\n"
                "Path('image.bin').write_bytes(bytes([0x89, 0x50, 0x4e, 0x47, 0xff, 0xd8, 0x00, 0x01]))\n"
            ),
        )
    )

    assert result.status.value == "succeeded"
    assert "image.bin" in result.output["files_created"]
    assert result.output["file_previews"] == []
    assert "Content of image.bin:" not in result.output["terminal_output"][0]["content"]


@pytest.mark.asyncio
async def test_code_interpreter_preview_caps_large_file_content(tmp_path) -> None:
    adapter = CodeInterpreterAdapter(_settings(tmp_path).adapters.code_interpreter)

    result = await adapter.execute(
        _request(
            tmp_path,
            "run_python",
            code=(
                "from pathlib import Path\n"
                "Path('big.txt').write_text('x' * 5000, encoding='utf-8')\n"
            ),
        )
    )

    assert result.status.value == "succeeded"
    preview = next(p for p in result.output["file_previews"] if p["path"] == "big.txt")
    assert len(preview["content"]) <= 601  # cap + ellipsis
    assert preview["content"].endswith("…")


# ---- _verify_code_interpreter (docs/ROADMAP.md "Finish the Proof": re-read
# the workspace, don't trust the adapter's own before/after diff) ----------

def _run_result(workspace: Path, **output) -> ToolCallResult:
    return ToolCallResult(
        request_id="toolres_ci",
        status=ToolResultStatus.SUCCEEDED,
        output={"workspace_dir": str(workspace), **output},
    )


def test_verify_code_interpreter_confirms_created_and_modified_files(tmp_path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "report.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    (workspace / "notes.txt").write_text("updated", encoding="utf-8")
    request = _request(tmp_path, "generate_and_run")
    result = _run_result(workspace, files_created=["report.csv"], files_modified=["notes.txt"], files_deleted=[])

    verification = _verify_code_interpreter(request, result)

    assert verification is not None
    assert verification.checked == 2
    assert verification.verified == 2
    assert verification.missing == []
    assert verification.ok is True


def test_verify_code_interpreter_flags_a_created_file_missing_after_the_fact(tmp_path) -> None:
    """The adapter's own before/after snapshot claimed report.csv was
    created, but it isn't actually there - verification must not take that
    claim at face value (the exact scenario docs/ROADMAP.md's "Proof" item
    exists to catch)."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    request = _request(tmp_path, "generate_and_run")
    result = _run_result(workspace, files_created=["report.csv"], files_modified=[], files_deleted=[])

    verification = _verify_code_interpreter(request, result)

    assert verification is not None
    assert verification.checked == 1
    assert verification.verified == 0
    assert verification.missing == ["created file not found: report.csv"]
    assert verification.ok is False


def test_verify_code_interpreter_flags_a_deleted_file_still_present(tmp_path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "old.txt").write_text("still here", encoding="utf-8")
    request = _request(tmp_path, "generate_and_run")
    result = _run_result(workspace, files_created=[], files_modified=[], files_deleted=["old.txt"])

    verification = _verify_code_interpreter(request, result)

    assert verification is not None
    assert verification.verified == 0
    assert verification.missing == ["deleted file still present: old.txt"]


def test_verify_code_interpreter_returns_none_when_nothing_touched_files(tmp_path) -> None:
    """inspect_state/health never claim to create, modify, or delete
    anything - nothing to re-check, distinct from a claim that turned out
    empty."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    request = _request(tmp_path, "inspect_state")
    result = _run_result(workspace, files_created=[], files_modified=[], files_deleted=[])

    assert _verify_code_interpreter(request, result) is None


def test_verify_code_interpreter_returns_none_without_a_workspace_dir(tmp_path) -> None:
    request = _request(tmp_path, "health")
    result = ToolCallResult(request_id="toolres_ci", status=ToolResultStatus.SUCCEEDED, output={})

    assert _verify_code_interpreter(request, result) is None
