"""Staying up: a backup that can be restored, one line each morning, and a check that the decision model's marks still separate (upkeep.py, calibrate.py)."""

from __future__ import annotations

import json
import os
import sqlite3
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent_control import upkeep
from agent_control.calibrate import calibrate, render
from agent_control.config import AppSettings
from agent_control.jobs.model import Job, JobStatus
from helpers import make_repos


def _backup(folder: Path, *, age: timedelta = timedelta(0), facts: int = 2) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    database = folder / "src.db"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE memory_facts (id TEXT)")
    connection.execute("CREATE TABLE tasks (id TEXT)")
    connection.executemany("INSERT INTO memory_facts VALUES (?)", [(str(i),) for i in range(facts)])
    connection.commit()
    connection.close()
    archive = folder / "ybm-backup-20261119T000000Z.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.write(database, arcname="agent_control.db")
    stamp = time.time() - age.total_seconds()
    os.utime(archive, (stamp, stamp))
    database.unlink()
    return archive


def test_a_backup_is_opened_the_way_a_restore_would_and_what_it_holds_is_counted(tmp_path) -> None:
    archive = _backup(tmp_path / "backups", facts=3)
    result = upkeep.restore_check(archive, tmp_path / "scratch")
    assert result["ok"] and result["counts"] == {"tasks": 0, "memory_facts": 3}
    assert not (tmp_path / "scratch").exists()  # nothing is left behind


def test_a_backup_that_cannot_be_read_is_reported_and_not_called_fine(tmp_path) -> None:
    broken = tmp_path / "ybm-backup-broken.zip"
    broken.write_bytes(b"not a zip at all")
    result = upkeep.restore_check(broken, tmp_path / "scratch")
    assert not result["ok"] and result["problem"]
    empty = tmp_path / "ybm-backup-empty.zip"
    with zipfile.ZipFile(empty, "w") as bundle:
        bundle.writestr("config.yaml", "a: 1\n")
    assert upkeep.restore_check(empty, tmp_path / "scratch")["problem"] == "the archive holds no database"


def test_the_morning_line_is_one_sentence_when_all_is_good_and_names_each_thing_that_is_not(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    repos, _audit = make_repos(tmp_path / "db")
    settings = AppSettings(_env_file=None)
    _backup(tmp_path / ".agent_control" / "backups")
    repos.jobs.create(Job(kind="research", goal="g", state={}, routing={}))

    line, problems = upkeep.morning_line(settings, repos, state={}, model_check=lambda url: True, disk_path=str(tmp_path))
    assert problems == [] and line.startswith("Morning check: all good.") and "model answers" in line and "1 job(s) running" in line and "backed up" in line

    failed = repos.jobs.create(Job(kind="research", goal="h", state={}, routing={}))
    repos.jobs.save(failed.id, state={}, status=JobStatus.FAILED)
    bad, problems = upkeep.morning_line(settings, repos, state={"restore_check": {"ok": False, "problem": "it is damaged"}}, model_check=lambda url: False, disk_path=str(tmp_path))
    assert len(problems) == 3 and "not answering" in bad and "stopped on errors" in bad and "restore check failed: it is damaged" in bad and not bad.startswith("Morning check: all good")

    _backup(tmp_path / ".agent_control" / "backups", age=timedelta(days=5))
    old, problems = upkeep.morning_line(settings, repos, state={}, model_check=lambda url: True, disk_path=str(tmp_path))
    assert any("backup" in p for p in problems) or "5 day(s) old" in old


@pytest.mark.asyncio
async def test_the_daily_chores_run_once_a_day_after_the_hour_and_the_restore_check_once_a_month(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    repos, _audit = make_repos(tmp_path / "db")
    settings = AppSettings(_env_file=None)
    _backup(tmp_path / ".agent_control" / "backups")
    said: list[str] = []

    async def say(text: str) -> None:
        said.append(text)

    def local(hour: int, day: int) -> datetime:
        return datetime(2026, 11, day, hour, 5, tzinfo=timezone.utc)

    state = tmp_path / "state.json"
    run = lambda now: upkeep.run_daily(settings, repos, say, now=now, state_path=state, backup=lambda: None, model_check=lambda url: True)  # noqa: E731

    assert await run(local(7, 19)) is None and said == []  # before the hour
    first = await run(local(8, 19))
    assert first and first.startswith("Morning check") and len(said) == 1
    assert json.loads(state.read_text(encoding="utf-8"))["restore_check"]["ok"] is True
    assert await run(local(9, 19)) is None and len(said) == 1  # once a day
    stamp = json.loads(state.read_text(encoding="utf-8"))["restore_checked_on"]
    await run(local(8, 20))
    assert json.loads(state.read_text(encoding="utf-8"))["restore_checked_on"] == stamp and len(said) == 2  # the next day: the line, not another restore check
    await run(local(8, 25) + timedelta(days=30))
    assert json.loads(state.read_text(encoding="utf-8"))["restore_checked_on"] != stamp  # a month on, it is checked again


@pytest.mark.asyncio
async def test_calibration_says_when_a_mark_no_longer_separates_and_where_to_move_it(tmp_path) -> None:
    skills = tmp_path / "skills"
    skills.mkdir()
    (skills / "a.md").write_text("---\nname: A\ndescription: d\nwhen: Is it about apples?\nwhen_at: 0.6\n---\nbody\n", encoding="utf-8")
    (skills / "b.md").write_text("---\nname: B\ndescription: d\nwhen: Is it about pears?\nwhen_at: 0.6\n---\nbody\n", encoding="utf-8")
    corpus = tmp_path / "corpus.json"
    corpus.write_text(json.dumps({"a.md": {"yes": ["an apple", "two apples"], "no": ["a pear"]}, "b.md": {"yes": ["a pear"], "no": ["an apple"]}, "missing.md": {"yes": ["x"], "no": ["y"]}}), encoding="utf-8")

    async def ask(request: str, questions: dict[str, str]) -> dict[str, float]:
        question = questions["q"]
        if "apples" in question:
            return {"q": 0.9 if "apple" in request else 0.1}
        return {"q": 0.5 if "pear" in request else 0.2}  # a model that is less sure about pears: its yes is under the mark

    results = await calibrate(skills, corpus, ask)
    by_name = {r.skill: r for r in results}
    assert by_name["a.md"].ok and by_name["a.md"].lowest_yes == 0.9 and by_name["a.md"].highest_no == 0.1
    assert not by_name["b.md"].ok and by_name["b.md"].suggested == 0.35 and "move the mark to about 0.35" in by_name["b.md"].detail
    assert not by_name["missing.md"].ok
    text = render(results)
    assert "2 of 3 skill(s) need a look" in text or "1 of 3" in text


def test_a_schedule_the_person_deleted_a_month_ago_is_removed_by_the_daily_upkeep(tmp_path) -> None:
    """docs/PLAN.md C2: the live database held 59 deleted schedules, every one of them a test's."""
    from agent_control.schemas import ScheduleRecord, ScheduleStatus

    repos, _audit = make_repos(tmp_path)
    old = repos.schedules.create(ScheduleRecord(objective="old", cadence="daily", next_run_at=datetime.now(timezone.utc)))
    new = repos.schedules.create(ScheduleRecord(objective="new", cadence="daily", next_run_at=datetime.now(timezone.utc)))
    keep = repos.schedules.create(ScheduleRecord(objective="live", cadence="daily", next_run_at=datetime.now(timezone.utc)))
    repos.schedules.update_status(old.id, ScheduleStatus.DELETED)
    repos.schedules.update_status(new.id, ScheduleStatus.DELETED)
    with repos.schedules.database.connect() as connection:
        connection.execute("UPDATE schedules SET updated_at = ? WHERE id = ?", ((datetime.now(timezone.utc) - timedelta(days=45)).isoformat(), old.id))
    removed = repos.schedules.purge_deleted((datetime.now(timezone.utc) - timedelta(days=30)).isoformat())
    assert removed == 1
    assert repos.schedules.get(old.id) is None and repos.schedules.get(new.id) is not None and repos.schedules.get(keep.id) is not None
