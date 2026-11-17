"""A runner's heartbeat word, settled in one call (`work/session_lifecycle.settle`): the parts of its order that the
route-level tests do not pin one by one. Everything is made up."""

from sqlalchemy import select

from app import db
from app.agents import roster
from app.jobsearch.missions import search_run as run
from app.jobsearch.models import SearchRun
from app.jobsearch.work import session_lifecycle
from tests.support import OWNER, load_profile, reported

SAID = "You've hit your session limit · resets 3pm (America/Los_Angeles)"


async def _open_search_with_a_role_reported(session) -> SearchRun:
    await load_profile(session, OWNER)
    await roster.ensure_roster(session, OWNER)
    started = await run.start_round(session, OWNER)
    await run.stage_report(session, started, [reported("Fabrikam", "Programs Lead")], done=False)
    await session.commit()
    return started


async def test_a_scout_that_ends_on_the_usage_limit_winds_its_search_down_not_closes_it_unfinished(js):
    async with db.session_factory()() as session:
        started = await _open_search_with_a_role_reported(session)
        search_id = started.id
        await session_lifecycle.settle(session, OWNER, {"session_id": "scout-aaaa", "state": "alive"})
        await session_lifecycle.settle(session, OWNER, {"session_id": "scout-aaaa", "state": "ended",
                                                        "usage_limit": SAID})
        await session.commit()
    async with db.session_factory()() as session:
        search = (await session.scalars(select(SearchRun).where(SearchRun.id == search_id))).one()
        assert search.status == "done" and search.counts["stopped"] == "usage"
        assert "ended before it said it was done" not in (search.summary or ""), "the limit's wind-down came first"


async def test_a_session_of_another_kind_leaves_the_open_searchs_check_in_alone(js):
    async with db.session_factory()() as session:
        started = await _open_search_with_a_role_reported(session)
        await session_lifecycle.settle(session, OWNER, {"session_id": "reader-1", "state": "alive"})
        await session.commit()
        assert started.session_id is None and started.session_seen_at is None
        await session_lifecycle.settle(session, OWNER, {"session_id": "scout-bbbb", "state": "alive"})
        await session.commit()
        assert started.session_id == "scout-bbbb" and started.session_seen_at is not None


async def test_a_failure_noting_the_browser_leaves_the_other_parts_of_the_heartbeat_saved(js, monkeypatch):
    from app.jobsearch import facts
    from app.jobsearch.pool import core as pool

    async def fails_after_a_write(session, owner, why):
        await facts.put(session, owner, "stray:note", "kept?")
        raise RuntimeError("the note failed")

    monkeypatch.setattr(pool, "note_browser", fails_after_a_write)
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        await roster.ensure_roster(session, OWNER)
        await session.commit()
        stop = await session_lifecycle.settle(session, OWNER, {"state": "idle", "browser": "Chrome is down"})
        await session.commit()
    async with db.session_factory()() as session:
        assert stop is False
        assert (await roster.connection_by_slug(session, OWNER, roster.SCOUT)).checked_in_at is not None
        assert await facts.get(session, OWNER, "stray:note") is None, "the failed part's own write is undone"


async def test_a_failure_stamping_the_search_leaves_the_other_parts_of_the_heartbeat_saved(js, monkeypatch):
    real = session_lifecycle.open_run
    calls = []

    async def fails_first_time(session, owner):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("the stamp failed")
        return await real(session, owner)

    monkeypatch.setattr(session_lifecycle, "open_run", fails_first_time)
    async with db.session_factory()() as session:
        started = await _open_search_with_a_role_reported(session)
        search_id = started.id
        await session_lifecycle.settle(session, OWNER, {"session_id": "scout-cccc", "state": "alive"})
        await session.commit()
    async with db.session_factory()() as session:
        assert (await roster.connection_by_slug(session, OWNER, roster.SCOUT)).checked_in_at is not None
        search = (await session.scalars(select(SearchRun).where(SearchRun.id == search_id))).one()
        assert search.session_id is None, "the failed stamp kept nothing"
