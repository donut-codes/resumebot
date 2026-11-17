"""What the copy reads and writes: for each table the app owns, the columns the old table has, the columns the
SQLite schema added (which the copy fills), and the table's two statements."""

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa

from app import db
from app.migrate import convert


class CopyError(Exception):
    """The copy cannot go on, or cannot be trusted: said in words, with nothing of the data in them."""


#: Tables the copy leaves out. `js_queue_dirty` holds lines that only say which roles to look at again (the triggers add
#: them); the stored order is worked out for every role at the end of the copy, so the lines would only be deleted.
#: `js_engine_facts` was made after the move to SQLite: the old Postgres database has no such table (the copy would stop
#: with "the old database has no table"), and the engine writes its facts again as it runs.
SKIPPED = {"js_queue_dirty", "js_engine_facts"}

#: What the SQLite schema adds to a table that the source database's table lacks, as a day worked out from a time of
#: the same row in the owner's zone (`convert.day_of`): {table: {new column: the column it is worked out from}}.
ADDED_DAYS = {
    "js_opportunities": {"created_day": "created_at"},
    "js_postings": {"seen_day": "first_seen"},
}
#: Columns the SQLite schema adds that the copy leaves empty: `raw_copies.repoint` sets them once every table is in.
ADDED_POINTERS = {"js_postings": {"current_cleansed_id"}}
#: Columns the copy writes, and the app then works out again for every role (`stored_order.settle`): the check of a
#: copy leaves them out.
RECOMPUTED = {"js_opportunities": {"queue_points", "queue_day", "queue_moment", "queue_until"}}

#: How many rows a cursor fetches at a time: a table of bodies is read a few at a time.
PREFETCH, PREFETCH_BLOBS = 1_000, 20


def target_tables() -> list[sa.Table]:
    """The tables of the new file, parents first, as `db.create_schema` makes them."""
    from process_engine import models as proc_models  # noqa: F401 - registers the engine's tables on its metadata
    from process_engine.base import Base as EngineBase

    from app.database import Base
    from app.jobsearch import models  # noqa: F401 - registers the tables
    from app.models import agent, handout, work  # noqa: F401
    from app.sites import models as site_models  # noqa: F401

    tables = [t for metadata in (EngineBase.metadata, Base.metadata) for t in metadata.sorted_tables
              if t.name in db.OWNED_TABLES]
    left = db.OWNED_TABLES - {t.name for t in tables}
    if left:
        raise CopyError(f"the models have no table for {', '.join(sorted(left))}")
    return tables


@dataclass
class TablePlan:
    """One table: the columns read from the old table, in the order they are read, and written to the new one."""

    table: sa.Table
    read: list[sa.Column[Any]]
    #: For a table the copy filters (the noise copies, `noise.py`): from a row as read to the row to write, None to
    #: leave it out. Set by the copy once it has worked out what to leave.
    leaving: Callable[[list[Any]], list[Any] | None] | None = None

    @property
    def name(self) -> str:
        return self.table.name

    @property
    def days(self) -> dict[sa.Column[Any], str]:
        """The new day columns of this table, each with the name of the time column it is worked out from."""
        return {self.table.c[new]: old for new, old in ADDED_DAYS.get(self.name, {}).items()}

    @property
    def key(self) -> sa.Column[Any]:
        keys = list(self.table.primary_key.columns)
        if len(keys) != 1:
            raise CopyError(f"{self.name} has no single primary key to check its rows by")
        return keys[0]

    @property
    def prefetch(self) -> int:
        return PREFETCH_BLOBS if any(convert.kind(c) == convert.BLOB for c in self.read) else PREFETCH

    def select(self) -> str:
        return f'SELECT {", ".join(_quote(c.name) for c in self.read)} FROM {_quote(self.name)}'

    def insert(self) -> str:
        names = [_quote(c.name) for c in [*self.read, *self.days]]
        return f'INSERT INTO {_quote(self.name)} ({", ".join(names)}) VALUES ({", ".join("?" * len(names))})'


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


async def make(source: Any) -> list[TablePlan]:
    """The plan for every table, from the old database's own columns (`source` is an open asyncpg connection). A table
    or a column that does not line up is a stop: it means the old database is not at the schema the models are (a
    release not yet run on it), or has something the copy would drop."""
    tables = [t for t in target_tables() if t.name not in SKIPPED]
    rows = await source.fetch(
        "SELECT table_name, column_name FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = ANY($1::text[])", [t.name for t in tables])
    have: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        have[row["table_name"]].add(row["column_name"])
    problems, plans = [], []
    for table in tables:
        theirs = have.get(table.name)
        if not theirs:
            problems.append(f"the old database has no table {table.name}")
            continue
        added = set(ADDED_DAYS.get(table.name, {})) | ADDED_POINTERS.get(table.name, set())
        mine = {c.name for c in table.columns}
        if mine - theirs - added:
            problems.append(f"{table.name}: the old table lacks {', '.join(sorted(mine - theirs - added))}")
        if theirs - mine:
            extra = ", ".join(sorted(theirs - mine))
            problems.append(f"{table.name}: the old table has {extra}, which the copy would drop")
        plans.append(TablePlan(table, [c for c in table.columns if c.name in theirs]))
    if problems:
        raise CopyError("the old database is not at the schema of this version of the app:\n  " + "\n  ".join(problems))
    return plans
