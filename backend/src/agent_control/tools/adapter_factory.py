from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import importlib.util
import inspect
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Callable

from agent_control.config import AdapterFactoryConfig
from agent_control.schemas import Capability, RiskLevel, ToolCallRequest, ToolCallResult, ToolResultStatus
from agent_control.tools.contracts import (
    AdapterFactoryAssessInput,
    AdapterFactoryAssessOutput,
    AdapterFactoryPromoteInput,
    AdapterFactorySandboxExecuteInput,
    AdapterFactorySandboxOutput,
    AdapterFactoryScaffoldInput,
    AdapterFactoryScaffoldOutput,
    AdapterFactoryTestConnectorInput,
)
from agent_control.tools.path_utils import safe_path_segment
from agent_control.tools.spec import Adapters, Definitions, RegistryDeps, ToolDefinition, capability_enabled, failed_result


@dataclass(frozen=True)
class _SandboxResult:
    returncode: int
    summary: str
    stdout: str = ""
    stderr: str = ""


class AdapterFactoryAdapter:
    """Scaffold, test, and promote generated adapter proposals."""

    def __init__(self, config: AdapterFactoryConfig) -> None:
        self.config = config
        self._promotion_callback: Callable[[Any, object], None] | None = None

    def set_promotion_callback(self, callback: Callable[[Any, object], None]) -> None:
        self._promotion_callback = callback

    async def execute(self, request: ToolCallRequest) -> ToolCallResult:
        if not self.config.enabled:
            return failed_result(request, "adapter factory is disabled")
        operation = str(request.input.get("operation") or "scaffold")
        try:
            if operation == "scaffold":
                output = self._scaffold(request)
            elif operation == "assess":
                output = self._assess(request)
            elif operation == "sandbox_execute_once":
                output = self._sandbox_execute_once(request)
            elif operation == "test_connector":
                output = self._test_connector(request)
            elif operation == "promote_after_approval":
                output = self._promote_after_approval(request)
            else:
                return failed_result(request, f"unsupported adapter factory operation: {operation}")
        except Exception as exc:
            return failed_result(request, f"adapter factory operation failed: {exc}")

        output["operation"] = operation
        output["terminal_output"] = [_terminal_output(operation, output)]
        return ToolCallResult(request_id=request.id, status=ToolResultStatus.SUCCEEDED, output=output)

    def _assess(self, request: ToolCallRequest) -> dict[str, Any]:
        objective = str(request.input.get("objective") or request.input.get("prompt") or "").strip()
        name = _adapter_name(request)
        return {
            "adapter_name": name,
            "assessment": _assessment(objective),
            "cacheable": True,
            "execution_policy": "sandbox_then_hot_register",
        }

    def _scaffold(self, request: ToolCallRequest) -> dict[str, Any]:
        objective = str(
            request.input.get("objective")
            or request.input.get("prompt")
            or request.input.get("description")
            or ""
        ).strip()
        capability = str(request.input.get("capability") or "filesystem.write").strip()
        tool_name = str(
            request.input.get("tool_name")
            or request.input.get("adapter_name")
            or request.input.get("name")
            or _adapter_name(request)
        ).strip()
        requested_operations = [
            str(item).strip()
            for item in (request.input.get("operations") or request.input.get("capabilities") or [])
            if str(item).strip()
        ]
        root = Path(self.config.root_dir).expanduser().resolve()
        adapter_dir = (root / safe_path_segment(tool_name, fallback="generated_adapter")).resolve()
        if root != adapter_dir and root not in adapter_dir.parents:
            raise ValueError("adapter path escaped configured root")
        adapter_dir.mkdir(parents=True, exist_ok=True)

        class_name = _class_name(tool_name)
        manifest = {
            "name": tool_name,
            "capability": capability,
            "status": "proposal",
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "objective": objective,
            "description": str(request.input.get("description") or objective).strip(),
            "api_endpoint": request.input.get("api_endpoint"),
            "auto_load": bool(request.input.get("auto_load", False)),
            "adapter_class": class_name,
            "operations": requested_operations,
            "default_operation": requested_operations[0] if requested_operations else None,
            "execution_policy": "sandbox_then_hot_register",
            "promotion_steps": [
                "Implement adapter.py.",
                "Make test_adapter.py pass.",
                "Run adapter.factory test_connector.",
                "Run adapter.factory promote_after_approval with approved=true.",
            ],
        }
        files = {
            "manifest.json": json.dumps(manifest, indent=2) + "\n",
            "README.md": _readme(tool_name, objective, capability),
            "adapter.py": _adapter_py(tool_name, class_name),
            "test_adapter.py": _test_py(tool_name, class_name),
        }
        written: list[str] = []
        for relative_path, content in files.items():
            target = adapter_dir / relative_path
            target.write_text(content, encoding="utf-8")
            written.append(str(target))
        return {
            "adapter_dir": str(adapter_dir),
            "adapter_name": tool_name,
            "files": written,
            "cacheable": True,
            "execution_policy": "sandbox_then_hot_register",
        }

    def _sandbox_execute_once(self, request: ToolCallRequest) -> dict[str, Any]:
        adapter_dir = str(request.input.get("adapter_dir") or "").strip() or None
        if not adapter_dir:
            adapter_id = str(request.input.get("adapter_id") or "").strip()
            if adapter_id:
                adapter_dir = str(Path(self.config.root_dir) / _safe_identifier_segment(adapter_id))
        if not adapter_dir:
            raise ValueError("adapter_dir (or adapter_id) is required for sandbox_execute_once")
        path = _require_adapter_dir_inside_root(adapter_dir, self.config.root_dir)
        result = _sandbox_import(path)
        return {
            "adapter_dir": str(path),
            "result": "adapter import sandbox passed" if result.returncode == 0 else "adapter import sandbox failed",
            "execution_policy": "sandbox_import_only",
            "returncode": result.returncode,
            "promoted": False,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }

    def _test_connector(self, request: ToolCallRequest) -> dict[str, Any]:
        adapter_dir = _require_adapter_dir_inside_root(str(request.input["adapter_dir"]), self.config.root_dir)
        result = _test_connector(adapter_dir)
        return {
            "adapter_dir": str(adapter_dir),
            "result": "connector proposal tests passed" if result.returncode == 0 else result.summary,
            "execution_policy": "sandbox_test_only",
            "returncode": result.returncode,
            "promoted": False,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }

    def _promote_after_approval(self, request: ToolCallRequest) -> dict[str, Any]:
        adapter_dir = _require_adapter_dir_inside_root(str(request.input["adapter_dir"]), self.config.root_dir)
        approved = bool(request.input.get("approved"))
        if not approved:
            return {
                "adapter_dir": str(adapter_dir),
                "result": "promotion requires explicit approval",
                "execution_policy": "approval_required",
                "returncode": 1,
                "promoted": False,
            }
        if self._promotion_callback is None:
            return {
                "adapter_dir": str(adapter_dir),
                "result": "runtime promotion callback is not configured",
                "execution_policy": "promotion_unavailable",
                "returncode": 1,
                "promoted": False,
            }
        test_result = _test_connector(adapter_dir)
        if test_result.returncode != 0:
            return {
                "adapter_dir": str(adapter_dir),
                "result": f"promotion blocked because sandbox tests failed: {test_result.summary}",
                "execution_policy": "sandbox_tests_required",
                "returncode": test_result.returncode,
                "promoted": False,
                "stdout": test_result.stdout,
                "stderr": test_result.stderr,
            }
        definition, adapter = _load_promotable_adapter(adapter_dir)
        self._promotion_callback(definition, adapter)
        _mark_promoted(adapter_dir, definition.name)
        return {
            "adapter_dir": str(adapter_dir),
            "adapter_name": definition.name,
            "result": f"adapter promoted and hot-registered as {definition.name}",
            "execution_policy": "hot_registered",
            "returncode": 0,
            "promoted": True,
            "registered_tool": definition.name,
            "stdout": test_result.stdout,
            "stderr": test_result.stderr,
        }


def _adapter_name(request: ToolCallRequest) -> str:
    # Accept the concise ``name`` spelling used naturally by operators and API
    # callers. The validated payload preserves extra fields, so normalizing at
    # this shared boundary prevents an explicit requested name from being
    # ignored in favor of an objective-derived slug.
    explicit = str(request.input.get("adapter_name") or request.input.get("name") or "").strip()
    if explicit:
        return explicit
    objective = str(request.input.get("objective") or request.input.get("prompt") or request.task_id)
    words = re.findall(r"[A-Za-z0-9]+", objective.lower())
    useful = [word for word in words if word not in {"create", "build", "make", "adapter", "tool", "for", "the", "and"}]
    return "_".join(useful[:5]) or f"generated_{request.task_id}"


def _safe_identifier_segment(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_]+", "_", value).strip("_")
    return cleaned or "generated_adapter"


def _require_adapter_dir_inside_root(adapter_dir: str, root_dir: str) -> Path:
    root = Path(root_dir).expanduser().resolve()
    path = Path(adapter_dir).expanduser().resolve()
    if root != path and root not in path.parents:
        raise ValueError("adapter path escaped configured root")
    if not path.exists():
        raise ValueError(f"adapter directory does not exist: {path}")
    return path


def _sandbox_import(adapter_dir: Path) -> _SandboxResult:
    missing = _missing_required_files(adapter_dir)
    if missing:
        return _SandboxResult(1, f"missing: {', '.join(missing)}")
    script = r"""
from __future__ import annotations
import importlib.util
import inspect
import json
from pathlib import Path
import sys

adapter_path = Path(sys.argv[1])
manifest = json.loads((adapter_path.parent / "manifest.json").read_text(encoding="utf-8"))
class_name = manifest.get("adapter_class")
spec = importlib.util.spec_from_file_location("_ybm_adapter_sandbox", adapter_path)
if spec is None or spec.loader is None:
    raise SystemExit("could not load adapter spec")
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
candidate = getattr(module, str(class_name), None) if class_name else None
if candidate is None:
    for value in vars(module).values():
        if inspect.isclass(value) and value.__name__.endswith("Adapter") and hasattr(value, "execute"):
            candidate = value
            break
if candidate is None:
    raise SystemExit("no adapter class with execute() found")
adapter = candidate()
if not inspect.iscoroutinefunction(getattr(adapter, "execute", None)):
    raise SystemExit("adapter execute() must be async")
print(candidate.__name__)
"""
    return _run_python(["-c", script, str(adapter_dir / "adapter.py")], cwd=adapter_dir, timeout_seconds=15)


def _test_connector(adapter_dir: Path) -> _SandboxResult:
    missing = _missing_required_files(adapter_dir)
    if missing:
        return _SandboxResult(1, f"missing: {', '.join(missing)}")
    import_result = _sandbox_import(adapter_dir)
    if import_result.returncode != 0:
        return import_result
    return _run_python(["-m", "pytest", "-q", "test_adapter.py"], cwd=adapter_dir, timeout_seconds=60)


def _missing_required_files(adapter_dir: Path) -> list[str]:
    required = ["manifest.json", "adapter.py", "test_adapter.py"]
    return [name for name in required if not (adapter_dir / name).exists()]


def _run_python(args: list[str], *, cwd: Path, timeout_seconds: int) -> _SandboxResult:
    env = os.environ.copy()
    src_root = str(Path(__file__).resolve().parents[2])
    env["PYTHONPATH"] = src_root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    try:
        completed = subprocess.run(
            [sys.executable, *args],
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return _SandboxResult(124, f"sandbox command timed out after {timeout_seconds}s", exc.stdout or "", exc.stderr or "")
    summary = "sandbox command passed" if completed.returncode == 0 else "sandbox command failed"
    return _SandboxResult(completed.returncode, summary, completed.stdout[-8000:], completed.stderr[-8000:])


def _load_promotable_adapter(adapter_dir: Path) -> tuple[Any, object]:
    manifest = _load_manifest(adapter_dir)
    safe_name = safe_path_segment(str(manifest.get("name") or adapter_dir.name), fallback="generated_adapter")
    module_name = f"_ybm_dynamic_{safe_name}_{datetime.now().strftime('%Y%m%d%H%M%S%f')}"
    adapter_path = adapter_dir / "adapter.py"
    spec = importlib.util.spec_from_file_location(module_name, adapter_path)
    if spec is None or spec.loader is None:
        raise ValueError("could not load adapter module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    class_name = manifest.get("adapter_class")
    adapter_cls = getattr(module, str(class_name), None) if class_name else None
    if adapter_cls is None:
        adapter_cls = _find_adapter_class(module)
    if adapter_cls is None:
        raise ValueError("no adapter class with async execute() found")
    adapter = adapter_cls()
    if not inspect.iscoroutinefunction(getattr(adapter, "execute", None)):
        raise ValueError("promoted adapter execute() must be async")
    return _definition_from_manifest(manifest, adapter_dir), adapter


def _find_adapter_class(module: object) -> type | None:
    for value in vars(module).values():
        if inspect.isclass(value) and value.__name__.endswith("Adapter") and hasattr(value, "execute"):
            return value
    return None


def _definition_from_manifest(manifest: dict[str, Any], adapter_dir: Path) -> Any:
    name = safe_path_segment(str(manifest.get("name") or adapter_dir.name), fallback="generated_adapter")
    capability = Capability(str(manifest.get("capability") or Capability.FILESYSTEM_WRITE.value))
    operations = tuple(str(item) for item in manifest.get("operations") or [] if str(item).strip())
    examples = tuple(item for item in manifest.get("examples") or [] if isinstance(item, dict))
    description = str(
        manifest.get("description")
        or manifest.get("objective")
        or f"generated adapter loaded from {adapter_dir}"
    ).strip()
    return ToolDefinition(
        name=name,
        capability=capability,
        enabled=True,
        description=description[:500] or f"generated adapter loaded from {adapter_dir}",
        operations=operations,
        lifecycle="dynamic",
        default_operation=str(manifest.get("default_operation") or "") or None,
        minimum_risk=RiskLevel.HIGH,
        approval_resolver=lambda _value: True,
        examples=examples,
    )


def _load_manifest(adapter_dir: Path) -> dict[str, Any]:
    payload = json.loads((adapter_dir / "manifest.json").read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("manifest.json must contain an object")
    return payload


def _mark_promoted(adapter_dir: Path, tool_name: str) -> None:
    manifest = _load_manifest(adapter_dir)
    manifest["name"] = tool_name
    manifest["status"] = "promoted"
    manifest["promoted_at"] = datetime.now().isoformat(timespec="seconds")
    (adapter_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def _assessment(objective: str) -> str:
    lowered = objective.lower()
    if any(word in lowered for word in ("browser", "desktop", "system", "delete", "move files")):
        return "high_risk_requires_review"
    if any(word in lowered for word in ("api", "cli", "file", "workspace", "telegram")):
        return "moderate_complexity_cacheable"
    return "simple_scaffold_cacheable"


def _readme(name: str, objective: str, capability: str) -> str:
    return f"""# {name}

Generated adapter proposal.

Capability: `{capability}`

Objective:

```text
{objective or "No objective supplied."}
```

This directory is a cache for generated adapter work. The worker can hot-register
the adapter only after `test_adapter.py` passes and `adapter.factory` receives an
approved `promote_after_approval` request for this directory.
"""


def _class_name(name: str) -> str:
    class_name = "".join(part.capitalize() for part in re.findall(r"[A-Za-z0-9]+", name)) or "Generated"
    if class_name[0].isdigit():
        class_name = f"Generated{class_name}"
    return f"{class_name}Adapter"


def _adapter_py(name: str, class_name: str) -> str:
    return f'''from __future__ import annotations

from agent_control.schemas import ErrorClass, ToolCallRequest, ToolCallResult, ToolResultStatus


class {class_name}:
    """Generated adapter proposal. Review before registering."""

    async def execute(self, request: ToolCallRequest) -> ToolCallResult:
        return ToolCallResult(
            request_id=request.id,
            status=ToolResultStatus.FAILED,
            error_class=ErrorClass.ADAPTER_FAILED,
            error_message="generated adapter has not been implemented or reviewed",
        )
'''


def _test_py(name: str, class_name: str) -> str:
    return f'''from __future__ import annotations

import asyncio

from agent_control.schemas import Capability, ToolCallRequest, ToolResultStatus
from adapter import {class_name}


def test_{_safe_identifier_segment(name).lower()}_adapter_succeeds() -> None:
    adapter = {class_name}()
    result = asyncio.run(
        adapter.execute(
            ToolCallRequest(
                task_id="test_task",
                tool_name="{safe_path_segment(name, fallback='generated_adapter')}",
                capability=Capability.FILESYSTEM_WRITE,
                input={{"operation": "test"}},
            )
        )
    )
    assert result.status == ToolResultStatus.SUCCEEDED, result.error_message
'''


def _terminal_output(operation: str, output: dict[str, Any]) -> dict[str, Any]:
    lines = [f"Adapter factory operation completed: {operation}"]
    if output.get("adapter_dir"):
        lines.append(f"Adapter cache: {output['adapter_dir']}")
    if output.get("adapter_name"):
        lines.append(f"Adapter: {output['adapter_name']}")
    if output.get("assessment"):
        lines.append(f"Assessment: {output['assessment']}")
    if output.get("result"):
        lines.append(str(output["result"]))
    if output.get("promoted"):
        lines.append("Promoted: true")
    if operation == "scaffold" and output.get("adapter_dir"):
        lines.append(
            "Proposal is cached and NOT loaded. If the request was only for a proposal or said not to load it, "
            "the task is complete; do not implement, test, or promote it unless explicitly requested."
        )
    return {
        "instance_id": "local-worker",
        "terminal_id": "adapter-factory",
        "content": "\n".join(lines),
        "command_id": None,
        "is_final": True,
        "exit_code": int(output.get("returncode") or 0),
        "source": "adapter_factory",
    }




def register(deps: RegistryDeps, definitions: Definitions, adapters: Adapters) -> None:
    settings = deps.settings
    enabled = capability_enabled(settings, Capability.FILESYSTEM_WRITE) and settings.adapters.adapter_factory.enabled
    definitions.append(
        ToolDefinition(
            name="adapter.factory",
            group="build",
            provides=('new', 'tool', 'integration', 'connector', 'adapter', 'build', 'extend', 'capability',),
            capability=Capability.FILESYSTEM_WRITE,
            enabled=enabled,
            description=(
                "scaffold, sandbox-test, and approval-promote generated adapter proposals under "
                f"{settings.adapters.adapter_factory.root_dir}"
            ),
            operations=("assess", "scaffold", "sandbox_execute_once", "test_connector", "promote_after_approval"),
            lifecycle="scaffold",
            operation_schemas={
                "assess": AdapterFactoryAssessInput,
                "scaffold": AdapterFactoryScaffoldInput,
                "sandbox_execute_once": AdapterFactorySandboxExecuteInput,
                "test_connector": AdapterFactoryTestConnectorInput,
                "promote_after_approval": AdapterFactoryPromoteInput,
            },
            operation_output_schemas={
                "assess": AdapterFactoryAssessOutput,
                "scaffold": AdapterFactoryScaffoldOutput,
                "sandbox_execute_once": AdapterFactorySandboxOutput,
                "test_connector": AdapterFactorySandboxOutput,
                "promote_after_approval": AdapterFactorySandboxOutput,
            },
            default_operation="scaffold",
            operation_risks={
                "assess": RiskLevel.LOW,
                "scaffold": RiskLevel.HIGH,
                "sandbox_execute_once": RiskLevel.HIGH,
                "test_connector": RiskLevel.HIGH,
                "promote_after_approval": RiskLevel.CRITICAL,
            },
            approval_required_operations=("promote_after_approval",),
            approval_reasons={
                "promote_after_approval": "hot-registers a generated, LLM-written adapter into the live tool registry",
            },
            examples=(
                {
                    "operation": "scaffold",
                    "adapter_name": "example_adapter",
                    "objective": "Create an inactive adapter proposal with a manifest and tests.",
                },
            ),
        )
    )
    if settings.adapters.adapter_factory.enabled:
        adapters["adapter.factory"] = AdapterFactoryAdapter(settings.adapters.adapter_factory)
