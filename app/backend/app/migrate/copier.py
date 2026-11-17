"""The copy: every table of the old Postgres into a new SQLite file, in one read-only snapshot.

1. **Snapshot.** One connection, one `REPEATABLE READ, READ ONLY` transaction for the whole copy and its check, so every
   table is read as of one moment, however long it takes, and nothing is written to the old database.
2. **Schema.** The new file is made by `db.create_schema` and stamped at the head revision, as an empty file is on a
   start (`migrations/env.py`). The stored-order triggers are dropped for the load and made again after it: they would
   add a line for every row loaded.
3. **Load.** Each table is streamed in batches (by rows and by bytes, so a table of bodies is never held whole), each
   value written the way the app writes it (`convert`), with foreign keys off for the length of the load: the check at
   the end says whether any row points at nothing.
4. **What the SQLite schema adds.** `created_day` and `seen_day` are worked out from the row's own time in the owner's
   zone as the rows are loaded; `raw_copies.repoint` sets every posting's current-copy pointer; `stored_order.settle`
   works the stored order out for every role, as the start does.
5. **Check** (`verify`): the rows of each table counted, `foreign_key_check`, `integrity_check`, and a sample of rows of
   each table compared with the old ones, inside the same snapshot.

A copy that fails part way is deleted. One that loads and does not check out is kept and reported, and its exit status
says it is not to be used.
"""

import contextlib
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import asyncpg
import sqlalchemy as sa
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import event
from sqlalchemy.engine import make_url

from app import db
from app.migrate import convert, noise, plan, verify
from app.migrate.plan import CopyError, TablePlan

#: A batch is written when it holds this many rows or this many bytes of text and blobs, whichever comes first.
BATCH_ROWS = 1_000
BATCH_BYTES = 32 * 1024 * 1024


@dataclass
class Report:
    """What a copy did: the check's findings, how many postings were pointed at their current copy, how many roles had
    their stored order written, how long it took, and the noise copies it left behind."""

    verified: verify.Verified
    repointed: int
    settled: int
    seconds: float
    left: noise.Noise = field(default_factory=noise.Noise)

    @property
    def ok(self) -> bool:
        return not self.verified.problems


def _open(path: Path) -> sa.Engine:
    """The new file as the load sees it: foreign keys off (the check looks for strays after), and no sync, since a copy
    that is cut short is deleted."""
    engine = sa.create_engine(f"sqlite:///{path.as_posix()}")

    @event.listens_for(engine, "connect")
    def pragmas(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=OFF")
            cursor.execute("PRAGMA foreign_keys=OFF")
        finally:
            cursor.close()

    return engine


def build(path: Path) -> sa.Engine:
    """Make the new file: every table and index from the models, stamped at head, the triggers dropped for the load."""
    from app.jobsearch.pipeline import queue_triggers

    engine = _open(path)
    config = Config(str(db.BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(db.BACKEND_DIR / "migrations"))
    with engine.begin() as connection:
        db.create_schema(connection)
        MigrationContext.configure(connection).stamp(ScriptDirectory.from_config(config), "head")
        for name, _statement in queue_triggers.TRIGGERS:
            connection.exec_driver_sql(f"DROP TRIGGER IF EXISTS {name}")
    return engine


def finish(engine: sa.Engine) -> None:
    """After the load: the triggers are made again, the planner's statistics are made (`ANALYZE`: a file without them
    is planned by the owner index, a call a second instead of milliseconds, and the app's settle below reads that
    way), and the write-ahead log is folded into the file."""
    from app.jobsearch.pipeline import queue_triggers

    with engine.begin() as connection:
        queue_triggers.make(connection)
    with engine.connect() as connection:
        connection.exec_driver_sql("ANALYZE")
        connection.commit()
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")


async def stream(source: Any, one: TablePlan, engine: sa.Engine) -> int:
    """Copy one table: read it as `source`'s snapshot has it, write it in batches, return the rows written."""
    converters = [convert.stored(column) for column in one.read]
    names = [column.name for column in one.read]
    days = [(convert.stored(column), names.index(origin)) for column, origin in one.days.items()]
    statement, written, batch, size = one.insert(), 0, [], 0
    with engine.connect() as connection:
        async for record in source.cursor(one.select(), prefetch=one.prefetch):
            values = tuple(record.values())
            if one.leaving is not None:
                kept = one.leaving(list(values))
                if kept is None:
                    continue
                values = tuple(kept)
            row = [put(value) for put, value in zip(converters, values, strict=True)]
            row += [put(convert.day_of(values[at])) for put, at in days]
            batch.append(tuple(row))
            size += sum(len(v) for v in row if isinstance(v, str | bytes))
            if len(batch) >= BATCH_ROWS or size >= BATCH_BYTES:
                connection.exec_driver_sql(statement, batch)
                connection.commit()
                written, batch, size = written + len(batch), [], 0
        if batch:
            connection.exec_driver_sql(statement, batch)
            connection.commit()
            written += len(batch)
    return written


async def settle(path: Path) -> tuple[int, int]:
    """On the finished file, through the app's own engine: point every posting at its current copy
    (`raw_copies.repoint`) and work out the stored order of every role (`stored_order.settle`). Returns how many
    postings were pointed and how many roles were written."""
    from app.jobsearch.models import JobPosting, Opportunity
    from app.jobsearch.pipeline import stored_order
    from app.jobsearch.postings import raw_copies

    engine = db.configure(db.sqlite_url(path))
    try:
        # These are the app's own whole-table passes: they run as background work (a long pass would hit the
        # foreground limit and the copy would fail).
        with db.background():
            async with db.session_factory()() as session:
                owners = (await session.scalars(sa.select(JobPosting.owner).distinct())).all()
                pointed = sum([await raw_copies.repoint(session, owner) for owner in owners])
                await session.commit()
            async with db.session_factory()() as session:
                owners = (await session.scalars(sa.select(Opportunity.owner).distinct())).all()
                written = sum([await stored_order.settle(session, owner, everything=True) for owner in owners])
                await session.commit()
    finally:
        await engine.dispose()
    return pointed, written


async def connect(url: str) -> Any:
    """An asyncpg connection to the old database. Its address is said in an error, its password never."""
    parts = make_url(url)
    if not parts.drivername.startswith("postgresql"):
        raise CopyError("--from takes a postgresql:// address")
    try:
        connection = await asyncpg.connect(host=parts.host, port=parts.port, user=parts.username,
                                           password=parts.password, database=parts.database)
        # The snapshot is idle on the server while a batch is written to the file, and a long copy runs for minutes: a
        # limit the role or the database sets for the app's own connections must not end it.
        await connection.execute("SET statement_timeout = 0; SET idle_in_transaction_session_timeout = 0")
        return connection
    except (OSError, asyncpg.PostgresError) as exc:
        raise CopyError(f"cannot reach the old database at {parts.host}:{parts.port}/{parts.database} "
                        f"({type(exc).__name__})") from exc


def remove(path: Path) -> None:
    """A copy that failed part way is no database: its file and its log are deleted."""
    for suffix in ("", "-wal", "-shm", "-journal"):
        with contextlib.suppress(OSError):
            Path(f"{path}{suffix}").unlink()


async def copy(source_url: str, target: Path, say: Callable[[str], None] = lambda _text: None) -> Report:
    """Copy the old database into a new SQLite file at `target` and check the copy (see the module's text). Raises
    `CopyError` for a stop it can name; the file is deleted when it does. `say` is told each step as it begins."""
    started = time.monotonic()
    target = Path(target)
    if target.exists():
        raise CopyError(f"{target} is there already: the copy makes a new file and never writes into an old one")
    connection = await connect(source_url)
    try:
        async with connection.transaction(isolation="repeatable_read", readonly=True):
            plans = await plan.make(connection)
            say("finding the noise copies of postings")
            left = await noise.find(connection)
            for one in plans:
                one.leaving = left.row(one.name, [c.name for c in one.read])
            try:
                say("making the new file")
                engine = build(target)
                try:
                    for one in plans:
                        say(f"copying {one.name}")
                        await stream(connection, one, engine)
                    finish(engine)
                finally:
                    engine.dispose()
                say("pointing postings at their current copy and working out the stored order")
                pointed, written = await settle(target)
                say("checking the copy")
                verified = await verify.verify(connection, target, plans, left)
            except BaseException:
                remove(target)
                raise
    finally:
        await connection.close()
    return Report(verified, pointed, written, time.monotonic() - started, left)
