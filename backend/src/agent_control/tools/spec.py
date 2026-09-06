"""Shared types for tool registration.

Lives separately from registry.py so each tool's own module (code_interpreter.py,
filesystem_manage.py, ...) can import ToolDefinition/RegistryDeps and declare its
own register() function without importing registry.py itself - registry.py is
the one importing *them*, and a two-way import would be a cycle. New tool =
new adapter module with a register() function, one import line added to
registry.py's _REGISTRARS - no editing this file or any other tool's code
(docs/HISTORY.md P3).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field

from pydantic import BaseModel, ValidationError

from agent_control.config import AppSettings
from agent_control.schemas import (
    Capability,
    ErrorClass,
    RiskLevel,
    ToolCallRequest,
    ToolCallResult,
    ToolResultStatus,
    ToolVerification,
)


def failed_result(request: ToolCallRequest, message: str) -> ToolCallResult:
    """Standard adapter failure result.

    Every tool adapter needs this and, until 2026-07-29, every one of the 13
    of them carried a byte-identical private `_failed()` copy. Same shape, so
    a change to the failure contract (a new field, a different ErrorClass)
    meant editing 13 files or - far more likely - editing one and leaving the
    others silently inconsistent.
    """
    return ToolCallResult(
        request_id=request.id,
        status=ToolResultStatus.FAILED,
        error_class=ErrorClass.ADAPTER_FAILED,
        error_message=message,
    )


CAPABILITY_MINIMUM_RISKS: dict[Capability, RiskLevel] = {
    Capability.TELEGRAM_RECEIVE: RiskLevel.LOW,
    Capability.TELEGRAM_SEND: RiskLevel.LOW,
    Capability.LLM_GENERATE: RiskLevel.LOW,
    Capability.STT_TRANSCRIBE: RiskLevel.LOW,
    Capability.TTS_SYNTHESIZE: RiskLevel.LOW,
    Capability.VSCODE_READ_STATE: RiskLevel.LOW,
    Capability.VSCODE_WRITE_FILES: RiskLevel.HIGH,
    Capability.TERMINAL_RUN: RiskLevel.HIGH,
    Capability.FILESYSTEM_READ: RiskLevel.LOW,
    Capability.FILESYSTEM_WRITE: RiskLevel.HIGH,
    Capability.DESKTOP_SCREENSHOT: RiskLevel.LOW,
    Capability.DESKTOP_CONTROL: RiskLevel.CRITICAL,
    Capability.BROWSER_OPEN: RiskLevel.LOW,
    Capability.BROWSER_CONTROL: RiskLevel.CRITICAL,
    Capability.NETWORK_HTTP: RiskLevel.HIGH,
    Capability.SCHEDULE_MANAGE: RiskLevel.MEDIUM,
    Capability.GITHUB_READ: RiskLevel.LOW,
    Capability.GITHUB_PUSH: RiskLevel.CRITICAL,
    Capability.DEPENDENCIES_INSTALL: RiskLevel.HIGH,
    Capability.MEMORY_MANAGE: RiskLevel.LOW,
}


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    capability: Capability
    enabled: bool
    description: str
    operations: tuple[str, ...] = ()
    lifecycle: str = "runtime"
    input_schema: type[BaseModel] | None = None
    operation_schemas: dict[str, type[BaseModel]] | None = None
    output_schema: type[BaseModel] | None = None
    operation_output_schemas: dict[str, type[BaseModel]] | None = None
    default_operation: str | None = None
    # Authorization metadata is owned by the runtime definition, never by the
    # model. A mixed read/write tool can lower or raise individual operations;
    # otherwise the capability's conservative minimum above applies.
    minimum_risk: RiskLevel | None = None
    operation_risks: dict[str, RiskLevel] = field(default_factory=dict)
    approval_required_operations: tuple[str, ...] = ()
    # Human-readable "why" for an approval_required_operations entry, shown
    # on the ApprovalRequest a human actually sees. Restores the specific
    # reasoning that ToolAdapter-raised exceptions used to carry before the
    # runtime-owned approval gate replaced them (docs/HISTORY.md Part 4's
    # concurrent-hardening note) - optional; operations not listed here still
    # get PolicyEngine.approval_request()'s generic "Approve X using Y".
    approval_reasons: dict[str, str] = field(default_factory=dict)
    risk_resolver: Callable[[dict], RiskLevel] | None = None
    approval_resolver: Callable[[dict], bool] | None = None
    # Worked usage examples shown to the Operator's decide() call. Each entry
    # is a `tool_input` dict the model can imitate - the 8B local model
    # imitates concrete examples much more reliably than it follows abstract
    # descriptions.
    examples: tuple[dict, ...] = ()
    # Operations whose SUCCEEDED result can carry a real destination this
    # machine contacted (docs/GAPS.md: "only http.request calls
    # record_egress... browser, MCP, coding-agent, and Telegram traffic is
    # invisible to receipts"). ToolExecutor consults this - a tool needs
    # zero manual record_egress() call sites of its own to be covered;
    # egress.extract_egress_hosts() does the actual URL/host scan over the
    # call's validated input and output.
    operation_egress: tuple[str, ...] = ()
    # Optional mechanical proof hook (docs/ROADMAP.md "Proof"): given the
    # completed request and result, return a ToolVerification describing
    # what was actually re-checked on the machine, or None if this call has
    # no defined verification. Same dispatch idiom as risk_resolver/
    # approval_resolver (inspect value["operation"] internally) but answers
    # "did it actually happen", not "is it allowed". Runs only once the
    # result already reports SUCCEEDED.
    verify: Callable[[ToolCallRequest, ToolCallResult], ToolVerification | None] | None = None

    def required_risk(self, value: dict) -> RiskLevel:
        if self.risk_resolver is not None:
            return self.risk_resolver(value)
        operation = str(value.get("operation") or self.default_operation or "")
        return self.operation_risks.get(
            operation,
            self.minimum_risk or CAPABILITY_MINIMUM_RISKS[self.capability],
        )

    def requires_approval(self, value: dict) -> bool:
        operation = str(value.get("operation") or self.default_operation or "")
        if operation in self.approval_required_operations:
            return True
        return bool(self.approval_resolver and self.approval_resolver(value))

    def approval_reason(self, value: dict) -> str | None:
        operation = str(value.get("operation") or self.default_operation or "")
        return self.approval_reasons.get(operation)

    def validate_input(self, value: dict) -> dict:
        return self._validate_schema(value, self.input_schema, self.operation_schemas, "input")

    def validate_output(self, value: dict) -> dict:
        return self._validate_schema(value, self.output_schema, self.operation_output_schemas, "output")

    def _validate_schema(
        self,
        value: dict,
        base_schema: type[BaseModel] | None,
        operation_schemas: dict[str, type[BaseModel]] | None,
        kind: str,
    ) -> dict:
        schema = base_schema
        payload = dict(value or {})
        if operation_schemas:
            operation = str(payload.get("operation") or self.default_operation or "")
            schema = operation_schemas.get(operation)
            if schema is None:
                expected = ", ".join(sorted(operation_schemas))
                raise ValueError(
                    f"unsupported operation for {self.name}: {operation or '<missing>'}; "
                    f"expected one of: {expected}"
                )
            payload["operation"] = operation
        if schema is None:
            return payload
        try:
            return schema.model_validate(payload).model_dump(mode="json", exclude_none=True, by_alias=True)
        except ValidationError as exc:
            raise ValueError(f"invalid {kind} for {self.name}: {exc}") from exc


@dataclass
class ToolRegistry:
    adapters: dict[str, object]
    definitions: tuple[ToolDefinition, ...]
    definition_index: dict[str, ToolDefinition] | None = None
    mcp_summary: str = ""
    mcp_summary_factory: Callable[[], str] | None = None

    def __post_init__(self) -> None:
        if self.definition_index is None:
            self.definition_index = {definition.name: definition for definition in self.definitions}

    def register_dynamic_tool(self, definition: ToolDefinition, adapter: object) -> None:
        # __post_init__ builds the index, but an `assert` for that vanishes
        # under `python -O` and leaves an AttributeError on None instead.
        if self.definition_index is None:
            self.definition_index = {existing.name: existing for existing in self.definitions}
        if definition.name in self.definition_index:
            raise ValueError(f"tool already registered: {definition.name}")
        self.adapters[definition.name] = adapter
        self.definition_index[definition.name] = definition
        self.definitions = (*self.definitions, definition)

    def context(self) -> str:
        """The tool catalog as the Operator sees it.

        This block is the largest single item in every Operator prompt, and
        the loop is prefill-bound (measured: ~4,700 prompt tokens per call
        against ~93 completion tokens), so what is spent here is spent on
        every step of every task. Two things were costing tokens without
        changing any decision the model can make:

        * Full descriptions and operation lists for tools that are switched
          off - the model cannot call them, so all it needs is to know the
          name exists in order to say "that capability is disabled". They are
          now one line at the end instead of ~350 tokens of detail.
        * `lifecycle=...` on every row, which no prompt rule refers to.
        """
        enabled = [definition for definition in self.definitions if definition.enabled]
        disabled = [definition for definition in self.definitions if not definition.enabled]
        lines = ["Available worker tools:"]
        for definition in enabled:
            operations = f" operations={','.join(definition.operations)}" if definition.operations else ""
            lines.append(
                f"- {definition.name} ({definition.capability.value}): "
                f"{definition.description}{operations}"
            )
            if definition.examples:
                # Show worked examples inline - the model imitates these
                # better than abstract descriptions of input shape.
                for ex in definition.examples:
                    lines.append(f"    example tool_input: {json.dumps(ex, ensure_ascii=False)}")
        if disabled:
            # Named, not described: enough for the model to tell the user a
            # capability exists but is turned off, without paying for detail
            # it can never act on.
            lines.append("")
            lines.append(
                "Disabled (cannot be called; tell the user to enable it in Access): "
                + ", ".join(definition.name for definition in disabled)
            )
        mcp_summary = self.mcp_summary_factory() if self.mcp_summary_factory is not None else self.mcp_summary
        if mcp_summary:
            lines.append("")
            lines.append(mcp_summary)
        return "\n".join(lines)

    def vault_summary(self) -> str:
        lines = ["Capability vault:"]
        for definition in self.definitions:
            state = "available" if definition.enabled else "known_gap"
            lines.append(f"- {definition.name}: {state}; {definition.description}")
        return "\n".join(lines)


@dataclass(frozen=True)
class RegistryDeps:
    """Bundle of optional dependencies the per-tool registrars consume."""
    settings: AppSettings
    backend_base_url: str
    provider: object | None = None
    should_continue: Callable[[str], bool] | None = None
    artifact_repository: object | None = None
    task_repository: object | None = None
    repositories: object | None = None
    audit_logger: object | None = None
    telegram_client: object | None = None


Definitions = list[ToolDefinition]
Adapters = dict[str, object]
Registrar = Callable[[RegistryDeps, Definitions, Adapters], None]


def capability_enabled(settings: AppSettings, capability: Capability) -> bool:
    policy = settings.capabilities.get(capability)
    return bool(policy and policy.enabled)


def same_output_schema(operations: tuple[str, ...], schema: type[BaseModel]) -> dict[str, type[BaseModel]]:
    return {operation: schema for operation in operations}
