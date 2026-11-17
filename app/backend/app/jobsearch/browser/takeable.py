"""Who may take a browser item (see `browser/items.py` for the whole account of the browser's work): one place rules
it. A `Taker` is a kind of caller (a session of one worker, the app's own code-first worker, or a session offered the
owner's items alone), and three questions are put to the queue for it:

- `ask`: the items this taker may be handed now or within `WAIT_UP_TO`, in the order they are handed out, each with
  when it could be leased (the hand-out, `queue.free`, `queue.open_work`);
- `recheck`: the same rules over rows the caller already holds (the lease, under its lock), so whatever `ask` offers,
  `recheck` accepts;
- `backlog`: how many items wait for a worker and since when (the stall and the health line). A different question on
  purpose: it ignores claims, busy domains and cooling, and it adds the pool's hold and the stops that end later.

The owner's items (`owners_item`), the hold for a followed board's own reader (`held_for_feed`) and the
app's-code-only items live here once, each as one pair: the rule on a row and the same rule as a condition of a query
(the query only narrows what the rule on a row then decides). All three questions read only: no flush, no commit, no
release and no lock, so a process step
may call them (the callers read ahead as they always did)."""

import asyncio
import uuid
from collections.abc import Collection, Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import ColumnElement, and_, func, not_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.jobsearch.address import is_board_link
from app.jobsearch.browser import pace
from app.jobsearch.browser.pace import (
    BACKLOG,
    LEASED,
    LISTING,
    MAP,
    RANK,
    READ,
    RECORD,
    SOURCE,
    TRACK,
    WAIT_UP_TO,
    WAITING,
    WORK,
    WORKER,
)
from app.jobsearch.fetch import host_stops
from app.jobsearch.models import BrowserDomain, BrowserItem
from app.jobsearch.postings.chase import YIELD_EVERY
from app.jobsearch.profile import store as profile_store
from app.jobsearch.work import availability, hand_outs
from app.jobsearch.work import hours as night
from app.sites import registry
from app.timeutil import aware

#: What the app's code-first worker takes: the kinds it tries before any session.
CODE_KINDS = (SOURCE, READ, RECORD, LISTING)


# --- the rules, each once -----------------------------------------------------------------------------------------


def owners_item(row: Any) -> bool:
    """An item taken at any hour: one the owner waits on or asked for (priority 0: the sourcing and reading of a role
    they starred, moved to Ready to Launch before it was read or asked to have researched, a job they added by its
    link, Go's check of a posting, a first recording), and the mapping of a site (`MAP`: training a new site, the
    owner, 2026-11-04). Everything else is the backlog, work the app queued by itself (a role's sourcing or reading
    from a sweep, the chase or a nightly pass, a site's scan, a recording's repair): taken only in the owner's off
    hours (`_owners_only`), the window of `night.in_off_hours` (the owner, 2026-11-04, which reverses the 2026-10-22
    rule that a `SOURCE` or `READ` goes at any hour)."""
    return row.priority == 0 or row.kind == MAP


def owners_items():
    """`owners_item` as the condition of a query: the waiting items taken at any hour."""
    return or_(BrowserItem.priority == 0, BrowserItem.kind == MAP)


def feed_reading() -> bool:
    """Whether a followed board's search is being read now (the board's own reader, `reader_now`: the one reader of the
    job search, run by the night's read or by the action door's read). It keeps its own clock for the board (PRD 32.6)
    and neither reads nor writes the queue's while it runs, so a page the queue loaded meanwhile would double the pace
    the board sees; as it ends it leaves one page gap on the queue's (`guest._leave_the_page_gap`)."""
    board = registry.board_reader("guest")  # it reads postings, which read the queue
    return board is not None and board.reader_now() is not None


def held_for_feed(row: Any) -> bool:
    """Whether an item of the app's queue waits for the read of a followed board's search to end (`feed_reading`):
    one that would load a page on that board (`pace.follow_domains`) and is not the owner's own (`owners_item`,
    priority 0: a star, a move to Ready to Launch, a research they asked for: those are the owner's and go on). Nothing
    is lost; the item is handed out when the read ends (docs/PRD.md 48.14)."""
    return row.domain in pace.follow_domains() and not owners_item(row)


def _held_for_feed_items():
    """`held_for_feed` as the condition of a query."""
    return and_(BrowserItem.domain.in_(pace.follow_domains()), not_(owners_items()))


def code_first(row: Any) -> bool:
    """Whether the app's code tries this item before a session: every `read`, a `source` on a board the code reads or
    whose link is no job board's (an employer's own posting page: code reads it and, when it holds the whole posting,
    sources the role at it), and every `record` and `listing` (which only the app's code does: no session takes one,
    `browser/recipes/record.py`, `browser/code.py` `listing_by_code`). An item that is `code_only` (a Followup needed
    role's `source`) is the app's code's on any domain: no session is ever handed it."""
    return row.kind in (READ, RECORD, LISTING) or row.code_only or (
        row.kind == SOURCE and (row.domain in pace.code_source_domains() or not is_board_link(row.link)))


def _code_waits(row: Any) -> bool:
    """Whether the item still waits for the app's code to try it (so no session is given it yet)."""
    return code_first(row) and row.code_at is None


def _unheld():
    """The SQL condition for an item the scope does not hold (a held one is no stall: nothing may take it)."""
    return or_(BrowserItem.last_note.is_(None), BrowserItem.last_note != pace.held_note())


async def _owners_only(session: AsyncSession, owner: str, worker: str, now: datetime) -> bool:
    """Whether workers of this kind take only the owner's items now (`owners_item`): outside the owner's off hours
    (`night.in_off_hours`), unless the owner started this kind lately (Run now, Wake up, Go: `pool.owner_running`).
    Otherwise a session handed out for one of the owner's items would take the app's own, and the code-first worker
    would source and end roles all day, and a morning's chase would be handed out at midday and keep Chrome open
    (PRD 52.9)."""
    from app.jobsearch.pool import core as pool  # the pool reads the queue; imported here, never at the top

    if await pool.owner_running(session, owner, worker, now):
        return False
    return not night.in_off_hours((await profile_store.current(session, owner)).settings)  # the wall clock, as the pool


async def _scheduled_open_since(session: AsyncSession, owner: str, worker: str, now: datetime) -> datetime | None:
    """When the scheduled items (the backlog: a scan, a role's sourcing or reading the app queued by itself, a
    recording's repair; never a map, which goes at any hour) of this kind of worker became takeable: the start of the
    owner's off hours now, or when the owner started the kind, whichever is earlier. None when nothing marks it (the
    window is every hour, or the items are not takeable now: `_owners_only`). A scan that has been ready since the
    afternoon has waited for a worker only since then."""
    from app.jobsearch.pool import core as pool  # the pool reads the queue; imported here, never at the top

    opened: list[datetime] = []
    if await pool.owner_running(session, owner, worker, now) and (
            asked := aware(await pool.owner_started_at(session, owner, worker))) is not None:
        opened.append(asked)
    settings = (await profile_store.current(session, owner)).settings
    if night.in_off_hours(settings) and (began := night.off_hours_began(settings, now)) is not None:  # the wall clock
        opened.append(began)
    return min(opened, default=None)


def _free_at(row: Any, domain_pace: BrowserDomain | None, now: datetime) -> datetime:
    """When an item could be leased, its domain free of other workers: its own wait, the domain's cooling and its
    next page."""
    times = [aware(row.next_at) or now]
    if domain_pace is not None:
        times += [t for t in (aware(domain_pace.cool_until), aware(domain_pace.next_page_at)) if t is not None]
    return max(times)


def _order(row: Any) -> tuple:
    """The order items are handed out in: the backlog (`BACKLOG`) last of all; then by kind (a source or a followed
    board's page read, then read, then scan), what the owner waits on first, then the youngest role first; the site
    scanned longest ago, or never mapped, first."""
    when = aware(row.order_at)
    stamp = when.timestamp() if when is not None else 0.0
    if row.kind in (SOURCE, READ, LISTING):
        within = (when is None, -stamp)
    else:
        within = (when is not None, stamp)
    made = aware(row.created_at) or datetime.min.replace(tzinfo=UTC)
    return (row.priority >= BACKLOG, RANK[row.kind], row.priority, *within, made.timestamp())


#: The columns of a waiting item that `ask` reads to order it and judge whether it can be handed out (`_order`,
#: `_free_at`, `owners_item`, `held_for_feed`, `_code_waits` and `host_stops.item_key`), and `target`, which the
#: callers and the tests name it by. `ask` returns these and nothing else, as plain rows and not as `BrowserItem`s:
#: nothing it hands back is written (a caller that leases an item takes the row again, whole and locked: `pace.whole`,
#: or `as_read.lock` in `items._pick`), and an instance of the model for each of thousands of waiting items was a third
#: of its cost. A row has the attribute of each name here and no other.
READY_COLUMNS = (BrowserItem.id, BrowserItem.kind, BrowserItem.target, BrowserItem.domain, BrowserItem.link,
                 BrowserItem.priority, BrowserItem.order_at, BrowserItem.next_at, BrowserItem.code_at,
                 BrowserItem.created_at, BrowserItem.code_only)


# --- who is asking ------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Taker:
    """A caller that may be handed browser items. `kinds` is `WORK[worker]` (narrowed by a session's track) or
    `CODE_KINDS`. `code_first`: the app's own code-first worker, which takes only what code has still to try; a session
    takes only what code has tried (or never tries) while the code-first worker runs. `owners_only`: True, only the
    owner's items (`owners_item`); False, any; None leaves it to the hour and to whether the owner started this kind
    lately, decided when the taker is asked (`decided`). `claimed`: the claims that are this session's own
    (`hand_outs`, path 47): an item claimed for another session that has not leased yet is held for it, and so is its
    domain."""

    owner: str
    worker: str
    kinds: tuple[str, ...]
    code_first: bool = False
    owners_only: bool | None = None
    claimed: frozenset[uuid.UUID] = frozenset()

    @staticmethod
    def session(owner: str, worker: str, *, track: str | None = None, claimed: Collection[uuid.UUID] = (),
                owners_only: bool | None = None) -> "Taker":
        """A session of this kind of worker, on this track (None: every kind it takes). An unknown worker raises."""
        kinds = tuple(k for k in WORK[worker] if track is None or TRACK[k] == track)
        return Taker(owner, worker, kinds, owners_only=owners_only, claimed=frozenset(claimed))

    @staticmethod
    def code(owner: str) -> "Taker":
        """The app's own code-first worker (`browser/code.py`)."""
        return Taker(owner, WORKER, CODE_KINDS, code_first=True)

    @staticmethod
    def owners(owner: str, worker: str, *, claimed: Collection[uuid.UUID] = ()) -> "Taker":
        """A session of this kind of worker, offered only the owner's items (`queue.free` with scope "owners")."""
        return Taker.session(owner, worker, claimed=claimed, owners_only=True)

    async def decided(self, session: AsyncSession, now: datetime) -> "Taker":
        """This taker with `owners_only` settled for `now` (as it stands when already settled): a caller that asks twice
        (the read before the lock and the one under it) settles it once and compares the takers."""
        if self.owners_only is not None:
            return self
        return replace(self, owners_only=await _owners_only(session, self.owner, self.worker, now))


# --- the questions ------------------------------------------------------------------------------------------------


async def ask(session: AsyncSession, taker: Taker, now: datetime) -> list[tuple[Any, datetime]]:
    """The waiting items of the taker's kinds on a domain no worker is on, in the order they are handed out, each with
    when it could be leased (only those within `WAIT_UP_TO`). A code-first taker gets only the items the app's code has
    still to try first; else, while the code-first worker runs, only those it has tried (or never tries). An item
    claimed for a session that has not started on it (`hand_outs.py`, path 47) is held for that session, and so is its
    domain: nobody else is given either, except the session whose claim it is (`taker.claimed`). While a followed
    board's search is being read (`feed_reading`) the backlog's items on it are left out (`held_for_feed`). Each
    item is a row of `READY_COLUMNS`."""
    taker = await taker.decided(session, now)
    code, mine = taker.code_first, bool(taker.owners_only)
    query = select(*READY_COLUMNS).where(
        BrowserItem.owner == taker.owner, BrowserItem.state == WAITING, BrowserItem.kind.in_(list(taker.kinds)),
        BrowserItem.next_at <= now + WAIT_UP_TO)
    if code:
        # `_judge` keeps only those the code has yet to try; an item only the code takes is tried whenever it is due
        query = query.where(or_(BrowserItem.code_at.is_(None), BrowserItem.code_only.is_(True)))
    else:
        query = query.where(BrowserItem.code_only.is_(False))  # no session is ever handed one
    if mine:
        query = query.where(owners_items())  # and only the owner's
    rows = (await session.execute(query)).all()
    # What the filters above left out `_judge` would drop whatever the domains say, so only the survivors' domains and
    # host-stop keys are read (the code-first and owner's reads of the idle poll cost every domain's rows).
    return await _judge(session, taker, rows, now, only_these=code or mine)


async def recheck(session: AsyncSession, taker: Taker, rows: Iterable[Any],
                  now: datetime) -> list[tuple[Any, datetime]]:
    """`rows` (items the caller already holds: a `BrowserItem` or a row of `READY_COLUMNS`) put in hand-out order and
    kept only if the taker may be handed them: what `ask` offers, `recheck` accepts. The domains the workers are on,
    the claims and the pace are read now, for the domains of `rows` alone (the hand-out looking at the few it will
    lock)."""
    return await _judge(session, taker, rows, now, only_these=True)


async def _judge(session: AsyncSession, taker: Taker, rows: Iterable[Any], now: datetime, *,
                 only_these: bool) -> list[tuple[Any, datetime]]:
    """The rules, over `rows`: the domain no worker is on or claimed for another session, not stopped, in scope, within
    the code-first rule and the owner's items, and free within `WAIT_UP_TO`. The domains the workers are on, the claims
    and the pace are read now, whatever `rows` were read when; `only_these` reads them for the domains of `rows`
    alone."""
    taker = await taker.decided(session, now)
    owner, code, mine = taker.owner, taker.code_first, bool(taker.owners_only)
    rows = list(rows)
    domains = {d for row in rows for d in (row.domain, host_stops.item_key(row))} if only_these else None
    busy_q = select(BrowserItem.domain).where(BrowserItem.owner == owner, BrowserItem.state == LEASED)
    if domains is not None:
        busy_q = busy_q.where(BrowserItem.domain.in_(domains))
    busy = set((await session.scalars(busy_q)).all())
    claimed = await hand_outs.held_of_kinds(session, owner, hand_outs.BROWSER_KINDS, now) - taker.claimed
    if claimed:
        claimed_q = select(BrowserItem.domain).where(BrowserItem.id.in_(claimed))
        if domains is not None:
            claimed_q = claimed_q.where(BrowserItem.domain.in_(domains))
        busy |= set((await session.scalars(claimed_q)).all())
    paces_q = select(BrowserDomain).where(BrowserDomain.owner == owner)
    if domains is not None:
        paces_q = paces_q.where(BrowserDomain.domain.in_(domains))
    paces = {p.domain: p for p in (await session.scalars(paces_q)).all()}
    # The hosts a refusal stopped (for good, or until the owner's next off hours): with none, an item's host
    # is not even worked out.
    stopped = {key for key, domain_pace in paces.items() if host_stops.in_force(domain_pace, now)}
    reading = feed_reading()
    unavailable = bool(registry.current().problems)  # a runbook that did not load leaves its hosts alone (design 2.4a)
    kept = []
    for n, row in enumerate(rows, 1):
        if n % YIELD_EVERY == 0:
            await asyncio.sleep(0)  # the loop is given back every few milliseconds of this one
        if row.domain in busy or (mine and not owners_item(row)) or (reading and held_for_feed(row)):
            continue
        if stopped and host_stops.item_key(row) in stopped:
            continue
        if unavailable and registry.current().refusal(row.domain):
            continue
        if row.code_only and not code:
            continue  # followup needed is sourced by code only (the owner, 2026-11-13): never a session's
        waits = row.code_only or _code_waits(row)
        if not code and not pace.in_scope(row.domain):
            continue  # held: a session takes nothing outside the scope; the app's own code still tries it
        if (code and not waits) or (not code and waits and owner in pace._code_on):
            continue
        kept.append(row)
    keyed = []
    for n, row in enumerate(kept, 1):  # only what can be handed out is put in order
        keyed.append((_order(row), row))
        if n % YIELD_EVERY == 0:
            await asyncio.sleep(0)
    keyed.sort(key=lambda pair: pair[0])  # stable, as `sorted(kept, key=_order)` was
    out = []
    for n, (_, row) in enumerate(keyed, 1):
        at = _free_at(row, paces.get(row.domain), now)
        if at - now <= WAIT_UP_TO:
            out.append((row, at))
        if n % YIELD_EVERY == 0:
            await asyncio.sleep(0)
    return out


@dataclass(frozen=True)
class Backlog:
    """How many items wait for a worker, and since when the oldest could have been taken (None: none wait)."""

    count: int
    since: datetime | None


async def backlog(session: AsyncSession, owner: str, worker: str, now: datetime) -> Backlog:
    """How many waiting items of this kind of worker a session could be handed now, and since when the oldest could
    have been: ready, not held by the install's scope, and, outside the owner's off hours, one the owner waits on or
    asked for, or a map (`_owners_only`). The rest, the backlog (a scan, a role's sourcing or reading the app queued by
    itself), is handed out only at night or when the owner started the kind, so work waiting for the night is not work
    that waits for a worker, and a scan ready since the afternoon has waited for one only since the night began
    (`_scheduled_open_since`). Otherwise "Stalled: nothing handed back" would show in the evening with only the
    night's scans ready. While the pool is held (peak hours, a usage limit, a pause, a drain) no session starts, so
    nothing is takeable (as in `queue.open_work`), and work the hold kept waiting has waited for a worker only since it
    lifted (`availability.counts_from`). Otherwise a site added in the morning would read "Stalled" at peak, as would a
    starred role's item. A domain stopped until a time that has not come (a stop that ends, PRD 48.14 d) leaves its
    items waiting for it, so they are no stall either. Unlike `ask`, it ignores claims, busy domains and cooling: it
    asks what waits for a worker, not what could be leased this moment."""
    if await availability.standing(session, owner, availability.Question.POOL):
        return Backlog(0, None)
    base = [BrowserItem.owner == owner, BrowserItem.state == WAITING, BrowserItem.kind.in_(list(WORK[worker])),
            BrowserItem.next_at <= now, _unheld(), BrowserItem.code_only.is_(False)]
    if feed_reading():  # held for the read of a followed board's search: it waits for that, not for a worker
        base.append(not_(_held_for_feed_items()))
    if until_the_night := await host_stops.waiting_out(session, owner, now):  # a stop that ends: the items wait for it
        base.append(BrowserItem.domain.not_in(until_the_night))
    owners = owners_items()
    groups: list[tuple[ColumnElement[bool], datetime | None]] = [(owners, None)]
    if not await _owners_only(session, owner, worker, now):
        groups.append((not_(owners), await _scheduled_open_since(session, owner, worker, now)))
    lifted = await availability.counts_from(session, owner, now=now)
    count, since = 0, []
    for condition, opened in groups:
        n, first = (await session.execute(select(func.count(BrowserItem.id), func.min(BrowserItem.next_at)).where(
            *base, condition))).one()
        count += int(n or 0)
        if (ready := aware(first)) is not None:
            since.append(max([ready, *(t for t in (opened, lifted) if t is not None)]))
    return Backlog(count, min(since, default=None))
