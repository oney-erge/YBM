"""What an answer says it did, set against what the task's record shows it did."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from agent_control.schemas import (
    ToolResultStatus,
)

# Operations and tools that actually put bytes on disk. Plan-producing
# operations (organize_plan, rename_plan) deliberately do not qualify - they
# describe a change without making it.
_WRITE_OPERATIONS = frozenset(
    {"write_text_file", "write_files", "materialize_static_app", "web_app_preview", "apply_manifest", "scaffold"}
)


_WRITE_CLAIM = re.compile(
    r"(?i)\b(?:created?|creating|wrote|written|writing|saved|saving|scaffolded|scaffolding|generated|"
    r"added|placed)\b[^.!?\n]{0,90}?\b(?:file|files|folder|directory|package\.json|readme|extension|script)\b"
    r"|\bfiles?\b[^.!?\n]{0,40}?\b(?:were|was|have been|has been|is|are)\s+(?:created|written|saved|added)\b"
)


_NEGATED_CLAIM = re.compile(
    r"(?i)\b(?:not|cannot|can't|can’t|could\s?n[o']?t|couldn’t|un(?:able|successful)|"
    r"fail(?:ed|s|ure)?|did\s?n[o']?t|was\s?n[o']?t|were\s?n[o']?t|no|never|without|skip(?:ped)?)\b"
)


# "The Downloads folder has been organized by type", "moved the PDFs into a Documents folder". A
# claim that files were rearranged, which the write claim above does not cover: a live run spent
# its steps listing and searching a folder, then answered that it had organized it, and completed.
_MOVE_CLAIM = re.compile(
    r"(?i)\b(?:folders?|files|downloads|desktop|documents)\b[^.!?\n]{0,60}?"
    r"\b(?:has been|have been|was|were|is now|are now)\s+(?:organi[sz]ed|rearranged|sorted|moved|grouped|tidied)\b"
    r"|\b(?:organi[sz]ed|rearranged|tidied up)\b[^.!?\n]{0,60}?\b(?:folders?|files|downloads|desktop|documents)\b"
    r"|\b(?:moved|sorted|grouped|relocated)\b[^.!?\n]{0,90}?\binto\b[^.!?\n]{0,60}?"
    r"\b(?:folders?|subfolders?|director(?:y|ies))\b"
)


# Words that make a move a plan, an offer or a state rather than something done: "24 files will be
# moved into folders once you approve", "the folder is already organized".
_UNDONE_MOVE = re.compile(
    r"(?i)\b(?:will|would|can|could|should|may|might|shall|already|plan(?:ned)?|propos(?:e|ed|al)|"
    r"approve|approval|once|if|ready to|to be)\b"
)


# Where a real move shows up: the tool layer's changed-path keys, or a succeeded apply_manifest that
# was not a dry run.
_MOVE_EVIDENCE_KEYS = ("organized_paths", "moved_files", "changed_paths", "changed_files")


def _unsupported_move_claim(answer: str, history: list[dict[str, Any]], metadata: dict[str, Any] | None) -> bool:
    """Whether the answer says files were moved or organized and nothing moved any."""
    claims = [
        match
        for match in _MOVE_CLAIM.finditer(answer)
        if not _claim_is_negated(answer, match.start()) and not _UNDONE_MOVE.search(_clause_before(answer, match.end()))
    ]
    if not claims:
        return False
    if isinstance(metadata, dict) and any(metadata.get(key) for key in _MOVE_EVIDENCE_KEYS):
        return False
    for entry in history:
        if not isinstance(entry, dict) or entry.get("status") != ToolResultStatus.SUCCEEDED.value:
            continue
        tool_input = entry.get("input")
        if (
            isinstance(tool_input, dict)
            and tool_input.get("operation") == "apply_manifest"
            and not tool_input.get("dry_run")
        ):
            return False
    return True


def _clause_before(answer: str, end: int) -> str:
    """The clause a claim ends in, up to the claim's end."""
    clause_start = max(answer.rfind(".", 0, end), answer.rfind("\n", 0, end), answer.rfind(";", 0, end))
    return answer[clause_start + 1:end]


def _claim_is_negated(answer: str, start: int) -> bool:
    """Whether a write claim sits inside a denial of that same write.

    "I could not create the files" states the truth this guard exists to
    protect; flagging it would push a correctly-behaving run into a pointless
    replan and teach the operator that honesty is penalized.
    """
    clause_start = max(
        answer.rfind(".", 0, start), answer.rfind("\n", 0, start), answer.rfind(";", 0, start)
    )
    window = answer[max(clause_start + 1, start - 60):start]
    return bool(_NEGATED_CLAIM.search(window))


# Metadata keys the tool layer promotes when something was actually produced.
# Same notion of evidence `fulfillment._postcondition_satisfied` checks, so the
# two guards cannot disagree about whether a write happened.
_WRITE_EVIDENCE_KEYS = (
    "changed_paths", "changed_files", "files_created", "files_modified",
    "organized_paths", "document_path", "adapter_dir",
)


def unsupported_write_claim(
    answer: str | None,
    history: list[dict[str, Any]],
    metadata: dict[str, Any] | None = None,
) -> str | None:
    """Reject a final answer that claims files were written when none were.

    A single unsupported-operation failure was enough for the operator to stop
    and synthesize "The following files were created:" for an empty workspace,
    and the task completed (docs/archive/E2E_FINDINGS.md P0-2). Objective-derived
    postconditions are the other guard, but they are inferred from wording; this
    one reads the claim the answer actually makes and demands matching evidence.

    Returns a gap reason for the caller's existing bounded-replan path, so a
    false positive costs two extra steps rather than blocking the task.
    """
    if not answer:
        return None
    if _unsupported_move_claim(answer, history, metadata):
        # Its own code, so the replan names the operations that actually move files.
        return "expected_file_organization_missing"
    claims = [match for match in _WRITE_CLAIM.finditer(answer) if not _claim_is_negated(answer, match.start())]
    if not claims:
        return None

    evidence = _recorded_write_evidence(history, metadata)
    if not evidence:
        return (
            "the answer states that files were created or written, but no successful write "
            "operation is recorded - either perform the write or say plainly that it did not happen"
        )

    # Evidence that *some* write happened is not evidence that the claimed one
    # did. P0-2 listed package.json and extension.ts for an empty workspace; a
    # task that had already written something unrelated would otherwise let the
    # same fabrication through.
    #
    # Only compared when both sides actually name files. A recorded write whose
    # path never surfaced (nothing in the tool input, nothing in the truncated
    # output summary) leaves nothing to compare against, and guessing wrong
    # there costs a correct task two replans and then blocks it - worse than
    # accepting a claim that is already backed by a real write.
    claimed = _claimed_filenames(answer)
    if claimed and _claimed_filenames(evidence) and not any(name in evidence for name in claimed):
        listed = ", ".join(sorted(claimed)[:5])
        return (
            f"the answer names files ({listed}) that no recorded write produced - write them, "
            "or name only what the tool history actually shows"
        )
    return None


# A filename as it appears in prose: a stem plus a short extension. Deliberately
# not matching bare words, so "created the extension" is not read as a file.
_CLAIMED_FILENAME = re.compile(r"\b[\w][\w.-]*\.[A-Za-z][A-Za-z0-9]{0,4}\b")


def _claimed_filenames(answer: str) -> set[str]:
    return {match.group(0).casefold() for match in _CLAIMED_FILENAME.finditer(answer)}


def _recorded_write_evidence(history: list[dict[str, Any]], metadata: dict[str, Any] | None) -> str:
    """Everything the run can show for a write, as one searchable blob."""
    parts: list[str] = []
    if isinstance(metadata, dict):
        for key in _WRITE_EVIDENCE_KEYS:
            value = metadata.get(key)
            if value:
                parts.append(json.dumps(value, default=str))
    for entry in history:
        if not isinstance(entry, dict) or entry.get("status") != ToolResultStatus.SUCCEEDED.value:
            continue
        tool_input = entry.get("input")
        if isinstance(tool_input, dict) and str(tool_input.get("operation") or "") in _WRITE_OPERATIONS:
            parts.append(json.dumps(tool_input, default=str))
            parts.append(str(entry.get("output_summary") or ""))
    return " ".join(parts).casefold()


# ---------------------------------------------------------------------------------------------------------------------------------
# What an answer says it did to the world, beyond a file: it deleted something, sent something to someone else, installed something,
# paid for something. Each is a sentence a small model writes with the same ease whether or not it happened, and each is checked here
# against what the task's record shows. The test runner (e2e) reads the same function, so what is measured is what is protected.
# ---------------------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Claim:
    """A sentence in an answer that says something was done, and the kind of thing it says."""

    kind: str  # created | moved | deleted | sent_elsewhere | installed | paid
    said: str  # the words, for the reason the model is shown


# Said of the future, offered, asked about or wished for: not a claim that it happened.
_NOT_DONE = re.compile(
    r"(?i)\b(?:will|would|can|could|should|may|might|shall|if|once|until|unless|want|wants|like|let me|let's|do you|did you|"
    r"ready to|about to|going to|need to|needs to|to be)\b"
)
# Said of nothing: "no files were deleted", "nothing was sent", "I did not install".
_DENIAL = re.compile(
    r"(?i)\b(?:not|n['’]t|never|nothing|none|nobody|no one|no|without|fail(?:ed|s)?|unable|instead of)\b"
)

_OBJECT = r"(?:files?|folders?|director(?:y|ies)|photos?|pictures?|images?|documents?|logs?|data|everything|them|it)"
_DELETED = re.compile(
    rf"(?i)\b(?:deleted|erased|wiped|trashed|removed)\b[^.!?\n]{{0,60}}?\b{_OBJECT}\b"
    rf"|\b{_OBJECT}\b[^.!?\n]{{0,40}}?\b(?:were|was|have been|has been|are now|is now)\s+(?:deleted|erased|wiped|removed|trashed)\b"
)
_SENT_ELSEWHERE = re.compile(
    r"(?i)\b(?:e-?mailed|texted|whatsapped|tweeted|forwarded)\b"
    r"|\b(?:sent|posted|submitted)\s+(?!you\b|it to you\b)[^.!?\n]{0,30}?\b(?:e-?mails?|messages?|texts?|invites?|dms?|requests?|replies|comments?)\b"
    r"|\b(?:e-?mails?|messages?|texts?|invites?)\b[^.!?\n]{0,30}?\b(?:has been|have been|was|were)\s+sent\b"
)
_INSTALLED = re.compile(
    r"(?i)\b(?:installed|downloaded|set up|loaded|enabled|turned on|switched on)\b[^.!?\n]{0,40}?\b"
    r"(?:model|package|library|tool|server|app|software|extension|plugin|voice|engine|driver|ffmpeg|whisper)\b"
    r"|\b(?:model|package|library|tool|server|software|voice|engine)\b[^.!?\n]{0,30}?\b"
    r"(?:has been|have been|is now|are now|was|were)\s+(?:installed|downloaded|loaded|enabled|set up)\b"
)
_PAID = re.compile(
    r"(?i)\b(?:i(?:'ve| have)?|we)\s+(?:just\s+)?(?:bought|purchased|ordered|booked|paid|reserved|subscribed)\b"
    r"|\b(?:your\s+|the\s+|an?\s+)?(?:order|purchase|booking|payment|reservation|subscription)\b[^.!?\n]{0,30}?\b"
    r"(?:has been|have been|was|were|is now)\s+(?:placed|made|completed|confirmed|processed|booked|submitted)\b"
)
#: The kinds that are about the world beyond a file on disk; the checks that already read file writes and moves leave these to this module.
WORLD_CLAIMS = frozenset({"deleted", "sent_elsewhere", "installed", "paid"})
_CLAIM_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("deleted", _DELETED),
    ("sent_elsewhere", _SENT_ELSEWHERE),
    ("installed", _INSTALLED),
    ("paid", _PAID),
)


def _clause_start(answer: str, position: int) -> int:
    return max(answer.rfind(".", 0, position), answer.rfind("\n", 0, position), answer.rfind(";", 0, position), answer.rfind("?", 0, position)) + 1


def _said_as_done(answer: str, match: re.Match[str]) -> bool:
    """Whether the sentence around a match says the thing happened (not offered, asked about, wished, or denied)."""
    clause = answer[_clause_start(answer, match.start()) : match.end()]
    return not _NOT_DONE.search(clause) and not _DENIAL.search(clause)


def _succeeded_entries(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in history if isinstance(e, dict) and e.get("status") == ToolResultStatus.SUCCEEDED.value]


def _records_a_deletion(history: list[dict[str, Any]]) -> bool:
    from agent_control.tools.effects import classify_effect

    return any(
        classify_effect(str(e.get("tool_name")), str((e.get("input") or {}).get("operation") or "") or None) == "deleted"
        for e in _succeeded_entries(history)
        if isinstance(e.get("input") or {}, dict)
    )


def _records_a_message_to_someone(history: list[dict[str, Any]]) -> bool:
    return any(str(e.get("tool_name") or "").split(".")[0] in {"mail", "email", "messaging", "sms"} for e in _succeeded_entries(history))


def _records_an_install(history: list[dict[str, Any]]) -> bool:
    for e in _succeeded_entries(history):
        tool = str(e.get("tool_name") or "")
        tool_input = e.get("input") if isinstance(e.get("input"), dict) else {}
        if tool in {"ability.acquire", "tts.install"} or (tool == "mcp.client" and tool_input.get("operation") == "install_server"):
            return True
        if tool == "code.interpreter" and re.search(r"pip(?:3)? install|ollama pull|winget install|uv pip", json.dumps(tool_input, default=str)):
            return True
    return False


def unbacked_claims(answer: str | None, history: list[dict[str, Any]], metadata: dict[str, Any] | None = None) -> list[Claim]:
    """What the answer says it did that the record does not show.

    A payment is never backed: this assistant does not pay (abilities/gaps.py), so a sentence saying it did is false by construction.
    Files written and moved are checked by `unsupported_write_claim` and returned here too, so one call lists everything.
    """
    if not answer:
        return []
    found: list[Claim] = []
    reason = unsupported_write_claim(answer, history, metadata)
    if reason:
        kind = "moved" if reason == "expected_file_organization_missing" else "created"
        found.append(Claim(kind, reason[:120]))
    backed = {
        "deleted": _records_a_deletion(history),
        "sent_elsewhere": _records_a_message_to_someone(history),
        "installed": _records_an_install(history),
        "paid": False,
    }
    for kind, pattern in _CLAIM_PATTERNS:
        if backed[kind]:
            continue
        for match in pattern.finditer(answer):
            if _said_as_done(answer, match):
                found.append(Claim(kind, match.group(0).strip()[:120]))
                break
    return found
