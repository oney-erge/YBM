"""What a request says, read once, in one place.

The worker used to read the user's words in a dozen places, each with its own pattern and its own idea of which text to read: the words
the user wrote, or those plus the objective the concierge made from them, or a lower-cased copy. A delete was recognised in one place,
an "ask me first" in another, a request for the newest file in a third, and nothing said that they were the same kind of thing or which
text they came from. This module is that one place. It holds every pattern that decides what a request is about, builds one
`RequestFacts` per task, and says in a comment which text each fact is read from, so that a guard asks the facts instead of matching
words itself.

Behaviour is exactly what each guard did before: a fact is read from the same text with the same pattern. Where a fact should one day
be settled by the decision model instead of by words (see `orchestration/intent_model.py`), this is the only place that changes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# --- patterns: what a request is about ---------------------------------------------------------------------------------------------------

#: Delete, in the words people use. YBM has no delete operation, so a request that only asks for one is refused.
DELETE = re.compile(r"\b(?:delete|erase|wipe|trash|get rid of|remove all|throw away|destroy)\b", re.IGNORECASE)
#: Move, rename, sort and the like. A request that also says one of these keeps its move or rename operations.
MOVE = re.compile(
    r"\b(?:move|organi[sz]e|rename|sort|tidy|clean ?up|archive|copy|group|put (?:them|it|these|those)? ?(?:in|into|under))\b", re.IGNORECASE
)
#: Add to the end of a file ("on a new line", "at the end", "underneath").
APPEND = re.compile(
    r"\bappend\b|\b(?:on|in|as|onto)\s+(?:a\s+|the\s+)?(?:new|next|separate|second|another)\s+line\b"
    r"|\b(?:at|to)\s+the\s+(?:end|bottom|back)\b|\badd\s+(?:one\s+more|another|a\s+new|a)\s+(?:line|entry|item|row)\b|\bunderneath\b",
    re.IGNORECASE,
)
#: Change an existing file ("change it to say goodbye", "update the file").
CHANGE = re.compile(r"\b(?:change|update|edit|modify|replace|overwrite|rewrite|revise|correct)\b", re.IGNORECASE)
#: Which is newest, oldest, last changed.
RECENCY = re.compile(
    r"\b(?:newest|oldest|latest|most recent(?:ly)?|last (?:modified|changed|updated|edited)|recently (?:modified|changed|updated|edited)|"
    r"modified (?:most )?recently|(?:when|what time) (?:was|were) \w+(?: \w+){0,4} (?:last )?(?:modified|changed|updated|edited))\b",
    re.IGNORECASE,
)
#: The request says to ask the user before acting.
ASK_FIRST = re.compile(
    r"\b(?:ask|check with|confirm with|run (?:it|this|that) by|clear (?:it|this|that) with)\s+me\b"
    r"|\b(?:wait for|get|need|want|require)\s+(?:my|your)\s+(?:ok|okay|approval|permission|go-ahead|confirmation|consent|sign-off)\b"
    r"|\blet me (?:approve|confirm|review|decide)\b"
    r"|\bonly (?:after|with) (?:my|i)\s+(?:approve|approval|confirm|ok)\b",
    re.IGNORECASE,
)
#: An outside coding agent asked for by description ("use a coding agent").
CODING_AGENT_MENTION = re.compile(r"\b(?:coding|external|code)\s+(?:agent|assistant|session)\b", re.IGNORECASE)
#: The request is to understand a document, not to read it out.
DOCUMENT_UNDERSTANDING = re.compile(
    r"\b(?:summar\w*|explain\w*|overview|gist|main points|what\b[^.?!\n]{0,40}\babout)\b", re.IGNORECASE
)
URL = re.compile(r"https?://[^\s<>\"')\]]+")
#: Make an account on a website ("create an account", "sign me up", "register").
NEW_ACCOUNT = re.compile(
    r"\b(?:create|make|open|set\s*up|start|register|get)\b[^.?!\n]{0,40}\b(?:an?\s+)?(?:new\s+)?(?:account|login|profile)\b|\bsign\s*(?:me\s*|us\s*)?up\b|\bregister\b",
    re.IGNORECASE,
)
#: Log in to a website.
LOG_IN = re.compile(r"\b(?:log\s*(?:me\s*)?in|sign\s*(?:me\s*)?in|login)\b", re.IGNORECASE)
_PROVIDERS = (
    ("codex", re.compile(r"\bcodex\b")),
    ("claude_code", re.compile(r"\bclaude(?:\s+code)?\b")),
    ("github_copilot", re.compile(r"\b(?:github\s+)?copilot\b")),
)


def own_words(task: Any) -> str:
    """What the user wrote, without the question and answer a clarification round appends to the task's objective.

    After "Question to the user: ... offer to move them to a folder ..." the objective holds the model's words as well, and a check that
    acts on the user's behalf (a delete refused, a file overwritten) must not read the model's words as theirs.
    """
    original = str(task.metadata.get("original_message_text") or "").strip()
    return original or str(task.objective or "")


def own_and_objective(task: Any) -> str:
    """The user's words and the objective made from them, for a fact the objective may state in other words (newest, ask me first)."""
    return " ".join(str(value or "") for value in (task.metadata.get("original_message_text"), task.objective))


@dataclass(frozen=True)
class RequestFacts:
    """Facts about one request. Each says which text it reads: `own` (what the user wrote) or `both` (that and the objective)."""

    own: str
    both: str

    # read from `own`
    @property
    def deletes(self) -> bool:
        return bool(DELETE.search(self.own))

    @property
    def moves(self) -> bool:
        return bool(MOVE.search(self.own))

    @property
    def appends(self) -> bool:
        return bool(APPEND.search(self.own))

    @property
    def changes(self) -> bool:
        return bool(CHANGE.search(self.own))

    @property
    def asks_to_understand_a_document(self) -> bool:
        return bool(DOCUMENT_UNDERSTANDING.search(self.own))

    @property
    def wants_a_new_account(self) -> bool:
        return bool(NEW_ACCOUNT.search(URL.sub(" ", self.own)))  # "/signup" in an address is not a request to sign up

    @property
    def wants_to_log_in(self) -> bool:
        return bool(LOG_IN.search(URL.sub(" ", self.own)))

    @property
    def urls(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(match.rstrip(".,;:!?") for match in URL.findall(self.own)))

    # read from `both`
    @property
    def wants_recency(self) -> bool:
        return bool(RECENCY.search(self.both))

    @property
    def asks_to_be_asked_first(self) -> bool:
        return bool(ASK_FIRST.search(self.both))

    @property
    def providers(self) -> tuple[str, ...]:
        text = self.both.casefold()
        return tuple(name for name, pattern in _PROVIDERS if pattern.search(text))

    @property
    def names_a_coding_agent(self) -> bool:
        return bool(self.providers or CODING_AGENT_MENTION.search(self.both))

    # derived
    @property
    def only_deletes(self) -> bool:
        """Asks to delete and does not also ask to move, rename or organise (a delete cannot be faked with those)."""
        return self.deletes and not self.moves

    @classmethod
    def from_task(cls, task: Any) -> RequestFacts:
        return cls(own=own_words(task), both=own_and_objective(task))

    @classmethod
    def from_text(cls, text: str) -> RequestFacts:
        return cls(own=text, both=text)
