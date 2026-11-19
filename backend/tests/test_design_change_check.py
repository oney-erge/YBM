"""The change rule's check (scripts/check_design_change.py, docs/DESIGN.md): a Minimal change may not do what only a Design change may do."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_design_change.py"
spec = importlib.util.spec_from_file_location("check_design_change", SCRIPT)
rule = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules["check_design_change"] = rule  # a dataclass looks its module up here
spec.loader.exec_module(rule)

P = rule.PRODUCT
GOOD = {"wording_patterns": 10, "call_rewrite_lines": 100}


def _body(minimal: bool = False, design: bool = False) -> str:
    return f"- [{'x' if minimal else ' '}] Minimal: fits a home\n- [{'X' if design else ' '}] Design: has an entry\n"


def test_the_ticked_box_is_the_label():
    assert rule.labels_in(_body(minimal=True)) == ["minimal"]
    assert rule.labels_in(_body(design=True)) == ["design"]
    assert rule.labels_in(_body()) == []
    assert rule.labels_in(_body(minimal=True, design=True)) == ["minimal", "design"]
    assert rule.labels_in("") == []


def test_a_change_with_no_product_code_needs_no_label():
    changes = [rule.Change("docs/USING.md", "M", ["words"]), rule.Change("backend/tests/test_x.py", "A", ["def test(): pass"])]
    assert rule.check(changes, "", GOOD, GOOD) == []


def test_product_code_needs_exactly_one_label():
    changes = [rule.Change(P + "goals.py", "M", ["x = 1"])]
    assert "neither" in rule.check(changes, "", GOOD, GOOD)[0]
    assert "both" in rule.check(changes, _body(minimal=True, design=True), GOOD, GOOD)[0]
    assert rule.check(changes, _body(minimal=True), GOOD, GOOD) == []


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        (rule.Change(P + "new_thing.py", "A", ["x = 1"]), "a new module"),
        (rule.Change(P + "storage/migrations.py", "M", ['    "CREATE TABLE plans (id TEXT)",']), "a new table"),
        (rule.Change(P + "storage/migrations.py", "M", ['    "ALTER TABLE tasks ADD COLUMN note TEXT",']), "a new table"),
        (rule.Change(P + "config.py", "M", ["    scope_more: bool = False"]), "a new config key"),
        (rule.Change(P + "tools/plan_manage.py", "M", ["    ToolDefinition("]), "a new tool"),
        (rule.Change(P + "tools/plan_manage.py", "M", ["        operations=('create', 'show'),"]), "a new tool"),
        (rule.Change(P + "tools/contracts.py", "M", ['    operation: Literal["a", "b"]']), "a new tool operation"),
        (rule.Change(P + "orchestration/ladder.py", "M", ["_X = re.compile(r'try again')"]), "new pattern that matches wording"),
        (rule.Change(P + "clarification.py", "M", ["_X = re.compile(r'you choose')"]), "new pattern that matches wording"),
        (rule.Change(P + "prompts/tasks/new.md", "A", ["a rule"]), "a new prompt file"),
    ],
)
def test_minimal_may_not_do_what_only_design_may(change, expected):
    problems = rule.check([change], _body(minimal=True), GOOD, GOOD)
    assert problems and expected in " ".join(problems), problems


def test_a_pattern_outside_the_homes_that_read_wording_is_not_wording():
    change = rule.Change(P + "storage/redaction.py", "M", ["_KEY = re.compile(r'sk-[a-z]+')"])
    assert rule.check([change], _body(minimal=True), GOOD, GOOD) == []


def test_minimal_is_small():
    big = rule.Change(P + "goals.py", "M", [f"x{i} = {i}" for i in range(rule.MINIMAL_LINES + 1)])
    assert "added lines of product code" in " ".join(rule.check([big], _body(minimal=True), GOOD, GOOD))
    comments_and_blanks = rule.Change(P + "goals.py", "M", ["# note", "", "x = 1"] * 100)
    assert rule.check([comments_and_blanks], _body(minimal=True), GOOD, GOOD) == []


def test_design_needs_an_entry_in_the_design_doc():
    changes = [rule.Change(P + "memory_index.py", "A", ["x = 1"])]
    assert "docs/DESIGN.md" in rule.check(changes, _body(design=True), GOOD, GOOD)[0]
    changes.append(rule.Change("docs/DESIGN.md", "M", ["entry"]))
    assert rule.check(changes, _body(design=True), GOOD, GOOD) == []


def test_a_count_may_go_down_only_when_it_is_written_down():
    lower = {"wording_patterns": 8, "call_rewrite_lines": 100}
    assert "went down" in rule.check([], "", lower, GOOD)[0]
    assert rule.check([rule.Change("docs/design_baseline.json", "M", ["8"])], "", lower, GOOD) == []


def test_a_count_goes_up_only_in_a_design_change_that_says_so():
    higher = {"wording_patterns": 11, "call_rewrite_lines": 100}
    product = rule.Change(P + "goals.py", "M", ["x = 1"])
    problems = rule.check([product], _body(minimal=True), higher, GOOD)
    assert any("went up" in p for p in problems)
    design = [product, rule.Change("docs/DESIGN.md", "M", ["entry"])]
    assert any("went up" in p for p in rule.check(design, _body(design=True), higher, GOOD))
    design.append(rule.Change("docs/design_baseline.json", "M", ["11"]))
    assert rule.check(design, _body(design=True), higher, GOOD) == []


def test_a_missing_baseline_is_asked_for():
    assert "no baseline" in rule.check([], "", GOOD, {})[0]


def test_the_counts_read_the_tree(tmp_path):
    (tmp_path / (P + "orchestration")).mkdir(parents=True)
    (tmp_path / (P + "orchestration") / "a.py").write_text("import re\nA = re.compile('x')\nB = re.compile('y')\n", encoding="utf-8")
    (tmp_path / (P + "orchestration") / "call_rewrites.py").write_text("one\ntwo\nthree\n", encoding="utf-8")
    (tmp_path / (P + "tools")).mkdir(parents=True)
    (tmp_path / (P + "tools") / "other.py").write_text("C = re.compile('z')\n", encoding="utf-8")
    assert rule.counts(tmp_path) == {"wording_patterns": 2, "call_rewrite_lines": 3}


def test_the_baseline_in_the_repository_is_today_s_count():
    # The ratchet only means something when the file in the repository is true: a count that went down was written down in the same change.
    assert rule.read_baseline() == {k: v for k, v in rule.counts().items()}


def test_the_diff_is_read_from_git(tmp_path, monkeypatch):
    def git(*args: str) -> None:
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-q", "-b", "main")
    (tmp_path / "a.py").write_text("one = 1\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-q", "-m", "base")
    git("checkout", "-q", "-b", "change")
    (tmp_path / "a.py").write_text("one = 1\ntwo = 2\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("three = 3\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-q", "-m", "change")
    monkeypatch.setattr(rule, "ROOT", tmp_path)
    got = {c.path: c for c in rule.changes_since("main")}
    assert got["a.py"].status == "M" and got["a.py"].added == ["two = 2"]
    assert got["b.py"].status == "A" and got["b.py"].added == ["three = 3"]
