"""The morning mission "Open my apply tabs" (path 18): Go by the clock, at 06:30 in the owner's time, once a day, inside
the off-hours window, off when switched off, and quiet when nothing is in Ready to Launch. Go itself (`apply_go.start`,
which runs in the background) is a stand-in here."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app import db
from app.jobsearch.browser import go as apply_go
from app.jobsearch.missions import core as missions
from app.jobsearch.models import Mission, MissionRun, Opportunity
from app.settings import settings
from tests.builders import counted
from tests.support import OWNER, load_profile

DAY = (2026, 10, 7)
#: The clock a run stamps its start with (`missions.start`): the time of the pass in hand.
CLOCK = [datetime(2026, 10, 7, tzinfo=UTC)]


def at(hour: int, minute: int, day: tuple = DAY) -> datetime:
    return datetime(*day, hour, minute, tzinfo=UTC)


@pytest.fixture
def went(monkeypatch):
    """Go's background start, counting its calls; `ready` is whether Ready to Launch holds a role."""
    monkeypatch.setattr(settings, "timezone", "UTC")
    monkeypatch.setattr(missions, "utc_now", lambda: CLOCK[0])
    calls = {"count": 0, "ready": True}

    async def start(owner):
        calls["count"] += 1
        return {}

    monkeypatch.setattr(apply_go, "start", start)
    return calls


async def _made(state: str = "on", ready: bool = True) -> None:
    """The missions, with the morning one last counted the day before at noon, so its next 06:30 is the one on `DAY`."""
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        await missions.ensure(session, OWNER)
        if ready and await session.scalar(select(Opportunity.id).where(Opportunity.owner == OWNER)) is None:
            session.add(Opportunity(owner=OWNER, slug="a", company="Firm a", title="Lead", match_key="firm a|lead",
                                    status="ready_to_launch", origin="scout", **counted()))
        mission = await session.scalar(select(Mission).where(Mission.owner == OWNER, Mission.key == missions.GO))
        mission.checked_at, mission.state = at(12, 0, (2026, 10, 6)), state
        await session.commit()


async def _pass(now: datetime) -> list[MissionRun]:
    CLOCK[0] = now
    async with db.session_factory()() as session:
        await missions.schedule_pass(session, OWNER, now=now)
        await session.commit()
        mission = await session.scalar(select(Mission).where(Mission.owner == OWNER, Mission.key == missions.GO))
        return list((await session.scalars(select(MissionRun).where(MissionRun.mission_id == mission.id))).all())


async def test_it_starts_on_at_half_past_six(js):
    async with db.session_factory()() as session:
        await missions.ensure(session, OWNER)
        row = await session.scalar(select(Mission).where(Mission.key == missions.GO))
    assert (row.state, row.schedule, row.name) == ("on", {"kind": "daily", "at": "06:30"}, "Open my apply tabs")


async def test_it_runs_go_at_half_past_six_and_not_a_minute_before(js, went):
    await _made()
    assert await _pass(at(6, 29)) == [] and went["count"] == 0
    runs = await _pass(at(6, 30))  # 06:30 is inside the off-hours window (1 to 7 AM) and still runs
    assert went["count"] == 1
    assert [(r.outcome, r.started_by) for r in runs] == [("done", "schedule")]
    assert runs[0].summary.startswith("Started Launch in Browser: it builds the packets and opens each tab")


async def test_it_runs_once_a_day_and_again_the_next(js, went):
    await _made()
    await _pass(at(6, 30))
    await _pass(at(6, 35))
    await _pass(at(14, 0))
    assert went["count"] == 1
    await _pass(at(6, 30, (2026, 10, 8)))
    assert went["count"] == 2


async def test_a_late_start_within_the_grace_runs_and_one_after_it_waits_for_tomorrow(js, went):
    await _made()
    await _pass(at(7, 40))
    assert went["count"] == 1
    await _made()
    await _pass(at(9, 0))
    assert went["count"] == 1, "the morning was over"
    await _pass(at(6, 30, (2026, 10, 8)))
    assert went["count"] == 2


async def test_switched_off_it_does_not_run(js, went):
    await _made(state="paused")
    assert await _pass(at(6, 30)) == [] and went["count"] == 0


async def test_an_empty_ready_to_launch_does_nothing_quietly(js, went):
    await _made(ready=False)
    assert await _pass(at(6, 30)) == []
    assert went["count"] == 0, "nothing in Ready to Launch: Go is not started"
    assert await _pass(at(6, 31)) == [] and went["count"] == 0, "and it is not tried again that day"


def test_peak_hours_never_hold_it():
    assert not missions.kind_of(missions.GO).needs_claude


def test_a_mission_made_in_the_afternoon_first_runs_the_next_morning():
    seed = missions.kind_of(missions.GO).first(None)
    now = at(14, 0)
    assert missions.next_time(seed.schedule, now, now) == now + timedelta(hours=16, minutes=30)
