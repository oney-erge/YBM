"""A local, personal document index (docs/archive/HISTORY.md Part 4 T2.7): lexical
search over a folder of the user's own reference material, so the Operator
can answer from what the user already has on disk instead of only from
tool output gathered mid-task.

Keyword-overlap scoring, not embeddings - see KnowledgeBaseAdapterConfig's
docstring for why. Re-indexed fresh on every search call, same reasoning as
skills.py: files are small-to-modest in number, local, and hand-edited, so a
worker process should see a newly added or edited file on its very next
call, not after a restart or a cache-invalidation step.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from agent_control.config import KnowledgeBaseAdapterConfig
# Reused rather than duplicated - filesystem_manage.py already has robust
# text extraction (plain text, HTML tag-stripping, PDF via pypdf with a raw
# fallback) and this needs exactly the same thing over the same file types.
from agent_control.tools.filesystem_manage import _extract_supported_text, _is_text_file

_WORD_PATTERN = re.compile(r"[A-Za-z0-9']+")
# Chunk size for splitting one file into independently-scored passages, so a
# match deep in a long document doesn't get diluted by the rest of the file
# in the overlap score, and the caller gets a short, relevant excerpt back
# rather than the whole file.
_CHUNK_CHARS = 1200


@dataclass(frozen=True)
class KnowledgeChunk:
    path: str
    chunk_index: int
    text: str


def _tokenize(text: str) -> set[str]:
    return {match.group(0).lower() for match in _WORD_PATTERN.finditer(text)}


def _is_indexable(path: Path) -> bool:
    # Same file types filesystem_manage.py already knows how to read text
    # out of - its own text-file set, plus PDF (which it also extracts).
    return _is_text_file(path) or path.suffix.lower() == ".pdf"


def list_sources(config: KnowledgeBaseAdapterConfig) -> list[str]:
    """Relative paths of every indexable file under root_dir, sorted."""
    root = Path(config.root_dir).expanduser()
    if not root.is_dir():
        return []
    paths = [p for p in root.rglob("*") if p.is_file() and _is_indexable(p)][: config.max_files_indexed]
    return sorted(str(p.relative_to(root)) for p in paths)


def _index(config: KnowledgeBaseAdapterConfig) -> list[KnowledgeChunk]:
    root = Path(config.root_dir).expanduser()
    if not root.is_dir():
        return []
    files = sorted(p for p in root.rglob("*") if p.is_file() and _is_indexable(p))[: config.max_files_indexed]
    chunks: list[KnowledgeChunk] = []
    for path in files:
        try:
            text = _extract_supported_text(path, max_chars=config.max_chars_per_file)
        except Exception:
            continue
        if not text.strip():
            continue
        relative = str(path.relative_to(root))
        for index, start in enumerate(range(0, len(text), _CHUNK_CHARS)):
            chunk_text = text[start : start + _CHUNK_CHARS].strip()
            if chunk_text:
                chunks.append(KnowledgeChunk(path=relative, chunk_index=index, text=chunk_text))
    return chunks


def search(config: KnowledgeBaseAdapterConfig, query: str) -> list[dict[str, object]]:
    """Top-N chunks ranked by keyword overlap with `query`, each as
    {"path", "chunk_index", "excerpt", "score"}. Empty list for an empty
    corpus or a query with no indexable words - never raises on "no
    results", since "nothing found" is a normal, expected outcome here."""
    query_words = _tokenize(query)
    if not query_words:
        return []
    scored = []
    for chunk in _index(config):
        chunk_words = _tokenize(chunk.text)
        overlap = query_words & chunk_words
        if not overlap:
            continue
        # Overlap count, normalized by how many distinct query words matched
        # relative to the query's own size - rewards chunks that cover more
        # of the query, not just chunks that happen to be long.
        score = len(overlap) / len(query_words)
        scored.append((score, chunk))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [
        {
            "path": chunk.path,
            "chunk_index": chunk.chunk_index,
            "excerpt": chunk.text[:800],
            "score": round(score, 4),
        }
        for score, chunk in scored[: config.max_results]
    ]
