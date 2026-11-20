"""After a failure: find out why, then try a different kind of approach before giving up (docs/DESIGN.md, "After a failure").

On 11/19 a plan edit failed twice with the same wrong call, and the task went on the way a fixed ladder goes: search the disk with other spellings, three sub-tasks
that ran out of steps, "write a Python program", and then a question to the owner whether to hand it to Claude Code, for a text edit. Nothing asked why it was failing:
the person meant the plan in the last reply, and every attempt assumed a file.

What the research on agents that recover says (Reflexion, AgentDebug, ADaPT, LATS, the stuck detectors of OpenHands and others, listed in docs/PLAN.md): notice
in plain code that it is stuck; say in words, once, what was assumed and which assumption was the first wrong one; take a different *kind* of approach, not the same
one reworded, because a loop persists when only the wording changes; and start the next attempt from the request and a short list of what failed, not from the failed
transcript. This module does the noticing, the choosing and the wording of the note. The one model call (the diagnosis) is made by the worker.

Nothing here runs a tool.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field

from agent_control.orchestration import ladder
from agent_control.orchestration.check_entries import CHECK_ENTRY_APPROACH, CHECK_ENTRY_NAMES

#: How many times the kind of approach may be changed before the task ends with what was tried (the blocker report).
MAX_CHANGES = 3

Cause = Literal["misread_the_request", "missing_context", "wrong_tool", "wrong_arguments", "does_not_exist", "refused", "too_big"]


class Diagnosis(BaseModel):
    """What the failed attempts took for granted, and which assumption was the first wrong one: the answer of one short model call."""

    asked: str = Field(max_length=300, description="What the person asked for, in one sentence, from their words and what was said just before.")
    assumed: str = Field(max_length=300, description="What the attempts so far took for granted about the request.")
    mistake: str = Field(max_length=300, description="The earliest assumption that was wrong, and what is true instead.")
    cause: Cause


DIAGNOSE_SYSTEM = """You help an assistant that keeps failing at a request. You are shown what the person asked, what was said just before, and what the assistant tried.
Do not solve the request. Say what went wrong, as plainly as you can:
- asked: what the person wants, in one sentence.
- assumed: what the attempts took for granted.
- mistake: the earliest assumption that was wrong, and what is true instead. Failures that come one after another usually share one early mistake: find that one.
- cause, one of:
  misread_the_request = the attempts are about the wrong thing (a file, when the person meant something said in the conversation, a plan, a job);
  missing_context = something the attempts needed was never looked at (what was said before, the open plan, a remembered fact);
  wrong_tool = the tool cannot do this, or something else owns it;
  wrong_arguments = the right tool was called with arguments it refused;
  does_not_exist = what the attempts look for is not there;
  refused = the environment said no (access, a block, a limit);
  too_big = one step is more than a single call can do."""


def diagnose_prompt(words: str, last_exchange: str, tried: list[str]) -> str:
    parts = [f"The person asked: {words.strip()[:1200]}"]
    if last_exchange.strip():
        parts.append(f"What was said just before:\n{last_exchange.strip()[:2200]}")
    parts.append("What was tried, in order (each failed or found nothing):\n" + ("\n".join(tried[-10:]) if tried else "(nothing yet)"))
    return "\n\n".join(parts)


# --- noticing ---------------------------------------------------------------------------------------------------------------------------

def work_steps(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The steps that did something: not the notes a check left in the history."""
    return [e for e in history if isinstance(e, dict) and e.get("tool_name") not in CHECK_ENTRY_NAMES]


def _bad(step: dict[str, Any]) -> bool:
    return str(step.get("status") or "") in {"failed", "denied"}


def _shape(step: dict[str, Any]) -> str:
    """An error with its numbers and ids taken out, so "step 3 not found" and "step 5 not found" are the same error."""
    return "".join("#" if ch.isdigit() else ch for ch in str(step.get("error") or "")[:80].lower())


def stuck(history: list[dict[str, Any]]) -> str | None:
    """Why the task is going nowhere, from its own steps, or None. Plain code: the model is asked only after this says so."""
    steps = work_steps(history)
    if len(steps) < 2:
        return None
    last, before = steps[-1], steps[-2]
    if _bad(last) and _bad(before) and last.get("tool_name") == before.get("tool_name") and _shape(last) == _shape(before):
        return "the same call failed with the same error twice"
    if ladder.found_nothing(last) and ladder.found_nothing(before):
        return "two lookups in a row found nothing"
    if len(steps) >= 3 and all(_bad(step) for step in steps[-3:]):
        return "three steps in a row failed"
    if last.get("tool_name") == "delegate" and _bad(last) and "budget" in str(last.get("error") or "").lower():
        return "a sub-task ran out of steps"
    return None


def tried_lines(history: list[dict[str, Any]]) -> list[str]:
    """One short line for each step, for the diagnosis and for the fresh attempt: what was called and how it ended."""
    lines = []
    for step in work_steps(history):
        tool_input = step.get("input")
        operation = str(tool_input.get("operation") or "") if isinstance(tool_input, dict) else ""
        called = f"{step.get('tool_name')}{(' ' + operation) if operation else ''}"
        if _bad(step):
            outcome = "failed: " + " ".join(str(step.get("error") or "").split())[:110]
        elif ladder.found_nothing(step):
            outcome = "found nothing"
        else:
            outcome = "worked"
        lines.append(f"- {called}: {outcome}")
    return lines


# --- choosing ---------------------------------------------------------------------------------------------------------------------------

#: The kinds of approach, in the order that suits each cause. A kind is tried once; one that does not fit this task is left out by `available`.
KINDS = {
    "answer_from_context": "Answer from what is already here (the conversation, the plan, memory)",
    "owning_home": "Use the part that owns this (the planner for a plan, the job's own controls)",
    "fix_the_call": "Fix the call from its error",
    "break_down": "Do one small part first",
    "other_tool": "Use a different tool, ability or skill",
    "write_code": "Write and run a short program",
    "look_up": "Look up how it is done",
    "step_back": "Say what result would satisfy the person, then do the smallest thing that gets it",
}
ORDER = {
    "misread_the_request": ("answer_from_context", "step_back", "owning_home"),
    "missing_context": ("answer_from_context", "owning_home", "step_back", "look_up"),
    "wrong_tool": ("owning_home", "other_tool", "look_up", "write_code", "step_back"),
    "wrong_arguments": ("fix_the_call", "owning_home", "other_tool", "break_down", "step_back"),
    "does_not_exist": ("step_back", "other_tool", "write_code", "look_up"),
    "refused": ("other_tool", "step_back", "look_up"),
    "too_big": ("break_down", "owning_home", "step_back"),
}


#: Kinds that come from the tool's own error, not from the model's guess at a cause: `available` has them only when the error says what to change. Live 11/19 (hands_search_page): the
#: 9B's diagnosis was nonsense (it blamed the url) and sent the next attempts to "answer from the conversation" while the error said which field names did not match.
EVIDENCE_FIRST = ("fix_the_call",)


def choose(cause: str, tried: list[str], available: set[str]) -> str | None:
    """The kind the evidence names, else the first that suits the cause, has not been tried and fits this task; None when there is none left."""
    for kind in EVIDENCE_FIRST:
        if kind in available and kind not in tried:
            return kind
    for kind in ORDER.get(cause, ORDER["missing_context"]):
        if kind in available and kind not in tried:
            return kind
    return None


@dataclass(frozen=True)
class Facts:
    """What the worker knows that decides the wording: the plan or job that owns the request, the last error, the tools that have not been tried."""

    home: str = ""
    last_error: str = ""
    untried_tools: tuple[str, ...] = ()
    last_exchange: str = ""


def approach_text(kind: str, diagnosis: Diagnosis | None, facts: Facts, tried: list[str], *, may_ask: bool = True) -> str:
    """The note the next attempt starts from: what went wrong, the kind of approach to take, and what already failed."""
    lines = []
    if diagnosis is not None:
        lines.append(f"What went wrong: {diagnosis.mistake.strip()} (the person asked: {diagnosis.asked.strip()})")
    else:
        lines.append("Several steps in a row did not work, and repeating them will not change that.")
    instruction = {
        "answer_from_context": "Do not search the disk or the web for this. What the person means is in this conversation, in what was said just before; use it, and do the work on that.",
        "owning_home": f"Use the part that owns this: {facts.home or 'the tool that was made for it'}. Give it the person's own words.",
        "fix_the_call": f"The tool told you what is wrong: {facts.last_error[:300] or 'read its error'}. Change the arguments it names, once, and keep the same tool.",
        "break_down": "Do only the first small part of this in one call, check what it gave, then the next. Do not try the whole thing at once.",
        "other_tool": "Use a different tool from the ones tried" + (f": {', '.join(facts.untried_tools[:6])}" if facts.untried_tools else "") + ". Look at what each one is for before you choose.",
        "write_code": "Write a short Python program that does it and run it with code.interpreter (operation run_python, the program in `code`). Print what it did, so the answer comes from what it printed.",
        "look_up": "Look up how this is done (web.search), then do it that way.",
        "step_back": "Say in one sentence what result would satisfy the person, then do the smallest thing that gets it. Do not repeat anything below.",
    }[kind]
    lines.append(f"Take a different approach: {KINDS[kind]}. {instruction}")
    if tried:
        lines.append("Already tried, and it did not work (do not repeat it):\n" + "\n".join(tried[-8:]))
    if may_ask:
        lines.append("If a different approach is truly not possible, say plainly what you tried, why it failed and what would unblock it.")
    else:
        lines.append(
            "Do not ask the person about this. If a different approach is truly not possible, say plainly what you tried, why it failed and what would unblock it (action done)."
        )
    return "\n".join(lines)


def compact_before_approach(history: list[dict[str, Any]]) -> tuple[list[str], list[dict[str, Any]]]:
    """For the prompt: what came before the latest change of approach as short lines, and the entries from that change on in full.

    A fresh attempt starts from the request and a list of what failed, not from the failed transcript: a model shown its own failing calls repeats them."""
    boundary = max((i for i, e in enumerate(history) if isinstance(e, dict) and e.get("tool_name") == CHECK_ENTRY_APPROACH), default=-1)
    if boundary < 0:
        return [], history
    return tried_lines(history[:boundary]), history[boundary:]
