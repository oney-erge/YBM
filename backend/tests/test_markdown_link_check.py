"""The documentation link checker must catch dead files and dead heading anchors.

A reader following `[Install](INSTALL.md#link-whatsapp)` into a page with no such heading lands at
the top of the file with no hint anything is wrong, which is how a broken anchor survived in the
docs for a long time. These pin the slug rule (GitHub's) the checker validates against.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_markdown_links.py"


@pytest.fixture(scope="module")
def checker():
    spec = importlib.util.spec_from_file_location("check_markdown_links", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("heading", "slug"),
    [
        ("Link WhatsApp", "link-whatsapp"),
        ("Decisions `decide()` can return", "decisions-decide-can-return"),
        ("Part 1 - P0–P6: the architecture overhaul", "part-1---p0p6-the-architecture-overhaul"),
        ("Windows option 1: MSI", "windows-option-1-msi"),
        ("[Linked](x.md) text", "linked-text"),
    ],
)
def test_slugify_follows_github(checker, heading, slug) -> None:
    assert checker.slugify(heading) == slug


def test_repeated_headings_get_numbered_anchors(checker, tmp_path) -> None:
    page = tmp_path / "page.md"
    page.write_text("# Setup\n\n## Notes\n\n## Notes\n", encoding="utf-8")

    assert {"setup", "notes", "notes-1"} <= checker.anchors(page)


def test_headings_inside_code_fences_are_not_anchors(checker, tmp_path) -> None:
    page = tmp_path / "fenced.md"
    page.write_text("# Real\n\n```bash\n# not a heading\n```\n", encoding="utf-8")

    assert "not-a-heading" not in checker.anchors(page)
    assert "real" in checker.anchors(page)


def test_links_to_real_files_and_headings_pass(checker, tmp_path) -> None:
    (tmp_path / "other.md").write_text("# Other\n\n## Link WhatsApp\n", encoding="utf-8")
    page = tmp_path / "page.md"
    page.write_text(
        "# Page\n\n## Local\n\n[a](other.md) [b](other.md#link-whatsapp) [c](#local) [d](https://example.com/#x)\n",
        encoding="utf-8",
    )

    assert checker.missing_links(page) == []


def test_a_missing_heading_is_reported(checker, tmp_path) -> None:
    (tmp_path / "other.md").write_text("# Other\n", encoding="utf-8")
    page = tmp_path / "page.md"
    page.write_text("[x](other.md#5-link-whatsapp-optional)\n\n[y](#nope)\n", encoding="utf-8")

    reported = checker.missing_links(page)

    assert [line for line, _ in reported] == [1, 3]
    assert all("no such heading" in target for _, target in reported)


def test_a_missing_file_is_reported(checker, tmp_path) -> None:
    page = tmp_path / "page.md"
    page.write_text("[gone](GAPS.md)\n", encoding="utf-8")

    assert checker.missing_links(page) == [(1, "GAPS.md")]


def test_the_repository_docs_have_no_broken_links_or_anchors(checker) -> None:
    if not (checker.REPO_ROOT / ".git").exists():
        pytest.skip("needs a git checkout to list the maintained Markdown files")
    broken = [
        f"{path.relative_to(checker.REPO_ROOT)}:{line}: {target}"
        for path in checker.markdown_files()
        for line, target in checker.missing_links(path)
    ]

    assert broken == []
