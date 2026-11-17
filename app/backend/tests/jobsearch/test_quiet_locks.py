"""The quiet rules lock only the roles they move and wait for no role (docs/PRD.md 14.7). Found 2026-11-04:
`quiet._locked_roles` read every role in play `FOR UPDATE`, in id order, and waited for any a transaction
held, while the browser queue's refresh inserted its items. An item's role is a foreign key, and its check takes the
role's row `FOR KEY SHARE` in the order the items were listed (not id order): the refresh held a later role and asked
for an earlier one the run already held, the run waited for the later one, and Postgres ended one of them with
"deadlock detected" (the night's step logged "moving the roles that went quiet failed"). Every name here is made up."""

import asyncio
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app import db
from app.jobsearch import stages
from app.jobsearch.models import BrowserItem
from app.jobsearch.pipeline import quiet
from app.settings import settings
from tests.jobsearch.test_quiet_posting_went import READ, SEEN, _role
from tests.support import OWNER, a_backend_waits_for_a_lock


@pytest.fixture(autouse=True)
def owner_days_are_utc(monkeypatch):
    monkeypatch.setattr(settings, "timezone", "UTC")


async def _list_item(session, role_id: uuid.UUID) -> None:
    """What the browser queue's refresh does for a role (`browser.queue._apply`): one row in `js_browser_items`, whose
    `role_id` is a foreign key. The insert takes the role's row `FOR KEY SHARE`, held until the transaction ends."""
    await session.execute(pg_insert(BrowserItem).values([{
        "id": uuid.uuid4(), "owner": OWNER, "kind": "read", "target": str(role_id), "role_id": role_id,
        "domain": "boards.example.test", "state": "waiting", "tries": 0, "priority": 1, "next_at": SEEN,
        "created_at": SEEN}]).on_conflict_do_nothing(index_elements=["owner", "kind", "target"]))


async def test_a_run_and_a_refresh_of_the_browser_queue_that_take_the_roles_in_another_order_do_not_deadlock(js):
    low, high = uuid.UUID(int=1), uuid.UUID(int=2)
    async with db.session_factory()() as session:
        session.add_all([_role("low", stages.APPLIED, id=low), _role("high", stages.APPLIED, id=high)])
        await session.commit()
    holds_high, run_done = asyncio.Event(), asyncio.Event()

    async def the_refresh() -> None:
        async with db.session_factory()() as session:
            await _list_item(session, high)  # the later role is listed first
            holds_high.set()
            while not run_done.is_set() and not await a_backend_waits_for_a_lock():  # the run met the hold, or is done
                await asyncio.sleep(0.01)
            await _list_item(session, low)  # and the earlier one after it
            await session.commit()

    async def the_run() -> quiet.Result:
        await holds_high.wait()
        async with db.session_factory()() as session:
            result = await quiet.run(session, OWNER, today=READ.date())
            await session.commit()
        run_done.set()
        return result

    got = await asyncio.wait_for(asyncio.gather(the_refresh(), the_run(), return_exceptions=True), 30)
    assert not [g for g in got if isinstance(g, BaseException)], got
    assert (got[1].went, got[1].silent) == (0, 0)
    async with db.session_factory()() as session:
        assert set((await session.scalars(select(BrowserItem.role_id))).all()) == {low, high}
