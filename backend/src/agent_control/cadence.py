"""Days of the week and a time of day: "every monday at 09:00", "every weekday at 08:30", "daily at 07:00".

The scheduler could only say "every N minutes/hours/days/weeks", so "every Monday at 9" became "daily", an hour-of-day or a day of the week
had no way in, and a request for exactly that was turned into the wrong schedule. This module is the one place that knows the calendar
grammar: what words mean it (`from_text`), the form that is stored and checked (`STORED`), the next moment it fires (`next_after`) and what
to take out of a request so the rest is the job (`strip`). `scheduler.py` calls it; nothing else parses a day or a time.

A stored cadence is always the same shape, so a person can read it in a list: `every monday and thursday at 18:00`, `every weekday at
08:30`, `every weekend`, `daily at 07:00`. A cadence with no time fires at the time of day of the moment it is computed from, so the second
run keeps the time of the first.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone, tzinfo

DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_DAY = "(?:" + "|".join(DAYS) + ")"
_CLOCK = r"(?:\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)|\d{1,2}:\d{2}|noon|midnight)"

#: The stored form, checked as is by validate_schedule.
STORED = re.compile(
    rf"^(?P<days>every weekday|every weekend|every {_DAY}(?: and {_DAY})*|daily)(?: at (?P<time>[01]\d:[0-5]\d|2[0-3]:[0-5]\d))?$"
)
# What a person writes: every/each, then weekday(s), weekend(s) or one or more day names (plural allowed), joined by commas, "and" or "&".
_SPOKEN_DAYS = re.compile(
    rf"\b(?P<every>(?:every|each)\s+)?(?:(?P<group>weekdays?|weekends?)|(?P<list>{_DAY}s?(?:\s*(?:,|and|&)\s*(?:{_DAY}s?))*))\b", re.IGNORECASE
)
_SPOKEN_DAILY = re.compile(r"\b(?:daily|every\s+day|each\s+day|every\s+(?:morning|evening|night))\b", re.IGNORECASE)
_SPOKEN_TIME = re.compile(rf"\b(?:at\s+)?(?P<clock>{_CLOCK})\b|\bat\s+(?P<hour>\d{{1,2}})\b(?!\s*(?:files?|items?|minutes?|hours?))", re.IGNORECASE)


def clock(text: str) -> str | None:
    """"9am", "6:30 pm", "18:00", "noon", "9" as "HH:MM", or None."""
    lowered = text.lower().replace(".", "").strip()
    if lowered in ("noon", "midnight"):
        return "12:00" if lowered == "noon" else "00:00"
    match = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", lowered)
    if not match:
        return None
    hour, minute, meridian = int(match.group(1)), int(match.group(2) or 0), match.group(3)
    if meridian:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if meridian == "pm" else 0)
    return f"{hour:02d}:{minute:02d}" if hour < 24 and minute < 60 else None


def _spoken_time(text: str) -> str | None:
    match = _SPOKEN_TIME.search(text)
    if match is None:
        return None
    return clock(match.group("clock") or match.group("hour"))


def from_text(text: str) -> str | None:
    """The stored cadence a request's words describe ("every monday at 9am"), or None when they describe no days or time of day."""
    time = _spoken_time(text)
    days = _SPOKEN_DAYS.search(text)
    # "every monday", "weekdays" and "mondays" repeat; "on monday" is one day, not a schedule.
    if days is not None and (days.group("every") or days.group("group") or re.search(rf"{_DAY}s\b", days.group("list") or "", re.IGNORECASE)):
        if days.group("group"):
            spoken = "every weekday" if days.group("group").lower().startswith("weekday") else "every weekend"
        else:
            found = [day for day in DAYS if re.search(rf"\b{day}", days.group("list"), re.IGNORECASE)]
            spoken = "every " + " and ".join(found)
        return spoken + (f" at {time}" if time else "")
    if time and _SPOKEN_DAILY.search(text):
        return f"daily at {time}"
    return None


def is_calendar(cadence: str) -> bool:
    match = STORED.match(cadence.strip().lower())
    return match is not None and (match.group("days") != "daily" or match.group("time") is not None)


def _wanted(days: str) -> set[int]:
    if days == "daily":
        return set(range(7))
    if days == "every weekday":
        return set(range(5))
    if days == "every weekend":
        return {5, 6}
    return {index for index, day in enumerate(DAYS) if re.search(rf"\b{day}\b", days)}


def next_after(cadence: str, local: datetime, tz: tzinfo) -> datetime:
    """The next moment after `local` (a time in `tz`) that is on one of the days at the time, in UTC.

    Built from the local calendar date and wall-clock time rather than by adding hours, so "every monday at 09:00" is 09:00 on both sides of a
    clock change.
    """
    match = STORED.match(cadence.strip().lower())
    if match is None:
        raise ValueError(f"not a calendar cadence: {cadence!r}")
    wanted = _wanted(match.group("days"))
    hour, minute = (int(part) for part in match.group("time").split(":")) if match.group("time") else (local.hour, local.minute)
    after = local.astimezone(timezone.utc)
    for offset in range(0, 9):
        day = local.date() + timedelta(days=offset)
        candidate = datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz)
        if day.weekday() in wanted and candidate.astimezone(timezone.utc) > after:
            return candidate.astimezone(timezone.utc)
    raise ValueError(f"no day in the next week matches {cadence!r}")  # unreachable: STORED always names a day


def strip(text: str) -> str:
    """The request without the words that said when, so what is left is the job: "every monday at 9am, send me the news" -> "send me the news"."""
    if from_text(text) is None:
        return text
    without = _SPOKEN_DAYS.sub(" ", text)
    without = _SPOKEN_TIME.sub(" ", without)
    without = _SPOKEN_DAILY.sub(" ", without)
    return re.sub(r"\s+", " ", without).strip(" ,.:;-")
