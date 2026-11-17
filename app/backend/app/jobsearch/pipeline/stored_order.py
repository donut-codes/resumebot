"""The stored order: what the lanes of the board are read by (docs/PRD.md 24.19, the owner, 2026-11-06): the board never
queries thousands of rows only to show the top few.

A lane's order is a number and an age (`row.py`). Reading a lane whole to put it in order meant receiving every role of
it, 23,000 in Closed and 10,000 in First Impression, for the fifty cards of a page. Both are kept on the role instead,
in `queue_points` (the number: `deck.points`) and `queue_day` and `queue_moment` (what the age counts from:
`freshness.age_basis`), so a lane is one statement that puts its roles in order in the database, indexed by the number,
and returns the roles of its page. They are worked out by the functions the card is
made by, so the number a card prints is the one the lane is sorted by, and a test holds the two to each other for every
role of a mixed world. What the database cannot keep is the clock: an age is counted from now, so the SQL of it
(`hours`) takes the day and the moment as the page does and counts the same sum (`freshness.AgeBasis.hours`), and a role
that changes of itself when a day turns (its first hours end, its posting turns stale) holds the instant it does in
`queue_until`.

**Kept current.** The triggers of migration 0151 add a line to `QueueDirty` in the transaction of every write of an
input (a rank, a read, a posting's dates, a role's dates, the profile's four documents, the owner's job sites),
insert-only so that no writer waits on another. `settle` works the roles named out again, with the roles whose
`queue_until` has passed, and deletes the lines it handled; it is asked by the engine's look as soon as a line is there
(`work/queue/loop.py`; a read of the board settles nothing and takes no turn: the owner, 2026-11-10), by the hour's tick
and at every start (which is also the backfill of the roles a release found with nothing stored). A role another
transaction holds is left for the next `settle`, never waited for: a read must not queue behind a row lock
(`db.LOCK_TIMEOUT_MS`). A pass of more than one chunk releases its transaction between chunks, so the roles it has
written are not held while it works the rest (a long hold would outlast the time a request waits for a lock).
**The reads take no turn.** Working a chunk out only reads, and it would be most of the time a
chunk holds the file's one writer turn; where the session holds nothing yet, the lines, the roles and the profile are
read in a reader's session (`off_turn.reading`) and only the write of a chunk is a writer's transaction.
Services flush; the caller commits."""

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from typing import cast as as_type

from sqlalchemy import (
    ColumnElement,
    Date,
    Integer,
    Table,
    bindparam,
    case,
    cast,
    delete,
    func,
    literal,
    select,
    update,
)
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import QueryableAttribute, load_only

from app import db
from app.jobsearch import off_turn
from app.jobsearch.fit import freshness, scoring
from app.jobsearch.models import Opportunity, QueueDirty
from app.jobsearch.pipeline import deck
from app.jobsearch.profile import store as profile_store
from app.jobsearch.research import analysis
from app.jobsearch.work import queue
from app.timeutil import aware, owner_today, owner_zone, utc_now

#: How many roles one pass reads and writes at a time: the facts of a role are a few hundred bytes, and a pass over
#: every role of the owner (a saved profile) is some seventeen of these.
CHUNK = 2_000

#: The columns of a `freshness.OrderRole`, in its order: what the number and the age are worked out from. A role is read
#: as these and nothing else (an `Opportunity` for each was most of what reading a lane whole cost).
ORDER_COLUMNS = tuple(getattr(Opportunity, name) for name in freshness.OrderRole._fields)

#: What a role with a Second Impression is scored by besides (`scoring.queue_percent`): its breakdown and target role.
#: Read for the few roles that have one, as an `Opportunity`.
SCORE_COLUMNS = (Opportunity.id, Opportunity.fit, Opportunity.fit_breakdown, Opportunity.target_role,
                 Opportunity.posted, Opportunity.found, Opportunity.created_at, Opportunity.origin,
                 Opportunity.board_url, Opportunity.status, Opportunity.ended_reason)

#: What is stored, in the order `_stored` reads it.
STORED = (Opportunity.queue_points, Opportunity.queue_day, Opportunity.queue_moment, Opportunity.queue_until)


# --- the order, in SQL ------------------------------------------------------------------------------------------------


_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MICROSECONDS_AN_HOUR = 3_600_000_000


def _microseconds(moment: QueryableAttribute[Any]) -> ColumnElement:
    """A stored time as whole microseconds since the epoch. The engine keeps a time as UTC text, `YYYY-MM-DD
    HH:MM:SS.ffffff`: the seconds are `strftime`'s and the six digits after the point are the microseconds. Whole
    numbers on purpose: a day number (`julianday`) is a float with about 50 microseconds to the digit at today's date,
    and the difference of two of them turns a time an exact hour ago into 59 minutes and a bit, where the age is whole
    hours and `freshness.hours_since` has the exact hour."""
    return cast(func.strftime("%s", moment), Integer) * 1_000_000 + cast(func.substr(moment, 21, 6), Integer)


def hours(on: date, now: datetime) -> ColumnElement:
    """A role's age in whole hours at `on` and `now`, from what is stored (the SQL of `freshness.AgeBasis.hours`): the
    hours since `queue_moment` (a moment ahead of now is 0), else the days since `queue_day` (a day ahead is 0) as
    hours, else nothing: a role no day dates sorts last. The days are `julianday`s, which are exact for a date (a whole
    or half number); the moment is counted in whole microseconds (`_microseconds`)."""
    since = func.max(literal((now - _EPOCH) // timedelta(microseconds=1), Integer)
                     - _microseconds(Opportunity.queue_moment), 0) // _MICROSECONDS_AN_HOUR
    days = func.max(cast(func.julianday(literal(on, Date)) - func.julianday(Opportunity.queue_day), Integer),
                    0) * freshness.HOURS_A_DAY
    return case((Opportunity.queue_moment.is_not(None), since), (Opportunity.queue_day.is_not(None), days))


def ordering(queued: bool, on: date, now: datetime) -> tuple[ColumnElement, ...]:
    """The order of a lane, the same as `deck.order` and `deck.age_key` put roles in: where the work is ahead (`queued`)
    the highest number first, then, and everywhere else from the first, the younger posting first (`hours`), then the
    one found more recently (a role with no time last), then by id so that the order never shifts between two reads. The
    id is the uuid itself: it sorts as the text `age_key` ends with does."""
    age = (hours(on, now).asc().nulls_last(), Opportunity.created_at.desc().nulls_last(), Opportunity.id.asc())
    return (Opportunity.queue_points.desc(), *age) if queued else age


async def page(session: AsyncSession, where: Sequence[ColumnElement], *, queued: bool, on: date, now: datetime,
               limit: int, offset: int = 0) -> list[uuid.UUID]:
    """The ids of the roles `where` names that stand from `offset` for `limit`, in the lane's order (`ordering`): the
    lane's page, read from the database in order and no more of it than the page."""
    found = await session.scalars(select(Opportunity.id).where(*where).order_by(*ordering(queued, on, now))
                                  .limit(limit).offset(offset))
    return list(found.all())


# --- working the stored values out ----------------------------------------------------------------------------------


def _midnight(day: date, plus: int) -> datetime | None:
    """The instant the owner's day that is `plus` days after `day` begins, in the owner's zone: from then on the owner's
    today is that day or later. None for a day past the calendar (a setting of a million days)."""
    try:
        return datetime.combine(day + timedelta(days=plus), time.min, tzinfo=owner_zone()).astimezone(UTC)
    except OverflowError:
        return None


def until(basis: freshness.AgeBasis, posted: freshness.PostedDay, rules: deck.QueueRules,
          now: datetime) -> datetime | None:
    """The next instant after `now` at which time alone changes the number of a role with a score (`deck.points`), or
    None: the end of its first hours (the bonus counts whole days from the posted day, so it ends as the owner's day
    turns to the day that makes `golden_hours`; a role no day dates counts them from the moment its age is counted from)
    and the start of its stale days (the owner's day after the `stale_days`th). A rule that adds or takes nothing has
    no instant."""
    found: list[datetime | None] = []
    if posted.day is not None:
        if rules.golden_bonus != 0 and rules.golden_hours > 0:
            found.append(_midnight(posted.day, -(-rules.golden_hours // freshness.HOURS_A_DAY)))
        if rules.stale_penalty != 0:
            found.append(_midnight(posted.day, rules.stale_days + 1))
    elif basis.moment is not None and rules.golden_bonus != 0 and rules.golden_hours > 0:
        try:
            found.append(basis.moment + timedelta(hours=rules.golden_hours))
        except OverflowError:
            pass
    return min((when for when in found if when is not None and when > now), default=None)


@dataclass(frozen=True)
class Stored:
    """What a role's stored order says: the four columns."""

    points: int
    day: date | None
    moment: datetime | None
    until: datetime | None


def _stored(row: Sequence) -> Stored:
    points, day, moment, then = row
    return Stored(points, day, aware(moment), aware(then))


@dataclass
class Refreshed:
    """What `refresh` did: how many roles it wrote, and the ones it left because they are gone."""

    written: int
    skipped: set[uuid.UUID]


async def refresh(session: AsyncSession, owner: str, ids: Iterable[uuid.UUID], *,
                  now: datetime | None = None) -> Refreshed:
    """Work out the stored order of these roles again, from their rank, their read, their postings, the owner's profile
    and the clock, and write it where it differs from what is stored: a pass that changes nothing writes nothing. A role
    that is not the owner's, or is gone, is left out. A role a transaction of another task holds is not waited for (it
    is in `skipped`: `settle` keeps its line for the next time). Each chunk but the last is committed where the
    transaction may be released (`db.release_if_free`: not inside a savepoint or a process step, where the whole pass
    is one transaction to its end): the lines are idempotent, so a pass cut short is finished by the next, and the roles
    written are let go of before the rest are worked. Where the session holds nothing, a chunk is read in a reader's
    session (`off_turn.reading`), so the writer's turn is held for its write alone."""
    wanted = list(dict.fromkeys(ids))
    now = now or utc_now()
    out = Refreshed(0, set())
    if not wanted:
        return out
    on = owner_today(now)
    async with off_turn.reading(session) as reader:
        current = await profile_store.current(reader, owner)
    span, rules = scoring.bounds(current), deck.queue_rules_of(current.settings)
    for start in range(0, len(wanted), CHUNK):
        async with off_turn.reading(session) as reader:
            worked = await work_out(reader, owner, wanted[start:start + CHUNK], on, now, span, rules)
        part = await _write(session, [(i, new) for i, was, new in worked if new != was])
        out.written += part.written
        out.skipped |= part.skipped
        if start + CHUNK < len(wanted):
            await db.release_if_free(session)
    return out


async def work_out(session: AsyncSession, owner: str, ids: list[uuid.UUID], on: date, now: datetime,
                   span: tuple[int, int] | None, rules: deck.QueueRules) -> list[tuple[uuid.UUID, Stored, Stored]]:
    """For each of these roles of the owner's: its id, what is stored for it, and what its stored order works out to at
    `on` and `now`, from the same functions the card is made by. Reads only."""
    found = (await session.execute(select(*ORDER_COLUMNS, *STORED).where(
        Opportunity.owner == owner, db.among(Opportunity.id, ids)))).all()
    slim = [freshness.OrderRole(*row[:len(ORDER_COLUMNS)]) for row in found]
    stored = {row[0]: _stored(row[len(ORDER_COLUMNS):]) for row in found}
    posted = await freshness.posted_by_role(session, owner, slim)
    ranks = await deck.ranks(session, owner, [r.id for r in slim])
    reads = await analysis.reads(session, [r.id for r in slim])
    whole = {r.id: r for r in (await session.scalars(select(Opportunity).where(db.among(Opportunity.id, reads))
                                                     .options(load_only(*SCORE_COLUMNS)))).all()} if reads else {}
    out = []
    for r in slim:
        role = whole.get(r.id, r)
        day, read, rank = posted[r.id], r.id in reads, ranks.get(r.id)
        basis = freshness.age_basis(r, day)
        scored = deck.has_score(role, rank, span, read)
        number = Stored(deck.points(role, rank, span, read, on, now, day, rules), basis.day, basis.moment,
                        until(basis, day, rules, now) if scored else None)
        out.append((r.id, stored[r.id], number))
    return out


async def _write(session: AsyncSession, changes: list[tuple[uuid.UUID, Stored]]) -> Refreshed:
    """Write each (role id, what to store), by primary key, in one statement for the lot: a role that is gone is matched
    by no row and so is not written, which is the whole of the condition (the database is SQLite with one writer, so
    no role is held by another transaction to be skipped, and none is read first to know which are there). Only when
    fewer rows were written than asked are the roles read, to name the ones that are gone."""
    if not changes:
        return Refreshed(0, set())
    table = as_type(Table, Opportunity.__table__)
    statement = update(table).where(table.c.id == bindparam("b_id")).values(
        queue_points=bindparam("b_points"), queue_day=bindparam("b_day"), queue_moment=bindparam("b_moment"),
        queue_until=bindparam("b_until"))
    with queue.exempt("the stored order is read only by the lanes (stored_order.py): no loop looks at it"):
        done = await session.execute(statement, [{"b_id": i, "b_points": n.points, "b_day": n.day, "b_moment": n.moment,
                                                  "b_until": n.until} for i, n in changes])
    written = as_type(CursorResult, done).rowcount
    if written == len(changes):
        return Refreshed(written, set())
    ids = [i for i, _ in changes]
    there = set((await session.scalars(select(Opportunity.id).where(db.among(Opportunity.id, ids)))).all())
    return Refreshed(written, set(ids) - there)


# --- keeping it current ---------------------------------------------------------------------------------------------


async def has_work(session: AsyncSession, owner: str, now: datetime | None = None) -> bool:
    """Whether `settle` would look at any role of the owner's: a line names one (or all), or a role's stored order has
    run out (`queue_until`). Two reads that stop at the first row, for a caller that holds no turn (the engine's look)
    and asks for one only when this says yes."""
    now = now or utc_now()
    if await session.scalar(select(QueueDirty.id).where(QueueDirty.owner == owner).limit(1)) is not None:
        return True
    return await session.scalar(select(Opportunity.id).where(
        Opportunity.owner == owner, Opportunity.queue_until.is_not(None), Opportunity.queue_until <= now).limit(1)
    ) is not None


async def settle(session: AsyncSession, owner: str, now: datetime | None = None, *, everything: bool = False) -> int:
    """Bring the stored order up to date: work out again the roles `QueueDirty` names (every role of the owner for a
    line that names none), and the roles whose `queue_until` has passed, then delete the lines handled. A line whose
    role another transaction holds stays for the next time. With `everything` every role of the owner is worked out
    again (the start's pass: a release that changed a rule of the number, or a write no trigger saw, is mended there);
    only a role whose stored order differs is written. Returns how many roles were written. Services flush; the caller
    commits (a read of a lane releases the transaction, `db.release_if_free`)."""
    now = now or utc_now()
    async with off_turn.reading(session) as reader:
        lines = (await reader.execute(select(QueueDirty.id, QueueDirty.role_id).where(QueueDirty.owner == owner))).all()
        wanted: list[uuid.UUID] = []
        if everything or any(role_id is None for _, role_id in lines):
            wanted = list((await reader.scalars(select(Opportunity.id).where(Opportunity.owner == owner))).all())
        else:
            wanted = [role_id for _, role_id in lines if role_id is not None]
        wanted += (await reader.scalars(select(Opportunity.id).where(
            Opportunity.owner == owner, Opportunity.queue_until.is_not(None), Opportunity.queue_until <= now))).all()
    done = await refresh(session, owner, wanted, now=now)
    # A role left for another transaction keeps a line of its own, so a line that named every role can go.
    named = {role_id for _, role_id in lines}
    session.add_all(QueueDirty(owner=owner, role_id=role_id) for role_id in done.skipped - named)
    handled = [i for i, role_id in lines if role_id not in done.skipped]
    if handled:
        with queue.exempt("the lines are what settle has just handled: nothing else reads them (stored_order.py)"):
            await session.execute(delete(QueueDirty).where(db.among(QueueDirty.id, handled)))
    await session.flush()
    return done.written
