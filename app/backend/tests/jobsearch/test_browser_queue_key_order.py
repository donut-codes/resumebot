"""The browser queue's refresh inserts its new items in key order. The refresh inserts its new
items in one statement, an item's role (or posting) is a foreign key, and the check
takes that row `FOR KEY SHARE`, row by row, in the order the items were listed (the order the roles were read in), held
until the transaction ends. The passes that lock roles `FOR UPDATE` and wait, in id order, could hold the earlier role
and ask for the later one the refresh held, while the refresh asked for the earlier (07:39 UTC, with the quiet pass,
which is fixed by itself: tests/jobsearch/test_quiet_locks.py). Every name here is made up."""

import asyncio
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app import db
from app.jobsearch.browser import queue as browser_queue
from app.jobsearch.browser.pace import READ, WAITING
from app.jobsearch.models import BrowserItem, JobPosting, Opportunity
from tests.jobsearch.test_quiet_posting_went import _employer, _posting, _role
from tests.support import OWNER, a_backend_waits_for_a_lock

LOW, HIGH = uuid.UUID(int=1), uuid.UUID(int=2)
NOW = datetime(2026, 11, 4, 7, 39, tzinfo=UTC)

#: The table an item's key points at, and the item's column for it.
KEYED = {"role": (Opportunity, "role_id"), "posting": (JobPosting, "posting_id")}


def _item(column: str, key: uuid.UUID) -> dict:
    """One new item of the plan, as `browser.queue._plan` makes it: a read of what the key names."""
    return {"id": uuid.uuid4(), "owner": OWNER, "kind": READ, "target": str(key), "state": WAITING, "tries": 0,
            "next_at": NOW, "created_at": NOW, "role_id": None, "posting_id": None, "site": None,
            "link": f"https://boards.example.test/{key.int}", "domain": "boards.example.test", "priority": 1,
            "order_at": None, **{column: key}}


async def _make(which: str) -> None:
    async with db.session_factory()() as session:
        if which == "role":
            session.add_all([_role("low", id=LOW), _role("high", id=HIGH)])
        else:
            acme = await _employer(session)
            session.add_all([_posting(acme.id, "low", id=LOW), _posting(acme.id, "high", id=HIGH)])
        await session.commit()


@pytest.mark.parametrize("which", sorted(KEYED))
async def test_a_refresh_that_lists_its_items_in_another_order_than_a_pass_takes_the_rows_does_not_deadlock(js, which):
    model, column = KEYED[which]
    await _make(which)
    plan = browser_queue._Plan(adds=[_item(column, HIGH), _item(column, LOW)])  # the later key is listed first
    holds_low, applied = asyncio.Event(), asyncio.Event()

    async def the_pass() -> None:  # as `deck.stop_waiting` and the others: rows `FOR UPDATE` in id order, waited for
        async with db.session_factory()() as session:
            await session.execute(select(model.id).where(model.id == LOW).with_for_update())
            holds_low.set()
            while not applied.is_set() and not await a_backend_waits_for_a_lock():  # the refresh met the hold
                await asyncio.sleep(0.01)
            await session.execute(select(model.id).where(model.id == HIGH).with_for_update())
            await session.commit()

    async def the_refresh() -> None:
        await holds_low.wait()
        async with db.session_factory()() as session:
            await browser_queue._apply(session, OWNER, plan, NOW)
            await session.commit()
        applied.set()

    got = await asyncio.wait_for(asyncio.gather(the_pass(), the_refresh(), return_exceptions=True), 30)
    assert not [g for g in got if isinstance(g, BaseException)], got
    async with db.session_factory()() as session:
        assert set((await session.scalars(select(getattr(BrowserItem, column)))).all()) == {LOW, HIGH}
