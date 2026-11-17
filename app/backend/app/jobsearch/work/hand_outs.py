"""The app claims the exact item it hands a runner (docs/PRD.md path 47, step 1).

Before, the answer to "what next?" was a kind of session ("titles", "parser"); the session then asked for its work
again and could find none, or the work another session was just handed. Now the ask that names a kind also claims the
items for it, in the same transaction: a row in `js_hand_outs` (the kind, the item ids, the runner, a lease), decided
one at a time for a kind (`lock`, a try-lock: an ask whose kind is being claimed by another is told nothing now, it
never waits). While the claim is untaken, no other ask or take-next call is handed those items (`held`). The runner
starts the session with the claim's id (`X-Claim`), the session's own take-next call takes the claim (`take`) and gets
exactly those items, and from then on the item's own lease or state holds it, as it always did. A claim nobody takes
lapses with its lease (`LEASE`), and the app's housekeeping removes the rows (`housekeeping.py`).

A batch a session has been handed is also leased on the row it was handed from (`lease`, `leased`, `session_took`), so
two sessions asking at once are handed disjoint batches (`lock_holder`): the one-off tagging job's roles use it.

The kinds that lease what they hand out (the analyst, the employer researcher, the Coach, the mail reader) take it the
same way, and the ledger runs that protocol (`take_next`): a kind says what is free (`Line.free`, the same list its ask
is answered from), its table and what a row must still satisfy to be taken (`Line.model`, `Line.where`), and what
taking and giving up on it mean, and the ledger owns the rest: the one-at-a-time decision, the claim's items first,
the lock on the rows it takes (`as_read.lock`: one order for every kind), the count of times an item was taken and
never answered (`MAX_CLAIMS`), and the batch.
"""
import uuid
from collections.abc import Awaitable, Callable, Collection, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import ColumnElement, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import db, program
from app.jobsearch import as_read
from app.jobsearch.models import HandOut
from app.jobsearch.work import queue

#: The kinds of session a claim is for, as `work.py` names them (the answer to an ask).
PARSER, CHECKER, GAPS, SEARCH, TITLES, ANALYSIS, RESEARCHER, SITE, SEAT, COACH, MAIL = (
    "parser", "checker", "gaps", "search", "titles", "analysis", "researcher", "site", "seat", "coach", "mail")
#: The browser's two kinds (step 2): the items are the browser queue's rows, the first of them the item the session is
#: handed first, and the domain it is on is held for the session.
BROWSER_READER, PATHFINDER = "browser_reader", "pathfinder"
BROWSER_KINDS = (BROWSER_READER, PATHFINDER)

#: How long a session has to take what was claimed for it before the items are free for another ask.
LEASE = timedelta(minutes=2)
#: How many times a session may take an item and never answer it before the kind gives up on it (`take_next`).
MAX_CLAIMS = 3
#: How long the rows are kept after they lapse, or after the session took them (for a look at what happened).
KEEP_LAPSED = timedelta(hours=1)
KEEP_TAKEN = timedelta(days=1)


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(UTC)


def _id(raw: object) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(raw))
    except ValueError:
        return None


def _key(owner: str, kind: str) -> str:
    return f"hand-out:{owner}:{kind}"


async def lock(session: AsyncSession, owner: str, kind: str) -> bool:
    """Decide the claims of this kind one at a time, until the transaction ends. False when another transaction is
    deciding them now: the caller hands out nothing of the kind this time, and does not wait (the ask never queues)."""
    program.require("engine")
    return await db.transaction_lock(session, _key(owner, kind), wait=0)


async def decide(session: AsyncSession, owner: str, kind: str) -> bool:
    """`lock` for a session's own take-next call, which waits for it (up to the lock wait, `db.transaction_lock`): an
    ask that is claiming this kind now has committed before the session decides what it takes, so the two never choose
    one item side by side. False when the ask held it past the wait: the session takes nothing this turn, and asks
    again."""
    program.require("engine")
    return await db.transaction_lock(session, _key(owner, kind))


async def held(session: AsyncSession, owner: str, kind: str, now: datetime | None = None) -> set[str]:
    """The ids of this kind claimed for a session that has not taken them yet, within their lease: not for another ask
    or take-next call."""
    rows = await session.scalars(select(HandOut.items).where(
        HandOut.owner == owner, HandOut.kind == kind, HandOut.session_id.is_(None), HandOut.lease_until > _now(now)))
    return {str(i) for items in rows for i in items}


async def held_uuids(session: AsyncSession, owner: str, kind: str, now: datetime | None = None) -> set[uuid.UUID]:
    """`held`, for kinds whose items are rows with a uuid."""
    return {k for i in await held(session, owner, kind, now) if (k := _id(i)) is not None}


async def held_of_kinds(session: AsyncSession, owner: str, kinds: Iterable[str],
                        now: datetime | None = None) -> set[uuid.UUID]:
    """`held_uuids` for several kinds at once, in one statement."""
    rows = await session.scalars(select(HandOut.items).where(
        HandOut.owner == owner, HandOut.kind.in_(list(kinds)), HandOut.session_id.is_(None),
        HandOut.lease_until > _now(now)))
    return {k for items in rows for i in items if (k := _id(i)) is not None}


def claimed_first[T](found: Iterable[T], mine: Collection[object], key: Callable[[T], object] = lambda x: x) -> list[T]:
    """`found` with the items claimed for the caller (`mine`: the ids `take` gave back) first and the rest after them,
    each in its own order: the one place a kind's `free` puts a session's own claim ahead of an older item. Ids are told
    apart as text, as a claim keeps them; `key` names the id of an item that is not one."""
    keep = {str(m) for m in mine}
    found = list(found)
    return [i for i in found if str(key(i)) in keep] + [i for i in found if str(key(i)) not in keep]


async def pending(session: AsyncSession, owner: str, runner: str | None,
                  now: datetime | None = None) -> HandOut | None:
    """The claim made for this runner (by the dispatcher, or by its own earlier ask) that no session has taken, within
    its lease: the oldest. The fallback check-in is answered with it, so a command that was lost on the way never
    strands the work (docs/PRD.md 47.3)."""
    if not runner:
        return None
    return await session.scalar(select(HandOut).where(
        HandOut.owner == owner, HandOut.runner == runner[:80], HandOut.session_id.is_(None),
        HandOut.lease_until > _now(now)).order_by(HandOut.created_at).limit(1))


async def runners_waiting(session: AsyncSession, owner: str, now: datetime | None = None) -> set[str]:
    """The runners a claim was made for that has not been taken within its lease: they have been told something and
    are not asked again until it is taken or lapses."""
    rows = await session.scalars(select(HandOut.runner).where(
        HandOut.owner == owner, HandOut.runner.is_not(None), HandOut.session_id.is_(None),
        HandOut.lease_until > _now(now)))
    return {r for r in rows if r}


async def release(session: AsyncSession, owner: str, claim_id: object) -> bool:
    """Give back a claim nobody took (its command did not reach the runner): the items are free for the next ask at
    once, not when the lease lapses. A claim a session has taken stays."""
    key = _id(claim_id)
    if key is None:
        return False
    gone = await session.execute(delete(HandOut).where(
        HandOut.id == key, HandOut.owner == owner, HandOut.session_id.is_(None)).returning(HandOut.kind))
    kinds = list(gone.scalars())
    # The items are free again at once: work of that kind is waiting for a session.
    for kind in kinds:
        queue.post(session, owner, queue.ready(kind), [""], cause="core:hand_outs.release")
    queue.post(session, owner, queue.POOL_OWED, [""] if kinds else [], cause="core:hand_outs.release")
    return bool(kinds)


async def claim(session: AsyncSession, owner: str, kind: str, ids: Iterable[object], runner: str | None = None,
                now: datetime | None = None) -> HandOut | None:
    """Claim `ids` (the items in the order they would be handed) for the runner that asked, and flush: the claim and the
    answer are one transaction. None when the kind is being claimed by another ask now (`lock`) or `ids` is empty. The
    caller has taken `lock` and read `held` before it chose the ids."""
    ids = [str(i) for i in ids]
    if not ids:
        return None
    moment = _now(now)
    row = HandOut(owner=owner, kind=kind, items=ids, runner=runner[:80] if runner else None,
                  created_at=moment, lease_until=moment + LEASE)
    session.add(row)
    await session.flush()
    return row


async def take(session: AsyncSession, owner: str, kind: str, claim_id: object, session_id: str | None,
               now: datetime | None = None) -> list[str] | None:
    """The session's own take-next call: the items claimed for it (the call named the claim, `X-Claim`), now taken by
    this session; None when it names none, the claim is another kind's, another session has it, or it has lapsed and
    was claimed afresh (the caller then takes what is free, never what another holds). The same session may ask again
    and is told the same items."""
    key = _id(claim_id)
    if key is None:
        return None
    moment = _now(now)
    row = await session.scalar(select(HandOut).where(HandOut.id == key, HandOut.owner == owner, HandOut.kind == kind))
    if row is None:
        return None
    if row.session_id is not None:
        return list(row.items) if row.session_id == (session_id or "")[:80] else None
    row.session_id, row.taken_at = (session_id or "")[:80] or "-", moment
    await session.flush()
    return list(row.items)


@dataclass(frozen=True)
class Line[Row]:
    """What a kind that leases its items says of its own waiting work, for `take_next`. The kind keeps its table and
    its lease; the ledger keeps the protocol."""

    #: The kind's `free` (`session_kinds`): `free(session, owner, now, *, mine) -> ids`, what a session could take now
    #: in the order it would be handed, the caller's own claim (`mine`) first. The one list the ask is answered from.
    free: Callable[..., Awaitable[list]]
    #: The kind's table: the ledger locks the rows of the ids it takes from it, for the one that takes them, in id order
    #: (`as_read.lock`); one another transaction has locked, or that no longer satisfies `where`, is left out.
    model: type[Any]
    #: Make the row this session's (its lease, its count). False when it turned out not to be takeable after all.
    take: Callable[[AsyncSession, Row, datetime], Awaitable[bool]]
    #: `where(now) -> conditions`: what a row must still satisfy, as the database judges it on the locked row, to be
    #: taken (waiting, and not claimed or leased at `now`). None: any row of the ids.
    where: Callable[[datetime], Sequence[ColumnElement[bool]]] | None = None
    #: How many times a session took the row and never answered it, and what giving up on a row taken `MAX_CLAIMS` times
    #: means (failed, filed or dropped). A kind that does not count, because its own time fails what nobody answers (a
    #: Find Gaps job), leaves both out.
    taken: Callable[[Row], int] | None = None
    give_up: Callable[[AsyncSession, Row], Awaitable[None]] | None = None


async def _anything_free[Row](session: AsyncSession, owner: str, lines: Sequence[Line[Row]], now: datetime) -> bool:
    """Whether any of the lines has an item to take, read with no lock (`take_next`'s first look)."""
    for line in lines:
        if await line.free(session, owner, now, mine=()):
            return True
    return False


async def take_next[Row](session: AsyncSession, owner: str, kind: str, lines: Sequence[Line[Row]], *, now: datetime,
                         session_id: str | None, claim: str | None, batch: int = 1) -> list[Row]:
    """The session's own take-next call for a kind that leases its items: up to `batch` rows from the first of `lines`
    that has any, the ones claimed for this session (`claim`, `take`) first, never one claimed for another session, and
    none while another transaction is deciding (the kind's ask, `decide`): empty then, and the session asks again.
    Sessions that ask at the same moment get different rows. A row taken `MAX_CLAIMS` times and never answered is given
    up on instead of handed over again. Decided one at a time, under the kind's turn
    (`decide`); each row taken is read again under its `where` (`as_read.lock`), so a row another call took first is
    not taken twice. The caller commits.

    A call that names no claim first reads what is free, and answers empty at once when no line has anything: an empty
    poll then does not take the kind's turn. It only ever answers empty
    there, never takes: when something is free the call goes on exactly as below and reads `free` again under the
    turn, the one list the decision is made from (what the other sessions hold now, the one-role-a-site rule). A call
    that names a claim always takes the turn, for `take` must run."""
    if not claim and not await _anything_free(session, owner, lines, now):
        return []
    if not await decide(session, owner, kind):
        return []
    taken = await take(session, owner, kind, claim, session_id, now) if claim else None
    mine = {k for i in taken or [] if (k := _id(i)) is not None}
    for line in lines:
        out: list[Row] = []
        passed: set[object] = set()
        while len(out) < batch:
            ids = [i for i in await line.free(session, owner, now, mine=mine) if i not in passed][: batch - len(out)]
            if not ids:
                break
            rows = (await as_read.lock(session, line.model, ids,
                                       where=line.where(now) if line.where else ())).rows
            for i in ids:
                passed.add(i)
                if (row := rows.get(i)) is None:
                    continue
                if line.taken is not None and line.give_up is not None and line.taken(row) >= MAX_CLAIMS:
                    await line.give_up(session, row)
                elif await line.take(session, row, now):
                    out.append(row)
        await session.flush()
        if out:
            return out
    return []


async def expire(session: AsyncSession, owner: str, now: datetime | None = None) -> int:
    """Remove the claims nobody took (an hour after they lapsed) and the ones taken (a day after). A lapsed claim
    already holds nothing back: `held` reads the lease. Returns how many rows went."""
    moment = _now(now)
    with queue.exempt("a lapsed claim already holds nothing back (`held` reads the lease): the rows are only tidied"):
        gone = await session.execute(delete(HandOut).where(
            HandOut.owner == owner,
            (HandOut.session_id.is_(None) & (HandOut.lease_until < moment - KEEP_LAPSED))
            | (HandOut.session_id.is_not(None) & (HandOut.taken_at < moment - KEEP_TAKEN))))
    return int(gone.rowcount or 0)  # pyright: ignore[reportAttributeAccessIssue]


async def count(session: AsyncSession, owner: str, kind: str | None = None) -> int:
    """How many claims are on record (a look for the tests and the housekeeping log)."""
    q = select(func.count(HandOut.id)).where(HandOut.owner == owner)
    return int(await session.scalar(q.where(HandOut.kind == kind) if kind else q) or 0)


# --- the lease of a batch on the row it was handed from ---------------------------------------------------------------

#: How long a batch is a session's once handed out: another session is not handed those items until it is answered or
#: this many seconds pass. A session answers a batch within minutes; a lapsed lease hands the batch to another.
ITEM_LEASE_SECONDS = 600
LEASES = "leases"
SESSIONS = "sessions"


def leased(holder, key: str, now: datetime | None = None) -> set[str]:
    """The ids (`key`: the ledger the holder keeps them under, "handed" for the tagging job) whose lease on `holder`
    (a tagging job: anything with `counts`) has not lapsed."""
    live = ((holder.counts or {}).get(LEASES) or {}).get(key) or {}
    return {i for i, until in live.items() if datetime.fromisoformat(until) > _now(now)}


def lease(holder, key: str, ids, now: datetime | None = None) -> None:
    """Lease these ids to the session just handed them, and drop the lapsed leases. The caller holds the row's lock
    (`lock_holder`) and the route commits."""
    moment = _now(now)
    until = (moment + timedelta(seconds=ITEM_LEASE_SECONDS)).isoformat()
    every = dict((holder.counts or {}).get(LEASES) or {})
    live = {i: u for i, u in (every.get(key) or {}).items() if datetime.fromisoformat(u) > moment}
    live.update({i: until for i in ids})
    holder.counts = {**(holder.counts or {}), LEASES: {**every, key: live}}


def session_took(holder, key: str, sid: str | None) -> tuple[int, int]:
    """(items, characters) the session `sid` has been handed from `holder` under `key`: what a session's batch is
    counted by. (0, 0) for a call that names no session."""
    if not sid:
        return 0, 0
    took = (((holder.counts or {}).get(SESSIONS) or {}).get(key) or {}).get(sid[:80]) or [0, 0]
    return int(took[0]), int(took[1])


def note_session(holder, key: str, sid: str | None, items: int, chars: int) -> None:
    """Add what the session `sid` was just handed to its count (`session_took`). The caller holds the row's lock."""
    if not sid or not items:
        return
    n, c = session_took(holder, key, sid)
    every = dict((holder.counts or {}).get(SESSIONS) or {})
    every[key] = {**(every.get(key) or {}), sid[:80]: [n + items, c + chars]}
    holder.counts = {**(holder.counts or {}), SESSIONS: every}


async def lock_holder(session: AsyncSession, holder) -> None:
    """Read the row again, so two sessions asking at once are handed their batches one after the other (the write
    transaction is the one writer) and the second sees the first's leases."""
    await session.refresh(holder)
