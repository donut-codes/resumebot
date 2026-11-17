"""The engine settles the stored order when a write has dirtied it (the owner, 2026-11-10: the page program runs no
work, so a read of the board settles nothing). The lines the triggers write post no to-do, so the loop asks at every
look, on a reader, whether the order is behind, and starts one settle when it is (`loop.Dispatcher.look`). Every name
is made up, and the clock is pinned."""

import asyncio
import logging
import uuid
from datetime import timedelta

from sqlalchemy import func, select, update

from app import db
from app.jobsearch import stages
from app.jobsearch.models import Opportunity, QueueDirty
from app.jobsearch.pipeline import stored_order
from app.jobsearch.work.queue import handlers, loop
from tests.support import NOON, OWNER, pin_clock

TODAY = NOON.date()


def _dispatcher() -> loop.Dispatcher:
    """A dispatcher with no handlers: only the look at the stored order is under test."""
    return loop.Dispatcher(OWNER, clock=lambda: NOON, table={}, gaps=False)


async def _two_roles_settled() -> dict[str, uuid.UUID]:
    """Two applied roles, `near` posted 5 days ago and `far` 10 days ago, committed, with the stored order settled."""
    async with db.session_factory()() as session:
        roles = [Opportunity(owner=OWNER, slug=name, company=f"Co {name}", title=f"Role {name}",
                             status=stages.APPLIED, origin="scout", match_key=f"co {name}|role {name}",
                             posted=TODAY - timedelta(days=days), found=TODAY - timedelta(days=days))
                 for name, days in (("near", 5), ("far", 10))]
        session.add_all(roles)
        await session.flush()
        await stored_order.settle(session, OWNER, NOON)
        await session.commit()
        return {r.slug: r.id for r in roles}


async def _far_is_posted_yesterday(ids: dict[str, uuid.UUID]) -> None:
    """A write of an input of the order: the trigger names the role, and nothing settles it here."""
    async with db.session_factory()() as session:
        await session.execute(update(Opportunity).where(Opportunity.id == ids["far"])
                              .values(posted=TODAY - timedelta(days=1)))
        await session.commit()


async def _lines() -> int:
    async with db.read_session_factory()() as session:
        return await session.scalar(select(func.count()).select_from(QueueDirty)) or 0


async def _order() -> list[uuid.UUID]:
    """The applied lane as the database orders it from what is stored."""
    async with db.read_session_factory()() as session:
        return await stored_order.page(session, [Opportunity.owner == OWNER, Opportunity.status == stages.APPLIED],
                                       queued=False, on=TODAY, now=NOON, limit=10)


async def test_a_look_settles_the_stored_order_a_write_has_dirtied(js, monkeypatch):
    pin_clock(monkeypatch, NOON)
    ids = await _two_roles_settled()
    await _far_is_posted_yesterday(ids)
    assert await _lines() > 0 and await _order() == [ids["near"], ids["far"]], "dirty, and not settled yet"
    d = _dispatcher()
    await d.look()
    task = d.in_flight.get(loop.ORDER)
    assert task is not None, "the look started the settle"
    await task
    assert await _lines() == 0 and await _order() == [ids["far"], ids["near"]]
    assert loop.ORDER not in d.in_flight
    assert d.event.is_set(), "a settle that was made asks for one more look"


async def test_a_look_with_the_order_current_takes_no_turn_and_starts_no_settle(js, monkeypatch):
    pin_clock(monkeypatch, NOON)
    await _two_roles_settled()
    d = _dispatcher()
    asked: list[float] = []
    db.turn_watchers.append(asked.append)
    try:
        await d.look()
    finally:
        db.turn_watchers.remove(asked.append)
    assert asked == [] and loop.ORDER not in d.in_flight and not d.event.is_set()


async def test_a_look_starts_no_second_settle_while_one_runs(js, monkeypatch):
    pin_clock(monkeypatch, NOON)
    ids = await _two_roles_settled()
    await _far_is_posted_yesterday(ids)
    release = asyncio.Event()
    calls: list[str] = []

    async def held(owner, now):
        calls.append(owner)
        await release.wait()
        return True

    monkeypatch.setattr(handlers, "settle_order_at", held)
    d = _dispatcher()
    await d.look()
    first = d.in_flight[loop.ORDER]
    await asyncio.sleep(0)  # the settle has begun and waits
    await d.look()
    assert d.in_flight[loop.ORDER] is first
    release.set()
    await first
    assert calls == [OWNER] and loop.ORDER not in d.in_flight


async def test_a_settle_that_fails_keeps_the_lines_and_asks_for_no_look(js, monkeypatch, caplog):
    pin_clock(monkeypatch, NOON)
    ids = await _two_roles_settled()
    await _far_is_posted_yesterday(ids)

    async def broken(session, owner, now=None, *, everything=False):
        raise RuntimeError("the settle broke")

    monkeypatch.setattr(stored_order, "settle", broken)
    caplog.set_level(logging.WARNING)
    d = _dispatcher()
    await d.look()
    task = d.in_flight.get(loop.ORDER)
    assert task is not None
    await task
    assert await _lines() > 0 and not d.event.is_set() and loop.ORDER not in d.in_flight
    assert [m for m in caplog.messages if "the look at the stored order failed" in m]
