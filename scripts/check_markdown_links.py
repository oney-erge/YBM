"""Fail when a maintained Markdown file links to a missing local path or heading anchor."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
import re
import subprocess
from urllib.parse import unquote, urlsplit


REPO_ROOT = Path(__file__).resolve().parents[1]
LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
HEADING_RE = re.compile(r"^ {0,3}#{1,6}[ \t]+(.+?)[ \t]*#*[ \t]*$")
HTML_ANCHOR_RE = re.compile(r"""<a\s+[^>]*?(?:id|name)\s*=\s*["']([^"']+)["']""", re.IGNORECASE)


def markdown_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "--", "*.md"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return sorted(
        {REPO_ROOT / value for value in result.stdout.splitlines() if (REPO_ROOT / value).exists()}
    )


def link_target(raw: str) -> str:
    value = raw.strip()
    if value.startswith("<") and ">" in value:
        return value[1:value.index(">")]
    return value.split(maxsplit=1)[0]


def slugify(heading: str) -> str:
    """GitHub's heading-to-anchor rule: drop markup and punctuation, lowercase, spaces to hyphens."""
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", heading)
    text = re.sub(r"<[^>]+>", "", text).replace("`", "").strip().lower()
    kept = "".join(ch for ch in text if ch.isalnum() or ch in " -_")
    return kept.replace(" ", "-")


@lru_cache(maxsize=None)
def anchors(path: Path) -> frozenset[str]:
    """Every anchor a link to this file may use: its headings (with GitHub's -1, -2 suffixes
    for repeats) and any explicit HTML anchors."""
    found: set[str] = set()
    seen: dict[str, int] = {}
    in_fence = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        found.update(match.lower() for match in HTML_ANCHOR_RE.findall(line))
        heading = HEADING_RE.match(line)
        if heading:
            slug = slugify(heading.group(1))
            count = seen.get(slug, 0)
            seen[slug] = count + 1
            found.add(slug if count == 0 else f"{slug}-{count}")
    return frozenset(found)


def missing_links(path: Path) -> list[tuple[int, str]]:
    missing: list[tuple[int, str]] = []
    in_fence = False
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        for match in LINK_RE.finditer(line):
            target = link_target(match.group(1))
            parsed = urlsplit(target)
            if not target or parsed.scheme or parsed.netloc:
                continue
            local_value = unquote(parsed.path)
            local_path = (path.parent / local_value).resolve() if local_value else path
            if not local_path.exists():
                missing.append((line_number, target))
                continue
            fragment = unquote(parsed.fragment).lower()
            if fragment and local_path.is_file() and local_path.suffix == ".md":
                if fragment not in anchors(local_path):
                    missing.append((line_number, f"{target} (no such heading)"))
    return missing


def main() -> int:
    failures = [
        (path, line_number, target)
        for path in markdown_files()
        for line_number, target in missing_links(path)
    ]
    if failures:
        for path, line_number, target in failures:
            print(f"{path.relative_to(REPO_ROOT)}:{line_number}: broken local link: {target}")
        return 1
    print("Maintained Markdown links and anchors are valid.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
