"""Who may take a browser item: one `Taker` is asked (`takeable.ask`), rechecked (`takeable.recheck`) and counted
(`takeable.backlog`) by the same rules. One matrix of items runs through all three, and each rule that is written twice
(on a row and as a condition of a query) is checked to keep, in SQL, every row it keeps in Python. Every name here is
made up."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app import db
from app.jobsearch.browser import pace as bp
from app.jobsearch.browser import takeable
from app.jobsearch.browser.takeable import Taker
from app.jobsearch.models import BrowserDomain, BrowserItem
from app.jobsearch.work import hand_outs
from app.jobsearch.work import hours as night
from app.settings import settings
from tests.jobsearch.made_up_sites_b import made_up_boards  # noqa: F401 (used through pytestmark)
from tests.support import DEFAULT_OWNER as OWNER

pytestmark = pytest.mark.usefixtures("made_up_boards")

NOW = datetime(2026, 10, 9, 22, 0, tzinfo=UTC)


def _item(key: str, *, kind: str = bp.SOURCE, domain: str = "alpha.test", priority: int = 0, code_only: bool = False,
          code_at: datetime | None = None, link: str | None = None, state: str = "waiting",
          next_at: datetime = NOW) -> BrowserItem:
    return BrowserItem(
        id=uuid.uuid5(uuid.NAMESPACE_URL, key), owner=OWNER, kind=kind, target=key,
        link=link or f"https://www.{domain}/jobs/{key}", domain=domain, state=state, tries=0, next_at=next_at,
        priority=priority, order_at=NOW, code_at=code_at, code_only=code_only, created_at=NOW)


def _id(key: str) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, key)


async def _seed(*rows: BrowserItem, domains: tuple[BrowserDomain, ...] = ()) -> None:
    async with db.session_factory()() as session:
        session.add_all([*rows, *domains])
        await session.commit()


def _keys(offered) -> list[str]:
    return [row.target for row, _ in offered]


async def _ask(taker: Taker, at: datetime = NOW) -> list[str]:
    async with db.session_factory()() as session:
        return _keys(await takeable.ask(session, taker, at))


async def _recheck(taker: Taker, at: datetime = NOW) -> list[str]:
    """`recheck` over every waiting item of the owner, as a caller that holds all of them would put it."""
    async with db.session_factory()() as session:
        rows = (await session.scalars(select(BrowserItem).where(
            BrowserItem.owner == OWNER, BrowserItem.state == bp.WAITING))).all()
        return _keys(await takeable.recheck(session, taker, rows, at))


async def _backlog(worker: str = bp.WORKER, at: datetime = NOW) -> takeable.Backlog:
    async with db.session_factory()() as session:
        return await takeable.backlog(session, OWNER, worker, at)


def _taker(*, code: bool = False, mine: bool = False, claimed=(), kinds=(bp.SOURCE, bp.READ, bp.SCAN)) -> Taker:
    return Taker(OWNER, bp.WORKER, tuple(kinds), code_first=code, owners_only=mine, claimed=frozenset(claimed))


def _no_reader(monkeypatch, reading: bool = False) -> None:
    monkeypatch.setattr(takeable, "feed_reading", lambda: reading)


async def _world(monkeypatch) -> None:
    """One of each thing a taker is ruled by. The owner's priority 0, the backlog's 2; every domain is its own."""
    _no_reader(monkeypatch)
    tried = NOW - timedelta(hours=1)
    await _seed(
        _item("owners", domain="alpha.test", kind=bp.SCAN, code_at=tried),
        _item("backlog", domain="beta.test", priority=2, kind=bp.SCAN),
        _item("code-only", domain="gamma.test", code_only=True),
        _item("busy", domain="delta.test", kind=bp.SCAN),
        _item("busy-holder", domain="delta.test", kind=bp.SCAN, state="leased"),
        _item("claimed-elsewhere", domain="epsilon.test", kind=bp.SCAN),
        _item("stopped", domain="zeta.test", kind=bp.SCAN),
        _item("cooling", domain="eta.test", kind=bp.SCAN),
        _item("untried-read", domain="theta.test", kind=bp.READ),
        domains=(
            BrowserDomain(owner=OWNER, domain="zeta.test", blocks=0, stopped_at=NOW - timedelta(days=1)),
            BrowserDomain(owner=OWNER, domain="eta.test", blocks=1, cool_until=NOW + timedelta(hours=1)),
        ))
    async with db.session_factory()() as session:
        await hand_outs.claim(session, OWNER, hand_outs.BROWSER_READER, [_id("claimed-elsewhere")], "another", NOW)
        await session.commit()


async def test_a_session_is_offered_what_it_may_take_and_recheck_accepts_exactly_that(js, monkeypatch):
    await _world(monkeypatch)
    taker = _taker()
    offered = await _ask(taker)
    # in hand-out order: the read, then the scan the owner waits on, then the backlog's scan last of all
    assert offered == ["untried-read", "owners", "backlog"], (
        "not: code-only (no session is handed one), busy (a worker is on its domain), claimed-elsewhere (claimed for "
        "another session), stopped (a host a refusal stopped), cooling (beyond the wait)")
    assert await _recheck(taker) == offered, "recheck accepts what ask offers, in the same order"


async def test_a_session_is_handed_its_own_claim_and_the_owners_scope_keeps_only_the_owners_items(js, monkeypatch):
    await _world(monkeypatch)
    assert "claimed-elsewhere" in await _ask(_taker(claimed=[_id("claimed-elsewhere")]))
    owners = Taker.owners(OWNER, bp.WORKER)
    assert await _ask(owners) == ["untried-read", "owners"], "the backlog's scan is left out"
    assert await _recheck(owners) == ["untried-read", "owners"]


async def test_the_code_first_taker_is_offered_what_code_has_still_to_try(js, monkeypatch):
    await _world(monkeypatch)
    code = Taker.code(OWNER)
    code = Taker(OWNER, code.worker, code.kinds, code_first=True, owners_only=False)
    offered = await _ask(code)
    assert offered == ["code-only", "untried-read"], "an item only code takes, and a read code has not tried"
    assert await _recheck(code) == offered


async def test_the_boardone_read_holds_the_backlogs_boardone_items_and_never_the_owners(js, monkeypatch):
    _no_reader(monkeypatch, reading=True)
    await _seed(_item("li-owners", domain="boardone.test", priority=0, link="https://www.boardone.test/jobs/view/1"),
                _item("li-backlog", domain="boardone.test", priority=2, link="https://www.boardone.test/jobs/view/2"),
                _item("other", domain="alpha.test", kind=bp.SCAN, priority=2))
    taker = _taker(code=True, kinds=takeable.CODE_KINDS)
    for who in (_taker(), taker):
        got = await _ask(who)
        assert "li-backlog" not in got and "li-owners" in got, got
        assert await _recheck(who) == got
    assert (await _backlog()).count == 2, "counted: the owner's Boardone item and the other item, not the held one"
    _no_reader(monkeypatch, reading=False)
    assert "li-backlog" in await _ask(_taker(code=True, kinds=takeable.CODE_KINDS))
    assert (await _backlog()).count == 3


async def test_the_session_scope_holds_what_the_install_limits_and_the_code_still_tries_it(js, monkeypatch):
    _no_reader(monkeypatch)
    monkeypatch.setattr(settings, "browser_domains", "alpha.test")
    await _seed(_item("inside", kind=bp.SCAN, domain="alpha.test"), _item("outside", kind=bp.READ, domain="beta.test"))
    assert await _ask(_taker()) == ["inside"]
    assert await _recheck(_taker()) == ["inside"]
    assert await _ask(_taker(code=True, kinds=takeable.CODE_KINDS)) == ["outside"]


async def test_backlog_agrees_on_the_owners_rule_and_differs_only_on_claims_busy_cooling_and_stops(js, monkeypatch):
    await _world(monkeypatch)
    monkeypatch.setattr(night, "in_off_hours", lambda s, now=None: False)
    by_day = await _backlog()
    assert by_day.count == 6, "by day only the owner's items count: not the backlog's scan, not code-only"
    assert by_day.since == NOW
    monkeypatch.setattr(night, "in_off_hours", lambda s, now=None: True)
    assert (await _backlog()).count == 7, "in the off hours the backlog's scan counts too"
    offered = await _ask(_taker())
    assert len(offered) == 3, (
        "ask leaves out what is busy, claimed for another session, stopped for good or cooling: backlog counts them "
        "(they wait for a worker, though not this moment)")


async def _all() -> list[BrowserItem]:
    async with db.session_factory()() as session:
        return list((await session.scalars(select(BrowserItem).where(BrowserItem.owner == OWNER))).all())


async def test_an_unknown_worker_is_refused_when_the_taker_is_made():
    with pytest.raises(KeyError):
        Taker.session(OWNER, "nobody")


# --- the twin predicates: the SQL keeps every row the Python keeps ------------------------------------------------


async def _sql_keeps(condition) -> set[str]:
    async with db.session_factory()() as session:
        found = await session.scalars(select(BrowserItem.target).where(BrowserItem.owner == OWNER, condition))
        return set(found.all())


async def test_each_rule_written_twice_keeps_in_sql_every_row_it_keeps_in_python(js):
    rows = [_item(f"{kind}-{priority}-{domain}", kind=kind, priority=priority, domain=domain)
            for kind in (bp.SOURCE, bp.READ, bp.SCAN, bp.MAP, bp.RECORD, bp.LISTING)
            for priority in (0, 1, bp.BACKLOG)
            for domain in ("boardone.test", "alpha.test")]
    await _seed(*rows)
    stored = await _all()
    owners_sql = await _sql_keeps(takeable.owners_items())
    assert {r.target for r in stored if takeable.owners_item(r)} == owners_sql
    held_sql = await _sql_keeps(takeable._held_for_feed_items())
    assert {r.target for r in stored if takeable.held_for_feed(r)} == held_sql
    assert held_sql and held_sql.isdisjoint(owners_sql), "the owner's own items are never held for the read"
