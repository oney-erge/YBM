from __future__ import annotations

import ast
import asyncio
from dataclasses import dataclass, field
import logging
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

from agent_control.config import CodeInterpreterAdapterConfig
from agent_control.llm.providers import LLMProvider, strip_code_fences
from agent_control.prompts import prompt_text, render_prompt
from agent_control.schemas import (
    Artifact,
    ArtifactType,
    Capability,
    ErrorClass,
    RiskLevel,
    ToolCallRequest,
    ToolCallResult,
    ToolResultStatus,
    ToolVerification,
)
from agent_control.tools.contracts import (
    CodeInterpreterBuildTempHelperInput,
    CodeInterpreterGenerateAndRunInput,
    CodeInterpreterHealthInput,
    CodeInterpreterInspectStateInput,
    CodeInterpreterOutput,
    CodeInterpreterRepairScriptInput,
    CodeInterpreterRunPythonInput,
    CodeInterpreterSolveOnceInput,
)
from agent_control.tools.path_utils import safe_path_segment
from agent_control.tools.spec import Adapters, Definitions, RegistryDeps, ToolDefinition, capability_enabled, failed_result


logger = logging.getLogger(__name__)


class GeneratedPythonScript(BaseModel):
    summary: str = Field(min_length=1)
    code: str = Field(min_length=1)
    expected_files: list[str] = Field(default_factory=list)


class ApprovalRequired(RuntimeError):
    pass


@dataclass(frozen=True)
class CodeExecutionPlan:
    request_id: str
    task_id: str
    code: str
    workspace: Path
    script_path: Path
    timeout_seconds: int
    generated: bool
    backend: str
    execution_profile: str
    allow_network: bool
    requirements: list[str] = field(default_factory=list)
    session_id: str | None = None
    persist_session: bool = False


@dataclass(frozen=True)
class CodeExecutionResult:
    returncode: int
    stdout: str
    stderr: str
    backend: str
    sandboxed: bool
    resource_usage: dict[str, Any] = field(default_factory=dict)
    resource_limits: dict[str, Any] = field(default_factory=dict)
    network_enabled: bool = False
    session_id: str | None = None
    rich_outputs: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class ProcessExecutionResult:
    returncode: int
    stdout: str
    stderr: str
    duration_seconds: float
    pid: int | None = None


class CodeExecutionBackend:
    name = "backend"
    sandboxed = False

    async def execute(self, plan: CodeExecutionPlan) -> CodeExecutionResult:
        raise NotImplementedError

    async def health(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "enabled": True,
            "available": False,
            "sandboxed": self.sandboxed,
            "summary": "backend does not implement health",
        }

    def available(self) -> bool:
        return True


class LocalSubprocessBackend(CodeExecutionBackend):
    name = "local_subprocess"
    sandboxed = False

    def __init__(self, config: CodeInterpreterAdapterConfig) -> None:
        self.config = config

    async def execute(self, plan: CodeExecutionPlan) -> CodeExecutionResult:
        executable = self.config.python_executable or sys.executable
        result = await _run_python_script(executable, plan.script_path, plan.workspace, timeout=plan.timeout_seconds)
        return CodeExecutionResult(
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            backend=self.name,
            sandboxed=self.sandboxed,
            resource_usage={"duration_seconds": result.duration_seconds, "pid": result.pid},
            resource_limits=_resource_limits_dict(self.config),
            network_enabled=True,
            session_id=plan.session_id,
        )

    async def health(self) -> dict[str, Any]:
        executable = self.config.python_executable or sys.executable
        path = shutil.which(executable) if executable != sys.executable else executable
        return {
            "name": self.name,
            "enabled": True,
            "available": bool(path),
            "sandboxed": self.sandboxed,
            "python_executable": executable,
            "summary": "local Python subprocess backend is available" if path else "python executable was not found",
        }


class DockerPythonBackend(CodeExecutionBackend):
    name = "docker_python"
    sandboxed = True

    def __init__(self, config: CodeInterpreterAdapterConfig) -> None:
        self.config = config

    def available(self) -> bool:
        if not self.config.docker.enabled:
            return False
        return shutil.which(self.config.docker.docker_path) is not None

    async def execute(self, plan: CodeExecutionPlan) -> CodeExecutionResult:
        if not self.config.docker.enabled:
            raise RuntimeError("docker backend is disabled")
        docker = self.config.docker.docker_path
        if shutil.which(docker) is None:
            raise RuntimeError("docker executable was not found")
        await self._ensure_image()
        container_name = f"ybm-code-{uuid4().hex[:12]}"
        mount_target = self.config.docker.workspace_mount_target.rstrip("/") or "/workspace"
        script_inside = f"{mount_target}/{plan.script_path.name}"
        command = self._container_command(plan, script_inside)
        args = [
            docker,
            "run",
            "--rm" if self.config.docker.remove_container else "--name",
        ]
        if self.config.docker.remove_container:
            args.extend(["--name", container_name])
        else:
            args.append(container_name)
        args.extend(
            [
                "--workdir",
                mount_target,
                "--mount",
                f"type=bind,source={plan.workspace},target={mount_target}",
                "--memory",
                self.config.resource_limits.memory,
                "--cpus",
                str(self.config.resource_limits.cpus),
                "--pids-limit",
                str(self.config.resource_limits.pids_limit),
                "--security-opt",
                "no-new-privileges",
            ]
        )
        if not plan.allow_network:
            args.extend(["--network", "none"])
        if self.config.docker.read_only_rootfs:
            args.extend(["--read-only", "--tmpfs", "/tmp:rw,nosuid,nodev,size=64m"])
        if self.config.docker.run_as_user:
            args.extend(["--user", self.config.docker.run_as_user])
        args.append(self.config.docker.image)
        args.extend(command)

        try:
            result = await _run_command(args, cwd=plan.workspace, timeout=plan.timeout_seconds)
        except TimeoutError:
            await _best_effort_docker_rm(docker, container_name)
            raise

        return CodeExecutionResult(
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            backend=self.name,
            sandboxed=self.sandboxed,
            resource_usage={
                "duration_seconds": result.duration_seconds,
                "pid": result.pid,
                "container_name": container_name,
            },
            resource_limits=_resource_limits_dict(self.config),
            network_enabled=plan.allow_network,
            session_id=plan.session_id,
        )

    async def health(self) -> dict[str, Any]:
        enabled = self.config.docker.enabled
        docker = self.config.docker.docker_path
        executable = shutil.which(docker)
        result: dict[str, Any] = {
            "name": self.name,
            "enabled": enabled,
            "available": False,
            "sandboxed": self.sandboxed,
            "docker_path": docker,
            "image": self.config.docker.image,
            "network_default": self.config.docker.network_enabled,
            "resource_limits": _resource_limits_dict(self.config),
        }
        if not enabled:
            result["summary"] = "docker backend is disabled"
            return result
        if executable is None:
            result["summary"] = "docker executable was not found"
            return result
        try:
            version = await _run_command(
                [docker, "version", "--format", "{{.Server.Version}}"],
                cwd=Path.cwd(),
                timeout=5,
            )
            result["available"] = version.returncode == 0
            result["server_version"] = version.stdout.strip()
            result["summary"] = "docker backend is available" if version.returncode == 0 else version.stderr.strip()
        except Exception as exc:
            result["summary"] = str(exc)
        return result

    async def _ensure_image(self) -> None:
        policy = self.config.docker.pull_policy
        if policy == "never":
            return
        docker = self.config.docker.docker_path
        image = self.config.docker.image
        if policy == "missing":
            inspect = await _run_command([docker, "image", "inspect", image], cwd=Path.cwd(), timeout=15)
            if inspect.returncode == 0:
                return
        pull = await _run_command([docker, "pull", image], cwd=Path.cwd(), timeout=300)
        if pull.returncode != 0:
            raise RuntimeError(f"docker image pull failed: {pull.stderr.strip() or pull.stdout.strip()}")

    def _container_command(self, plan: CodeExecutionPlan, script_inside: str) -> list[str]:
        if not plan.requirements:
            return ["python", script_inside]
        if self.config.package_policy != "allow_request":
            raise ValueError("package installation is disabled for code.interpreter")
        _validate_requested_packages(plan.requirements, set(self.config.allowed_packages))
        packages = " ".join(shlex.quote(item) for item in plan.requirements)
        script_arg = shlex.quote(script_inside)
        return ["sh", "-lc", f"python -m pip install --disable-pip-version-check {packages} && python {script_arg}"]


class UnavailableExecutionBackend(CodeExecutionBackend):
    sandboxed = True

    def __init__(self, name: str, reason: str) -> None:
        self.name = name
        self.reason = reason

    def available(self) -> bool:
        return False

    async def execute(self, plan: CodeExecutionPlan) -> CodeExecutionResult:
        raise RuntimeError(self.reason)

    async def health(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "enabled": False,
            "available": False,
            "sandboxed": self.sandboxed,
            "summary": self.reason,
        }


class CodeInterpreterAdapter:
    """Bounded local Python execution in a managed task workspace."""

    def __init__(
        self,
        config: CodeInterpreterAdapterConfig,
        provider: LLMProvider | None = None,
        artifacts: object | None = None,
    ) -> None:
        self.config = config
        self.provider = provider
        # Optional artifact repository - when present, files produced by a script
        # are registered as task artifacts so later steps (e.g. artifact.deliver)
        # can find them by id without any path guessing.
        self.artifacts = artifacts
        self._backends = self._build_backends()
        self._last_backend_failures: dict[str, str] = {}

    async def execute(self, request: ToolCallRequest) -> ToolCallResult:
        if not self.config.enabled:
            return failed_result(request, "code interpreter adapter is disabled")
        operation = str(request.input.get("operation") or "run_python")
        try:
            if operation == "run_python":
                output = await self._run_python(request, generated=False)
            elif operation in {"generate_and_run", "solve_once", "build_temp_helper", "repair_script"}:
                output = await self._generate_and_run(_normalize_generation_request(request, operation))
            elif operation == "inspect_state":
                output = self._inspect_state(request)
            elif operation == "health":
                output = await self._health(request)
            else:
                return failed_result(request, f"unsupported code interpreter operation: {operation}")
        except ApprovalRequired as exc:
            return ToolCallResult(
                request_id=request.id,
                status=ToolResultStatus.NEEDS_APPROVAL,
                error_class=ErrorClass.POLICY_DENIED,
                error_message=str(exc),
            )
        except TimeoutError:
            return ToolCallResult(
                request_id=request.id,
                status=ToolResultStatus.TIMEOUT,
                error_class=ErrorClass.TRANSIENT,
                error_message="code interpreter command timed out",
            )
        except Exception as exc:
            return failed_result(request, f"code interpreter operation failed: {exc}")

        output["operation"] = operation
        output["terminal_output"] = [_terminal_output(operation, output)]
        status = ToolResultStatus.SUCCEEDED if int(output.get("returncode") or 0) == 0 else ToolResultStatus.FAILED
        return ToolCallResult(
            request_id=request.id,
            status=status,
            output=output,
            error_class=ErrorClass.ADAPTER_FAILED if status == ToolResultStatus.FAILED else None,
            error_message=(output.get("summary") if status == ToolResultStatus.FAILED else None),
        )

    async def _generate_and_run(self, request: ToolCallRequest) -> dict[str, Any]:
        if self.provider is None:
            raise ValueError("LLM provider is required for generate_and_run")
        objective = str(request.input["objective"]).strip()
        workspace = self._workspace(request)
        context_text = str(request.input.get("context") or "No extra context.")
        generation_repaired = False
        regenerated_from_failure = False

        async def _generate(previous_attempt: str) -> "GeneratedPythonScript":
            return await self.provider.generate_structured(
                prompt_text("base/code_interpreter_system.md"),
                render_prompt(
                    "tasks/code_interpreter_user.md",
                    objective=objective,
                    context=context_text,
                    workspace_dir=str(workspace),
                    previous_attempt=previous_attempt,
                ),
                GeneratedPythonScript,
            )

        try:
            generated = await _generate("")
            generated = generated.model_copy(update={"code": _clean_generated_code(generated.code)})
        except Exception:
            fallback = _fallback_generated_script(objective)
            if fallback is None:
                raise
            generated = fallback
            generation_repaired = True

        updated = request.model_copy(update={"input": {**request.input, "code": generated.code}})
        try:
            output = await self._run_python(updated, generated=True)
        except (SyntaxError, ValueError) as exc:
            # Syntax errors mean the LLM produced invalid Python. Regenerate
            # with the offending code + the parser error. If the retry still
            # raises (e.g. the LLM produced the same broken code, or
            # regeneration itself failed), fall back to the static template.
            regenerated_from_failure = True
            output = None
            try:
                retry_gen = await _generate(_previous_attempt_block(generated.code, str(exc), kind="parse_error"))
                retry_generated = retry_gen.model_copy(update={"code": _clean_generated_code(retry_gen.code)})
                retry_updated = request.model_copy(update={"input": {**request.input, "code": retry_generated.code}})
                try:
                    output = await self._run_python(retry_updated, generated=True)
                    generated = retry_generated
                    updated = retry_updated
                except (SyntaxError, ValueError):
                    output = None
            except Exception:
                output = None
            if output is None:
                fallback = _fallback_generated_script(objective)
                if fallback is None:
                    raise exc
                generation_repaired = True
                generated = fallback
                updated = request.model_copy(update={"input": {**request.input, "code": generated.code}})
                output = await self._run_python(updated, generated=True)

        if int(output.get("returncode") or 0) != 0:
            # Runtime crash. Show the LLM what its code produced and ask it to
            # take a different approach (different libraries, different
            # algorithm, defensive defaults). This is the key fix for the
            # "LLM regenerates the same broken script" failure class.
            regenerated_from_failure = True
            previous_block = _previous_attempt_block(
                generated.code,
                f"{output.get('stderr','')}\nstdout: {output.get('stdout','')}",
                kind="runtime_error",
            )
            try:
                retry_gen = await _generate(previous_block)
                generated = retry_gen.model_copy(update={"code": _clean_generated_code(retry_gen.code)})
                updated = request.model_copy(update={"input": {**request.input, "code": generated.code}})
                output = await self._run_python(updated, generated=True)
            except Exception:
                # If the LLM regen call itself fails (provider error, schema
                # validation, etc.), fall back to the static template as a
                # final attempt - same as before.
                fallback = _fallback_generated_script(objective)
                if fallback is not None and fallback.code != generated.code:
                    generation_repaired = True
                    generated = fallback
                    updated = request.model_copy(update={"input": {**request.input, "code": generated.code}})
                    output = await self._run_python(updated, generated=True)

        output["generation_summary"] = generated.summary
        output["expected_files"] = generated.expected_files
        if generation_repaired:
            output["generation_repaired"] = True
        if regenerated_from_failure:
            output["regenerated_from_failure"] = True
        return output

    def _inspect_state(self, request: ToolCallRequest) -> dict[str, Any]:
        workspace = self._workspace(request)
        files = _relative_files(workspace, max_files=int(request.input.get("max_files") or self.config.max_files_listed))
        return {
            "workspace_dir": str(workspace),
            "script_path": None,
            "files_before": files,
            "files_after": files,
            "files_created": [],
            "stdout": "\n".join(files),
            "stderr": "",
            "returncode": 0,
            "summary": f"Inspected {len(files)} file(s) in {workspace}.",
            "generated": False,
            "backend": None,
            "execution_profile": "inspect",
            "sandboxed": False,
            "resource_usage": {},
            "resource_limits": _resource_limits_dict(self.config),
            "network_enabled": False,
            "session_id": request.input.get("session_id"),
            "files_modified": [],
            "files_deleted": [],
            "rich_outputs": [],
        }

    async def _health(self, request: ToolCallRequest) -> dict[str, Any]:
        backends = []
        for name, backend in self._backends.items():
            health = await backend.health()
            if name in self._last_backend_failures:
                health["last_failure"] = self._last_backend_failures[name]
            backends.append(health)
        configured_remote = sorted(self.config.remote_backends)
        available = [item["name"] for item in backends if item.get("available")]
        safety_warnings = []
        if not self.config.blocked_imports:
            safety_warnings.append("dangerous import blocklist is empty; local subprocess execution is not sandboxed")
        if self.config.untrusted_default_backend == "docker_python" and "docker_python" not in available:
            safety_warnings.append(
                "untrusted_default_backend is docker_python but the Docker backend is unavailable "
                "(disabled or not running); "
                + (
                    "local fallback is enabled, so untrusted/generated code currently runs UNSANDBOXED "
                    "on the host. Enable adapters.code_interpreter.docker or set "
                    "fallback_to_local_when_backend_unavailable: false to require Docker."
                    if self.config.fallback_to_local_when_backend_unavailable
                    else "untrusted/generated runs will be refused until Docker is available."
                )
            )
        stdout = "\n".join(f"{item['name']}: {item.get('summary', '')}" for item in backends)
        if safety_warnings:
            stdout = f"{stdout}\nSafety warnings:\n" + "\n".join(f"- {item}" for item in safety_warnings)
        summary = (
            f"Code interpreter health: {len(available)} available backend(s)."
            if available
            else "Code interpreter health: no execution backend is currently available."
        )
        return {
            "workspace_dir": str(self._workspace(request)),
            "script_path": None,
            "files_before": [],
            "files_after": [],
            "files_created": [],
            "files_modified": [],
            "files_deleted": [],
            "artifact_ids": [],
            "stdout": stdout,
            "stderr": "",
            "returncode": 0 if available else 1,
            "summary": summary,
            "generated": False,
            "backend": None,
            "execution_profile": "health",
            "sandboxed": False,
            "resource_usage": {},
            "resource_limits": _resource_limits_dict(self.config),
            "network_enabled": False,
            "session_id": None,
            "rich_outputs": [],
            "health": {
                "default_backend": self.config.default_backend,
                "untrusted_default_backend": self.config.untrusted_default_backend,
                "configured_backends": list(self.config.backends),
                "available_backends": available,
                "backends": backends,
                "configured_remote_backends": configured_remote,
                "last_failures": dict(self._last_backend_failures),
                "safety_warnings": safety_warnings,
            },
        }

    async def _run_python(self, request: ToolCallRequest, *, generated: bool) -> dict[str, Any]:
        code = str(request.input["code"])
        if len(code) > self.config.max_code_chars:
            raise ValueError(f"code exceeds configured limit of {self.config.max_code_chars} characters")
        _validate_python(code, allowed_imports=set(self.config.allowed_imports), blocked_imports=set(self.config.blocked_imports))
        workspace = self._workspace(request)
        workspace.mkdir(parents=True, exist_ok=True)
        execution_profile = _execution_profile(request, generated=generated)
        # Backend selection has no side effects (it doesn't touch the
        # filesystem or execute anything) - resolving it before the approval
        # check lets that check see whether this run silently fell back to
        # an unsandboxed backend, not just whether the code is "generated".
        backend, backend_fallback_warning = self._select_backend(request, generated=generated, execution_profile=execution_profile)
        if self._approval_required_for_run_python(
            request,
            generated=generated,
            execution_profile=execution_profile,
            backend_fallback_warning=backend_fallback_warning,
        ):
            reason = (
                f"generated code would run unsandboxed: {backend_fallback_warning}"
                if generated and backend_fallback_warning
                else "untrusted run_python requires approval before execution"
            )
            raise ApprovalRequired(reason)
        before_snapshot = _file_snapshot(workspace, max_files=self.config.max_files_listed)
        before = sorted(before_snapshot)
        script_path = _safe_child_path(workspace, str(request.input.get("script_name") or "script.py"))
        if script_path.suffix.lower() != ".py":
            script_path = script_path.with_suffix(".py")
        script_path.write_text(code, encoding="utf-8")
        timeout = int(request.input.get("timeout_seconds") or self.config.timeout_seconds)
        allow_network = self._network_enabled(request)
        plan = CodeExecutionPlan(
            request_id=request.id,
            task_id=request.task_id,
            code=code,
            workspace=workspace,
            script_path=script_path,
            timeout_seconds=timeout,
            generated=generated,
            backend=backend.name,
            execution_profile=execution_profile,
            allow_network=allow_network,
            requirements=[str(item).strip() for item in request.input.get("requirements", []) if str(item).strip()],
            session_id=str(request.input.get("session_id") or "") or None,
            persist_session=bool(request.input.get("persist_session") or False),
        )
        try:
            execution = await backend.execute(plan)
        except Exception as exc:
            self._last_backend_failures[backend.name] = str(exc)
            raise
        stdout = execution.stdout[: self.config.max_output_chars]
        stderr = execution.stderr[: self.config.max_output_chars]
        after_snapshot = _file_snapshot(workspace, max_files=self.config.max_files_listed)
        after = sorted(after_snapshot)
        created = sorted(set(after_snapshot) - set(before_snapshot))
        modified = sorted(
            name
            for name, metadata in after_snapshot.items()
            if name in before_snapshot and before_snapshot[name] != metadata
        )
        deleted = sorted(set(before_snapshot) - set(after_snapshot))
        summary = _summary(execution.returncode, stdout, stderr, created)
        if backend_fallback_warning:
            summary = f"{summary}\nWarning: {backend_fallback_warning}"
        artifact_ids = self._register_created_artifacts(request.task_id, workspace, created)
        # Same exclusion as _register_created_artifacts: script.py is the
        # generator's own implementation detail, not a user-facing output.
        preview_candidates = [name for name in created + modified if name != "script.py"]
        file_previews = _file_content_previews(workspace, preview_candidates)
        return {
            "workspace_dir": str(workspace),
            "script_path": str(script_path),
            "files_before": before,
            "files_after": after,
            "files_created": created,
            "files_modified": modified,
            "files_deleted": deleted,
            "file_previews": [{"path": name, "content": text} for name, text in file_previews],
            "artifact_ids": artifact_ids,
            "stdout": stdout,
            "stderr": stderr,
            "returncode": execution.returncode,
            "summary": summary,
            "generated": generated,
            "backend": execution.backend,
            "execution_profile": execution_profile,
            "sandboxed": execution.sandboxed,
            "resource_usage": execution.resource_usage,
            "resource_limits": execution.resource_limits,
            "network_enabled": execution.network_enabled,
            "session_id": execution.session_id,
            "rich_outputs": execution.rich_outputs,
            "backend_fallback_warning": backend_fallback_warning,
        }

    def _build_backends(self) -> dict[str, CodeExecutionBackend]:
        backends: dict[str, CodeExecutionBackend] = {
            "local_subprocess": LocalSubprocessBackend(self.config),
            "docker_python": DockerPythonBackend(self.config),
            "jupyter_kernel": UnavailableExecutionBackend(
                "jupyter_kernel",
                "jupyter backend is configured for a future stateful session runner and is disabled in this build",
            ),
        }
        for name, remote in self.config.remote_backends.items():
            reason = (
                f"remote backend {name!r} is disabled"
                if not remote.enabled
                else f"remote backend {name!r} is configured but no SDK adapter is installed in this build"
            )
            backends.setdefault(name, UnavailableExecutionBackend(name, reason))
        for name in ("e2b", "daytona", "modal", "openai_code_interpreter"):
            backends.setdefault(name, UnavailableExecutionBackend(name, f"remote backend {name!r} is not configured"))
        return backends

    def _select_backend(
        self,
        request: ToolCallRequest,
        *,
        generated: bool,
        execution_profile: str,
    ) -> tuple[CodeExecutionBackend, str | None]:
        explicit = str(request.input.get("backend") or "").strip()
        requested = explicit
        if not requested:
            requested = self.config.untrusted_default_backend if _is_untrusted_profile(execution_profile) else self.config.default_backend
        if requested not in self._backends:
            raise ValueError(f"unsupported code interpreter backend: {requested}")
        backend = self._backends[requested]
        if requested == "docker_python" and not backend.available():
            if explicit or not self.config.fallback_to_local_when_backend_unavailable:
                raise RuntimeError("docker backend is not available")
            warning = (
                "docker_python backend is unavailable (Docker disabled or not running); fell back to "
                f"local_subprocess, which does NOT sandbox this {execution_profile} code. Enable "
                "adapters.code_interpreter.docker or set fallback_to_local_when_backend_unavailable: false "
                "to refuse untrusted runs instead."
            )
            return self._backends["local_subprocess"], warning
        if requested not in self.config.backends and requested not in self.config.remote_backends:
            if explicit:
                raise ValueError(f"code interpreter backend is not enabled in config: {requested}")
            return self._backends.get(self.config.default_backend, self._backends["local_subprocess"]), None
        return backend, None

    def _network_enabled(self, request: ToolCallRequest) -> bool:
        requested = bool(request.input.get("allow_network") or False)
        if self.config.network_policy == "always_disabled":
            return False
        if self.config.network_policy in {"allow_if_requested", "disabled_by_default"}:
            return requested
        return bool(self.config.docker.network_enabled)

    def _approval_required_for_run_python(
        self,
        request: ToolCallRequest,
        *,
        generated: bool,
        execution_profile: str,
        backend_fallback_warning: str | None,
    ) -> bool:
        if not self.config.require_approval_for_untrusted_run_python:
            return False
        if bool(request.input.get("approved") or False):
            return False
        if generated:
            # generate_and_run is meant to be a self-contained, automatic
            # operation, so generated code is normally exempt from approval -
            # but not when it silently fell back from the configured
            # sandboxed backend to unsandboxed local_subprocess (Docker
            # unavailable): that's full process-privilege execution of
            # LLM-authored code with no human review at all (docs/HISTORY.md
            # P5). An admin who explicitly configured local_subprocess as
            # untrusted_default_backend (no fallback occurred, this is None)
            # has already made that call and isn't re-prompted per call.
            return backend_fallback_warning is not None
        if not _is_untrusted_profile(execution_profile):
            return False
        return True

    def _register_created_artifacts(self, task_id: str, workspace: Path, created: list[str]) -> list[str]:
        """Register each newly-created file as a task artifact.

        Skip the generator's own ``script.py`` (an implementation detail, not a
        user-facing output). The returned ids let downstream tools like
        ``artifact.deliver`` resolve the file without path guessing.
        """
        if self.artifacts is None or not created:
            return []
        ids: list[str] = []
        for relative in created:
            if relative == "script.py":
                continue
            full = (workspace / relative).resolve()
            try:
                if not full.is_file():
                    continue
            except OSError:
                continue
            try:
                artifact = self.artifacts.create(   # type: ignore[attr-defined]
                    Artifact(
                        task_id=task_id,
                        type=ArtifactType.GENERATED_FILE,
                        uri=str(full),
                        content_preview=f"Generated by code.interpreter: {relative}",
                        metadata={"source": "code.interpreter", "workspace_dir": str(workspace)},
                    )
                )
                ids.append(artifact.id)
            except Exception:
                # Registration is best-effort - a missing repo or DB hiccup must
                # not break the actual code execution result. But it does mean
                # a generated file that artifact.deliver won't be able to find
                # later, which is worth knowing about, not just swallowing.
                logger.warning("failed to register generated artifact %s", relative, exc_info=True)
                continue
        return ids

    def _workspace(self, request: ToolCallRequest) -> Path:
        """One stable workspace per TASK, not per call.

        This used to append `uuid4().hex[:8]`, giving every individual
        code.interpreter call its own fresh directory. Task ids are already
        unique, so that suffix only ever distinguished repeated calls *within
        one task* - and it silently broke every multi-step file workflow the
        Operator loop is built to do:

        - `generate_and_run` writes report.csv into dir A; the next
          `generate_and_run` in the same task gets empty dir B and cannot see
          it, so "make a file, then summarize it" can never work.
        - `inspect_state` always got a brand-new empty directory, so it
          reported zero files no matter what the task had just created.
        - `run_python` could never read a file an earlier step generated.

        Found 2026-07-29 re-recording the code_interpreter_csv_summary
        scenario (docs/HISTORY.md Part 2 §4 item 7), which failed identically
        on three independent live attempts. A caller can still pass an
        explicit `workspace_dir` to opt out; the containment check below is
        what keeps that from escaping the configured root.
        """
        root = Path(self.config.workspace_root).expanduser().resolve()
        if request.input.get("workspace_dir"):
            workspace = Path(str(request.input["workspace_dir"])).expanduser().resolve()
        else:
            workspace = root / f"task_{safe_path_segment(request.task_id, fallback='task')}"
        if root != workspace and root not in workspace.parents:
            raise ValueError(f"workspace is outside configured code interpreter root: {workspace}")
        return workspace


def _validate_python(code: str, *, allowed_imports: set[str], blocked_imports: set[str]) -> None:
    tree = ast.parse(code, mode="exec")
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                _validate_import(alias.name, allowed_imports=allowed_imports, blocked_imports=blocked_imports)
        elif isinstance(node, ast.ImportFrom):
            _validate_import(node.module or "", allowed_imports=allowed_imports, blocked_imports=blocked_imports)
        elif isinstance(node, ast.Call):
            name = _call_name(node.func)
            if name in {"eval", "exec", "compile", "__import__", "input"}:
                raise ValueError(f"blocked unsafe builtin call: {name}")


def _validate_import(module: str, *, allowed_imports: set[str], blocked_imports: set[str]) -> None:
    root = module.split(".", 1)[0]
    if root in blocked_imports:
        raise ValueError(f"blocked import: {root}")
    # When an allowlist is configured (non-empty), any import outside it is
    # rejected. Empty allowlist = no whitelist constraint (default behavior).
    if allowed_imports and root not in allowed_imports:
        raise ValueError(f"import not in allowed_imports allowlist: {root}")


def _call_name(value: ast.AST) -> str | None:
    if isinstance(value, ast.Name):
        return value.id
    if isinstance(value, ast.Attribute):
        return value.attr
    return None


def _normalize_generation_request(request: ToolCallRequest, operation: str) -> ToolCallRequest:
    payload = dict(request.input)
    objective = str(payload.get("objective") or "").strip()
    context = str(payload.get("context") or "").strip()
    if operation == "solve_once":
        context = f"{context}\nSolve this with one bounded Python helper run.".strip()
    elif operation == "build_temp_helper":
        context = f"{context}\nBuild a temporary helper script; do not promote it as a permanent connector.".strip()
    elif operation == "repair_script":
        failing_code = str(payload.get("failing_code") or "").strip()
        error_text = str(payload.get("error_text") or "").strip()
        repair_context = _previous_attempt_block(failing_code, error_text, kind="runtime_error") if failing_code or error_text else ""
        context = f"{context}\n{repair_context}".strip()
        if not objective:
            objective = "Repair the failing Python helper script and run the corrected version."
    payload["operation"] = "generate_and_run"
    if objective:
        payload["objective"] = objective
    if context:
        payload["context"] = context
    return request.model_copy(update={"input": payload})


def _previous_attempt_block(prev_code: str, error_text: str, *, kind: str) -> str:
    """Format the failed prior attempt so the LLM can fix its own mistake.

    ``kind`` is ``"parse_error"`` or ``"runtime_error"``. Truncated aggressively
    to keep the regeneration prompt under control.
    """
    error_snippet = (error_text or "").strip()[-1500:]
    code_snippet = (prev_code or "").strip()
    if len(code_snippet) > 1800:
        code_snippet = code_snippet[:1800] + "\n# ... (truncated)"
    label = "parse error" if kind == "parse_error" else "runtime error"
    return (
        f"Previous attempt to write code for this objective FAILED with a {label}.\n"
        f"DO NOT regenerate the same script. Take a different approach: use a different\n"
        f"library, a different algorithm, or simpler defensive defaults. If the previous\n"
        f"attempt depended on a non-standard package that wasn't installed, switch to the\n"
        f"Python standard library only.\n"
        f"\n"
        f"Previous code:\n"
        f"```python\n{code_snippet}\n```\n"
        f"\n"
        f"Error/stderr from the failed run:\n"
        f"```\n{error_snippet}\n```"
    )


def _clean_generated_code(code: str) -> str:
    text = strip_code_fences(code)
    if "\\n" in text and "\n" not in text:
        try:
            text = text.encode("utf-8").decode("unicode_escape")
        except Exception:
            logger.debug("unicode_escape decode failed; leaving text unchanged", exc_info=True)
    return text


def _fallback_generated_script(objective: str) -> GeneratedPythonScript | None:
    output_name = _output_name_from_objective(objective)
    if output_name is None:
        return None
    suffix = Path(output_name).suffix.lower()
    if suffix == ".xlsx":
        return _fallback_excel_script(output_name, objective)
    if suffix not in {".md", ".txt", ".json", ".csv"}:
        return None
    content = _fallback_content(objective, output_name)
    code = (
        "from pathlib import Path\n"
        f"output = Path({output_name!r})\n"
        f"output.write_text({content!r}, encoding='utf-8')\n"
        "print(f'created {output}')\n"
    )
    return GeneratedPythonScript(
        summary=f"Create {output_name} with a deterministic fallback script.",
        code=code,
        expected_files=[output_name],
    )


def _output_name_from_objective(objective: str) -> str | None:
    match = re.search(r"\b(?:named|called|file)\s+([A-Za-z0-9_.-]+\.(?:md|txt|json|csv|xlsx))\b", objective, flags=re.IGNORECASE)
    if match:
        return _safe_output_name(match.group(1))
    match = re.search(r"\b([A-Za-z0-9_.-]+\.(?:md|txt|json|csv|xlsx))\b", objective, flags=re.IGNORECASE)
    if match:
        return _safe_output_name(match.group(1))
    if re.search(r"\b(excel|xlsx|spreadsheet|workbook)\b", objective, flags=re.IGNORECASE):
        return "workbook.xlsx"
    return None


def _safe_output_name(value: str) -> str | None:
    name = Path(value.replace("\\", "/")).name.strip()
    if not name or name in {".", ".."}:
        return None
    if ".." in Path(name).parts:
        return None
    return name


def _fallback_content(objective: str, output_name: str) -> str:
    suffix = Path(output_name).suffix.lower()
    notes = _notes_from_objective(objective)
    if suffix == ".md":
        title = Path(output_name).stem.replace("-", " ").replace("_", " ").title()
        bullets = "\n".join(f"- {item}" for item in notes) if notes else f"- Generated from request: {objective}"
        return f"# {title}\n\n{bullets}\n"
    if suffix == ".json":
        import json

        return json.dumps({"source": objective, "items": notes}, indent=2) + "\n"
    if suffix == ".csv":
        rows = ["item"] + [item.replace(",", " ") for item in notes]
        return "\n".join(rows) + "\n"
    return "\n".join(notes or [objective]) + "\n"


def _fallback_excel_script(output_name: str, objective: str) -> GeneratedPythonScript:
    rows = [["Item", "Value"], ["alpha", "2"], ["beta", "4"], ["gamma", "6"]]
    if "hello" in objective.lower():
        rows = [["Message"], ["Hello from the generated workbook"]]
    code = f"""
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED
from xml.sax.saxutils import escape
import re

output = Path({output_name!r})
rows = {rows!r}

def sheet_xml(rows):
    xml_rows = []
    for row_index, row in enumerate(rows, 1):
        cells = []
        for col_index, value in enumerate(row, 1):
            col = chr(64 + col_index)
            text = escape(str(value))
            cells.append(f'<c r="{{col}}{{row_index}}" t="inlineStr"><is><t>{{text}}</t></is></c>')
        xml_rows.append(f'<row r="{{row_index}}">' + ''.join(cells) + '</row>')
    return '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>' + ''.join(xml_rows) + '</sheetData></worksheet>'

with ZipFile(output, "w", ZIP_DEFLATED) as workbook:
    workbook.writestr("[Content_Types].xml", '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>')
    workbook.writestr("_rels/.rels", '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
    workbook.writestr("xl/workbook.xml", '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>')
    workbook.writestr("xl/_rels/workbook.xml.rels", '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>')
    workbook.writestr("xl/worksheets/sheet1.xml", sheet_xml(rows))

with ZipFile(output) as workbook:
    xml = workbook.read("xl/worksheets/sheet1.xml").decode("utf-8")
loaded_values = re.findall(r"<t>(.*?)</t>", xml)
print(f"created and loaded {{output}}")
print("values: " + ", ".join(loaded_values))
"""
    return GeneratedPythonScript(
        summary=f"Create and load {output_name} using only the Python standard library.",
        code=_clean_generated_code(code),
        expected_files=[output_name],
    )


def _notes_from_objective(objective: str) -> list[str]:
    text = objective.split(":", 1)[1] if ":" in objective else objective
    parts = re.split(r"[,;\n]+|\s+-\s+", text)
    notes = [re.sub(r"\s+", " ", item).strip(" .") for item in parts]
    return [item for item in notes if item][:20]


def _execution_profile(request: ToolCallRequest, *, generated: bool) -> str:
    explicit = str(request.input.get("execution_profile") or "").strip().lower()
    if explicit:
        return explicit
    return "generated" if generated else "trusted"


def _is_untrusted_profile(value: str) -> bool:
    return value.strip().lower() in {"generated", "telegram", "untrusted", "external", "user"}


def _resource_limits_dict(config: CodeInterpreterAdapterConfig) -> dict[str, Any]:
    return {
        "timeout_seconds": config.timeout_seconds,
        "memory": config.resource_limits.memory,
        "cpus": config.resource_limits.cpus,
        "pids_limit": config.resource_limits.pids_limit,
        "max_code_chars": config.max_code_chars,
        "max_output_chars": config.max_output_chars,
    }


def _validate_requested_packages(requirements: list[str], allowed_packages: set[str]) -> None:
    if not requirements:
        return
    for requirement in requirements:
        name = re.split(r"[<>=!~\[]", requirement, maxsplit=1)[0].strip().lower()
        if not name:
            raise ValueError(f"invalid package requirement: {requirement}")
        if allowed_packages and name not in allowed_packages:
            raise ValueError(f"package is not allowed for code.interpreter: {name}")


async def _run_python_script(executable: str, script_path: Path, workspace: Path, *, timeout: int) -> ProcessExecutionResult:
    return await _run_command([executable, str(script_path)], cwd=workspace, timeout=timeout)


async def _run_command(args: list[str], *, cwd: Path, timeout: int) -> ProcessExecutionResult:
    creationflags = 0
    start_new_session = False
    if sys.platform == "win32":
        creationflags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        start_new_session = True
    started = time.perf_counter()
    process = await asyncio.create_subprocess_exec(
        *args,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.DEVNULL,
        creationflags=creationflags,
        start_new_session=start_new_session,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        await _terminate_process_tree(process)
        await process.communicate()
        raise TimeoutError from exc
    duration = time.perf_counter() - started
    return ProcessExecutionResult(
        returncode=process.returncode or 0,
        stdout=stdout.decode(errors="replace"),
        stderr=stderr.decode(errors="replace"),
        duration_seconds=round(duration, 3),
        pid=process.pid,
    )


async def _terminate_process_tree(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    if sys.platform == "win32":
        try:
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/PID",
                str(process.pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            await asyncio.wait_for(killer.communicate(), timeout=10)
            return
        except Exception:
            logger.debug("taskkill failed; falling back to process.kill", exc_info=True)
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
            return
        except Exception:
            logger.debug("process group kill failed; falling back to process.kill", exc_info=True)
    try:
        process.kill()
    except ProcessLookupError:
        pass


async def _best_effort_docker_rm(docker: str, container_name: str) -> None:
    try:
        process = await asyncio.create_subprocess_exec(
            docker,
            "rm",
            "-f",
            container_name,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        await asyncio.wait_for(process.communicate(), timeout=10)
    except Exception:
        # No further fallback after this - an orphaned container is a real
        # resource leak (unlike a zombie process, it keeps consuming disk/CPU
        # until someone notices and runs `docker rm` by hand).
        logger.warning("docker cleanup failed for %s; container may be orphaned", container_name, exc_info=True)


def _safe_child_path(workspace: Path, relative_path: str) -> Path:
    # Block obviously-malicious traversal even when an absolute path is given.
    if ".." in Path(relative_path).parts:
        raise ValueError(f"script path escaped workspace: {relative_path}")
    # Accept either a relative path or an absolute path that already points
    # INTO the workspace (e.g. the LLM emitted "{{workspace_dir}}/script.py"
    # which the executor substituted into an absolute path).
    raw = Path(relative_path)
    if raw.is_absolute():
        target = raw.resolve()
    else:
        cleaned = relative_path.replace("\\", "/").strip().lstrip("/")
        if not cleaned:
            raise ValueError(f"script path escaped workspace: {relative_path}")
        target = (workspace / cleaned).resolve()
    if workspace != target and workspace not in target.parents:
        raise ValueError(f"script path escaped workspace: {relative_path}")
    return target


def _relative_files(workspace: Path, *, max_files: int) -> list[str]:
    if not workspace.exists():
        return []
    files = []
    for path in sorted(item for item in workspace.rglob("*") if item.is_file()):
        files.append(str(path.resolve().relative_to(workspace)))
        if len(files) >= max_files:
            break
    return files


def _file_snapshot(workspace: Path, *, max_files: int) -> dict[str, tuple[int, int]]:
    if not workspace.exists():
        return {}
    snapshot: dict[str, tuple[int, int]] = {}
    for path in sorted(item for item in workspace.rglob("*") if item.is_file()):
        try:
            stat = path.stat()
            snapshot[str(path.resolve().relative_to(workspace))] = (stat.st_size, stat.st_mtime_ns)
        except OSError:
            continue
        if len(snapshot) >= max_files:
            break
    return snapshot


def _summary(returncode: int, stdout: str, stderr: str, created: list[str]) -> str:
    if returncode == 0:
        prefix = f"Python completed successfully and created {len(created)} file(s)."
        text = stdout.strip()
        return f"{prefix} {text[:500]}" if text else prefix
    text = (stderr or stdout).strip()
    return f"Python failed with exit code {returncode}." + (f" {text[:500]}" if text else "")


# Bounds for the file-content previews added to created/modified files below.
# Existed to close a real gap (docs/HISTORY.md Part 3 W1): the Auditor grounds
# its "did this actually satisfy the objective" check in terminal_output text,
# but that used to list only file NAMES plus stdout - a script that computed
# the wrong number and wrote it to result.json passed every check because
# nothing downstream ever looked inside the file. Kept deliberately small:
# the Auditor's own raw_output budget is 6000 chars total
# (orchestration/auditor.py), and this is meant to give it enough to check an
# answer against the objective, not to replace filesystem.manage.read_file
# for a task that actually needs the full file.
_FILE_PREVIEW_MAX_FILES = 3
_FILE_PREVIEW_MAX_CHARS_PER_FILE = 600


def _file_content_previews(workspace: Path, names: list[str]) -> list[tuple[str, str]]:
    """Best-effort small text previews of the given files, workspace-relative.

    Silently skips anything that isn't decodable as UTF-8 text (binaries,
    images) or that no longer exists - this is a preview for grounding an
    answer, not a guarantee, so a skip here should never fail the tool call.
    """
    previews: list[tuple[str, str]] = []
    for name in names[:_FILE_PREVIEW_MAX_FILES]:
        path = workspace / name
        try:
            raw = path.read_bytes()[: _FILE_PREVIEW_MAX_CHARS_PER_FILE * 4]
            text = raw.decode("utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        text = text.strip()
        if not text:
            continue
        if len(text) > _FILE_PREVIEW_MAX_CHARS_PER_FILE:
            text = text[:_FILE_PREVIEW_MAX_CHARS_PER_FILE] + "…"
        previews.append((name, text))
    return previews


def _terminal_output(operation: str, output: dict[str, Any]) -> dict[str, Any]:
    lines = [f"Code interpreter operation completed: {operation}"]
    lines.append(f"Workspace: {output.get('workspace_dir')}")
    if output.get("backend"):
        sandbox = "sandboxed" if output.get("sandboxed") else "not sandboxed"
        lines.append(f"Backend: {output.get('backend')} ({sandbox})")
    if output.get("script_path"):
        lines.append(f"Script: {output['script_path']}")
    lines.append(f"Return code: {output.get('returncode')}")
    if output.get("files_created"):
        lines.append("Created files:")
        lines.extend(f"- {path}" for path in output["files_created"])
    if output.get("files_modified"):
        lines.append("Modified files:")
        lines.extend(f"- {path}" for path in output["files_modified"])
    if output.get("files_deleted"):
        lines.append("Deleted files:")
        lines.extend(f"- {path}" for path in output["files_deleted"])
    for preview in output.get("file_previews") or []:
        lines.append(f"Content of {preview['path']}:")
        lines.append(str(preview["content"]))
    if output.get("stdout"):
        lines.append("Stdout:")
        lines.append(str(output["stdout"])[:2000])
    if output.get("stderr"):
        lines.append("Stderr:")
        lines.append(str(output["stderr"])[:2000])
    return {
        "instance_id": "local-worker",
        "terminal_id": "code-interpreter",
        "content": "\n".join(line for line in lines if line is not None),
        "is_final": True,
        "exit_code": output.get("returncode") or 0,
        "source": "code_interpreter",
    }


def _verify_code_interpreter(request: ToolCallRequest, result: ToolCallResult) -> ToolVerification | None:
    """Re-reads the workspace after a SUCCEEDED run to confirm every file
    the adapter's own before/after snapshot diff claimed as created or
    modified now actually exists, and every file it claimed deleted does
    not (docs/ROADMAP.md "Finish the Proof"). files_created/files_modified/
    files_deleted are workspace-relative names computed by _file_snapshot's
    own diff, not user input, and workspace_dir is the adapter's own
    resolved absolute path - both already trustworthy by the time a
    SUCCEEDED result carries them.

    Operations that never touch files (inspect_state, health) report empty
    lists, so this returns None for those - nothing was claimed, so there
    is nothing to re-check, the same "no verification possible" as a dry
    run in filesystem_manage's verifier.
    """
    workspace_dir = result.output.get("workspace_dir")
    if not isinstance(workspace_dir, str) or not workspace_dir:
        return None
    workspace = Path(workspace_dir)
    checked = 0
    missing: list[str] = []
    for name in result.output.get("files_created") or []:
        if not isinstance(name, str) or not name:
            continue
        checked += 1
        if not (workspace / name).exists():
            missing.append(f"created file not found: {name}")
    for name in result.output.get("files_modified") or []:
        if not isinstance(name, str) or not name:
            continue
        checked += 1
        if not (workspace / name).exists():
            missing.append(f"modified file not found: {name}")
    for name in result.output.get("files_deleted") or []:
        if not isinstance(name, str) or not name:
            continue
        checked += 1
        if (workspace / name).exists():
            missing.append(f"deleted file still present: {name}")
    if checked == 0:
        return None
    return ToolVerification(checked=checked, verified=checked - len(missing), missing=missing)


def register(deps: RegistryDeps, definitions: Definitions, adapters: Adapters) -> None:
    settings = deps.settings
    enabled = capability_enabled(settings, Capability.TERMINAL_RUN) and settings.adapters.code_interpreter.enabled
    definitions.append(
        ToolDefinition(
            name="code.interpreter",
            capability=Capability.TERMINAL_RUN,
            enabled=enabled,
            description=(
                "generate and run bounded Python scripts through configured local/container backends under "
                f"{settings.adapters.code_interpreter.workspace_root}"
            ),
            operations=(
                "run_python",
                "generate_and_run",
                "solve_once",
                "inspect_state",
                "build_temp_helper",
                "repair_script",
                "health",
            ),
            operation_schemas={
                "run_python": CodeInterpreterRunPythonInput,
                "generate_and_run": CodeInterpreterGenerateAndRunInput,
                "solve_once": CodeInterpreterSolveOnceInput,
                "inspect_state": CodeInterpreterInspectStateInput,
                "build_temp_helper": CodeInterpreterBuildTempHelperInput,
                "repair_script": CodeInterpreterRepairScriptInput,
                "health": CodeInterpreterHealthInput,
            },
            operation_output_schemas={
                "run_python": CodeInterpreterOutput,
                "generate_and_run": CodeInterpreterOutput,
                "solve_once": CodeInterpreterOutput,
                "inspect_state": CodeInterpreterOutput,
                "build_temp_helper": CodeInterpreterOutput,
                "repair_script": CodeInterpreterOutput,
                "health": CodeInterpreterOutput,
            },
            default_operation="run_python",
            operation_risks={
                "inspect_state": RiskLevel.LOW,
                "health": RiskLevel.LOW,
                "run_python": RiskLevel.HIGH,
                "generate_and_run": RiskLevel.HIGH,
                "solve_once": RiskLevel.HIGH,
                "build_temp_helper": RiskLevel.HIGH,
                "repair_script": RiskLevel.HIGH,
            },
            # Execution may fall back to a local unsandboxed process. This
            # gate is runtime-owned and cannot be bypassed by Full Access or
            # by setting input.approved in model output.
            approval_required_operations=(
                "run_python",
                "generate_and_run",
                "solve_once",
                "build_temp_helper",
                "repair_script",
            ),
            approval_reasons={
                "run_python": "runs the given Python code, possibly unsandboxed if Docker is unavailable",
                "generate_and_run": "generates Python code from your objective, then runs it - possibly unsandboxed if Docker is unavailable - review what it wrote before approving",
                "solve_once": "generates and runs Python code to answer a one-off computation",
                "build_temp_helper": "generates and runs a helper Python script",
                "repair_script": "generates and runs a modified version of a previously failing script",
            },
            verify=_verify_code_interpreter,
            examples=(
                {"operation": "generate_and_run",
                 "objective": "compute the 20th Fibonacci number and print it"},
                {"operation": "generate_and_run",
                 "objective": "write a Python script using openpyxl that creates sales_data.xlsx with sample sales rows"},
            ),
        )
    )
    if settings.adapters.code_interpreter.enabled:
        adapters["code.interpreter"] = CodeInterpreterAdapter(
            settings.adapters.code_interpreter,
            provider=deps.provider,  # type: ignore[arg-type]
            artifacts=deps.artifact_repository,
        )
