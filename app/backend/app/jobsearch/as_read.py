"""Rows written only as they were read: the one lock behind every decision made from a read that held no lock.

A pass reads with nothing locked, ends its transaction (`db.release_if_free`), decides in memory, and only then locks
the rows it will write and writes the ones still as it read them (a pass that decided inside one long transaction would
hold its rows for seconds). `lock` is that last step, for every pass: it takes the rows, in one order, and says which of
them are still as the decision found them. What a pass decides, and what it writes, stay its own.

    held = await as_read.lock(session, Opportunity, [role for role, _ in moves], stands=lifecycle.STANDS)
    for role, target in moves:
        if role in held:  # locked, and still as it was read: the only rows the pass may write
            role.status = target

**One order for the whole app.** The ids are sorted once and read in statements of at most `STATEMENT_ROWS`, each
`ORDER BY id`, so the rows of a table are taken in id order across statements. A caller chooses no order and no batch
size.

**No row lock.** The database is SQLite with one writer: the
transaction that writes holds the whole database, so no other transaction changes a row between `lock`'s read and the
caller's write, and there is no row another transaction holds to skip or to wait for. What `lock` still does is the
other half of its job: read the rows again, in place, and say which of them are still as the decision found them
(`stands`) and still satisfy `where`. A row that is not is in `Held.skipped` (or, when a stand changed, in neither list)
and is left for the next turn.

**Called in this order**: read, `db.release_if_free`, decide, `lock`, write only `Held` rows, flush, commit or release.
The module never writes, flushes, commits or releases: its callers run inside requests, savepoints and process steps,
where a release is refused. (SQLAlchemy's own autoflush still runs before the lock's statement, as before any query.)
"""

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy import ColumnElement, orm, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute, Mapped
from sqlalchemy.orm.attributes import instance_state
from sqlalchemy.orm.base import PASSIVE_NO_INITIALIZE

#: How many ids one statement locks (a statement carries a parameter for each). The tests patch it.
STATEMENT_ROWS = 1000


class _Keyed(Protocol):
    """What `lock` asks of a model: a row has an id."""

    id: Mapped[uuid.UUID]


@dataclass(frozen=True)
class Held[R: _Keyed]:
    """What `lock` took, as the caller reads it."""

    #: The rows locked and still as read: the only ones the caller may write. Locked until the caller's transaction
    #: ends.
    ids: frozenset[uuid.UUID]
    #: The instances of `ids`, in id order: the ones passed, read again, or loaded from bare ids. Empty with
    #: `refresh=False`.
    rows: dict[uuid.UUID, R]
    #: Asked for and not locked, in id order: held by another transaction (under SKIP), gone, or no longer satisfying
    #: `where`. The database cannot say which, and no second read is made. Untouched: an instance passed for one is
    #: still the row as it was read, so a caller that tries them again passes them again.
    skipped: tuple[uuid.UUID, ...]
    # A row locked and changed in a `stands` column since the read is in neither `ids` nor `skipped`: it is left for the
    # next turn, and its instance holds the new values (it was read again). Do not write it.

    def __contains__(self, row: R | uuid.UUID) -> bool:
        """Whether this row (an instance or its id) is locked and was still as read."""
        return (row if isinstance(row, uuid.UUID) else _id_of(row)) in self.ids


def _id_of(row: Any) -> uuid.UUID:
    """A row's id as the instance holds it (`Mapped[UUID]` as the models declare it, a `UUID` or, unflushed, None)."""
    return row.id


def _values(row: object, keys: Sequence[str]) -> tuple:
    return tuple(getattr(row, key) for key in keys)


def _stood_on(row: _Keyed, keys: Sequence[str]) -> tuple:
    """What the decision stood on in this row, as the instance holds it now."""
    state = instance_state(row)
    for key in keys:
        if key in state.unloaded:
            raise ValueError(f"{key} of {type(row).__name__} {_id_of(row)} is not loaded: no decision stood on it")
        if state.get_history(key, PASSIVE_NO_INITIALIZE).has_changes():
            raise ValueError(
                f"{key} of {type(row).__name__} {_id_of(row)} was changed and not flushed: it is not as read")
    return _values(row, keys)


async def lock[R: _Keyed](
    session: AsyncSession,
    model: type[R],
    rows: Iterable[R | uuid.UUID],
    *,
    stands: Sequence[InstrumentedAttribute[Any]] = (),
    where: Sequence[ColumnElement[bool]] = (),
    defer: Sequence[InstrumentedAttribute[Any]] = (),
    refresh: bool = True,
) -> Held[R]:
    """Lock these rows (instances of `model` in this session, bare ids, or a mix; a row given twice is one, and the
    instance wins over its id), and name the ones still as they were read.

    `stands` are the columns the decision rested on. A row is as read when they equal what its instance held when this
    was called, so the instances must be the rows as read (not refreshed or reloaded since) and their stands loaded and
    flushed: an instance without a stand loaded, or with one changed and not flushed, raises `ValueError`, and so do
    `stands` with a bare id, with a stand in `defer`, and a row that is not a `model`. `where` is what each row must
    still satisfy, judged by the database on the locked row (a hybrid such as `JobPosting.is_open` is fine there). A row
    that fails it is `skipped`.

    With `refresh` (the default) the rows come back in `Held.rows`, read again in place (`populate_existing`) as the
    lock gives them, so the caller writes through attributes and the work queue's `before_flush` hook sees real
    history.
    `defer` are columns not read again: one the caller's own read loaded stays as it was, one it did not load stays not
    loaded (pass what the read deferred, so the lock reads no more than it did). With `refresh=False` the lock reads
    nothing back and touches no instance: `Held.ids` is the whole answer.

    At most `ceil(n / STATEMENT_ROWS)` statements are sent, each a plain `SELECT ... ORDER BY id`; with no rows, none.
    There is no row lock: the transaction that writes holds the database (one writer), so no other transaction changes
    a row between this read and the caller's write."""
    asked: dict[uuid.UUID, R | None] = {}
    for row in rows:
        if isinstance(row, uuid.UUID):
            asked.setdefault(row, None)
        elif isinstance(row, model):
            if _id_of(row) is None:
                raise ValueError(f"a {model.__name__} with no id yet cannot be locked: flush it first")
            asked[_id_of(row)] = row
        else:
            raise ValueError(f"{row!r} is neither an id nor a {model.__name__}")
    if stands and any(row is None for row in asked.values()):
        raise ValueError("stands need the rows as they were read: pass instances, not bare ids")
    if stands and {a.key for a in stands} & {a.key for a in defer}:
        raise ValueError("a stand that is deferred is never read again, so no row could be found changed in it")
    keys = [a.key for a in stands]
    read = {i: _stood_on(row, keys) for i, row in asked.items() if row is not None} if stands else {}
    if not asked:
        return Held(frozenset(), {}, ())

    table: Any = model
    locked: dict[uuid.UUID, Any] = {}  # each id the statements returned, with its instance (refresh) or stands (not)
    order = sorted(asked)
    for at in range(0, len(order), STATEMENT_ROWS):
        wanted = table.id.in_(order[at:at + STATEMENT_ROWS])
        if refresh:
            query = select(table).where(wanted, *where).order_by(table.id)
            if defer:
                query = query.options(*(orm.defer(a) for a in defer))
            query = query.execution_options(populate_existing=True)
        else:
            query = select(table.id, *stands).where(wanted, *where).order_by(table.id)
        if refresh:
            locked.update((row.id, row) for row in (await session.scalars(query)).all())
        else:
            locked.update((row[0], tuple(row[1:])) for row in (await session.execute(query)).all())

    # A row read again holds its stands now; one not read again was asked for them (`select(id, *stands)`).
    as_read = {i: v for i, v in locked.items() if not stands or (_values(v, keys) if refresh else v) == read[i]}
    return Held(
        ids=frozenset(as_read),
        rows={i: locked[i] for i in order if i in as_read} if refresh else {},
        skipped=tuple(i for i in order if i not in locked))
