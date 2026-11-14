"""What an answer says it did, set against what the task's record shows it did."""
from __future__ import annotations

import json
import re
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
