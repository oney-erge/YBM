"""docs/archive/UI_UX_AUDIT.md Phase 6: a read-only "is a newer release out"
check against GitHub's public releases API - no auth, no auto-apply,
degrades to a plain result on any failure rather than raising.
"""

from __future__ import annotations

import json
from urllib.error import HTTPError, URLError

import pytest

from agent_control import updates


class _FakeResponse:
    def __init__(self, status: int, payload: dict) -> None:
        self.status = status
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc) -> None:
        return None


def test_update_available_when_the_latest_tag_is_newer(monkeypatch) -> None:
    monkeypatch.setattr(updates, "current_version", lambda: "0.1.0")
    monkeypatch.setattr(
        updates, "urlopen",
        lambda request, timeout=5.0: _FakeResponse(200, {"tag_name": "v0.2.0", "html_url": "https://example.com/v0.2.0"}),
    )

    result = updates.check_for_updates()

    assert result.status == "update_available"
    assert result.current_version == "0.1.0"
    assert result.latest_version == "v0.2.0"
    assert result.release_url == "https://example.com/v0.2.0"


def test_up_to_date_when_the_latest_tag_matches_current(monkeypatch) -> None:
    monkeypatch.setattr(updates, "current_version", lambda: "0.1.0")
    monkeypatch.setattr(updates, "urlopen", lambda request, timeout=5.0: _FakeResponse(200, {"tag_name": "v0.1.0"}))

    result = updates.check_for_updates()

    assert result.status == "up_to_date"


def test_up_to_date_when_current_is_ahead_of_latest(monkeypatch) -> None:
    """Guards against flagging a dev/pre-release checkout as behind."""
    monkeypatch.setattr(updates, "current_version", lambda: "0.3.0")
    monkeypatch.setattr(updates, "urlopen", lambda request, timeout=5.0: _FakeResponse(200, {"tag_name": "v0.2.0"}))

    result = updates.check_for_updates()

    assert result.status == "up_to_date"


def test_no_releases_reported_as_such_not_an_error(monkeypatch) -> None:
    """GitHub's "no releases yet" response is an HTTP 404 that urlopen
    raises as an HTTPError, not a normal response object with
    .status == 404 - this test would have caught the original version of
    this function, which checked the wrong thing and never actually hit
    this branch against the real API."""
    monkeypatch.setattr(updates, "current_version", lambda: "0.1.0")

    def _raise_404(request, timeout=5.0):
        raise HTTPError(updates.LATEST_RELEASE_API, 404, "Not Found", hdrs=None, fp=None)

    monkeypatch.setattr(updates, "urlopen", _raise_404)

    result = updates.check_for_updates()

    assert result.status == "no_releases"


def test_other_http_errors_are_reported_as_check_failed(monkeypatch) -> None:
    monkeypatch.setattr(updates, "current_version", lambda: "0.1.0")

    def _raise_500(request, timeout=5.0):
        raise HTTPError(updates.LATEST_RELEASE_API, 500, "Internal Server Error", hdrs=None, fp=None)

    monkeypatch.setattr(updates, "urlopen", _raise_500)

    result = updates.check_for_updates()

    assert result.status == "check_failed"
    assert "500" in result.detail


def test_network_failure_degrades_to_check_failed_not_a_raise(monkeypatch) -> None:
    monkeypatch.setattr(updates, "current_version", lambda: "0.1.0")

    def _raise(request, timeout=5.0):
        raise URLError("no route to host")

    monkeypatch.setattr(updates, "urlopen", _raise)

    result = updates.check_for_updates()

    assert result.status == "check_failed"
    assert "no route to host" in result.detail


def test_unparseable_version_strings_fail_the_comparison_cleanly(monkeypatch) -> None:
    monkeypatch.setattr(updates, "current_version", lambda: "0.1.0")
    monkeypatch.setattr(updates, "urlopen", lambda request, timeout=5.0: _FakeResponse(200, {"tag_name": "not-a-version"}))

    result = updates.check_for_updates()

    assert result.status == "check_failed"


@pytest.mark.parametrize(
    ("text", "expected"),
    [("v1.2.3", (1, 2, 3)), ("1.2.3", (1, 2, 3)), ("v0.1.0", (0, 1, 0)), ("garbage", None), ("", None)],
)
def test_parse_semver(text, expected) -> None:
    assert updates._parse_semver(text) == expected
