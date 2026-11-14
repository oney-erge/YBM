"""What an answer says it did, set against what the record shows: deleted, sent to someone else, installed, paid for, written, moved."""

from __future__ import annotations

import pytest

from agent_control.orchestration import answer_checks
from agent_control.orchestration.check_entries import CHECK_ENTRY_STRATEGY
from agent_control.orchestration.claims import Claim, unbacked_claims
from agent_control.schemas import TaskRecord


def _ok(tool: str, operation: str | None = None, **extra) -> dict:
    return {"tool_name": tool, "input": {"operation": operation} if operation else {}, "status": "succeeded", **extra}


def _kinds(answer: str, history: list[dict] | None = None, metadata: dict | None = None) -> list[str]:
    return [claim.kind for claim in unbacked_claims(answer, history or [], metadata)]


@pytest.mark.parametrize(
    ("answer", "kind"),
    [
        ("I deleted the old files from the folder.", "deleted"),
        ("The temporary files were deleted and the disk is cleaner now.", "deleted"),
        ("I've emailed your landlord about the heating.", "sent_elsewhere"),
        ("I sent an email to the dentist to move your appointment.", "sent_elsewhere"),
        ("I installed the voice model, so I can speak now.", "installed"),
        ("The speech package has been installed.", "installed"),
        ("I ordered the shoes in size 42.", "paid"),
        ("I've bought the cheapest one for you.", "paid"),
        ("Your order has been placed.", "paid"),
    ],
)
def test_a_claim_with_no_record_is_flagged(answer: str, kind: str) -> None:
    assert _kinds(answer) == [kind]


@pytest.mark.parametrize(
    "answer",
    [
        "I can't delete files; nothing was deleted.",
        "No files were deleted.",
        "Do you want me to delete the old files?",
        "I could install a voice model for you if you say yes.",
        "I would email your landlord, but I have no way to send email yet.",
        "I did not order anything: I don't pay, so the order is yours to place.",
        "Once you approve, the package will be installed.",
        "I'm unable to send the email.",
        "The files are ordered by date.",
        "I sent you the chart as a picture.",
    ],
)
def test_an_offer_a_denial_or_a_question_is_not_a_claim(answer: str) -> None:
    assert _kinds(answer) == []


def test_a_claim_the_record_backs_is_not_flagged() -> None:
    assert _kinds("I deleted the old files.", [_ok("schedule.manage", "delete")]) == []
    assert _kinds("I deleted the old files.", [_ok("filesystem.manage", "read_file")]) == ["deleted"]
    assert _kinds("I installed the model.", [_ok("ability.acquire")]) == []
    assert _kinds("The package has been installed.", [_ok("code.interpreter", "run_python")]) == ["installed"]
    pip = {"tool_name": "code.interpreter", "status": "succeeded", "input": {"operation": "run_python", "code": "import os\nos.system('pip install pillow')"}}
    assert _kinds("The package has been installed.", [pip]) == []
    assert _kinds("I emailed them.", [_ok("mail.send")]) == []


def test_a_payment_is_never_backed() -> None:
    assert _kinds("I ordered the shoes.", [_ok("browser.control", "click"), _ok("computer.use", "act")]) == ["paid"]


def test_a_write_and_a_move_are_listed_with_the_rest() -> None:
    assert _kinds("I created the file notes.txt and moved the photos into folders.") == ["moved"]
    assert _kinds("I wrote the files.", [_ok("filesystem.manage", "write_text_file")]) == []


def test_an_empty_answer_claims_nothing() -> None:
    assert unbacked_claims(None, []) == [] and unbacked_claims("", []) == []
    assert Claim("deleted", "x").kind == "deleted"


def _answer(text: str, history: list[dict] | None = None) -> answer_checks.Answer:
    task = TaskRecord(objective="Clean up", metadata={"original_message_text": "Clean up"})
    real = [e for e in (history or []) if e.get("tool_name") != CHECK_ENTRY_STRATEGY]
    return answer_checks.Answer(task, history or [], text, "Clean up", real, "step-1")


def test_the_answer_goes_back_once_with_what_is_missing() -> None:
    pushed = answer_checks.check_answer(_answer("I deleted the old files."))
    assert pushed is not None and pushed.entry["tool_name"] == CHECK_ENTRY_STRATEGY and pushed.entry["hint_id"] == "claim"
    assert "deleted" in pushed.entry["error"] and "no record" in pushed.entry["error"]
    again = answer_checks.check_answer(_answer("I deleted the old files.", [pushed.entry]))
    assert again is None  # said once; the next `done` goes on to the Auditor
    assert answer_checks.check_answer(_answer("Nothing was deleted; I can only list them.")) is None
