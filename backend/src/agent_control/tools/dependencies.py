"""Install packages into a task workspace, on purpose and on the record.

`dependencies.install` was a capability with no tool behind it: the enum
existed, the risk table scored it HIGH, the access-mode presets offered it as
a switch, and config could enable it - but nothing in `tools/` ever declared
it, so the switch did nothing and an agent asked to "install what you need"
had no way to comply.

Every guard here exists because this is the tool most able to change a
machine permanently:

* **Allowlist, not denylist.** `adapters.dependencies.allowed_packages` is
  empty by default, so a fresh install can enable the capability and still
  install nothing until someone names what is acceptable. Enumerating what is
  dangerous is a losing game; enumerating what is wanted is not.
* **Never the system environment.** Installs go to a per-task directory under
  the workspace root via `--target`, so a bad package cannot alter the
  interpreter that runs YBM itself.
* **Approval on every call**, not merely at or above a risk threshold - see
  `approval_required_operations` below. Installing arbitrary code is exactly
  the operation a compromised prompt would reach for.
* **No index switching.** `--index-url` / `--extra-index-url` are refused, so
  an install cannot be pointed at an attacker's package index.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
import re
import sys
from typing import Any

from agent_control.config import DependenciesAdapterConfig
from agent_control.schemas import Capability, ToolCallRequest, ToolCallResult, ToolResultStatus
from agent_control.tools.contracts import DependenciesInstallInput, DependenciesOutput
from agent_control.tools.spec import (
    Adapters,
    Definitions,
    RegistryDeps,
    ToolDefinition,
    capability_enabled,
    failed_result,
    same_output_schema,
)


# PEP 508 name plus an optional exact/compatible pin. Deliberately narrow: no
# URLs, no local paths, no VCS refs, no environment markers - each of those is
# a way to fetch code from somewhere the allowlist never described.
_REQUIREMENT = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]{0,63})\s*(==|~=|>=)?\s*([A-Za-z0-9._-]{1,32})?$")


def _package_name(requirement: str) -> str | None:
    match = _REQUIREMENT.match(requirement.strip())
    return match.group(1).lower().replace("_", "-") if match else None


class DependenciesAdapter:
    def __init__(self, config: DependenciesAdapterConfig) -> None:
        self.config = config

    async def execute(self, request: ToolCallRequest) -> ToolCallResult:
        operation = str(request.input.get("operation") or "install")
        try:
            if operation == "install":
                output = await self._install(request)
            elif operation == "list_allowed":
                output = self._list_allowed()
            else:
                return failed_result(request, f"unsupported dependencies operation: {operation}")
        except Exception as exc:  # noqa: BLE001 - reported to the operator
            return failed_result(request, f"dependency install failed: {exc}")
        output["operation"] = operation
        return ToolCallResult(request_id=request.id, status=ToolResultStatus.SUCCEEDED, output=output)

    def _list_allowed(self) -> dict[str, Any]:
        allowed = sorted(self.config.allowed_packages)
        return {
            "allowed_packages": allowed,
            "installed": [],
            "summary": (
                f"{len(allowed)} package(s) may be installed: {', '.join(allowed)}"
                if allowed
                else "No packages are allowed yet. Add them to adapters.dependencies.allowed_packages."
            ),
        }

    async def _install(self, request: ToolCallRequest) -> dict[str, Any]:
        requirements = [str(item).strip() for item in (request.input.get("packages") or []) if str(item).strip()]
        if not requirements:
            raise ValueError("no packages given")
        if len(requirements) > self.config.max_packages_per_call:
            raise ValueError(
                f"{len(requirements)} packages requested; at most "
                f"{self.config.max_packages_per_call} per call"
            )

        allowed = {name.lower().replace("_", "-") for name in self.config.allowed_packages}
        for requirement in requirements:
            if any(flag in requirement for flag in ("--index-url", "--extra-index-url", "-i ")):
                raise ValueError(f"refusing to switch package index: {requirement!r}")
            name = _package_name(requirement)
            if name is None:
                raise ValueError(
                    f"{requirement!r} is not a plain name==version requirement "
                    "(URLs, paths and VCS refs are refused)"
                )
            if name not in allowed:
                raise ValueError(
                    f"{name!r} is not in adapters.dependencies.allowed_packages "
                    f"({', '.join(sorted(allowed)) or 'empty'})"
                )

        target = Path(self.config.target_root).expanduser().resolve() / (request.task_id or "shared")
        target.mkdir(parents=True, exist_ok=True)
        command = [
            sys.executable, "-m", "pip", "install",
            "--target", str(target),
            "--no-input", "--disable-pip-version-check", "--no-color",
            *requirements,
        ]
        process = await asyncio.create_subprocess_exec(
            *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=self.config.timeout_seconds)
        except asyncio.TimeoutError:
            process.kill()
            raise ValueError(f"pip install exceeded {self.config.timeout_seconds}s") from None
        text = (stdout or b"").decode("utf-8", errors="replace")[-self.config.max_output_chars:]
        if process.returncode != 0:
            raise ValueError(f"pip exited {process.returncode}: {text[-800:]}")
        return {
            "installed": requirements,
            "allowed_packages": sorted(allowed),
            "target_dir": str(target),
            "summary": (
                f"Installed {', '.join(requirements)} into {target}. "
                "Add this directory to sys.path in code.interpreter to import them."
            ),
            "terminal_output": [{"command": "pip install", "output": text}],
        }


def register(deps: RegistryDeps, definitions: Definitions, adapters: Adapters) -> None:
    settings = deps.settings
    config = settings.adapters.dependencies
    enabled = config.enabled and capability_enabled(settings, Capability.DEPENDENCIES_INSTALL)
    definitions.append(
        ToolDefinition(
            name="dependencies.install",
            group="code",
            provides=('install', 'package', 'pip', 'library', 'module', 'dependency',),
            capability=Capability.DEPENDENCIES_INSTALL,
            enabled=enabled,
            description=(
                "install Python packages into a per-task directory (never the system environment), "
                f"limited to the configured allowlist: {', '.join(sorted(config.allowed_packages)) or '<none>'}"
            ),
            operations=("install", "list_allowed"),
            operation_schemas={
                "install": DependenciesInstallInput,
                "list_allowed": DependenciesInstallInput,
            },
            output_schema=DependenciesOutput,
            operation_output_schemas=same_output_schema(("install", "list_allowed"), DependenciesOutput),
            # Not merely "high risk, so it lands above the approval floor":
            # bound to the operation so it is asked for even under an
            # access-mode preset that raises the floor. Running someone else's
            # code on this machine is the request a hijacked prompt makes.
            approval_required_operations=("install",),
            approval_reasons={"install": "Installs third-party code onto this machine."},
        )
    )
    if enabled:
        adapters["dependencies.install"] = DependenciesAdapter(config)
