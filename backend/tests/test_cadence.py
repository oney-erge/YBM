"""Days of the week and a time of day as a schedule: "every Monday at 9am" is a Monday at 09:00, not "daily" (cadence.py, scheduler.py)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agent_control import cadence
from agent_control.scheduler import cadence_from_text, next_run_after, normalize_cadence, objective_from_schedule_text, validate_schedule


@pytest.mark.parametrize(
    ("said", "stored"),
    [
        ("Every Monday at 9am, send me the top 3 Hacker News stories.", "every monday at 09:00"),
        ("every monday and thursday at 6:30 pm", "every monday and thursday at 18:30"),
        ("every Mon, Wed & Friday at 07:00", None),  # abbreviations are not days; only the full names
        ("remind me every weekday at 8:30 to stand up", "every weekday at 08:30"),
        ("every weekend at noon", "every weekend at 12:00"),
        ("Mondays at 9", "every monday at 09:00"),
        ("every Tuesday", "every tuesday"),
        ("send the report daily at 7am", "daily at 07:00"),
        ("every day at 18:00 check the disk", "daily at 18:00"),
        ("every morning at 6 water the plants", "daily at 06:00"),
        ("every friday at midnight", "every friday at 00:00"),
    ],
)
def test_the_words_of_a_request_become_the_stored_cadence(said: str, stored: str | None) -> None:
    assert cadence.from_text(said) == stored


@pytest.mark.parametrize(
    "said",
    ["on Monday at 9am call the bank", "meet me at 9 files", "check the folder every 3 days", "weekly", "what is on Monday?", "I sat down on Sunday"],
)
def test_a_one_off_day_or_a_plain_interval_is_not_a_calendar_schedule(said: str) -> None:
    assert cadence.from_text(said) is None


def test_what_is_stored_is_checked_and_what_is_not_a_cadence_is_refused() -> None:
    for good in ("every monday at 09:00", "every monday and thursday at 18:00", "every weekday at 08:30", "every weekend", "daily at 07:00"):
        validate_schedule(good, "UTC")
    for bad in ("every monday at 9am", "every funday", "every monday at 25:00", "monday", "at 09:00"):
        with pytest.raises(ValueError, match="days and a time"):
            validate_schedule(bad, "UTC")
    validate_schedule("every 3 days", "UTC")  # the old forms still work
    validate_schedule("weekly", "UTC")


def test_a_cadence_a_model_wrote_is_normalized_and_anything_else_is_left_alone() -> None:
    assert normalize_cadence("Every Monday at 9am") == "every monday at 09:00"
    assert normalize_cadence("weekly") == "weekly" and normalize_cadence("every 3 days") == "every 3 days"


def test_the_job_is_what_is_left_when_the_when_is_taken_out() -> None:
    assert objective_from_schedule_text("Every Monday at 9am, send me the top 3 Hacker News stories.") == "send me the top 3 Hacker News stories"
    assert objective_from_schedule_text("send me the report every weekday at 8:30") == "send me the report"
    assert objective_from_schedule_text("on Monday at 9am call the bank") == "on Monday at 9am call the bank"  # one-off: untouched


def test_cadence_from_text_prefers_the_calendar_and_keeps_the_old_forms() -> None:
    assert cadence_from_text("Every Monday at 9am, send me the news") == "every monday at 09:00"
    assert cadence_from_text("please run every 3 days") == "every 3 days"
    assert cadence_from_text("check it") == "daily"


def _utc(*parts: int) -> datetime:
    return datetime(*parts, tzinfo=timezone.utc)


def test_the_next_run_is_the_next_matching_day_at_the_time_in_the_schedules_timezone() -> None:
    # 2026-11-14 is a Saturday. Chicago is UTC-6 in November, so 09:00 there is 15:00 UTC.
    saturday = _utc(2026, 11, 14, 12, 0)
    assert next_run_after("every monday at 09:00", saturday, "America/Chicago") == _utc(2026, 11, 16, 15, 0)
    assert next_run_after("every weekday at 08:30", saturday, "America/Chicago") == _utc(2026, 11, 16, 14, 30)
    assert next_run_after("every weekend at 10:00", saturday, "America/Chicago") == _utc(2026, 11, 14, 16, 0)  # later today
    assert next_run_after("every weekend at 05:00", saturday, "America/Chicago") == _utc(2026, 11, 15, 11, 0)  # today has passed
    assert next_run_after("every monday and thursday at 18:00", _utc(2026, 11, 16, 23, 59), "UTC") == _utc(2026, 11, 19, 18, 0)
    assert next_run_after("daily at 07:00", _utc(2026, 11, 14, 6, 59), "UTC") == _utc(2026, 11, 14, 7, 0)
    assert next_run_after("daily at 07:00", _utc(2026, 11, 14, 7, 0), "UTC") == _utc(2026, 11, 15, 7, 0)  # never "now"


def test_a_run_a_week_later_is_the_same_wall_clock_time_across_a_clock_change() -> None:
    # Chicago leaves daylight time on Sunday 2026-11-01: 09:00 is 14:00 UTC the week before it and 15:00 UTC on and after it.
    before = next_run_after("every sunday at 09:00", _utc(2026, 10, 20, 0, 0), "America/Chicago")
    assert before == _utc(2026, 10, 25, 14, 0)
    assert next_run_after("every sunday at 09:00", before, "America/Chicago") == _utc(2026, 11, 1, 15, 0)


def test_a_cadence_with_no_time_keeps_the_time_of_the_run_before() -> None:
    first = next_run_after("every tuesday", _utc(2026, 11, 14, 13, 45), "UTC")
    assert first == _utc(2026, 11, 17, 13, 45)
    assert next_run_after("every tuesday", first, "UTC") == _utc(2026, 11, 24, 13, 45)


def test_the_old_cadences_are_computed_as_before() -> None:
    base = _utc(2026, 11, 14, 12, 0)
    assert next_run_after("daily", base, "UTC") == _utc(2026, 11, 15, 12, 0)
    assert next_run_after("weekly", base, "UTC") == _utc(2026, 11, 21, 12, 0)
    assert next_run_after("every 15 minutes", base, "UTC") == _utc(2026, 11, 14, 12, 15)


def test_clock_reads_the_ways_people_write_a_time() -> None:
    assert [cadence.clock(text) for text in ("9am", "6:30 pm", "12am", "12pm", "18:00", "noon", "midnight", "9", "7:5", "25:00", "13pm")] == [
        "09:00", "18:30", "00:00", "12:00", "18:00", "12:00", "00:00", "09:00", None, None, None,
    ]
