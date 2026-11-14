"""Find the abilities that fit a request.

A request is matched against every ability by the words it shares with the ability's name, what it provides, its summary and its
detail (BM25: a rare word such as "pdf" counts for more than a common one such as "file"). That is enough to answer "what could
do this?" over a hundred records without a model call, so it can run on every message and in front of every model: the decision
model and the Operator see the few records that fit instead of every record there is.

This is retrieval, not judgement. The index says which abilities are candidates and what state each is in; choosing among them is
the model's job, and a record that does not fit is simply not shown.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable

from agent_control.abilities.model import Ability, AbilityState

_WORD = re.compile(r"[a-z0-9]+")
_STOP = frozenset(
    "a an the and or of to in on at for from with by is are was be it its this that these those my me i you your we us our can could "
    "would should will do does did please just some any all there here as if then so how what which who when where why not no yes "
    "get got make made want need like".split()
)

# BM25's usual constants. The documents are a line or two long, so length normalisation matters little.
_K1 = 1.4
_B = 0.5
_NAME_WEIGHT = 3
_PROVIDES_WEIGHT = 3
_SUMMARY_WEIGHT = 2


def _stem(word: str) -> str:
    """Strip a plural or a verb ending, so "files" finds "file" and "ordering" finds "order". Rough on purpose: both sides use it."""
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 5 and word.endswith("ing"):
        return word[:-3]
    if len(word) > 4 and word.endswith("ed"):
        return word[:-2]
    if len(word) > 4 and re.search(r"(ss|x|ch|sh)es$", word):
        return word[:-2]
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def tokens(text: str) -> list[str]:
    """The words of a text that carry meaning, lower-cased and reduced to a stem. Dotted names split: "filesystem.manage" gives two."""
    return [_stem(word) for word in _WORD.findall((text or "").lower()) if word not in _STOP and len(word) > 1]


class AbilityIndex:
    """A snapshot of every known ability, searchable. Build a new one when the sources change; it is cheap."""

    def __init__(self, abilities: Iterable[Ability]) -> None:
        self._by_id: dict[str, Ability] = {}
        for ability in abilities:
            self._by_id.setdefault(ability.id, ability)  # the first record of an id wins: sources are listed most specific first
        self._abilities = tuple(self._by_id.values())
        self._documents: list[Counter[str]] = []
        for ability in self._abilities:
            document: Counter[str] = Counter()
            for weight, text in (
                (_NAME_WEIGHT, ability.name),
                (_PROVIDES_WEIGHT, " ".join(ability.provides)),
                (_SUMMARY_WEIGHT, ability.summary),
                (1, ability.detail),
            ):
                for word in tokens(text):
                    document[word] += weight
            self._documents.append(document)
        total = sum(sum(document.values()) for document in self._documents)
        self._average_length = (total / len(self._documents)) if self._documents else 1.0
        self._document_frequency: Counter[str] = Counter()
        for document in self._documents:
            self._document_frequency.update(document.keys())

    def __len__(self) -> int:
        return len(self._abilities)

    def all(self) -> tuple[Ability, ...]:
        return self._abilities

    def get(self, ability_id: str) -> Ability | None:
        return self._by_id.get(ability_id)

    def find_by_name(self, name: str) -> Ability | None:
        """An ability by its name or its id, ignoring case: what a model writes when it names one."""
        wanted = (name or "").strip().casefold()
        for ability in self._abilities:
            if wanted in {ability.name.casefold(), ability.id.casefold()}:
                return ability
        return None

    def groups(self) -> dict[str, tuple[Ability, ...]]:
        grouped: dict[str, list[Ability]] = {}
        for ability in self._abilities:
            grouped.setdefault(ability.group or "other", []).append(ability)
        return {group: tuple(items) for group, items in sorted(grouped.items())}

    def search(
        self,
        text: str,
        limit: int = 8,
        *,
        states: Iterable[AbilityState] | None = None,
    ) -> list[tuple[Ability, float]]:
        """The abilities that share words with ``text``, best first, with their scores. Nothing shared means nothing returned."""
        query = Counter(tokens(text))
        if not query or not self._abilities:
            return []
        allowed = set(states) if states is not None else None
        count = len(self._abilities)
        scored: list[tuple[Ability, float]] = []
        for ability, document in zip(self._abilities, self._documents):
            if allowed is not None and ability.state not in allowed:
                continue
            length = sum(document.values()) or 1
            score = 0.0
            for word in query:
                frequency = document.get(word, 0)
                if not frequency:
                    continue
                containing = self._document_frequency[word]
                idf = math.log(1 + (count - containing + 0.5) / (containing + 0.5))
                score += idf * (frequency * (_K1 + 1)) / (frequency + _K1 * (1 - _B + _B * length / self._average_length))
            if score > 0:
                scored.append((ability, score))
        scored.sort(key=lambda pair: (-pair[1], pair[0].name))
        return scored[:limit]
