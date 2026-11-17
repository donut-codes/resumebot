"""The schedules read the clock as instants in the owner's zone: a scheduled night found due
after its time is skipped, the repeated hour of fall-back night is read once, windows are counted in elapsed time,
recipe health days are the owner's, a zone-less time is UTC, a refusal's stop counts from the refusal, the night's
quotas count from the night's start, and a `Retry-After` date is read. Everything here is made up: the zones are far
from UTC, and the fall-back date is the real one of the zone."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app import db
from app.inbox import service as inbox
from app.jobsearch.browser.recipes import live as recipe_live
from app.jobsearch.fetch import polite
from app.jobsearch.missions import core as missions
from app.jobsearch.models import Mission, MissionChange, MissionRun
from app.jobsearch.pool import budget
from app.jobsearch.pool import core as pool
from app.jobsearch.profile import store as profile_store
from app.jobsearch.profile.schema import Settings
from app.jobsearch.work import hours as night
from app.models.work import Task
from app.settings import settings
from app.sites.models import SiteRecipe
from tests.support import DEFAULT_OWNER as OWNER
from tests.support import load_profile

#: Fall-back night in Auckland, 2026-04-05: at 03:00 NZDT (+13) the clocks go back to 02:00 NZST (+12), so 02:00 to
#: 02:59 happens twice: the first pass is 13:00Z to 13:59Z on the 4th, the second 14:00Z to 14:59Z.
FALL = "Pacific/Auckland"


def utc(day: int, hour: int, minute: int = 0, month: int = 4) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=UTC)


@pytest.fixture
def fall_zone(monkeypatch):
    monkeypatch.setattr(settings, "timezone", FALL)


# --- a late scheduled night is skipped ------------------------------------------------------------------


async def _scout(session):
    await load_profile(session, OWNER)
    await missions.ensure(session, OWNER)
    return await session.scalar(select(Mission).where(Mission.owner == OWNER, Mission.key == missions.SCOUT))


async def _windows(session, start: str, end: str) -> None:
    current = (await profile_store.current(session, OWNER)).settings
    await profile_store.save(session, OWNER, "settings",
                             {**current.model_dump(mode="json"), "off_hours_start": start, "off_hours_end": end},
                             by="test")


async def test_a_scout_night_found_due_at_five_past_eight_is_skipped_and_one_inside_its_hours_is_not(js, monkeypatch):
    monkeypatch.setattr(settings, "timezone", "UTC")
    async with db.session_factory()() as session:
        scout = await _scout(session)
        kind = missions.kind_of(missions.SCOUT)
        due = utc(7, 1, month=10)
        assert await kind.hold(session, OWNER, scout, utc(7, 8, 5, month=10), due) == "skip", "past 06:45 and 7 AM"
        assert await kind.hold(session, OWNER, scout, utc(7, 6, 50, month=10), due) == "skip", "past its finish time"
        assert await kind.hold(session, OWNER, scout, utc(7, 5, 0, month=10), due) is None, "inside both"


async def test_the_pass_that_finds_the_night_late_gives_the_day_up_and_records_no_failed_run(js, monkeypatch):
    monkeypatch.setattr(settings, "timezone", "UTC")
    now = utc(7, 8, 5, month=10)
    async with db.session_factory()() as session:
        scout = await _scout(session)
        scout.state, scout.checked_at = "on", utc(6, 12, month=10)
        await session.commit()
        await missions.schedule_pass(session, OWNER, now=now)
        await session.commit()
        runs = (await session.scalars(select(MissionRun).where(MissionRun.mission_id == scout.id))).all()
        scout = await session.get(Mission, scout.id)
    assert list(runs) == [], "no run was opened for nobody to work, so none ends failed and Run now is not held"
    assert scout.checked_at == now, "the day is over: the next night is the next 01:00"


async def test_a_window_that_opens_before_the_finish_time_is_waited_for_and_one_that_does_not_gives_the_night_up(
        js, monkeypatch):
    monkeypatch.setattr(settings, "timezone", "UTC")
    async with db.session_factory()() as session:
        scout = await _scout(session)
        kind = missions.kind_of(missions.SCOUT)
        await _windows(session, "03:00", "07:00")
        assert await kind.hold(session, OWNER, scout, utc(7, 1, 5, month=10), utc(7, 1, month=10)) == "wait"
        await _windows(session, "00:00", "01:30")
        assert await kind.hold(session, OWNER, scout, utc(7, 3, 0, month=10), utc(7, 1, month=10)) == "skip"


# --- the repeated hour ---------------------------------------------------------------------------------


def test_a_finish_time_inside_the_repeated_hour_is_the_next_pass_through_it_never_a_past_instant(fall_zone):
    assert night.finish_moment(utc(4, 13, 10), "02:30") == utc(4, 13, 30), "02:10 NZDT: the first 02:30 is ahead"
    assert night.finish_moment(utc(4, 14, 10), "02:30") == utc(4, 14, 30), "02:10 NZST: only the second is ahead"
    assert night.finish_moment(utc(4, 14, 40), "02:30") == utc(5, 14, 30), "past both: the next day's 02:30 NZST"


def test_a_daily_time_inside_the_repeated_hour_runs_once_that_day(fall_zone):
    schedule = {"kind": "daily", "at": "02:30"}
    assert missions.next_time(schedule, utc(3, 13, 30), utc(3, 20)) == utc(4, 13, 30)
    assert missions.next_time(schedule, utc(4, 13, 30), utc(4, 20)) == utc(5, 14, 30), "not again an hour later"
    assert missions.next_time(schedule, utc(4, 14, 10), utc(4, 20)) == utc(5, 14, 30), "a start in the second pass"


def test_a_window_that_began_in_the_repeated_hour_began_at_its_first_pass(fall_zone):
    s = Settings(off_hours_start="02:30", off_hours_end="07:00")
    assert night.off_hours_began(s, utc(4, 14, 10)) == utc(4, 13, 30)
    assert night.next_off_hours_start(s, utc(4, 14, 10)) == utc(5, 14, 30), "the night after, never an hour on"


# --- a window is counted in elapsed time --------------------------------------------------------------


def test_the_budgets_window_is_as_long_as_the_night_was_on_the_night_the_clocks_change(fall_zone):
    s = Settings(off_hours_start="01:00", off_hours_end="07:00")
    assert budget._window(s, utc(4, 15)) == (utc(4, 12), timedelta(hours=7)), "fall back: 6 wall hours, 7 elapsed"
    assert budget._window(s, utc(20, 15, month=3)) == (utc(20, 12, month=3), timedelta(hours=6))


def test_the_budgets_window_on_spring_forward_night_is_an_hour_shorter(fall_zone):
    s = Settings(off_hours_start="01:00", off_hours_end="07:00")
    # 2026-09-27 02:00 NZST (+12) becomes 03:00 NZDT (+13): 01:00 NZST is 09-26 13:00Z, 07:00 NZDT is 09-26 18:00Z.
    assert budget._window(s, datetime(2026, 9, 26, 16, tzinfo=UTC)) == (
        datetime(2026, 9, 26, 13, tzinfo=UTC), timedelta(hours=5))


# --- recipe health days are the owner's ----------------------------------------------------------------


async def test_a_recipe_reads_day_is_the_owners_day_not_the_utc_date(js, monkeypatch):
    monkeypatch.setattr(settings, "timezone", "Pacific/Kiritimati")  # UTC+14
    async with db.session_factory()() as session:
        row = SiteRecipe(owner=OWNER, site_id=None, host="jobs.example.test", version=1, recipe={}, state="live",
                         created_at=utc(7, 1, month=10))
        session.add(row)
        await session.flush()
        await recipe_live.note_posting_read(session, row.id, True, now=utc(7, 23, 30, month=10))
        assert [h["day"] for h in row.health] == ["2026-10-08"], "23:30Z is already the 8th at UTC+14"


# --- a time with no zone is UTC, and the owner reads their own zone -----------------------------


async def test_the_inbox_history_reads_a_before_with_no_zone_as_utc(js):
    async with db.session_factory()() as session:
        for hours in (1, 3):
            session.add(Task(owner=OWNER, kind="decision", title=f"t{hours}", state="done", assignee="owner",
                             done_at=utc(7, 12, month=10) - timedelta(hours=hours), created_at=utc(1, 0, month=10)))
        await session.commit()
        out = await inbox.history(session, OWNER, datetime(2026, 10, 7, 10, 0), 20)  # no zone: 10:00Z
        assert [i["title"] for i in out["items"]] == ["t3"]
        again = await inbox.history(session, OWNER, utc(7, 10, month=10), 20)
        assert [i["title"] for i in again["items"]] == ["t3"]


async def test_a_pause_until_a_time_with_no_zone_is_utc_and_says_it_in_the_owners_zone(js, monkeypatch):
    monkeypatch.setattr(settings, "timezone", "Pacific/Pago_Pago")  # UTC-11
    until = datetime(2999, 1, 2, 3, 0)
    async with db.session_factory()() as session:
        scout = await _scout(session)
        await missions.pause(session, scout, by="owner", until=until)
        assert scout.paused_until == until.replace(tzinfo=UTC)
        notes = (await session.scalars(select(MissionChange).where(MissionChange.mission_id == scout.id))).all()
    assert notes[-1].what == "Paused until Jan 1, 4:00 PM.", notes[-1].what


# --- a refusal's stop counts from the refusal ---------------------------------------------------------




# --- the night's quotas, and the page counts' clock ---------------------------------------------


async def test_the_nights_quota_counts_from_the_start_of_the_off_hours_window(js, monkeypatch):
    monkeypatch.setattr(settings, "timezone", "UTC")
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        monkeypatch.setattr(pool, "utc_now", lambda: utc(8, 1, 30, month=10))
        assert await pool._night_began(session, OWNER) == utc(8, 1, month=10), "not 24 hours back (01:30 the 7th)"
        monkeypatch.setattr(pool, "utc_now", lambda: utc(8, 15, month=10))
        assert await pool._night_began(session, OWNER) == utc(8, 1, month=10), "through the day it is this night's"


async def test_the_page_counts_are_kept_by_the_monotonic_clock_not_the_wall_clock(monkeypatch):
    monkeypatch.setattr(pool, "COUNTS_FOR", timedelta(seconds=30))
    pool._counts.clear()
    made = []
    ticks = [500.0]

    async def counts(session, owner):
        made.append(1)
        return {"x": len(made)}

    monkeypatch.setattr(pool, "waiting_counts", counts)
    monkeypatch.setattr(pool.time, "monotonic", lambda: ticks[0])
    monkeypatch.setattr(pool, "utc_now", lambda: utc(1, 0) - timedelta(days=ticks[0]))  # the wall clock jumps back
    assert await pool._page_counts(None, OWNER) == {"x": 1}
    ticks[0] += 10
    assert await pool._page_counts(None, OWNER) == {"x": 1}, "10 seconds on: kept, whatever the wall clock did"
    ticks[0] += 25
    assert await pool._page_counts(None, OWNER) == {"x": 2}
    pool._counts.clear()


# --- a Retry-After date ---------------------------------------------------------------------------------


def test_retry_after_reads_seconds_an_http_date_and_what_it_cannot_read():
    now = datetime(2026, 10, 7, 12, 0, 0, tzinfo=UTC)
    assert polite.retry_after("30", 5.0, now) == 30.0
    assert polite.retry_after("Wed, 07 Oct 2026 12:02:00 GMT", 5.0, now) == 120.0
    assert polite.retry_after("Wed, 07 Oct 2026 11:00:00 GMT", 5.0, now) == 0.0, "a date already past: no wait"
    assert polite.retry_after("-4", 5.0, now) == 0.0
    assert polite.retry_after(None, 5.0, now) == 5.0
    assert polite.retry_after("soon", 5.0, now) == 5.0
    assert polite.retry_after("nan", 5.0, now) == 5.0




async def test_an_every_n_hours_scout_is_not_skipped_for_the_hour_but_a_passed_finish_time_still_skips_it(
        js, monkeypatch):
    monkeypatch.setattr(settings, "timezone", "UTC")
    async with db.session_factory()() as session:
        scout = await _scout(session)
        scout.schedule = {"kind": "every", "hours": 1}
        kind = missions.kind_of(missions.SCOUT)
        assert await kind.hold(session, OWNER, scout, utc(7, 14, 0, month=10), utc(7, 13, 0, month=10)) is None
        assert await kind.hold(session, OWNER, scout, utc(8, 7, 0, month=10), utc(8, 5, 0, month=10)) == "skip"
