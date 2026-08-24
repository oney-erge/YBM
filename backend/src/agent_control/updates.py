"""Check whether a newer YBM release exists (docs/UI_UX_AUDIT.md
Phase 6) - read-only, no auth, degrades to a plain "couldn't check"
result on any failure rather than raising. There is no auto-apply step:
this only ever reports a URL for a person to act on themselves, matching
the project's stance against unattended external writes.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version as _pkg_version
import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

LATEST_RELEASE_API = "https://api.github.com/repos/oney-erge/YBM/releases/latest"


@dataclass
class UpdateCheckResult:
    status: str  # "up_to_date" | "update_available" | "no_releases" | "check_failed"
    current_version: str
    latest_version: str | None = None
    release_url: str | None = None
    detail: str = ""


def current_version() -> str:
    try:
        return _pkg_version("agent-control-backend")
    except PackageNotFoundError:
        # Running from a source checkout without an installed distribution
        # (e.g. a fresh clone before `uv sync`) - matches admin.py's own
        # bootstrap-endpoint fallback for the same lookup.
        return "dev"


def _parse_semver(text: str) -> tuple[int, ...] | None:
    parts = text.strip().lstrip("v").split(".")
    try:
        return tuple(int(p) for p in parts[:3])
    except ValueError:
        return None


def check_for_updates() -> UpdateCheckResult:
    current = current_version()
    request = Request(LATEST_RELEASE_API, headers={"User-Agent": "ybm-control-update-check", "Accept": "application/vnd.github+json"})
    try:
        with urlopen(request, timeout=5.0) as response:  # noqa: S310 - fixed, hardcoded GitHub API URL
            if not (200 <= response.status < 300):
                return UpdateCheckResult("check_failed", current, detail=f"GitHub API returned HTTP {response.status}.")
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        # GitHub's "no releases yet" response IS an HTTP 404 raised as an
        # exception by urlopen, not a normal response object - confirmed
        # against the real API before this fix (the first version of this
        # function checked response.status == 404, which this code path
        # never reaches).
        if exc.code == 404:
            return UpdateCheckResult("no_releases", current, detail="No releases have been published yet.")
        return UpdateCheckResult("check_failed", current, detail=f"GitHub API returned HTTP {exc.code}.")
    except URLError as exc:
        return UpdateCheckResult("check_failed", current, detail=f"Could not reach GitHub: {exc.reason}")
    except (OSError, ValueError, TimeoutError) as exc:
        return UpdateCheckResult("check_failed", current, detail=f"Update check failed: {exc}")

    latest_tag = str(payload.get("tag_name") or "").strip()
    release_url = payload.get("html_url")
    latest_parsed = _parse_semver(latest_tag)
    current_parsed = _parse_semver(current)
    if latest_parsed is None or current_parsed is None:
        return UpdateCheckResult(
            "check_failed", current, latest_version=latest_tag or None, release_url=release_url,
            detail=f"Could not compare versions ({current!r} vs {latest_tag!r}).",
        )
    if latest_parsed > current_parsed:
        return UpdateCheckResult("update_available", current, latest_version=latest_tag, release_url=release_url)
    return UpdateCheckResult("up_to_date", current, latest_version=latest_tag, release_url=release_url)
