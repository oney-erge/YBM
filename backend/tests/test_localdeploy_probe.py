"""The hardware HTTP probe can be exercised without a running service."""

import httpx

from agent_control.llm import hardware


def test_probe_localdeploy_uses_next_endpoint_after_connection_failure(monkeypatch):
    endpoints = ("http://first.invalid", "http://second.invalid")
    monkeypatch.setattr(hardware, "_LOCALDEPLOY_BASE_URLS", endpoints)
    calls = []

    def fake_get(url, *, timeout):
        calls.append((url, timeout))
        if len(calls) == 1:
            raise httpx.ConnectError("unavailable")
        return httpx.Response(
            200,
            json={"success": True, "gpus": [], "system": {"memory_total_mb": 16384}},
        )

    monkeypatch.setattr(httpx, "get", fake_get)
    result = hardware.probe_localdeploy(timeout=0.25)

    assert calls == [(f"{base}/system/hardware", 0.25) for base in endpoints]
    assert result is not None and result.ram_gb == 16.0
