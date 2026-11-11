"""Remember what a person says about themselves as separate facts, not one sentence.

"Remember these about me: my GitHub username is oney-erge, my favorite language is Python, and my
t-shirt size is L" used to be stored as that whole sentence, under "preference". A form that asks for
a GitHub username then had to find it inside a paragraph, and a later "my t-shirt size is XL" added a
second sentence that contradicted the first instead of replacing it.

Each "my <thing> is <value>" clause becomes one fact, "<Thing>: <value>", in the `profile` category,
and saying the same thing again updates it. Text that is not made of such clauses (a rule such as
"remember that I prefer short answers") is not touched and keeps its old path.
"""

from __future__ import annotations

import re

from agent_control.schemas import MemoryFact, MemorySource
from agent_control.storage.repositories import Repositories

PROFILE_CATEGORY = "profile"

_PREAMBLE = re.compile(
    r"^\s*(?:please\s+)?(?:remember|don'?t\s+forget)(?:\s+(?:that|these|this|the\s+following))?(?:\s+about\s+me)?\s*[:,]?\s*",
    re.IGNORECASE,
)
_CLAUSE = re.compile(
    r"\bmy\s+([A-Za-z][A-Za-z0-9' \-]{1,50}?)\s+(?:is|are)\s+(?:called\s+|named\s+)?([^,;\n]+?)"
    r"(?=\s*(?:,|;|\.(?:\s|$)|\n|$)|\s+and\s+my\b)",
    re.IGNORECASE,
)
#: Fewer clause words than this share of the statement means it is mostly something else (a rule, a story).
_MIN_COVERAGE = 0.5
_MAX_LABEL_WORDS = 6
_MAX_VALUE_CHARS = 120


def profile_pairs(text: str) -> list[tuple[str, str]]:
    """The (label, value) pairs a statement is made of, or none when it is mostly something else."""
    body = _PREAMBLE.sub("", (text or "").strip(), count=1)
    # A profile statement starts with "my ...". "Whenever you summarize a PDF, my boss is the audience" is a rule.
    if not re.match(r"my\s", body, re.IGNORECASE):
        return []
    pairs: list[tuple[str, str]] = []
    covered = 0
    for match in _CLAUSE.finditer(body):
        label = " ".join(match.group(1).split())
        value = match.group(2).strip().strip("\"'").rstrip(".").strip()
        if not value or len(value) > _MAX_VALUE_CHARS or len(label.split()) > _MAX_LABEL_WORDS:
            continue
        pairs.append((label, value))
        covered += len(match.group(0))
    if not pairs or covered / max(len(body), 1) < _MIN_COVERAGE:
        return []
    return pairs


def store_profile_facts(repositories: Repositories, pairs: list[tuple[str, str]]) -> list[MemoryFact]:
    """One fact per pair; a label that is already known is updated, never duplicated."""
    known = repositories.memory_facts.list_all(category=PROFILE_CATEGORY)
    stored: list[MemoryFact] = []
    for label, value in pairs:
        heading = label[0].upper() + label[1:]
        content = f"{heading}: {value}"
        existing = next((f for f in known if f.content.casefold().startswith(f"{label.casefold()}:")), None)
        if existing is not None:
            updated = repositories.memory_facts.update_content(existing.id, category=PROFILE_CATEGORY, content=content)
            if updated is not None:
                stored.append(updated)
                continue
        stored.append(repositories.memory_facts.create(
            MemoryFact(category=PROFILE_CATEGORY, content=content, source=MemorySource.USER_STATED)
        ))
    return stored


def describe_pairs(pairs: list[tuple[str, str]]) -> str:
    """`your GitHub username (oney-erge), your favorite language (Python) and your shirt size (L)`."""
    parts = [f"your {label} ({value})" for label, value in pairs]
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]
