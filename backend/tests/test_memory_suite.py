"""The memory suite (e2e/memory_cases.json): what is told once, kept, changed and found again, in the owner's kind of conversation (docs/PLAN.md, section 5)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_the_cases_cover_the_five_things_a_memory_must_do() -> None:
    cases = json.loads((ROOT / "e2e" / "memory_cases.json").read_text(encoding="utf-8"))
    assert len(cases) == 8 and len({c["id"] for c in cases}) == 8
    assert all(c["id"].startswith("mem_") and c["suites"] == ["memory"] and c["follow_ups"] and c["rubric"] for c in cases)
    ids = {c["id"] for c in cases}
    assert {"mem_one_fact_comes_back", "mem_two_sessions_are_combined", "mem_a_changed_fact_is_the_current_one", "mem_what_was_never_said_is_not_invented", "mem_a_date_is_kept_and_found"} <= ids


def test_the_runner_knows_the_suite() -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location("run_all_e2e_tests", ROOT / "scripts" / "run_all_e2e_tests.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    assert sum(1 for case in module.load_cases() if "memory" in (case.get("suites") or [])) == 8
