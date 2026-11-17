"""The housekeeping pass holds a row only while it works on it (the project rules "A transaction is held only for its
database
work"). The pass has eight parts, each in a savepoint; before, all eight ran in one
transaction, so the row the first part wrote stayed locked through the heavy reads of the others. Now each part ends its
own transaction. Everything is made up."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import DBAPIError

from app import db
from app.agents import roster
from app.jobsearch.missions import search_run as run
from app.jobsearch.models import SearchRun
from app.jobsearch.work import housekeeping, session_kinds
from tests.builders import settle_housekeeping
from tests.support import OWNER, load_profile

STAGED = [{"company": "Fabrikam", "title": "Programs Lead", "url": "https://example.com/1"}]


async def _orphaned_search():
    """An open search whose session went quiet an hour and a half ago: the first part of a pass (`search_steps`)
    picks it up again, which is a write."""
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        await roster.ensure_roster(session, OWNER)
        started = await run.start_round(session, OWNER)
        now = datetime.now(UTC)
        started.started_at = now - timedelta(minutes=95)
        started.session_id, started.session_seen_at = "scout-old", now - timedelta(minutes=90)
        started.reported = STAGED
        await session.commit()
        return started.id


async def test_a_housekeeping_pass_is_a_short_transaction_for_each_of_its_parts(js, transactions, monkeypatch):
    search_id = await _orphaned_search()
    free: list[bool] = []

    async def pull(session, owner):
        """The heavy part. By the time it runs, the row the first part wrote is no one's: a second transaction can take
        it without waiting."""
        async with db.session_factory()() as other:
            try:
                await other.execute(select(SearchRun.id).where(SearchRun.id == search_id).with_for_update(nowait=True))
                free.append(True)
            except DBAPIError:
                free.append(False)
        return 0

    monkeypatch.setattr(session_kinds, "pull", pull)
    transactions.clear()
    did = await settle_housekeeping(OWNER)
    assert {"search_steps", "jobs", "browser", "tagging", "catch_up", "queued", "claims", "tabs"} <= set(did), did
    assert free == [True], "the search the first part picked up again was not still locked when the pull began"
    ran = [t for t in transactions.ended if not any("FOR UPDATE NOWAIT" in s for s in t)]
    assert len(ran) >= 7, f"{len(ran)} transactions for 8 parts: each part ends its own"
    async with db.session_factory()() as session:
        assert (await session.get(SearchRun, search_id)).session_id is None, "and what the first part did was kept"


async def test_a_part_that_fails_is_logged_and_the_parts_before_it_are_kept_and_the_ones_after_it_still_run(
        js, monkeypatch, caplog):
    search_id = await _orphaned_search()

    async def broken(session, owner):
        raise RuntimeError("a part broke")

    monkeypatch.setattr(session_kinds, "pull", broken)
    async with db.session_factory()() as session:
        did = await housekeeping.settle(session, OWNER)
        await session.rollback()  # whatever the pass left open: the parts before are already committed
    assert "queued" not in did and "claims" in did and "tabs" in did, did
    assert "housekeeping: queued failed" in caplog.text
    async with db.session_factory()() as session:
        assert (await session.get(SearchRun, search_id)).session_id is None
