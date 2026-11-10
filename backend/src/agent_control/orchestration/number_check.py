"""A number the model calculated in its head is checked against a tool result before it is given to the user.

A local model reads cells correctly and then adds them wrongly: asked for the total of a spreadsheet it answered 48076.25
where the cells add up to 611. Words cannot be checked cheaply, but a number can: if the answer to a request that asks
for a calculation holds a number no tool result produced, the model is sent back once to calculate it with code.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from decimal import Decimal, InvalidOperation
from typing import Any

# A request that asks the assistant to work something out, as opposed to reading a value that is already written down.
_CALCULATION = re.compile(
    r"\b(?:total|sum|add(?:ed)?\s+up|adding|average|mean|median|multiply|multiplied|times|product|percent(?:age)?|"
    r"compute|calculate|calculation|how\s+many\s+days|difference|compound(?:ed)?|interest|convert|minus|divided|"
    r"fibonacci|square[sd]?)\b|%",
    re.IGNORECASE,
)
_NUMBER = re.compile(r"(?<![\w.])-?\d[\d,]*(?:\.\d+)?(?![\w])")
_MIN_DIGITS = 3  # "2 files" and "step 3" are counted, not calculated; 441 and 49.75 are results


def _normalise(token: str) -> str | None:
    cleaned = token.replace(",", "")
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        return None
    text = format(value.normalize(), "f")
    return text[:-2] if text.endswith(".0") else text


def _numbers(text: str) -> set[str]:
    return {n for n in (_normalise(t) for t in _NUMBER.findall(text or "")) if n is not None}


def asks_for_a_calculation(request_text: str) -> bool:
    return bool(_CALCULATION.search(request_text or ""))


def _is_result_sized(number: str) -> bool:
    digits = number.lstrip("-").replace(".", "")
    return len(digits) >= _MIN_DIGITS or "." in number


def ungrounded_numbers(request_text: str, answer: str, history: Iterable[dict[str, Any]]) -> list[str]:
    """Numbers in ``answer`` that neither the request nor any tool result contains.

    Empty unless the request asks for a calculation. Every number the user wrote, and every number in any tool output or
    error, counts as grounded; so does a number a script printed. Only result-sized numbers (three digits or a decimal
    point) are checked, so a count such as "3 files" never triggers it.
    """
    if not asks_for_a_calculation(request_text):
        return []
    grounded = _numbers(request_text)
    for entry in history:
        if not isinstance(entry, dict) or str(entry.get("tool_name") or "").startswith("_"):
            continue
        grounded |= _numbers(str(entry.get("output_summary") or ""))
        grounded |= _numbers(str(entry.get("error") or ""))
        stdout = entry.get("output") if isinstance(entry.get("output"), dict) else {}
        grounded |= _numbers(str(stdout.get("text") or "") + " " + str(stdout.get("stdout") or ""))
    found = []
    for token in _NUMBER.findall(answer or ""):
        number = _normalise(token)
        if number is not None and number not in grounded and _is_result_sized(number) and number not in found:
            found.append(number)
    return found
