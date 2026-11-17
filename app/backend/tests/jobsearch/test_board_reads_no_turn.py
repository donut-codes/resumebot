"""A read of the board takes no writer's turn, whatever a write has left to settle (the owner, 2026-11-10: the page
program runs no work). The read shows the stored order as it stands; one look of the engine's work queue settles it
(`loop.Dispatcher.look`), and the next read shows the role in its place. Every name is made up, and the clock is
pinned."""

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, select, update

from app import db
from app.jobsearch import stages
from app.jobsearch.models import Opportunity, QueueDirty
from app.jobsearch.pipeline import stored_order
from app.jobsearch.work.queue import loop
from tests.support import NOON, OWNER, pin_clock

TODAY = NOON.date()


@pytest.fixture
def page(client, monkeypatch):
    """The app with no engine beside it: the stand-in look `tests/conftest.py` makes before a read of the board is
    taken away, so only the read itself runs. The clock says `NOON` for the rows and for the app."""
    pin_clock(monkeypatch, NOON)
    client.event_hooks = {"request": [], "response": []}
    return client


async def _near_and_far_with_far_posted_again() -> dict[str, uuid.UUID]:
    """Two applied roles, `near` posted 5 days ago and `far` 10 days ago, settled; then `far` is posted yesterday by a
    write nothing has settled: the trigger's line waits, and the stored order still has `near` first."""
    async with db.session_factory()() as session:
        roles = [Opportunity(owner=OWNER, slug=name, company=f"Co {name}", title=f"Role {name}",
                             status=stages.APPLIED, origin="scout", match_key=f"co {name}|role {name}",
                             posted=TODAY - timedelta(days=days), found=TODAY - timedelta(days=days))
                 for name, days in (("near", 5), ("far", 10))]
        session.add_all(roles)
        await session.flush()
        await stored_order.settle(session, OWNER, NOON)
        await session.commit()
        ids = {r.slug: r.id for r in roles}
    async with db.session_factory()() as session:
        await session.execute(update(Opportunity).where(Opportunity.id == ids["far"])
                              .values(posted=TODAY - timedelta(days=1)))
        await session.commit()
    return ids


async def _lines() -> int:
    async with db.read_session_factory()() as session:
        return await session.scalar(select(func.count()).select_from(QueueDirty)) or 0


async def _applied(page) -> list[str]:
    board = await page.get("/api/v1/board", params={"column": stages.APPLIED})
    assert board.status_code == 200
    return [card["title"] for card in board.json()["columns"][0]["cards"]]


async def test_a_board_read_with_the_order_behind_takes_no_turn_and_settles_nothing(page):
    await _near_and_far_with_far_posted_again()
    lines = await _lines()
    assert lines > 0
    asked: list[float] = []
    db.turn_watchers.append(asked.append)
    try:
        shown = await _applied(page)
    finally:
        db.turn_watchers.remove(asked.append)
    assert asked == [], "the read asked for no turn"
    assert await _lines() == lines, "the read settled nothing"
    assert shown == ["Role near", "Role far"], "the lane is read from the order as it is stored"


async def test_one_look_of_the_engine_puts_the_role_in_its_place_for_the_next_read(page):
    await _near_and_far_with_far_posted_again()
    d = loop.Dispatcher(OWNER, clock=lambda: NOON, table={}, gaps=False)
    await d.look()
    await d.in_flight[loop.ORDER]
    assert await _lines() == 0
    assert await _applied(page) == ["Role far", "Role near"]
