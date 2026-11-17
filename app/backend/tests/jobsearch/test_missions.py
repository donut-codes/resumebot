"""Missions (path 18): the missions every install starts with, schedules and when each comes up, the schedule pass
(pausing stops it, a pause runs out), Run now on the Go mission, runs read again as their work ends, and every
change kept with who and why. Everything is made up."""

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select

from app import db
from app.agents import roster
from app.jobsearch.browser import go as apply_go
from app.jobsearch.missions import core as missions
from app.jobsearch.models import Mission, MissionChange, MissionRun, Opportunity, SearchRun
from app.settings import settings
from tests.builders import counted, save_documents
from tests.support import OWNER, load_profile


async def _setup(roles=(("Globex", 3), ("Initech", 5), ("Umbrella", 4), ("Hooli", 1))) -> None:
    """Four roles in Ready to Launch, each with a Second Impression that graded its posting (Go opens no other:
    docs/PRD.md 24.13), with fits on the made-up profile's old scale (0 to 5)."""
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        await save_documents(session)
        for company, fit in roles:
            session.add(Opportunity(owner=OWNER, slug=company.lower(), company=company, title="Programs Lead", fit=fit,
                                    origin="scout", match_key=f"{company.lower()}|programs lead",
                                    status="ready_to_launch",
                                    found=date.today(), **counted()))
        await roster.ensure_roster(session, OWNER)
        await session.commit()


async def _missions(session) -> dict[str, Mission]:
    return {m.key: m for m in (await session.scalars(select(Mission).where(Mission.owner == OWNER))).all()}


async def test_a_new_install_starts_with_the_sweep_and_the_read_on_and_the_rest_paused(js):
    async with db.session_factory()() as session:
        assert await missions.ensure(session, OWNER) == 8
        assert await missions.ensure(session, OWNER) == 0, "safe to run again"
        await session.commit()
        rows = await _missions(session)
    assert {k: m.state for k, m in rows.items()} == {
        missions.READ: "on", missions.READ_MAIL: "paused", missions.RESEARCH: "paused",
        missions.BROWSER: "paused", missions.SCOUT: "paused",
        missions.ANALYSE: "paused",
        missions.SWEEP: "on", missions.GO: "on"}
    assert rows[missions.READ].schedule == {"kind": "daily", "at": "01:00"}, "the window opens at 1 AM"
    assert rows[missions.READ].settings == {"details_per_night": 150}
    assert rows[missions.SCOUT].schedule == {"kind": "daily", "at": "01:00"}, "the nightly scan time"
    assert rows[missions.ANALYSE].schedule == {"kind": "after", "mission": missions.SCOUT}
    assert rows[missions.RESEARCH].schedule == {"kind": "after", "mission": missions.READ}, (
        "the employer researcher runs nightly, after the boards are read (docs/PRD.md 36.1)")
    assert rows[missions.RESEARCH].playbook == "jobsearch_research_employer"
    assert rows[missions.RESEARCH].settings == {"target": 25, "again_after_days": 90, "finish_by": "06:45"}
    assert rows[missions.GO].schedule == {"kind": "daily", "at": "06:30"}, "Go is the one place a packet is built"
    assert "forge_packets" not in rows
    assert rows[missions.SCOUT].settings == {"focus": "", "keep_target": 20, "max_sessions": 4, "finish_by": "06:45"}


async def test_a_start_brings_what_each_mission_is_for_up_to_date_and_leaves_what_the_owner_set_alone(js):
    """What a mission is for is the app's own words, stored when the mission is made. The owner cannot change them, so
    every start gives each mission the current text: an install's had gone stale (a mission kept describing a rule a
    release had removed)."""
    async with db.session_factory()() as session:
        await missions.ensure(session, OWNER)
        rows = await _missions(session)
        current = {key: m.purpose for key, m in rows.items()}
        assert current == {key: missions.kind_of(key).purpose() for key in missions.KINDS}
        for m in rows.values():
            m.purpose = f"Older words for {m.key}."
        # Another kind of standing work, a row of its own: its words are its own. What the owner set is theirs too.
        session.add(Mission(owner=OWNER, key="custom_work", name="Custom work", purpose="Mine.", settings={},
                            schedule={"kind": "on_demand"}, state="paused"))
        rows[missions.ANALYSE].state = "on"
        rows[missions.SCOUT].schedule = {"kind": "daily", "at": "02:15"}
        rows[missions.SCOUT].settings = {**rows[missions.SCOUT].settings, "keep_target": 7}
        await session.flush()
        assert await missions.ensure(session, OWNER) == 0, "none is made: they are all there"
        await session.commit()
        session.expire_all()
        rows = await _missions(session)
        logged = (await session.scalars(select(MissionChange.what))).all()
    assert {key: m.purpose for key, m in rows.items() if key in missions.KINDS} == current
    assert rows["custom_work"].purpose == "Mine."
    assert rows[missions.ANALYSE].state == "on" and rows[missions.SCOUT].schedule == {"kind": "daily", "at": "02:15"}
    assert rows[missions.SCOUT].settings["keep_target"] == 7
    assert len(logged) == len(missions.KINDS) and all(w.startswith("Made it") for w in logged), (
        "the app's own words are no change of the owner's to keep in the history")


async def test_an_install_that_has_searched_keeps_searching_from_where_its_clock_was(js):
    # Fixed times, so the 1:00 schedule reads the same whatever the hour the tests run (the tests' zone is UTC).
    searched = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    async with db.session_factory()() as session:
        session.add(SearchRun(owner=OWNER, status="done", started_at=searched))
        await session.flush()
        await missions.ensure(session, OWNER)
        rows = await _missions(session)
        scout = rows[missions.SCOUT]
        assert scout.state == "on"
        assert rows[missions.ANALYSE].state == "paused", "each research is a session on the owner's subscription"
        assert not await missions.due(session, scout, now=searched + timedelta(hours=3)), "searched three hours ago"
        assert await missions.due(session, scout, now=datetime(2026, 7, 2, 1, 1, tzinfo=UTC)), "the next 1:00 came"


async def test_a_changed_schedule_counts_from_now_and_doesnt_run_at_once(js):
    async with db.session_factory()() as session:
        await missions.ensure(session, OWNER)
        scout = (await _missions(session))[missions.SCOUT]
        await missions.resume(session, scout, by="Tester")
        scout.checked_at = datetime.now(UTC) - timedelta(days=3)
        assert await missions.due(session, scout), "three days since it last looked"
        await missions.change(session, scout, by="Tester", schedule={"kind": "daily", "at": "02:00"})
        assert not await missions.due(session, scout), "the new schedule's first run is the next 2:00"


async def test_resuming_a_mission_counts_from_now_and_doesnt_run_at_once(js):
    async with db.session_factory()() as session:
        await missions.ensure(session, OWNER)
        scout = (await _missions(session))[missions.SCOUT]
        scout.checked_at = datetime.now(UTC) - timedelta(days=3)
        await missions.pause(session, scout, by="Tester")
        await missions.resume(session, scout, by="Tester")
        assert not await missions.due(session, scout), "switched on in the afternoon: its next time, not at once"


def test_schedules_say_when_they_come_up_and_refuse_nonsense():
    last = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
    assert missions.next_time({"kind": "every", "hours": 6}, last) == last + timedelta(hours=6)
    assert missions.next_time({"kind": "every", "hours": 6}, None, now=last) == last, "never ran: now"
    assert missions.next_time({"kind": "on_demand"}, last) is None
    daily = missions.next_time({"kind": "daily", "at": "02:00"}, last)
    assert daily > last and daily - last <= timedelta(days=1)
    assert missions.schedule_words({"kind": "every", "hours": 0.25}) == "every 15 minutes"
    assert missions.schedule_words({"kind": "every", "hours": 48}) == "every 2 days"
    assert missions.check_schedule({"kind": "daily", "at": "2:05"}) == {"kind": "daily", "at": "02:05"}
    for bad in ({"kind": "daily", "at": "25:00"}, {"kind": "every", "hours": 0}, {"kind": "weekly"}):
        with pytest.raises(missions.MissionError):
            missions.check_schedule(bad)
    with pytest.raises(missions.MissionError, match="roles to research: 1 to 1000"):
        missions.check_settings(missions.ANALYSE, {"target": 4000})
    assert missions.check_settings(missions.GO, {"count": 40}) == {}, "Go has no done conditions of its own"
    assert missions.check_settings(missions.ANALYSE, {"target": 3, "finish_by": ""}) == {"target": 3, "finish_by": None}
    assert missions.schedule_words({"kind": "after", "mission": missions.SCOUT}) == "when Scout the boards finishes"


DAILY = {"kind": "daily", "at": "02:00"}


@pytest.mark.parametrize("zone, last, expected", [
    # the owner's 2:00 is the next 2:00 on their clock, after the last run, whatever the zone
    ("UTC", (2026, 10, 7, 12, 0), (2026, 10, 8, 2, 0)),
    ("UTC", (2026, 10, 7, 1, 59), (2026, 10, 7, 2, 0)),
    ("UTC", (2026, 10, 7, 2, 0), (2026, 10, 8, 2, 0)),  # a run at 2:00 sharp is done: the next is tomorrow's
    ("America/Los_Angeles", (2026, 10, 7, 12, 0), (2026, 10, 8, 9, 0)),  # 5:00 PDT -> 2:00 PDT tomorrow
    ("America/Los_Angeles", (2026, 10, 7, 8, 59), (2026, 10, 7, 9, 0)),
    ("Asia/Kolkata", (2026, 10, 7, 12, 0), (2026, 10, 7, 20, 30)),  # 17:30 IST -> 2:00 IST tomorrow, a half hour zone
    ("Pacific/Auckland", (2026, 10, 7, 12, 0), (2026, 10, 7, 13, 0)),  # 1:00 NZDT the next day -> 2:00 NZDT
])
def test_a_daily_schedule_comes_up_at_the_owners_hour_on_the_owners_clock(monkeypatch, zone, last, expected):
    monkeypatch.setattr(settings, "timezone", zone)
    when = missions.next_time(DAILY, datetime(*last, tzinfo=UTC))
    assert when == datetime(*expected, tzinfo=UTC) and when.tzinfo is not None


def test_a_daily_schedule_follows_the_clocks_across_a_change_to_and_from_summer_time(monkeypatch):
    monkeypatch.setattr(settings, "timezone", "America/Los_Angeles")

    def after(*moment):
        return missions.next_time(DAILY, datetime(*moment, tzinfo=UTC))
    # the autumn change (Nov 1, 2026): 2:00 PDT on Oct 31 is 09:00Z, 2:00 PST on Nov 1 is 10:00Z, a 25-hour day
    assert after(2026, 10, 31, 9, 0) == datetime(2026, 11, 1, 10, 0, tzinfo=UTC)
    assert after(2026, 11, 1, 10, 0) == datetime(2026, 11, 2, 10, 0, tzinfo=UTC)
    # the spring change (Mar 14, 2027): 2:00 does not exist that day, and the schedule comes up at 3:00 PDT (10:00Z)
    assert after(2027, 3, 13, 10, 0) == datetime(2027, 3, 14, 10, 0, tzinfo=UTC)
    assert after(2027, 3, 14, 10, 0) == datetime(2027, 3, 15, 9, 0, tzinfo=UTC), "and the next day is 2:00 PDT again"


def test_a_daily_schedule_that_never_ran_is_due_at_once_in_any_zone_and_every_defaults_to_a_day(monkeypatch):
    now = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
    for zone in ("UTC", "America/Los_Angeles", "Asia/Kolkata"):
        monkeypatch.setattr(settings, "timezone", zone)
        assert missions.next_time(DAILY, None, now=now) <= now, zone
    monkeypatch.setattr(settings, "timezone", "UTC")
    assert missions.next_time({"kind": "daily"}, datetime(2026, 10, 7, 1, 0, tzinfo=UTC)) == datetime(
        2026, 10, 7, 2, 0, tzinfo=UTC), "no hour named: 2:00"
    last = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
    assert missions.next_time({"kind": "every"}, last) == last + timedelta(hours=24)
    assert missions.next_time({"kind": "every", "hours": 0.5}, last) == last + timedelta(minutes=30)


async def test_the_schedule_loop_looks_at_the_clock_every_five_minutes_and_a_bad_pass_does_not_stop_it(js, monkeypatch):
    from app.jobsearch.loops import schedule

    passes: list[str] = []
    waits: list[float] = []

    async def failing_then_fine(session, owner):
        passes.append(owner)
        if len(passes) == 1:
            raise RuntimeError("the database blinked")
        return []

    class Stop(Exception):
        pass

    async def sleep(seconds):
        waits.append(seconds)
        if len(waits) == 3:
            raise Stop

    monkeypatch.setattr(missions, "schedule_pass", failing_then_fine)
    monkeypatch.setattr(schedule.asyncio, "sleep", sleep)
    with pytest.raises(Stop):
        await schedule.schedule_loop()
    assert len(passes) == 3 and waits == [300, 300, 300] and schedule.SCHEDULE_CHECK_SECONDS == 300


async def test_pausing_the_search_mission_stops_the_search_and_a_pause_runs_out(js):
    await _setup()
    async with db.session_factory()() as session:
        await missions.ensure(session, OWNER)
        rows = await _missions(session)
        scout = rows[missions.SCOUT]
        # The pass below is an hour on from the real clock, so any other mission whose daily time falls in that hour
        # would start for real: Go's 06:30 (UTC) falls inside it when the test runs between 05:30 and 06:30 UTC, and
        # a due Go starts a real run (the four Ready to Launch roles made above) that outlives the test on the
        # session's event loop, so the next test's Go answers "running already".
        # Only the scout is under test: every other mission, Go among them, is paused.
        for key, other in rows.items():
            if key != missions.SCOUT:
                await missions.pause(session, other, by="Tester")
        await missions.resume(session, scout, by="Tester")
        # Due since an hour ago on an hourly clock with no finish time: a night found due after its finish time is
        # skipped, not started late (`Scout.hold`, docs/PRD.md 25.10), so the pause running out must meet one still on.
        scout.schedule = {"kind": "every", "hours": 1}
        scout.settings = {**scout.settings, "finish_by": None}
        scout.checked_at = datetime.now(UTC) - timedelta(hours=2)
        await missions.pause(session, scout, by="Tester", why="away this week")
        assert await missions.schedule_pass(session, OWNER) == []
        assert await session.scalar(select(SearchRun)) is None, "paused: no search"
        until = datetime.now(UTC) + timedelta(hours=1)
        await missions.pause(session, scout, by="Tester", until=until)
        knocks = await missions.schedule_pass(session, OWNER, now=until + timedelta(minutes=1))
        assert scout.state == "on"
        search = await session.scalar(select(SearchRun))
        assert search is not None and knocks == [(str(search.id), "search_started")]
        assert not apply_go.running(OWNER), "the pass started the scout and nothing else, at any hour"
        await session.commit()


async def test_run_now_on_the_go_mission_starts_go_when_roles_are_ready_and_does_nothing_when_none_are(
        client, monkeypatch):
    started: list[str] = []

    async def start(owner):
        started.append(owner)
        return {}

    monkeypatch.setattr(apply_go, "start", start)
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        await missions.ensure(session, OWNER)
        await session.commit()
    listed = (await client.get("/api/v1/missions")).json()["missions"]
    assert [m["name"] for m in listed] == ["Read the employers' boards", "Read the mail", "Research employers",
                                             "Read in your browser", "Scout the boards", "Analyse the finds",
                                             "Sweep up stuck runs", "Open my apply tabs"]
    go = next(m for m in listed if m["key"] == missions.GO)
    scout = next(m for m in listed if m["key"] == missions.SCOUT)
    changed = await client.patch(f"/api/v1/missions/{scout['id']}",
                                 json={"settings": {"focus": "operations"}, "why": "a narrower night"})
    assert changed.status_code == 200
    assert changed.json()["changes"][0]["what"] == "Focus: '' → 'operations'."
    assert changed.json()["changes"][0]["why"] == "a narrower night"
    nothing = (await client.post(f"/api/v1/missions/{go['id']}/run")).json()
    assert nothing["summary"] == "Nothing was ready to open." and started == [], "nothing is Ready to Launch"
    await _setup()
    ran = (await client.post(f"/api/v1/missions/{go['id']}/run")).json()
    assert ran["outcome"] == "done" and ran["started_by"] == "you" and started == [OWNER]
    assert ran["summary"].startswith("Started Launch in Browser")


async def test_the_sweep_leaves_no_run_when_nothing_was_stuck_and_controls_are_kept(client):
    async with db.session_factory()() as session:
        await missions.ensure(session, OWNER)
        await session.commit()
        sweep = (await _missions(session))[missions.SWEEP]
        assert await missions.schedule_pass(session, OWNER) == []
        assert await session.scalar(select(MissionRun).where(MissionRun.mission_id == sweep.id)) is None
        assert sweep.checked_at is not None
        await session.commit()
        sweep_id = str(sweep.id)
    bad = await client.patch(f"/api/v1/missions/{sweep_id}", json={"schedule": {"kind": "every", "hours": -1}})
    assert bad.status_code == 422
    paused = (await client.post(f"/api/v1/missions/{sweep_id}/pause", json={"why": "testing"})).json()
    assert paused["state"] == "paused"
    resumed = (await client.post(f"/api/v1/missions/{sweep_id}/resume", json={})).json()
    assert resumed["state"] == "on"
    assert [c["what"] for c in resumed["changes"]][:2] == ["Resumed.", "Paused."]
    assert (await client.get(f"/api/v1/missions/{'0' * 8}-0000-0000-0000-{'0' * 12}")).status_code == 404
