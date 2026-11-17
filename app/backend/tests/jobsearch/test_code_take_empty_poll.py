"""An empty poll of the code-first worker takes no writer turn (`items.code_take`).

The worker polls for an item the app's code has still to try. When the read made with no turn finds nothing and no
lease has run out, there is nothing to write, so the mission's row is never locked; a lease that ran out, or an item
found, still takes the turn as before. Every name here is made up, and no test reaches the network."""

from datetime import timedelta

import pytest

from app import db
from app.jobsearch.browser import items as bd
from app.jobsearch.browser import pace as bp
from tests.builders import WORKER_B, browser_world, items, lease
from tests.jobsearch.test_browser_work import NOW, _refresh, _sourced
from tests.support import DEFAULT_OWNER as OWNER


@pytest.fixture
def locks(monkeypatch):
    taken: list[str] = []
    real = bp.lock_mission

    async def spy(session, owner):
        taken.append(owner)
        await real(session, owner)

    monkeypatch.setattr(bp, "lock_mission", spy)
    return taken


async def _take():
    async with db.session_factory()() as session:
        row = await bd.code_take(session, OWNER, "code-test0001", NOW)
        await session.commit()
        return row


async def test_no_waiting_item_and_no_expired_lease_returns_none_without_the_turn(js, locks):
    await browser_world(extra_sites=False)
    await _refresh()
    assert await _take() is None
    assert locks == []


async def test_a_waiting_item_still_takes_the_turn_and_is_leased(js, locks):
    await browser_world(extra_sites=False)
    role = await _sourced(1)
    await _refresh()
    row = await _take()
    assert row is not None and row.target == str(role)
    assert locks == [OWNER]


async def test_an_expired_lease_takes_the_turn_and_the_item_is_freed_and_taken(js, locks):
    await browser_world(extra_sites=False)
    role = await _sourced(1)
    await _refresh()
    await lease("read", str(role), WORKER_B, NOW - timedelta(minutes=30))
    row = await _take()
    assert locks == [OWNER]
    assert row is not None and row.target == str(role) and row.leased_by == "code-test0001"
    assert [i.leased_by for i in await items(kind="read", target=str(role))] == ["code-test0001"]


async def test_a_paused_mission_returns_none(js, locks):
    await browser_world(extra_sites=False, resumed=False)
    await _sourced(1)
    await _refresh()
    assert await _take() is None
