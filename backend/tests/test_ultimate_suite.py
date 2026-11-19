"""The acceptance suite for docs/ULTIMATE_ASSISTANT_PLAN.md (e2e/ultimate_cases.json): frozen by hash, run against the real bot. Written after the build: a check, not a blind exam."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CASES = ROOT / "e2e" / "ultimate_cases.json"
FROZEN = ROOT / "e2e" / "ultimate_cases.sha256"


def test_the_file_is_the_one_that_was_frozen() -> None:
    assert hashlib.sha256(CASES.read_bytes().replace(b"\r\n", b"\n")).hexdigest() == FROZEN.read_text(encoding="ascii").strip()  # line endings made plain: a checkout on any system agrees


def test_fourteen_cases_each_with_a_rubric_and_a_check_a_run_can_make() -> None:
    cases = json.loads(CASES.read_text(encoding="utf-8"))
    assert len(cases) == 14 and len({c["id"] for c in cases}) == 14 and all(c["id"].startswith("ult_") for c in cases)
    assert all(c["rubric"] and c["assertions"]["final_status_in"] and c["suites"] == ["ultimate"] for c in cases)
    jobs = {c["id"]: c["assertions"].get("tools_all") for c in cases}
    assert jobs["ult_daycares"] == ["job.manage:start_survey"] and jobs["ult_devices"] == ["job.manage:start_watch"] and jobs["ult_research"] == ["job.manage:start_research"]


def test_the_only_placeholder_is_the_fixture_servers_address() -> None:
    names = set(re.findall(r"\{\{(\w+)\}\}", CASES.read_text(encoding="utf-8")))
    assert names <= {"fixture_base_url"}
