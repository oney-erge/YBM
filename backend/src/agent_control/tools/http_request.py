from __future__ import annotations

import copy
import json
import time
from typing import Any
from urllib.parse import urlparse

import httpx

from agent_control.config import HttpRequestAdapterConfig, SecretVaultConfig
from agent_control.schemas import Capability, RiskLevel, ToolCallRequest, ToolCallResult, ToolResultStatus
from agent_control.storage.redaction import redact_payload
from agent_control.storage.secrets import SecretVault, SecretVaultError
from agent_control.tools.contracts import HttpRequestInput, HttpRequestOutput
from agent_control.tools.spec import (
    UNTRUSTED_EXTERNAL,
    Adapters,
    Definitions,
    RegistryDeps,
    ToolDefinition,
    capability_enabled,
    failed_result,
    same_output_schema,
)


_SENSITIVE_PATTERNS = ("token", "api_key", "secret", "password", "authorization", "cookie", "set-cookie")


class HttpRequestAdapter:
    """Policy-gated direct HTTP/REST client for allowlisted API calls."""

    def __init__(
        self,
        config: HttpRequestAdapterConfig,
        secrets_config: SecretVaultConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.config = config
        self.secrets = SecretVault(secrets_config)
        self._transport = transport

    async def execute(self, request: ToolCallRequest) -> ToolCallResult:
        if not self.config.enabled:
            return failed_result(request, "http.request adapter is disabled")
        operation = str(request.input.get("operation") or "request")
        if operation != "request":
            return failed_result(request, f"unsupported HTTP operation: {operation}")
        try:
            output = await self._request(request)
        except Exception as exc:
            return failed_result(request, f"HTTP request failed: {exc}")
        output["operation"] = operation
        output["terminal_output"] = [_terminal_output(output)]
        return ToolCallResult(request_id=request.id, status=ToolResultStatus.SUCCEEDED, output=output)

    async def _request(self, request: ToolCallRequest) -> dict[str, Any]:
        url = str(request.input["url"]).strip()
        _require_allowed_url(url, self.config)
        method = str(request.input.get("method") or "GET").upper()
        headers = {str(key): str(value) for key, value in dict(request.input.get("headers") or {}).items()}
        headers.setdefault("User-Agent", self.config.user_agent)
        query = dict(request.input.get("query") or {})
        json_body = copy.deepcopy(request.input.get("json_body"))
        body = request.input.get("body")
        injected_values = self._inject_secrets(
            dict(request.input.get("secret_refs") or {}),
            headers=headers,
            query=query,
            json_body=json_body,
        )
        max_response_chars = min(
            int(request.input.get("max_response_chars") or self.config.max_response_chars),
            self.config.max_response_chars,
        )
        _check_body_size(json_body=json_body, body=body, max_chars=self.config.max_body_chars)
        timeout = min(int(request.input.get("timeout_seconds") or request.timeout_seconds or self.config.timeout_seconds), self.config.timeout_seconds)
        start = time.perf_counter()
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,
            transport=self._transport,
        ) as client:
            response = await client.request(
                method,
                url,
                params=query,
                headers=headers,
                json=json_body if json_body is not None else None,
                content=str(body).encode("utf-8") if body is not None else None,
            )
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        text = response.text
        truncated = len(text) > max_response_chars
        if truncated:
            text = text[:max_response_chars]
        parsed_json = None
        if bool(request.input.get("parse_json", True)):
            try:
                parsed_json = response.json()
            except ValueError:
                parsed_json = None
        output = {
            "url": url,
            "method": method,
            "status_code": response.status_code,
            "ok": response.is_success,
            "headers": dict(response.headers),
            "json": parsed_json,
            "text": text,
            "truncated": truncated,
            "elapsed_ms": elapsed_ms,
            "summary": f"HTTP {method} {url} returned {response.status_code}.",
        }
        return redact_payload(output, _SENSITIVE_PATTERNS, injected_values)

    def _inject_secrets(
        self,
        secret_refs: dict[str, Any],
        *,
        headers: dict[str, str],
        query: dict[str, Any],
        json_body: Any,
    ) -> list[str]:
        injected: list[str] = []
        for target, spec in secret_refs.items():
            secret, rendered = self._render_secret(spec)
            injected.append(secret)
            _apply_secret(str(target), rendered, headers=headers, query=query, json_body=json_body)
        return injected

    def _render_secret(self, spec: Any) -> tuple[str, str]:
        if isinstance(spec, str):
            secret = self.secrets.get_ref(spec)
            return secret, secret
        if not isinstance(spec, dict):
            raise SecretVaultError("secret_refs values must be a reference string or object")
        ref = spec.get("ref")
        if not isinstance(ref, str):
            service = spec.get("service")
            key = spec.get("key")
            if not isinstance(service, str) or not isinstance(key, str):
                raise SecretVaultError("secret_refs object requires 'ref' or both 'service' and 'key'")
            ref = f"{service}.{key}"
        secret = self.secrets.get_ref(ref)
        template = str(spec.get("template") or "{secret}")
        return secret, template.replace("{secret}", secret)


def _require_allowed_url(url: str, config: HttpRequestAdapterConfig) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("http.request only supports absolute http:// or https:// URLs")
    if "@" in parsed.netloc:
        # Embedded userinfo (user:pass@host) makes the string before "@" look
        # like a host without being one - urlparse correctly resolves
        # .hostname past it, but a naive allowed_url_prefixes string-prefix
        # check does not, so "https://api.example.com@attacker.com/x" reads
        # as prefixed by "https://api.example.com" while actually connecting
        # to attacker.com. No legitimate use needs this here (secret_refs
        # already covers auth); reject outright.
        raise ValueError("http.request does not support URLs with embedded userinfo (user:pass@host)")
    host = parsed.hostname.lower()
    netloc = parsed.netloc.lower()
    blocked = {item.lower() for item in config.blocked_hosts}
    if host in blocked or netloc in blocked:
        raise ValueError(f"HTTP host is blocked: {host}")
    if not config.allowed_hosts and not config.allowed_url_prefixes:
        raise ValueError("no HTTP allowlist is configured")
    normalized_url = url.lower()
    if any(_prefix_matches(normalized_url, host, prefix) for prefix in config.allowed_url_prefixes):
        return
    if any(_host_matches(host, netloc, allowed) for allowed in config.allowed_hosts):
        return
    raise ValueError(f"HTTP host is not allowlisted: {host}")


def _prefix_matches(normalized_url: str, host: str, prefix: str) -> bool:
    """A URL matches an allowed prefix only when the literal prefix matches
    *and* the URL's own parsed host equals the prefix's parsed host.

    A bare string-prefix check alone would let
    "https://api.example.com.attacker.com/x" match the prefix
    "https://api.example.com" - the character sequence lines up even though
    the real host is an attacker-controlled subdomain of a different domain.
    """
    prefix = prefix.lower()
    if not normalized_url.startswith(prefix):
        return False
    prefix_host = urlparse(prefix).hostname
    return prefix_host is not None and prefix_host == host


def _host_matches(host: str, netloc: str, allowed: str) -> bool:
    allowed = allowed.lower().strip()
    if not allowed:
        return False
    if ":" in allowed:
        return netloc == allowed
    if allowed.startswith("*."):
        suffix = allowed[1:]
        return host.endswith(suffix) and host != allowed[2:]
    return host == allowed


def _check_body_size(*, json_body: Any, body: Any, max_chars: int) -> None:
    if max_chars <= 0 and (json_body is not None or body is not None):
        raise ValueError("request bodies are disabled for http.request")
    if json_body is not None:
        size = len(json.dumps(json_body, ensure_ascii=False, default=str))
    elif body is not None:
        size = len(str(body))
    else:
        size = 0
    if size > max_chars:
        raise ValueError(f"HTTP request body exceeds configured limit of {max_chars} characters")


def _apply_secret(
    target: str,
    value: str,
    *,
    headers: dict[str, str],
    query: dict[str, Any],
    json_body: Any,
) -> None:
    target = target.strip()
    if target.startswith("headers."):
        headers[target.removeprefix("headers.")] = value
        return
    if target.startswith("header."):
        headers[target.removeprefix("header.")] = value
        return
    if target.startswith("query."):
        query[target.removeprefix("query.")] = value
        return
    if target.startswith("json."):
        if json_body is None:
            raise ValueError("json secret target requires json_body")
        _set_nested_json_value(json_body, target.removeprefix("json."), value)
        return
    raise ValueError(f"unsupported secret injection target: {target}")


def _set_nested_json_value(payload: Any, dotted_path: str, value: str) -> None:
    if not isinstance(payload, dict):
        raise ValueError("json secret injection requires json_body to be an object")
    current = payload
    parts = [part for part in dotted_path.split(".") if part]
    if not parts:
        raise ValueError("json secret injection target cannot be empty")
    for part in parts[:-1]:
        child = current.setdefault(part, {})
        if not isinstance(child, dict):
            raise ValueError(f"json secret injection path is not an object: {part}")
        current = child
    current[parts[-1]] = value


def _terminal_output(output: dict[str, Any]) -> dict[str, Any]:
    return {
        "instance_id": "local-worker",
        "terminal_id": "http-request",
        "content": output.get("summary") or f"HTTP request returned {output.get('status_code')}",
        "command_id": None,
        "is_final": True,
        "exit_code": 0 if output.get("ok") else 1,
        "source": "http_request",
    }




def register(deps: RegistryDeps, definitions: Definitions, adapters: Adapters) -> None:
    settings = deps.settings
    allowlist = [*settings.adapters.http_request.allowed_hosts, *settings.adapters.http_request.allowed_url_prefixes]
    enabled = (
        capability_enabled(settings, Capability.NETWORK_HTTP)
        and settings.adapters.http_request.enabled
        and bool(allowlist)
    )
    definitions.append(
        ToolDefinition(
            name="http.request",
            capability=Capability.NETWORK_HTTP,
            enabled=enabled,
            description=(
                "call allowlisted HTTP/REST APIs with optional secret injection; "
                f"allowed targets: {', '.join(allowlist) or '<none configured>'}"
            ),
            operations=("request",),
            input_schema=HttpRequestInput,
            output_schema=HttpRequestOutput,
            operation_output_schemas=same_output_schema(("request",), HttpRequestOutput),
            default_operation="request",
            operation_egress=("request",),
            operation_content_trust={"request": UNTRUSTED_EXTERNAL},
            risk_resolver=_http_required_risk,
            approval_resolver=lambda value: bool(value.get("secret_refs")),
            examples=(
                {"operation": "request", "method": "GET", "url": "https://api.example.com/status"},
                {
                    "operation": "request",
                    "method": "GET",
                    "url": "https://api.example.com/user",
                    "secret_refs": {"headers.Authorization": {"ref": "example.token", "template": "Bearer {secret}"}},
                },
            ),
        )
    )
    if settings.adapters.http_request.enabled:
        adapters["http.request"] = HttpRequestAdapter(settings.adapters.http_request, settings.secrets)


def _http_required_risk(value: dict[str, Any]) -> RiskLevel:
    if value.get("secret_refs"):
        return RiskLevel.HIGH
    method = str(value.get("method") or "GET").upper()
    return RiskLevel.LOW if method in {"GET", "HEAD"} else RiskLevel.HIGH
