"""Taking a waiting row: one conditional `UPDATE ... WHERE id IN (SELECT ... LIMIT n) RETURNING ...`.

The database is SQLite with one writer: a single statement that
finds the waiting rows and flips them runs whole, so two sessions asking at once are never handed the same row and
none waits for the other. This is what `SELECT ... FOR UPDATE SKIP LOCKED` followed by a save was on Postgres; there is
no row lock to take or to skip here, and a read followed by a write is a race the first time a read and its write
are not one statement.

`claim` takes the select that says which rows are next (its filter, its order, its limit), and the columns that make
a row taken. It returns the rows as they now stand. The work queue's to-dos for the change are posted here, by the
model's own watch list (`queue.post_claimed`), because a statement that is not an ordinary save is not seen by the
session's hook: a job taken posts what an ORM change of the same column would have posted."""

from typing import Any

from sqlalchemy import Select, inspect, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.jobsearch.work import queue


async def claim[M](session: AsyncSession, query: Select[M], **values: Any) -> list[M]:
    """Set `values` on the rows `query` selects (a `select(Model)` with its `where`, `order_by` and `limit`) and return
    them, in one statement. The rows are fresh in the session (`populate_existing`). An empty list is the answer when
    nothing waits: not an error, and nothing is written.

    Other columns of a claimed row are written afterwards by the caller, through the session as usual: the row is no
    longer one the query selects, so no other claimer reaches it."""
    entity = query.column_descriptions[0]["entity"]
    key = inspect(entity).primary_key
    if len(key) != 1:
        raise ValueError(f"{entity.__name__} has no single key to claim by")
    taken = query.with_only_columns(key[0]).correlate(None)
    statement = (update(entity).where(key[0].in_(taken)).values(**values).returning(entity)
                 .execution_options(populate_existing=True, synchronize_session=False))
    rows = list((await session.execute(statement)).scalars())
    queue.post_claimed(session, entity, rows, values)
    return rows


async def claim_one[M](session: AsyncSession, query: Select[M], **values: Any) -> M | None:
    """`claim` for the next one row: `query` is limited to one by this call."""
    rows = await claim(session, query.limit(1), **values)
    return rows[0] if rows else None
