"""The few places the engine's SQL differs between SQLite and Postgres, in one module.

The app runs on SQLite (the sqlite port and sharing spec); the engine is a package and stays neutral, so
what depends on which database is behind a session lives here, and no other module names a dialect, a driver or a
statement only one of them has (`tests/test_portable.py` fails when one does). This imports SQLAlchemy and nothing else.

**The counting key.** `AutoKey` is `proc_history.id`: `BIGINT` (a sequence) on Postgres, `INTEGER` on SQLite, where
only a column of that exact name is the row's own number and counts up by itself (a `BIGINT` primary key there is a
plain column that is never filled in).

**The upsert.** `insert_ignore(session, table, values, conflict=...)` is `INSERT ... ON CONFLICT (columns) DO NOTHING`,
which both speak, built with each one's own `insert`.

**Locks.** `for_update(query, skip_locked=...)` and `get_locked(session, model, key)` ask for the rows read to be locked
for the rest of the transaction, so two requests for one instance take turns and `tick` can leave alone what another
request holds (`skip_locked`). Postgres locks the rows (`FOR UPDATE [SKIP LOCKED]`). SQLAlchemy leaves the clause out
where the database has none, and on SQLite that is right, not a gap: it has one writer. A transaction there that may
write begins `BEGIN IMMEDIATE`, which holds the whole database from its first statement, so no other transaction can
write between a read and the write that follows it, and there is no row another request holds for `tick` to skip: a
request that reaches the engine already has its turn.

That is what a host owes the engine on SQLite, and the package's own tests do it (`tests/conftest.py`):

- open each connection with `isolation_level = None` and begin every transaction that may write with `BEGIN IMMEDIATE`
  (a `begin` event on the engine). A transaction that began as a read and writes later can fail at once with
  `SQLITE_BUSY_SNAPSHOT`, and two of them could overwrite each other, with no row lock behind the call to stop it;
- the same two settings make savepoints (`live.attempt`, `tick`) safe. With the driver's default, the sqlite3 module
  begins a transaction only before a write, so a savepoint opened after reads only is the outermost one, and its
  release commits the whole transaction: a later rollback of the caller's transaction then undoes nothing.
"""

from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import BigInteger, Integer, Select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncSession

__all__ = ["AutoKey", "for_update", "get_locked", "insert_ignore"]

#: A number the database counts up: `bigint` (a sequence) on Postgres, `INTEGER` on SQLite (64 bits wide there too).
AutoKey = BigInteger().with_variant(Integer(), "sqlite")


def for_update(query: Select[Any], *, skip_locked: bool = False) -> Select[Any]:
    """`query` asking for the rows it reads to be locked for the rest of the transaction (Postgres: `FOR UPDATE`, with
    `SKIP LOCKED` when `skip_locked`, to leave a row another transaction holds). On SQLite the clause is left out, and
    the write transaction already holds the database: see the module's words."""
    return query.with_for_update(skip_locked=skip_locked)


async def get_locked[M](session: AsyncSession, model: type[M], key: Any) -> M | None:
    """`session.get(model, key)` that reads the row afresh (an earlier copy in the session is refreshed, so a request
    that waited for its turn sees what the one before it committed) and locks it for the rest of the transaction."""
    return await session.get(model, key, with_for_update=True, populate_existing=True)


async def insert_ignore(
    session: AsyncSession, table: Any, values: Mapping[str, Any], *, conflict: Sequence[str],
) -> None:
    """Insert the row `values` into `table` (a model or a table) unless one with the same `conflict` columns is already
    there: `INSERT ... ON CONFLICT (columns) DO NOTHING`. `conflict` is the columns of a unique constraint or primary
    key. Two requests inserting the same row at once both succeed; one of them inserts it."""
    name = session.get_bind().dialect.name
    if name == "postgresql":
        statement = postgresql.insert(table)
    elif name == "sqlite":
        statement = sqlite.insert(table)
    else:
        raise ValueError(f"process_engine runs on SQLite or Postgres, not {name!r}")
    await session.execute(statement.values(**values).on_conflict_do_nothing(index_elements=list(conflict)))
