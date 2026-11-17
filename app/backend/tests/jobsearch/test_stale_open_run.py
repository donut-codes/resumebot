"""A search left open with no session for over half an hour is picked up at the scout's next check-in, not left reading
"not responding" with its staged reports waiting. Everything is made up."""

from datetime import UTC, datetime, timedelta

from app import db
from app.agents import roster
from app.jobsearch.missions import search_run as run
from app.jobsearch.work import housekeeping, session_kinds
from tests.support import OWNER, load_profile

STAGED = [{"company": "Fabrikam", "title": "Programs Lead", "url": "https://example.com/1"}]


async def _open_run(session, seen_minutes_ago: int):
    await load_profile(session, OWNER)
    await roster.ensure_roster(session, OWNER)
    started = await run.start_round(session, OWNER)
    now = datetime.now(UTC)
    started.started_at = now - timedelta(minutes=seen_minutes_ago + 5)
    started.session_id, started.session_seen_at = "scout-old", now - timedelta(minutes=seen_minutes_ago)
    started.reported = STAGED
    await session.commit()
    return started


async def _label(session) -> str | None:
    conn = await roster.connection_by_slug(session, OWNER, roster.SCOUT)
    return roster._session_state(conn, await roster.work_of(session, OWNER, conn), datetime.now(UTC))


async def test_a_stale_open_run_is_picked_up_at_check_in_and_keeps_its_staged_reports(js):
    async with db.session_factory()() as session:
        started = await _open_run(session, seen_minutes_ago=90)
        assert await _label(session) == "not_responding"
        await housekeeping.settle(session, OWNER)  # picking an orphaned search up again is the app's own pass
        assert await session_kinds.next_kind(session, OWNER) == session_kinds.SEARCH, (
            "the scout is handed the open search again")
        await session.commit()
        assert started.status == "open" and started.reported == STAGED, "nothing it staged is lost"
        assert started.session_id is None, "no session has it until the next one says so"
        assert await _label(session) == "live", "it no longer reads as not responding"
        assert await run.settle_stuck(session, OWNER) == 0, "and the sweep's clock runs from the pick-up"


async def test_a_fresh_open_run_is_left_as_it_was(js):
    async with db.session_factory()() as session:
        started = await _open_run(session, seen_minutes_ago=5)
        seen = started.session_seen_at
        assert await session_kinds.next_kind(session, OWNER) == session_kinds.SEARCH
        await session.commit()
        assert started.session_id == "scout-old" and started.session_seen_at == seen
        assert started.reported == STAGED
