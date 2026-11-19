"""The hands suite (e2e/hands_cases.json): the assistant using the computer and the web itself. A development suite, not a sealed one."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CASES_PATH = ROOT / "e2e" / "hands_cases.json"

sys.path.insert(0, str(ROOT / "e2e"))
import gap_fixtures  # noqa: E402
import hands_fixtures as fx  # noqa: E402


@pytest.fixture(scope="module")
def cases() -> list[dict]:
    return json.loads(CASES_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def values(tmp_path_factory) -> dict[str, str]:
    return fx.prepare_hands_fixtures(tmp_path_factory.mktemp("hands"), gap_fixtures.remove_tree)


def test_ten_cases_with_unique_ids_that_say_what_good_looks_like(cases) -> None:
    assert len(cases) == 10 and len({c["id"] for c in cases}) == 10
    assert all(c["id"].startswith("hands_") and c["suites"] == ["hands"] and c["rubric"] and c["assertions"].get("final_status_in") for c in cases)


def test_every_placeholder_is_a_fixture_value_or_the_web_servers_address(cases, values) -> None:
    known = set(values) | {"fixture_base_url"}
    for case in cases:
        for name in re.findall(r"\{\{(\w+)\}\}", json.dumps(case)):
            assert name in known, (case["id"], name)


def test_the_pages_hold_the_one_checkable_result_each_case_expects(cases, values) -> None:
    pages = {name: (Path(values["h_out"]).parents[1] / "web" / "hands" / name).read_text(encoding="utf-8") for name in fx.PAGES}
    expected = {"hands_form": ("form.html", "CONF-7731"), "hands_cookie_wall": ("cookie.html", "29.99"), "hands_moving_button": ("moving.html", "GO-5519"), "hands_cart_stops_for_a_yes": ("shop.html", "ORD-2290")}
    by_id = {c["id"]: c for c in cases}
    for cid, (page, marker) in expected.items():
        assert marker in pages[page] and any(marker in word for word in by_id[cid]["assertions"]["bot_reply_contains_any"]), cid
    assert (Path(values["h_out"]).parents[1] / "web" / "hands" / "report.txt").read_text(encoding="utf-8").startswith("Quarterly summary")
    assert by_id["hands_cart_stops_for_a_yes"]["assertions"]["tool_approvals_min"] == 1  # the payment click asks, whatever the level
