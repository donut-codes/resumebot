"""A session to read with that takes no writer turn.

The database is SQLite with one writer: any statement in a writer's session, a read too, begins `BEGIN IMMEDIATE` and
takes the file's one turn, held to the commit. A pass that reads thousands of rows to decide what to write held that
turn for the whole read (a chunk of the stored order, the employer limit's read of the deck, the check
of whether the stops and scores are current). `reading` gives such a read a
reader's session (`db.read_session_factory`), which takes no turn and in WAL never waits for the writer; the pass then
takes the turn for its write alone (`as_read`: read, decide, lock, write).

    async with off_turn.reading(session) as reader:
        rows = await reader.scalars(...)      # no turn
    ...                                       # decide in memory
    await write(session, ...)                 # the turn, for the write

The reader sees what is committed. So it is a reader's only where `session` holds nothing that the read must see: no
transaction open, nothing added, changed or deleted in it, and the transaction may be ended (`db.can_release`: not in a
savepoint or a process step, where the caller's transaction is the whole). Anywhere else it is `session` itself, and the
pass reads and writes as it always did. Rows a reader loads are detached when it closes: read their loaded columns,
never a deferred one."""

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy import Select
from sqlalchemy.ext.asyncio import AsyncSession

from app import db


def is_free(session: AsyncSession) -> bool:
    """Whether `session` holds nothing yet and may be committed: a read may be made apart from it."""
    return db.can_release(session) and not session.in_transaction() and not (
        session.new or session.dirty or session.deleted)


@contextlib.asynccontextmanager
async def reading(session: AsyncSession, *, apart: bool = True) -> AsyncIterator[AsyncSession]:
    """A reader's session where `session` is free (`is_free`) and `apart`, else `session` itself. `apart` is False for a
    caller that reads what it has just written in `session`, or plans in it (a dry run)."""
    if apart and is_free(session):
        async with db.read_session_factory()() as reader:
            yield reader
    else:
        yield session


#: How many rows `scalars_in_slices` builds before it gives the loop back.
SLICE = 250


async def scalars_in_slices(
    session: AsyncSession, stmt: Select[*tuple[Any, ...]], rows: int = SLICE
) -> list[Any]:
    """The same list as `(await session.scalars(stmt)).all()`, in the same order, with the loop given back after each
    `rows` rows: building thousands of instances in one stretch holds the event loop (and so every other request) for
    the whole build. The stream is read to its end before this returns, so the caller sees no difference."""
    res = await session.stream_scalars(stmt.execution_options(yield_per=rows))
    out: list[Any] = []
    try:
        async for part in res.partitions(rows):
            out.extend(part)
            await asyncio.sleep(0)
    finally:
        await res.close()
    return out


async def rows_in_slices(
    session: AsyncSession, stmt: Select[*tuple[Any, ...]], rows: int = SLICE
) -> list[Any]:
    """The same list as `(await session.execute(stmt)).all()` (rows of columns, not instances), in the same order, with
    the loop given back after each `rows` rows, as `scalars_in_slices` does for instances: decoding every row of a
    whole-table read (an id map) in one stretch holds the event loop for the whole decode."""
    res = await session.stream(stmt.execution_options(yield_per=rows))
    out: list[Any] = []
    try:
        async for part in res.partitions(rows):
            out.extend(part)
            await asyncio.sleep(0)
    finally:
        await res.close()
    return out
