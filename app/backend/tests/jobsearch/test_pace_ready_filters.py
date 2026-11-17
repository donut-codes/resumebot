"""`takeable.ask` filters in SQL what `recheck` throws away (the code-first and the owner's reads of the idle poll).

`code_first` asks only for items the app's code has not tried (`code_at` unset) and `owners_only` only for the owner's
(`owners_items`): both are necessary conditions of what `recheck` keeps, so they are in the item select, and the domain,
claim and pace reads cover the survivors' domains alone. The answer is the same list in the same order. Every name here
is made up."""

import contextlib
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, select

from app import db
from app.jobsearch.browser import pace as bp
from app.jobsearch.browser import takeable
from app.jobsearch.browser.takeable import Taker
from app.jobsearch.models import BrowserItem
from tests.support import DEFAULT_OWNER as OWNER

NOW = datetime(2026, 10, 9, 22, 0, tzinfo=UTC)


def _item(n: int, *, kind: str, domain: str, priority: int, code_at: datetime | None = None,
          state: str = "waiting") -> BrowserItem:
    return BrowserItem(
        owner=OWNER, kind=kind, target=f"t{n}", link=f"https://www.{domain}/jobs/{n}", domain=domain, state=state,
        tries=0, next_at=NOW, priority=priority, order_at=NOW - timedelta(minutes=n), code_at=code_at,
        created_at=NOW - timedelta(seconds=n))


async def _seed() -> None:
    tried = NOW - timedelta(hours=1)
    rows = [
        _item(1, kind=bp.READ, domain="alpha.test", priority=0),
        _item(2, kind=bp.READ, domain="alpha.test", priority=1),                  # not the owner's
        _item(3, kind=bp.READ, domain="beta.test", priority=0, code_at=tried),    # the code has tried it
        _item(4, kind=bp.READ, domain="gamma.test", priority=0),
        _item(5, kind=bp.READ, domain="gamma.test", priority=bp.BACKLOG),         # not the owner's
        _item(6, kind=bp.RECORD, domain="delta.test", priority=0),
        _item(7, kind=bp.MAP, domain="epsilon.test", priority=1),                 # the owner's by its kind
        _item(8, kind=bp.READ, domain="zeta.test", priority=0),                   # a worker is on its domain
        _item(9, kind=bp.READ, domain="zeta.test", priority=0, state="leased"),
        _item(10, kind=bp.READ, domain="eta.test", priority=0),
    ]
    async with db.session_factory()() as session:
        session.add_all(rows)
        await session.commit()


def _taker(kinds, *, code: bool, mine: bool) -> Taker:
    return Taker(OWNER, bp.WORKER, tuple(kinds), code_first=code, owners_only=mine)


async def _old(session, kinds, *, code: bool, mine: bool):
    """`ask` as it was: every waiting item read, `recheck` left to throw out what it does."""
    rows = (await session.execute(select(*takeable.READY_COLUMNS).where(
        BrowserItem.owner == OWNER, BrowserItem.state == bp.WAITING, BrowserItem.kind.in_(list(kinds)),
        BrowserItem.next_at <= NOW + bp.WAIT_UP_TO))).all()
    return await takeable.recheck(session, _taker(kinds, code=code, mine=mine), rows, NOW)


@contextlib.contextmanager
def sent():
    seen: list[str] = []

    def note(_conn, _cursor, statement, _parameters, _context, _many) -> None:
        seen.append(" ".join(statement.split()))

    engine = db.engine().sync_engine
    event.listen(engine, "before_cursor_execute", note)
    try:
        yield seen
    finally:
        event.remove(engine, "before_cursor_execute", note)


@pytest.mark.parametrize("code,mine", [(True, True), (True, False), (False, True), (False, False)])
async def test_ask_answers_what_it_did_before_the_filters_moved_into_sql(js, code, mine):
    await _seed()
    kinds = (bp.READ, bp.RECORD, bp.MAP)
    async with db.session_factory()() as session:
        want = [(r.id, at) for r, at in await _old(session, kinds, code=code, mine=mine)]
        got = [(r.id, at) for r, at in await takeable.ask(session, _taker(kinds, code=code, mine=mine), NOW)]
    assert got == want
    if code and mine:
        assert len(got) == 4, "the owner's items the code has not tried, on a domain no worker is on"


async def test_ask_asks_sql_for_the_code_and_owner_filters_and_reads_the_survivors_domains_only(js):
    await _seed()
    with sent() as seen:
        async with db.session_factory()() as session:
            await takeable.ask(session, _taker((bp.READ, bp.RECORD, bp.MAP), code=True, mine=True), NOW)
    items = [q for q in seen if "FROM js_browser_items" in q and "next_at <=" in q]
    assert len(items) == 1 and "code_at IS NULL" in items[0] and "priority = " in items[0], items
    domains = [q for q in seen if "FROM js_browser_domains" in q]
    busy = [q for q in seen if "SELECT js_browser_items.domain" in q]
    assert domains and all("domain IN" in q for q in domains), domains
    assert busy and all("domain IN" in q for q in busy), busy


async def test_ask_leaves_the_unfiltered_reads_as_they_were(js):
    await _seed()
    with sent() as seen:
        async with db.session_factory()() as session:
            await takeable.ask(session, _taker((bp.READ,), code=False, mine=False), NOW)
    (items,) = [q for q in seen if "FROM js_browser_items" in q and "next_at <=" in q]
    assert "code_at IS NULL" not in items
    assert not [q for q in seen if "FROM js_browser_domains" in q and "domain IN" in q]
