"""The owner's Run now asks and the browser-down note are rows (`js_engine_facts`, `facts.py`), not values in one
program's memory: the page writes an ask and the engine's hand-out reads it, the engine is told the browser is down and
the page says so (the split of the app into a page and an engine, the owner's point 3). Each case writes in one session
and reads in another, a reader's, which sees only what was committed. Every name here is made up."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app import db
from app.jobsearch import facts
from app.jobsearch.missions import core as missions
from app.jobsearch.models import EngineFact
from app.jobsearch.pool import core as pool
from tests.support import OWNER, committed, pin_clock

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
CLOSED = "Open Chrome to let the browser jobs run"
READER = pool.BROWSER_READER


async def _read(call, *args, **kwargs):
    """What `call(reader, ...)` answers in a reader's session of its own: only what is committed."""
    async with db.read_session_factory()() as reader:
        return await call(reader, *args, **kwargs)


async def test_the_browser_down_line_one_session_wrote_is_read_by_another(js):
    assert not hasattr(pool, "_browser_down"), "the line is a row: no program keeps it in memory"
    assert await _read(pool.browser_down, OWNER) is None
    await committed(pool.note_browser, OWNER, f"  {CLOSED}\n")
    assert await _read(pool.browser_down, OWNER) == CLOSED, "one line, as Run now and Wake up show it"
    assert await _read(pool.browser_held, OWNER, READER, NOW) is True, "no session is started only to fail"
    await committed(pool.note_browser, OWNER, "")
    assert await _read(pool.browser_down, OWNER) is None, "the runner said the browser is back"
    assert await _read(pool.browser_held, OWNER, READER, NOW) is False


async def test_a_line_said_again_is_not_written_again(js, monkeypatch):
    pin_clock(monkeypatch, NOW)
    await committed(pool.note_browser, OWNER, CLOSED)
    pin_clock(monkeypatch, NOW + timedelta(hours=1))
    await committed(pool.note_browser, OWNER, CLOSED)  # every idle heartbeat says it: nothing changed, nothing written
    await committed(pool.note_browser, "someone-else", "")  # nothing to take away: nothing written
    async with db.read_session_factory()() as reader:
        rows = (await reader.scalars(select(EngineFact))).all()
    assert [(r.owner, r.key, r.value, r.updated_at) for r in rows] == [(OWNER, "browser_down", CLOSED, NOW)]


async def test_a_run_now_ask_one_session_made_stands_for_an_hour_in_another(js):
    assert not hasattr(pool, "_asked"), "the ask is a row: no program keeps it in memory"
    assert pool.ASK_STANDS == timedelta(hours=1)
    assert await _read(pool.browser_asked, OWNER, NOW) is False
    await committed(pool.ask_browser, OWNER, pool.PATHFINDER, NOW)
    assert await _read(pool.browser_asked, OWNER, NOW + timedelta(minutes=59)) is True
    assert await _read(pool.browser_asked, OWNER, NOW + pool.ASK_STANDS) is False, "an ask stands for an hour"


async def test_an_ask_is_used_up_once_and_using_it_up_again_changes_nothing(js):
    await committed(pool.ask_browser, OWNER, READER, NOW)
    await committed(pool.ask_browser, OWNER, pool.PATHFINDER, NOW)
    await committed(pool.use_up_browser_ask, OWNER, READER)
    assert await _read(facts.get_many, OWNER, "asked:") == {f"asked:{pool.PATHFINDER}": NOW.isoformat()}
    await committed(pool.use_up_browser_ask, OWNER, READER)  # nothing left of it: no error, nothing changed
    assert await _read(pool.browser_asked, OWNER, NOW) is True, "the other kind's ask is its own"


async def test_a_browser_that_is_down_is_tried_again_for_the_kind_the_owner_asked_for(js):
    await committed(pool.note_browser, OWNER, CLOSED)
    await committed(pool.ask_browser, OWNER, READER, NOW)
    assert await _read(pool.browser_held, OWNER, READER, NOW) is False, "asked: tried again"
    assert await _read(pool.browser_held, OWNER, pool.PATHFINDER, NOW) is True, "the kind not asked for stays held"
    assert await _read(pool.browser_held, OWNER, READER, NOW + pool.ASK_STANDS) is True, "the ask has run out"


async def test_work_the_owner_started_is_theirs_for_an_hour_whoever_reads(js):
    assert not hasattr(pool, "_owner_asked"), "the owner's hour is a row: no program keeps it in memory"
    await committed(pool.owner_started_mission, OWNER, missions.ANALYSE, NOW)
    assert await _read(pool.owner_started_at, OWNER, pool.RESEARCHER) == NOW
    assert await _read(pool.owner_running, OWNER, pool.RESEARCHER, NOW + timedelta(minutes=59)) is True
    assert await _read(pool.owner_running, OWNER, pool.RESEARCHER, NOW + pool.ASK_STANDS) is False
    assert await _read(pool.owner_running, OWNER, pool.WRITER, NOW) is False, "each kind has its own hour"
    assert await _read(pool.owner_started_at, OWNER, pool.WRITER) is None
    assert await _read(pool.owners_running, OWNER, NOW) == {pool.RESEARCHER}
    assert await _read(pool.owners_running, OWNER, NOW + pool.ASK_STANDS) == set()


async def test_the_claim_uses_up_the_ask_and_not_the_owners_hour(js):
    await committed(pool.ask_browser, OWNER, READER, NOW)
    await committed(pool.use_up_browser_ask, OWNER, READER)
    assert await _read(pool.browser_asked, OWNER, NOW) is False
    assert await _read(pool.owner_running, OWNER, READER, NOW) is True
    assert await _read(pool.owners_running, OWNER, NOW) == {READER}
