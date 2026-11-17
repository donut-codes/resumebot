"""The owner's off hours: the pool starts work of its own only in the window (1 to 7 AM
unless the owner chose another), what the owner starts runs at any time, a search round the pool starts follows the
once-a-day rule for reads and titles as for the search, the shared Chrome's wake and the page's counts. Everything here
is made up."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app import db
from app.jobsearch import stages
from app.jobsearch.browser import queue as bq
from app.jobsearch.missions import core as missions
from app.jobsearch.missions import numbers as mission_numbers
from app.jobsearch.models import Opportunity, Packet, SearchRun
from app.jobsearch.pool import core as pool
from app.jobsearch.profile.schema import Settings
from app.jobsearch.research import analysis
from app.jobsearch.work import hours as night
from app.jobsearch.work import session_kinds
from app.jobsearch.writing import packets
from app.settings import settings
from app.sites.models import Site
from tests.builders import analysis_setup, browser_world
from tests.jobsearch.test_browser_work_pool import _role
from tests.jobsearch.test_morning_tabs import (
    _made,
    _pass,
    at,
    went,  # noqa: F401 (a fixture)
)
from tests.jobsearch.test_pool_queue import KINDS, _asks, _count, _drafted
from tests.support import DEFAULT_OWNER as OWNER
from tests.support import OWNER as TESTER
from tests.support import committed, forget_asks


def _at(hour: int, minute: int = 0) -> datetime:
    """An instant that is this local time in the test zone (UTC-4 in October)."""
    return datetime(2026, 10, 16, hour, minute, tzinfo=UTC) + timedelta(hours=4)


def _by_day(monkeypatch) -> None:
    """The pool is outside the owner's off hours (the tests are in them unless they say so)."""
    monkeypatch.setattr(night, "in_off_hours", lambda s, now=None: False)


def test_the_off_hours_are_one_to_seven_local_time_by_default_and_the_owner_can_change_them(monkeypatch):
    monkeypatch.setattr(settings, "timezone", "America/New_York")
    on = Settings()
    assert (on.off_hours_start, on.off_hours_end) == ("01:00", "07:00")
    assert not night.in_off_hours(on, _at(0, 59)) and night.in_off_hours(on, _at(1, 0))
    assert night.in_off_hours(on, _at(6, 59)) and not night.in_off_hours(on, _at(7, 0))
    assert not night.in_off_hours(on, _at(12, 0)) and not night.in_off_hours(on, _at(23, 0))
    late = Settings(off_hours_start="22:00", off_hours_end="04:00")  # across midnight
    assert night.in_off_hours(late, _at(23, 0)) and night.in_off_hours(late, _at(3, 59))
    assert not night.in_off_hours(late, _at(4, 0)) and not night.in_off_hours(late, _at(12, 0))
    assert night.in_off_hours(Settings(off_hours_start="00:00", off_hours_end="00:00"), _at(12, 0)), "no window: always"


def test_the_off_hours_began_at_the_window_start_even_when_the_window_crosses_midnight(monkeypatch):
    monkeypatch.setattr(settings, "timezone", "America/New_York")
    on = Settings()
    assert night.off_hours_began(on, _at(3, 0)) == _at(1, 0) == night.off_hours_began(on, _at(1, 0))
    late = Settings(off_hours_start="22:00", off_hours_end="04:00")
    assert night.off_hours_began(late, _at(23, 0)) == _at(22, 0)
    assert night.off_hours_began(late, _at(2, 0)) == _at(22, 0) - timedelta(days=1)
    assert night.off_hours_began(Settings(off_hours_start="00:00", off_hours_end="00:00"), _at(12, 0)) is None


def test_the_next_off_hours_start_is_the_first_window_start_after_now_from_inside_the_window_too(monkeypatch):
    """What a refusal at Boardone stops it until (docs/PRD.md 48.14 d): by day that is tonight's start, inside the
    window it is the next night's (the night after), and at the very start it is the following one."""
    monkeypatch.setattr(settings, "timezone", "America/New_York")
    on, tomorrow = Settings(), timedelta(days=1)
    assert night.next_off_hours_start(on, _at(12, 0)) == _at(1, 0) + tomorrow, "by day: the next 1 AM"
    assert night.next_off_hours_start(on, _at(0, 59)) == _at(1, 0), "a minute before the window"
    assert night.next_off_hours_start(on, _at(1, 0)) == _at(1, 0) + tomorrow, "at the start, the next is tomorrow's"
    assert night.next_off_hours_start(on, _at(3, 0)) == _at(1, 0) + tomorrow, "inside the window: the night after"
    late = Settings(off_hours_start="22:00", off_hours_end="04:00")  # across midnight
    assert night.next_off_hours_start(late, _at(23, 0)) == _at(22, 0) + tomorrow
    assert night.next_off_hours_start(late, _at(2, 0)) == _at(22, 0), "after midnight, still inside last night's window"
    assert night.next_off_hours_start(late, _at(12, 0)) == _at(22, 0)
    every_hour = Settings(off_hours_start="00:00", off_hours_end="00:00")
    assert night.next_off_hours_start(every_hour, _at(12, 0)) == _at(12, 0) + tomorrow, "no window: a day later"


def test_the_off_hours_are_a_setting_on_the_missions_screen_with_times_of_day():
    names = [n.name for n in mission_numbers.NUMBERS[mission_numbers.PEAK]]
    assert "off_hours_start" in names and "off_hours_end" in names
    assert mission_numbers.BY_NAME["off_hours_start"].kind == "time"


async def test_outside_the_off_hours_the_pool_starts_only_the_decks_research_and_no_packet_or_search(js, monkeypatch):
    await analysis_setup()
    await _drafted(1)
    _by_day(monkeypatch)
    assert await _asks() == [session_kinds.ANALYSIS], "the deck's research is bounded: the pool starts it at any hour"
    assert await _count(Packet) == 0
    assert await _count(Opportunity, Opportunity.analysis_state.is_not(None)) == 1, "one job at a time"
    async with db.session_factory()() as session:  # the session that took it, and the next role queued behind it
        for role in (await session.scalars(select(Opportunity).where(Opportunity.analysis_state == "waiting"))).all():
            analysis.claim(role)
        await session.commit()
    assert await _asks(2) == [None, None], "no search, reader or employer round of the pool's own by day"
    assert await _count(SearchRun) == 0
    assert await _count(Packet) == 0, "and never a packet: only Go and Build now build one"


async def test_work_the_app_queued_in_the_off_hours_waits_for_the_next_window_by_day(js, monkeypatch):
    """The owner, 2026-10-17: every self-started worker keeps to the window (analysts and browser sessions the night
    had queued kept starting after 7 AM). A session at work finishes; nothing new of the app's starts."""
    await analysis_setup()
    assert await _asks() == [session_kinds.ANALYSIS]  # started at night: a session is told, none took it
    _by_day(monkeypatch)
    assert await _asks() == [session_kinds.ANALYSIS], "the deck's research is bounded and is handed out by day too"
    await committed(pool.owner_started_mission, TESTER, missions.ANALYSE)  # the owner's Run now on its mission
    assert await _asks() == [session_kinds.ANALYSIS]


async def test_by_day_a_packet_or_research_the_owner_asked_for_is_handed_out(js, monkeypatch):
    await analysis_setup()
    await _drafted(1)
    _by_day(monkeypatch)
    async with db.session_factory()() as session:
        role = (await session.scalars(select(Opportunity).where(Opportunity.status == stages.READY_TO_LAUNCH))).one()
        await packets.start(session, TESTER, role.id)  # the owner's own ask, from the role's card
        await session.commit()
    assert await _asks() == [session_kinds.SEAT]


async def test_by_day_every_lane_is_held_to_the_owners_limit_and_at_night_all_work(js, monkeypatch):
    await analysis_setup()
    assert await _asks() == [session_kinds.ANALYSIS]
    async with db.session_factory()() as session:
        assert await session_kinds.next_kind(session, TESTER, runner="session-3") == session_kinds.ANALYSIS, (
            "nights use all")
        await session.commit()
    _by_day(monkeypatch)
    await committed(pool.owner_started_mission, TESTER, missions.ANALYSE)
    async with db.session_factory()() as session:
        assert await session_kinds.next_kind(session, TESTER, runner="session-3") is None, "past the daytime limit of 1"
        assert await session_kinds.next_kind(session, TESTER, runner="session-1") == session_kinds.ANALYSIS


async def _queue_made() -> None:
    """The browser queue made again from the roles now (it is kept for a few minutes otherwise)."""
    async with db.session_factory()() as session:
        await bq.refresh(session, OWNER)
        await session.commit()


async def _pool_asks() -> str | None:
    async with db.session_factory()() as session:
        kind = await session_kinds.next_kind(session, OWNER)
        await session.commit()
    return kind


async def test_at_seven_fifty_eight_a_roles_source_and_read_items_wait_for_the_night_as_a_scan_does(js, monkeypatch):
    """No self-started browser work at 07:58. Changed 2026-11-04 (the owner: "backlog should wait
    until 1-7am", PRD 52.9): the items that get a role its original posting and its text, which the 2026-10-22 rule let
    go at any hour, keep to the off hours too, with the scans. The owner's own items do not (below)."""
    monkeypatch.setattr(settings, "timezone", "America/New_York")
    wrapped = night.in_off_hours
    monkeypatch.setattr(night, "in_off_hours", lambda s, now=None: wrapped(s, now or _at(7, 58)))
    await browser_world(extra_sites=False)
    assert await _pool_asks() is None, "a site's scan is scheduled: nothing for it at 07:58"
    await _role(1)  # a board role's original posting to find (a source item)
    await _role(2, link="https://careers.northwind2.test/jobs/2")  # a posting to read
    await _queue_made()
    assert await _pool_asks() is None, "a role's items are the backlog: nothing for them at 07:58 either"
    monkeypatch.setattr(night, "in_off_hours", lambda s, now=None: wrapped(s, now or _at(6, 58)))
    assert await _pool_asks() == session_kinds.BROWSER_READER, "inside the window the night drains them"


async def test_at_seven_fifty_eight_a_starred_roles_source_item_is_handed_out(js, monkeypatch):
    """What the owner starred is theirs, not the backlog: sourcing it is handed out at any hour."""
    monkeypatch.setattr(settings, "timezone", "America/New_York")
    wrapped = night.in_off_hours
    monkeypatch.setattr(night, "in_off_hours", lambda s, now=None: wrapped(s, now or _at(7, 58)))
    await browser_world(extra_sites=False)
    await _role(1, favorited_at=datetime.now(UTC))
    await _queue_made()
    assert await _pool_asks() == session_kinds.BROWSER_READER


async def test_at_half_past_six_the_morning_tabs_mission_runs_inside_the_window(js, went):  # noqa: F811
    assert night.in_off_hours(Settings(), at(6, 30)), "06:30 is inside the owner's off hours"
    await _made()
    runs = await _pass(at(6, 30))
    assert went["count"] == 1 and [r.outcome for r in runs] == ["done"], "Go opened the batch all the same"


async def test_when_the_window_ends_the_health_line_says_what_was_left_and_why(js, monkeypatch):
    monkeypatch.setattr(settings, "timezone", "America/New_York")
    await analysis_setup()
    async with db.session_factory()() as session:
        assert not await pool.note_window(session, TESTER, _at(6, 55))
        assert await pool.note_window(session, TESTER, _at(7, 0)), "taken as the window ends"
        assert not await pool.note_window(session, TESTER, _at(7, 5)), "once"
        await session.commit()  # the lines are rows now: the schedule's loop commits them, and the page reads them
    monkeypatch.setattr(pool, "owner_today", lambda now=None: _at(7, 0).astimezone(pool.owner_zone()).date())
    async with db.session_factory()() as session:
        lines = {k["kind"]: k["health"] for k in await pool.listing(session, TESTER)}
    assert lines[pool.RESEARCHER].startswith("At 7:00 AM: 3 left"), lines[pool.RESEARCHER]
    assert lines[pool.WRITER].startswith("At 7:00 AM: none left"), "the pool queues no packet, so none is left"
    assert lines[pool.SEARCH].startswith("At 7:00 AM: ")


async def test_the_owners_run_now_works_at_any_hour(js, client, monkeypatch, knocks):
    await analysis_setup()
    _by_day(monkeypatch)
    ran = await client.post(f"{KINDS}/researcher/run")
    assert ran.json() == {"kind": "researcher", "queued": 3, "held": None}, "Run now is the owner's, whatever the hour"
    assert await _asks() == [session_kinds.ANALYSIS]
    assert knocks == [("researcher", "run_now")], "the first lane is woken at once: the lanes look only every 5 minutes"
    assert await _count(Opportunity, Opportunity.analysis_state == "waiting") == 3


async def test_run_now_knocks_nothing_when_it_queued_nothing(js, client, knocks):
    await analysis_setup()
    assert (await client.post(f"{KINDS}/writer/run")).json()["queued"] == 0
    assert knocks == []


async def test_by_day_the_pool_hands_out_a_scan_or_a_roles_backlog_item_only_when_asked(js, monkeypatch):
    """Changed 2026-11-04 (the owner: "backlog should wait until 1-7am", PRD 52.9): a role's source or read item the app
    queued by itself waits for the off hours like a scan; it was handed out unasked before. Run now (`ask_browser`)
    hands both out, and a role the owner starred needs no ask."""
    await browser_world(extra_sites=False)
    _by_day(monkeypatch)
    await _queue_made()  # the queue is made by the app's own pass, not by a look
    async with db.session_factory()() as session:
        assert await session_kinds.next_kind(session, OWNER) is None, "the scan waits for the off hours"
        await session.commit()
    await committed(pool.ask_browser, OWNER, pool.BROWSER_READER)
    async with db.session_factory()() as session:
        assert await session_kinds.next_kind(session, OWNER) == session_kinds.BROWSER_READER
    await forget_asks(OWNER)
    await _role(1)
    await _queue_made()
    async with db.session_factory()() as session:
        assert await session_kinds.next_kind(session, OWNER) is None, "a role's item, unasked: it waits for the night"
    await committed(pool.ask_browser, OWNER, pool.BROWSER_READER)
    async with db.session_factory()() as session:
        assert await session_kinds.next_kind(session, OWNER) == session_kinds.BROWSER_READER, "and Run now takes it"
    await forget_asks(OWNER)
    await _role(2, favorited_at=datetime.now(UTC))
    await _queue_made()
    async with db.session_factory()() as session:
        assert await session_kinds.next_kind(session, OWNER) == session_kinds.BROWSER_READER, "a star needs no ask"


async def test_by_day_the_pool_hands_out_the_mapping_of_a_new_site_unasked(js, monkeypatch):
    """Training new sites can happen anytime (the owner, 2026-11-04). A map was a scheduled item, handed out only in
    the off hours; it is the owner's now (`owners_item`)."""
    await browser_world(extra_sites=False)
    async with db.session_factory()() as session:
        session.add(Site(owner=OWNER, url="https://jobs.otherboard.test/", kind="job_board", read_by="browser",
                         created_at=datetime.now(UTC)))
        await session.commit()
    _by_day(monkeypatch)
    await _queue_made()
    async with db.session_factory()() as session:
        assert await session_kinds.next_kind(session, OWNER) == session_kinds.PATHFINDER


async def test_the_pool_queues_nothing_for_the_title_checker_since_the_title_pass_is_retired(js):
    async with db.session_factory()() as session:
        assert await pool.queue_kind(session, OWNER, pool.TITLE_CHECKER, manual=True) == 0, "the title pass is retired"


async def test_the_nights_limits_and_schedules_start_as_the_audit_set_them(js):
    assert missions.kind_of(missions.ANALYSE).defaults["target"] == -1
    for key in (missions.SCOUT, missions.ANALYSE, missions.RESEARCH):
        assert missions.check_settings(key, {})["finish_by"] == "06:45"
    assert missions.kind_of(missions.READ).first(None).schedule == {"kind": "daily", "at": "01:00"}
    assert missions.check_settings(missions.ANALYSE, {"target": 150})["target"] == 150, "150 is within the bounds"


async def test_the_pages_waiting_counts_are_kept_for_a_while(js, monkeypatch):
    await analysis_setup()
    calls = []
    real = pool.waiting_counts

    async def counted(session, owner):
        calls.append(1)
        return await real(session, owner)

    monkeypatch.setattr(pool, "waiting_counts", counted)
    monkeypatch.setattr(pool, "COUNTS_FOR", timedelta(seconds=30))
    async with db.session_factory()() as session:
        first = await pool.listing(session, OWNER)
        again = await pool.listing(session, OWNER)
    assert len(calls) == 1 and [k["waiting"] for k in first] == [k["waiting"] for k in again]
    monkeypatch.setattr(pool, "COUNTS_FOR", timedelta(0))
    async with db.session_factory()() as session:
        await pool.listing(session, OWNER)
    assert len(calls) == 2, "with no keeping, every look works them out again"
