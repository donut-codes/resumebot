"""The app's database: its own engine and sessions, one metadata, and Alembic for the schema.

The app's tables sit on `app.database.Base` (the job-search tables, the agent, hand-out, task, notification and trail
tables). The process engine package keeps its tables on its own `process_engine.base.Base`; of those this app owns the
four live-run tables (instances, tokens, waits, history), the files runs started on and the owner's versions of the
process files. The engine's shadow, mode and gate tables are not this app's: the migrations never create them.

The database is SQLite in WAL mode: every connection is opened with
the PRAGMAs below and works under the writer turn (`_guard`): one writer at a time for a file, queued in the app, and a
statement that runs past its limit is interrupted. A call that only reads takes a reader's session (`ReadSession`,
`read_session_factory`): it takes no turn and never waits for a writer, and a write in it raises (`WriteInReader`).
The schema is Alembic's (`backend/migrations/`): `init_schema` runs `upgrade head` and is safe on every start. An empty
file is not migrated from the first migration (those are Postgres-era and never run here): it is made from the models
by `create_schema` and stamped at head, and only migrations written after that run on it.
"""

import asyncio
import contextlib
import contextvars
import functools
import json
import logging
import re
import sqlite3
import sys
import time
import traceback
import weakref
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Iterable, Iterator
from contextvars import ContextVar
from pathlib import Path
from typing import Annotated, Any

from alembic import command
from alembic.config import Config
from fastapi import Depends
from sqlalchemy import String, Uuid, bindparam, event, func, inspect, select, text
from sqlalchemy.dialects import sqlite
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as PoolTimeout
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session as SyncSession
from sqlalchemy.sql.base import Executable
from sqlalchemy.types import TypeEngine
from sqlalchemy.util import await_only

from app import program
from app.config import settings as base_settings
from app.errors import Busy
from app.settings import settings

log = logging.getLogger(__name__)

_engine: AsyncEngine | None = None
_sessions: async_sessionmaker[AsyncSession] | None = None
_read_sessions: async_sessionmaker[AsyncSession] | None = None

#: The tables this app owns and Alembic manages. (The process engine's shadow, mode and gate tables share the metadata
#: but are not made here.)
OWNED_TABLES = frozenset({
    "js_opportunities", "js_search_runs", "profile_versions", "js_packets", "js_employers",
    "js_missions", "js_mission_runs", "js_mission_changes", "js_stage_moves",
    "js_postings", "js_parse_jobs", "js_role_tag_jobs", "js_sites", "js_site_skills", "projects", "plans", "plan_items",
    "plan_tasks", "review_reports", "lessons", "js_check_jobs", "js_fits_none_ok", "js_instruction_files",
    "js_gap_runs", "js_gap_drafts", "js_gap_answers", "agent_runners", "agent_kind_pauses",
    "proc_instances", "proc_tokens", "proc_waits", "proc_history", "proc_files", "proc_versions",
    "agent_roles", "agent_connections", "task_handouts", "tasks", "notifications", "trail_events",
    "js_browser_items", "js_browser_attempts", "js_browser_domains", "js_cost_nights", "js_agent_spend", "js_go_runs",
    "js_story_library", "js_seat_runs", "js_structured_records", "js_raw_postings", "js_cleansed_postings",
    "js_role_ranks", "js_site_recordings", "js_site_recipes", "js_hand_outs",
    "js_interviews", "js_interview_preps", "js_employer_list",
    "js_open_tabs", "js_action_runs", "js_mail_messages", "js_mail_logins",
    "js_gap_cards", "js_gap_not_true", "js_gap_coach_jobs", "js_proposals", "js_todos", "js_queue_dirty",
    "js_engine_facts",
})

#: backend/ (the folder with alembic.ini and migrations/), from backend/app/db.py.
BACKEND_DIR = Path(__file__).resolve().parents[1]


def database_url() -> str:
    """The database this install uses: JOBSEARCH_DATABASE_URL (tests: DATABASE_URL). Neither set is a setup mistake, so
    the start stops with that said, rather than reaching for a database on localhost."""
    url = settings.database_url or base_settings.database_url
    if not url:
        raise RuntimeError("no database URL: set JOBSEARCH_DATABASE_URL")
    return url


# --- the limits every connection works under --------------------------------------------------------------------
#
# The app can freeze in one way: one long transaction holds what every other call
# needs (the browser mission's row, the scout's connection), the others queue behind it, each holding a pooled
# connection, until the pool is gone and even /health cannot answer. SQLite has one writer, so the same
# shape would end the same way; these limits make it end by itself:
#
# - the writer's turn is waited for a few seconds at most (`LOCK_TIMEOUT_MS`): the waiter is told the app is busy
#   (`Busy`) and lets its connection go, instead of joining a queue that holds the pool;
# - a statement of a request or a tool call runs for a bounded time (`STATEMENT_TIMEOUT_MS`), enforced by interrupting
#   the connection, and the app's own background passes (`background`) get longer limits;
# - a free pooled connection is waited for briefly (`POOL_WAIT_SECONDS`), then the call is answered `Busy`;
# - /health and the agents' heartbeats use a small pool of their own (`lifeline_session_factory`) with shorter limits,
#   so a busy app never looks dead.

#: How long one of a request's or a tool call's transactions may wait for its turn to write, and one statement run.
LOCK_TIMEOUT_MS = 5_000
STATEMENT_TIMEOUT_MS = 30_000
#: The same for the app's own background passes (`background`): longer, never unbounded.
BACKGROUND_LOCK_TIMEOUT_MS = 20_000
BACKGROUND_STATEMENT_TIMEOUT_MS = 300_000
#: The shared pool, and how long a call waits for a free connection in it before it is answered `Busy`. It is sized for
#: the work that really is in flight at once: a call holds a connection only while it runs a statement or a short
#: transaction (a wait on a row lock is made in the app first, `browser_pace.lock_mission`, and pages are read with none
#: open), and in the busiest hour that is the night's read and its steps, the two code-first workers, up to ten browser
#: sessions' calls, the five lanes' tool calls, the schedule's and housekeeping's passes and the owner's page loads.
#: 10 + 10 covers them with room, and with the lifeline pool (4) stays far under Postgres's 100 connections.
POOL_SIZE, POOL_OVERFLOW, POOL_WAIT_SECONDS = 10, 10, 10
#: The lifeline pool (/health, heartbeats): small, quick and strict.
LIFELINE_SIZE, LIFELINE_OVERFLOW, LIFELINE_WAIT_SECONDS = 2, 2, 5
LIFELINE_LOCK_TIMEOUT_MS, LIFELINE_STATEMENT_TIMEOUT_MS = 3_000, 5_000

#: SQLSTATEs that mean "busy for a moment": a lock not granted in time (55P03), a statement past its time (57014).
#: (the Postgres SQLSTATEs, kept while the Postgres copy tool exists.)
BUSY_STATES = frozenset({"55P03", "57014"})
#: The SQLite result codes that mean the same, from `sqlite_errorcode` (its low byte): SQLITE_BUSY, SQLITE_LOCKED and
#: SQLITE_INTERRUPT (a statement stopped at its limit).
SQLITE_BUSY, SQLITE_LOCKED, SQLITE_INTERRUPT = 5, 6, 9

_lifeline_engine: AsyncEngine | None = None
_lifeline_sessions: async_sessionmaker[AsyncSession] | None = None


def sqlite_url(path: Path) -> str:
    """The URL of a SQLite file for this app's engines."""
    return f"sqlite+aiosqlite:///{path.as_posix()}"


def _pragmas(busy_ms: int) -> Callable[[Any, Any], None]:
    """What every new connection is told: no transaction of the driver's own (`isolation_level = None`: the driver
    would begin one only before a write, so a savepoint opened after reads alone is the outermost transaction and its
    release commits everything; `_guard` begins each transaction itself), WAL (readers never wait for the writer),
    foreign keys on (SQLite leaves them off unless asked on each connection), a lock held by another process waited for
    `busy_ms` before the call is answered busy, and a sync at each checkpoint rather than each commit (safe in WAL: a
    power cut can lose the last commit, never the file)."""

    def on_connect(dbapi_connection: Any, record: Any) -> None:
        dbapi_connection.isolation_level = None
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            if program.PROGRAM != "page":  # the page never analyses: it can ANALYZE under the write lock
                optimize(cursor, OPTIMIZE_OPEN)
            cursor.execute(f"PRAGMA busy_timeout={int(busy_ms)}")
        finally:
            cursor.close()
        if record is not None:  # what `_busy_for` compares against, kept in the pooled connection's info
            record.info[_BUSY_BASE] = record.info[_BUSY_NOW] = int(busy_ms)

    return on_connect


#: `PRAGMA optimize` as SQLite's documentation advises an application: at the open of a long-lived connection, and just
#: before it closes (`_optimize_at_close`). The mask 0x10002 (SQLite 3.46) is "analyse what the planner would use, and
#: look at every table, not only those this connection has queried": a file with no `sqlite_stat1` (a copy, a new
#: install whose tables then grew) is planned by the owner index at a second a call instead of by the id index at
#: milliseconds. Older SQLite takes the plain `optimize`.
#: The page program (`app.program`) never says it, at open or at close; the engine and role `all` do.
OPTIMIZE_OPEN = "PRAGMA optimize=0x10002" if sqlite3.sqlite_version_info >= (3, 46) else "PRAGMA optimize"
OPTIMIZE_CLOSE = "PRAGMA optimize"


def optimize(cursor: Any, statement: str) -> None:
    """Say `statement` (a `PRAGMA optimize`) on `cursor` without ever waiting: its analysis writes `sqlite_stat1`, and a
    writer holding the file answers busy at once, so the connection opens (or closes) and the next one does it."""
    try:
        cursor.execute("PRAGMA busy_timeout=0")
        cursor.execute(statement)
        cursor.fetchall()
    except Exception:  # sqlite3.Error, or the driver's own when the connection is already going away
        log.debug("%s did not run", statement, exc_info=True)


def _optimize_at_close(dbapi_connection: Any, _record: Any) -> None:
    if program.PROGRAM == "page":
        return
    try:
        cursor = dbapi_connection.cursor()
        try:
            optimize(cursor, OPTIMIZE_CLOSE)
        finally:
            cursor.close()
    except Exception:  # a connection already going away: the pool is closing it, and that is all that matters
        log.debug("the connection could not say %s", OPTIMIZE_CLOSE, exc_info=True)


def _new_engine(url: str, size: int, overflow: int, wait: int, busy_ms: int, *, lifeline: bool = False) -> AsyncEngine:
    if not url.startswith("sqlite+aiosqlite:"):
        raise RuntimeError("the app's database is SQLite: set JOBSEARCH_DATABASE_URL to a sqlite+aiosqlite URL")
    made = create_async_engine(url, pool_pre_ping=True, pool_size=size, max_overflow=overflow, pool_timeout=wait)
    event.listen(made.sync_engine, "connect", _pragmas(busy_ms))
    event.listen(made.sync_engine, "close", _optimize_at_close)
    _guard(made.sync_engine, str(made.url.database), _lifeline_limits if lifeline else _limits)
    return made


def configure(url: str | None = None) -> AsyncEngine:
    """Point the app at a database (the start does it from the environment; a test hands its own). Replaces any
    earlier engine, and the lifeline engine with it."""
    global _engine, _sessions, _read_sessions, _lifeline_engine, _lifeline_sessions
    from app.jobsearch.work.queue import hook  # noqa: F401 - registers the work queue's session listeners
    url = url or database_url()
    _engine = _new_engine(url, POOL_SIZE, POOL_OVERFLOW, POOL_WAIT_SECONDS, LOCK_TIMEOUT_MS)
    _sessions = async_sessionmaker(_engine, expire_on_commit=False, sync_session_class=AppSession)
    # The same pool and the same turn, with every transaction a reader's (`READ_ONLY` as the engine's own option, so a
    # session that commits and goes on is a reader still).
    _read_sessions = async_sessionmaker(_engine.execution_options(**READ_ONLY), expire_on_commit=False,
                                        sync_session_class=AppSession)
    _lifeline_engine = _new_engine(url, LIFELINE_SIZE, LIFELINE_OVERFLOW, LIFELINE_WAIT_SECONDS,
                                   LIFELINE_LOCK_TIMEOUT_MS, lifeline=True)
    _lifeline_sessions = async_sessionmaker(_lifeline_engine, expire_on_commit=False, sync_session_class=AppSession)
    return _engine


def engine() -> AsyncEngine:
    return _engine or configure()


def session_factory() -> async_sessionmaker[AsyncSession]:
    engine()
    assert _sessions is not None
    return _sessions


def read_session_factory() -> async_sessionmaker[AsyncSession]:
    """Sessions on the shared pool whose every transaction is a reader's (`read_only`): they take no turn and, in WAL,
    never wait for the writer, now or after a commit (`release`). For a call that never writes (`ReadSession`, a tool
    registered `reads=True`); a write in one is a bug (`WriteInReader`)."""
    engine()
    assert _read_sessions is not None
    return _read_sessions


def lifeline_session_factory() -> async_sessionmaker[AsyncSession]:
    """Sessions on the lifeline pool: for /health and the agents' heartbeats only, so they answer while the shared pool
    is busy. Its transactions wait 3 seconds for the writer's turn and run a statement 5 seconds at most."""
    engine()
    assert _lifeline_sessions is not None
    return _lifeline_sessions


async def get_session() -> AsyncIterator[AsyncSession]:
    async with session_factory()() as session:
        yield session


async def get_read_session() -> AsyncIterator[AsyncSession]:
    async with read_session_factory()() as session:
        yield session


async def get_lifeline_session() -> AsyncIterator[AsyncSession]:
    async with lifeline_session_factory()() as session:
        yield session


#: A route's session: `async def route(session: Session)`. The route commits; services only flush.
Session = Annotated[AsyncSession, Depends(get_session)]
#: A route's session for a request that never writes (a GET that only shows): its transactions are a reader's, take no
#: turn and never wait for a writer (`read_session_factory`). A route that must write takes `Session`, or its own
#: session for the small write.
ReadSession = Annotated[AsyncSession, Depends(get_read_session)]
#: A heartbeat route's session, on the lifeline pool (`lifeline_session_factory`).
LifelineSession = Annotated[AsyncSession, Depends(get_lifeline_session)]


async def ping(seconds: float = 3.0) -> bool:
    """Whether the database answers a plain query on the lifeline pool within `seconds` (/health)."""
    try:
        async with asyncio.timeout(seconds), lifeline_session_factory()() as session:
            await read_only(session)  # a reader takes no turn: /health answers while a writer holds it
            await session.execute(text("SELECT 1"))
        return True
    except Exception:  # any failure means the same thing here
        return False


@functools.cache
def code_head() -> str:
    """The newest migration the code ships (Alembic's single head), read from the scripts once. It reads files, not the
    database."""
    from alembic.script import ScriptDirectory

    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    head = ScriptDirectory.from_config(cfg).get_current_head()  # typed `str | None` in Alembic
    if head is None:
        raise RuntimeError("the migration scripts have no head")
    return head


async def schema_is_current(seconds: float = 3.0) -> bool:
    """Whether the database is at the code's newest migration: its `alembic_version` holds the code's head and nothing
    else. A read on the lifeline pool, in a reader's transaction: it takes no turn of the file's one writer, creates and
    changes nothing, and is safe from the page program while the engine migrates. False when the table is missing (a
    file the engine has not set up), when the revision differs (behind, or ahead of this code), or when the database
    does not answer within `seconds`. `init_schema` is what migrates, and only the engine calls it."""
    try:
        head = await asyncio.to_thread(code_head)
        async with asyncio.timeout(seconds), lifeline_session_factory()() as session:
            await read_only(session)
            found = (await session.execute(text("SELECT version_num FROM alembic_version"))).scalars().all()
        return list(found) == [head]
    except Exception:  # any failure means "not yet" here
        return False


# --- busy: a lock wait or a statement past its limit, or no free connection --------------------------------------


def is_busy(exc: BaseException | None) -> bool:
    """Whether an error means the database is busy for a moment (the writer's turn not given within its wait, a
    statement past its limit, no pooled connection free in time, or SQLite itself answering busy or locked because
    another process holds the file) rather than a fault. Follows the wrapped causes."""
    seen = 0
    while exc is not None and seen < 8:
        if isinstance(exc, PoolTimeout | Busy):
            return True
        if (getattr(exc, "sqlstate", None) or getattr(exc, "pgcode", None)) in BUSY_STATES:
            return True
        if ((getattr(exc, "sqlite_errorcode", None) or 0) & 0xFF) in (SQLITE_BUSY, SQLITE_LOCKED, SQLITE_INTERRUPT):
            return True
        exc = getattr(exc, "orig", None) or exc.__cause__ or exc.__context__
        seen += 1
    return False


async def try_lock(session: AsyncSession, statement: Executable) -> bool:
    """Take the row lock(s) `statement` asks for (a SELECT ... FOR UPDATE), in a savepoint: True when taken (held to the
    end of the transaction), False when another transaction held them past the lock wait. The caller then skips its
    turn (hands out nothing, says to wait) and its transaction goes on, so no request errors and none queues."""
    try:
        async with session.begin_nested():
            await session.execute(statement)
    except (DBAPIError, Busy) as exc:
        if not is_busy(exc):
            raise
        log.info("a row lock was busy, so this turn is skipped: %s", statement)
        return False
    return True


async def lock_or_busy(session: AsyncSession, statement: Executable, why: str) -> None:
    """`try_lock`, raising `Busy(why)` when the lock is busy: for a call that must not go on without the row."""
    if not await try_lock(session, statement):
        raise Busy(why)


# --- a list of ids as one argument -------------------------------------------------------------------------------


_STORING = sqlite.dialect()


def json_ids(ids: Iterable[object], kind: TypeEngine | None = None) -> str:
    """The ids as one JSON array, each written as the column stores it (`kind`, a uuid column's by default: a uuid is
    stored as its 32 hex digits, a time as its UTC text), for `json_each`: the one argument of a statement for any
    number of ids. A statement of text asks `WHERE id IN (SELECT value FROM json_each(:ids))`; `among` is for a
    column."""
    process = (kind or Uuid()).dialect_impl(_STORING).bind_processor(_STORING)
    return json.dumps([process(i) if process else i for i in ids])


def among(column, ids: Iterable[object]):
    """`column IN (SELECT value FROM json_each(:ids))`: the ids as one JSON argument, so the statement carries one
    argument for any number of ids. An `IN` list carries one for each id, and a database holds a statement to a fixed
    number of arguments (on Postgres, a query over every role that has a copy failed for want of them). The answer is
    the same as for the list. For a list that grows with the owner's data; a short, fixed list may stay an `IN`. The ids
    are written as the column stores them (`json_ids`): a uuid column holds its hex digits, which `json_each` gives back
    as the text they are."""
    each = func.json_each(bindparam("ids", json_ids(ids, column.type), type_=String, unique=True)).table_valued("value")
    return column.in_(select(each.c.value))


# --- background work: longer limits, set on each of its transactions ---------------------------------------------

_background: ContextVar[bool] = ContextVar("db_background", default=False)


@contextlib.contextmanager
def background() -> Iterator[None]:
    """The app's own background work (the schedule, the pipeline's turn, the code-first browser worker, a night's read,
    the start's repairs): its transactions wait `BACKGROUND_LOCK_TIMEOUT_MS` for the writer's turn and its statements
    run `BACKGROUND_STATEMENT_TIMEOUT_MS`. Tasks started inside it inherit it."""
    token = _background.set(True)
    try:
        yield
    finally:
        _background.reset(token)


def background_work[**P, R](fn: Callable[P, Awaitable[R]]) -> Callable[P, Coroutine[Any, Any, R]]:
    """Run a coroutine function as background work (`background`), wherever it is started from."""

    @functools.wraps(fn)
    async def run(*args: P.args, **kwargs: P.kwargs) -> R:
        with background():
            return await fn(*args, **kwargs)

    return run


def in_background() -> bool:
    return _background.get()


# --- named locks: one holder at a time for a name, in this process ----------------------------------------------

#: A lock for each name, with the loop it was made on (a new loop, a test's, begins with free locks).
_named: dict[str, tuple[asyncio.AbstractEventLoop, asyncio.Lock]] = {}
#: In `session.info`: the named locks a session holds until its transaction ends, by name.
_HELD = "db_named_locks"
_LISTENING = "db_named_locks_listening"


def lock_wait_seconds() -> float:
    """How long a call waits for a lock held by another, inside a transaction, before it is told busy: the writer's
    turn too (`LOCK_TIMEOUT_MS`, `BACKGROUND_LOCK_TIMEOUT_MS` for the app's background work)."""
    return (BACKGROUND_LOCK_TIMEOUT_MS if _background.get() else LOCK_TIMEOUT_MS) / 1000


def _lock_named(key: str) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    held = _named.get(key)
    if held is None or held[0] is not loop:
        held = _named[key] = (loop, asyncio.Lock())
    return held[1]


async def _take(lock: asyncio.Lock, wait: float | None) -> bool:
    """Whether `lock` was taken within `wait` seconds: None waits as long as it takes, 0 takes it only if it is free."""
    if wait is None:
        await lock.acquire()
        return True
    try:
        async with asyncio.timeout(wait):  # a free lock is taken without waiting, so 0 is a try
            await lock.acquire()
    except TimeoutError:
        return False
    return True


@contextlib.asynccontextmanager
async def named_lock(key: str, *, wait: float | None = None, why: str | None = None) -> AsyncIterator[None]:
    """A lock for the name `key`, held for the block: one holder at a time in this process, which holds the database
    (what an advisory lock did across connections). The block may commit, release and open sessions of its own: it holds
    no transaction itself, which is why `wait` may be as long as it takes (the default) and the caller holds nothing
    while it waits. With `wait` seconds, a caller that did not get the lock in that time is told `Busy(why)`; 0 asks
    only for a lock that is free now. For a lock that lasts to the end of a transaction, `transaction_lock`."""
    lock = _lock_named(key)
    if not await _take(lock, wait):
        raise Busy(why or "another call holds this one's turn; nothing was changed, call again in a few seconds")
    try:
        yield
    finally:
        lock.release()


def _let_go_named(session: SyncSession, transaction: Any) -> None:
    """The session's outermost transaction ended: its named locks are let go (a savepoint's end is not that)."""
    if transaction.parent is not None:
        return
    for lock in (session.info.pop(_HELD, None) or {}).values():
        lock.release()


async def transaction_lock(session: AsyncSession, key: str, *, wait: float | None = None) -> bool:
    """The named lock `key`, held until `session`'s transaction ends (a commit, a rollback or closing the session; a
    savepoint's end does not let it go), as Postgres's transaction-level advisory lock was. True when taken; False
    when another session held it past `wait` seconds (default: the lock wait, `lock_wait_seconds`; 0 asks only for a
    lock that is free now), and the caller then skips its turn. A session that holds it takes it again without
    waiting. Taking it begins the session's transaction, which is what the lock lasts to (and a transaction that may
    write holds the writer's turn: two sessions that hold transaction locks are not both in a transaction at once,
    whatever the names, unless one is a reader's: `read_only`).

    The turn first, the lock second: a session whose transaction has no connection yet (`_connected`) is given the
    lock only once it has one, and so, for a writer, the turn. It holds no lock while it waits for its turn, and one
    refused its turn is told `Busy` and holds none. (A lock held without the turn is two calls waiting on each other:
    this one for the turn, the turn's holder for the lock. Seen live, when a start report whose first savepoint had been
    refused the turn was handed `browser_mode:<owner>`.)

    A wait inside a transaction is never as long as it takes: a queue of them behind one slow holder, each holding a
    pooled connection, is what freezes the app. For a wait of that kind take `named_lock` before the
    transaction begins, as the re-gate does. `transaction_lock_or_busy` is this for a call that must not go on without
    it."""
    held: dict[str, asyncio.Lock] = session.info.setdefault(_HELD, {})
    if key in held:
        return True
    lock = _lock_named(key)
    limit = lock_wait_seconds() if wait is None else wait
    taken = await _take(lock, limit)
    if taken and not _connected(session):
        # The lock is free (or came free within the wait), and the session has no turn: it lets the lock go, takes its
        # turn (the transaction the lock lasts to), and only then the lock.
        lock.release()
        await session.connection()
        taken = await _take(lock, limit)
    if not taken:
        if wait != 0:
            log.info("the lock %s was busy past the lock wait, so this turn is skipped", key)
        return False
    held[key] = lock
    if not session.info.get(_LISTENING):
        session.info[_LISTENING] = True
        event.listen(session.sync_session, "after_transaction_end", _let_go_named)
    return True


def _connected(session: AsyncSession) -> bool:
    """Whether `session`'s transaction has its connection, and with it, for a writer, the turn. `in_transaction()` does
    not say so: a transaction begun by a savepoint (`begin_nested`) whose first statement was refused the turn is open
    with no connection and no turn."""
    root: Any = session.sync_session.get_transaction()
    return root is not None and bool(root._connections)


async def transaction_lock_or_busy(session: AsyncSession, key: str, why: str, *, wait: float | None = None) -> None:
    """`transaction_lock`, raising `Busy(why)` when the lock is busy: for a call that must not go on without it. (One
    refused the writer's turn is told `Busy` in the turn's own words.)"""
    if not await transaction_lock(session, key, wait=wait):
        raise Busy(why)


# --- the writer turn: one writer at a time for a file, and a statement runs for a bounded time -------------------

#: What a reader's connection is asked for (`read_only`), as an execution option.
READ_ONLY = {"db_read_only": True}
#: A statement whose verb is one of these writes. The verb is the statement's first word, or, after a `WITH`, the word
#: that follows its common table expressions (`_verb`).
WRITES = ("INSERT", "UPDATE", "DELETE", "REPLACE", "CREATE", "DROP", "ALTER", "VACUUM", "PRAGMA =")
_VERBS = frozenset({"SELECT", "VALUES", "INSERT", "UPDATE", "DELETE", "REPLACE"})
_WORD = re.compile(r"\s+|--[^\n]*|/\*.*?\*/|'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"|`[^`]*`|\[[^\]]*\]|(\w+)|(.)", re.S)


def _verb(statement: str) -> str:
    """The upper-case verb of a statement: its first word, or for a `WITH` the first word outside every bracket that
    is one of `_VERBS` (after the common table expressions), so a CTE-led INSERT, UPDATE or DELETE is seen. A writing
    PRAGMA (one that sets a value with `=`) is named `PRAGMA =`."""
    depth = 0
    first = ""
    for match in _WORD.finditer(statement):
        word, mark = match.group(1), match.group(2)
        if mark:
            depth += (mark == "(") - (mark == ")")
            continue
        if not word:
            continue
        word = word.upper()
        if not first:
            first = word
            if first == "PRAGMA":
                return "PRAGMA =" if "=" in statement else first
            if first != "WITH":
                return first
        elif depth == 0 and word in _VERBS:
            return word
    return first
#: A write made in a reader's transaction raises (`WriteInReader`) instead of being logged once for each statement. The
#: app's test suite turns it on for every test (`tests/conftest.py`), so no GET that writes can land unseen.
strict_readers = False
#: Each `WriteInReader` raised while `strict_readers` is on: the tests fail on one even when the code that met it
#: caught it.
reader_wrote: list[str] = []
_reader_logged: set[str] = set()


class WriteInReader(RuntimeError):
    """A statement that writes was begun in a reader's transaction (`read_only`, `ReadSession`): it holds no turn, so
    it would race the writers (SQLITE_BUSY_SNAPSHOT) instead of taking its turn."""


def _reader_wrote(statement: str) -> None:
    said = " ".join(statement.split())[:160]
    if strict_readers:
        reader_wrote.append(said)
        raise WriteInReader(f"a write in a reader's transaction, which takes no turn: {said}")
    if said not in _reader_logged:
        _reader_logged.add(said)
        log.warning("a write in a reader's transaction (it holds no turn, so it may fail busy): %s", said)
#: Called with the seconds a transaction waited for its turn (taken or not) each time one asks: for
#: tests and for timing the writers' queue.
turn_watchers: list[Callable[[float], None]] = []
#: Called once for each writer's turn when it is over, with the seconds it waited and the seconds it held the turn
#: (None when it was told `Busy` and held nothing): the measurement of the 250 ms write-wait target (`turn_log`).
turn_ends: list[Callable[[float, float | None], None]] = []
#: In a pooled connection's `info` (it outlives each checkout): the turn the connection holds, and the timer that will
#: interrupt the statement it is running.
_TURN = "db_turn"
_TIMER = "db_statement_timer"
#: In the same `info`: (seconds the turn waited, when it was taken) for `turn_ends`.
_TURN_FACTS = "db_turn_facts"
#: Set to `module:function:line` of the code that took the turn while `turn_ends` is being called (None otherwise): the
#: callbacks keep their two arguments and read it here (`turn_log`).
ending_place: str | None = None
_NOT_A_PLACE = ("app.db", "app.jobsearch.as_read", "app.turn_log", "sqlalchemy", "asyncio", "greenlet", "contextlib")
#: In the same `info`: the connection's own busy wait in ms (set at connect) and the one it has now (`_busy_for`).
_BUSY_BASE = "db_busy_base_ms"
_BUSY_NOW = "db_busy_now_ms"
#: A writer that waited less than this for its turn in the program keeps the connection's full wait for the file.
TURN_WAIT_COUNTED = 0.25


def _limits() -> tuple[float, float]:
    """(seconds to wait for the writer's turn, seconds a statement may run) of a request, a tool call or, in
    `background`, one of the app's own passes."""
    if _background.get():
        return BACKGROUND_LOCK_TIMEOUT_MS / 1000, BACKGROUND_STATEMENT_TIMEOUT_MS / 1000
    return LOCK_TIMEOUT_MS / 1000, STATEMENT_TIMEOUT_MS / 1000


def _lifeline_limits() -> tuple[float, float]:
    """The same for the lifeline engine: its own, short."""
    return LIFELINE_LOCK_TIMEOUT_MS / 1000, LIFELINE_STATEMENT_TIMEOUT_MS / 1000


def _place_of_turn() -> str | None:
    """Where the turn is being taken: the first frame outside this file, `turn_log`, SQLAlchemy, asyncio and
    contextlib, as `module:function:line`. SQLAlchemy runs its sync code in a greenlet, so the task's own frames are
    those of the greenlet's parent (the stack above this one is only SQLAlchemy's). A short walk of `f_back`; None
    when nothing is found."""
    from greenlet import getcurrent

    here = getcurrent()
    frames = [sys._getframe(1)]
    if here.parent is not None and here.parent.gr_frame is not None:
        frames.append(here.parent.gr_frame)
    for frame in frames:
        while frame is not None:
            name = str(frame.f_globals.get("__name__", ""))
            if not any(name == m or name.startswith(m + ".") for m in _NOT_A_PLACE):
                return f"{name}:{frame.f_code.co_name}:{frame.f_lineno}"
            frame = frame.f_back
    return None


def _end_turn(waited: float, held: float | None, place: str | None) -> None:
    global ending_place
    ending_place = place
    try:
        for end in list(turn_ends):
            end(waited, held)
    finally:
        ending_place = None


def _let_go_turn(info: dict[str, Any] | None) -> None:
    """Let go of the turn a pooled connection holds (nothing when it holds none). Said more than once for a transaction
    (its commit, its rollback and the connection's return to the pool each say it), it lets go once."""
    lock = info.pop(_TURN, None) if info is not None else None
    facts = info.pop(_TURN_FACTS, None) if info is not None else None
    if lock is not None and lock.locked():
        lock.release()
        if facts is not None:
            _end_turn(facts[0], time.perf_counter() - facts[1], facts[2])


def _disarm(info: dict[str, Any] | None) -> None:
    """Cancel the timer of the statement a pooled connection was running."""
    timer = info.pop(_TIMER, None) if info is not None else None
    if timer is not None:
        timer.cancel()


def _busy_for(conn: Connection, limit: float, waited: float) -> None:
    """Before `BEGIN IMMEDIATE`: the wait for the file (`busy_timeout`) is what is left of `limit` after the `waited`
    seconds spent on the program's own turn, never more than the connection's own, and never under 50 ms. A writer that
    waited under `TURN_WAIT_COUNTED` keeps the full wait (no statement is said for it). Another program holding the
    file is what this wait is for (the page and the engine take turns through SQLite's own lock)."""
    info = conn.info
    base = info.get(_BUSY_BASE)
    if base is None:  # an engine not made by `_pragmas`
        return
    want = base if waited < TURN_WAIT_COUNTED else min(base, max(50, int((limit - waited) * 1000)))
    if info.get(_BUSY_NOW) != want:
        conn.exec_driver_sql(f"PRAGMA busy_timeout={want}")
        info[_BUSY_NOW] = want


def _interrupt(driver: Any) -> None:
    """Stop the statement the driver's connection is running (`sqlite3`'s `interrupt`, safe from any thread)."""
    with contextlib.suppress(Exception):  # the connection may have closed meanwhile
        driver._conn.interrupt()


def _guard(sync_engine: Any, path: str, limits: Callable[[], tuple[float, float]]) -> None:
    """Put an engine under the writer turn and the statement limit. `path` is the file: every engine on it (the shared
    pool and the lifeline's) shares one turn. `limits` gives (the wait for the turn, the statement limit) in seconds,
    read each time they apply.

    **The turn.** SQLite has one writer. A transaction that begins as a read and writes later can fail at once with
    SQLITE_BUSY_SNAPSHOT (another writer got in between), so every transaction that may write begins `BEGIN IMMEDIATE`,
    and does so holding one `asyncio.Lock` for the file: the writers queue here, in the app, and none is answered busy
    by SQLite. The lock is taken before the `BEGIN` and let go once the commit or the rollback is done (the dialect's
    `do_commit` and `do_rollback`: the connection's own `commit` event comes before the commit, which would let the next
    writer begin while this one still holds the file). A transaction that is not given its turn within the wait is told
    `Busy` and holds nothing, so a queue behind a slow holder cannot hold the pool (the freeze). A session
    that only reads says so (`read_only`): its transaction is a plain `BEGIN`, takes no turn, and in WAL never waits for
    the writer. No network wait is made while the turn is held: `outside_transaction` raises for it, for any open
    transaction.

    **Two programs.** The page and the engine share the file. Between programs SQLite's own write lock is the only
    turn-taker: a writer that finds it held waits in SQLite's busy handler, in the sqlite thread, for what is left of
    the limit after its wait in this program (`_busy_for`), then fails busy (`database is locked`, which `is_busy`
    accepts).

    **The statement limit.** SQLite has no statement timeout, so a timer is set for each statement and, when it fires
    while the statement still runs, interrupts the connection (`sqlite3`'s `interrupt`, safe from another thread: the
    statement stops with SQLITE_INTERRUPT, and an interrupt that arrives between statements is dropped when the next
    begins). The interrupted statement is raised as `Busy`; nothing was changed by it (an interrupted write rolls the
    transaction back)."""
    key = f"\0turn:{path}"

    @event.listens_for(sync_engine, "begin")
    def begin(conn: Connection) -> None:
        if conn.get_execution_options().get("db_read_only"):
            conn.exec_driver_sql("BEGIN")
            return
        _nested_writer()
        lock = _lock_named(key)
        asked = time.perf_counter()
        limit = limits()[0]
        taken = await_only(_take(lock, limit))
        waited = time.perf_counter() - asked
        for watch in list(turn_watchers):
            watch(waited)
        place = _place_of_turn() if turn_ends else None
        if not taken:
            _end_turn(waited, None, place)
            raise Busy("another call is writing and this one's turn did not come in time; nothing was changed, "
                       "call again in a few seconds")
        conn.info[_TURN] = lock
        conn.info[_TURN_FACTS] = (waited, time.perf_counter(), place)
        try:
            _busy_for(conn, limit, waited)
            conn.exec_driver_sql("BEGIN IMMEDIATE")
        except BaseException:
            _let_go_turn(conn.info)
            raise

    def let_go_after(ended: Callable[[Any], None]) -> Callable[[Any], None]:
        def run(dbapi_connection: Any) -> None:
            try:
                ended(dbapi_connection)
            finally:
                try:
                    info = dbapi_connection.info
                except NotImplementedError:  # the engine's first-connect check, on a connection not yet pooled
                    info = None
                _let_go_turn(info)

        return run

    for name in ("do_commit", "do_rollback"):
        setattr(sync_engine.dialect, name, let_go_after(getattr(sync_engine.dialect, name)))

    @event.listens_for(sync_engine.pool, "checkin")
    def returned(_dbapi_connection: Any, record: Any) -> None:  # whatever way the transaction ended, it is over now
        if record is not None:
            _let_go_turn(record.info)
            _disarm(record.info)

    @event.listens_for(sync_engine, "before_cursor_execute")
    def arm(conn: Connection, _cursor: Any, statement: str, *_: Any) -> None:
        if conn.get_execution_options().get("db_read_only") and _verb(statement) in WRITES:
            _reader_wrote(statement)
        info = conn.info
        _disarm(info)
        driver = conn.connection.driver_connection
        if driver is None:
            return

        def interrupt() -> None:
            info.pop(_TIMER, None)
            _interrupt(driver)

        info[_TIMER] = asyncio.get_running_loop().call_later(limits()[1], interrupt)

    @event.listens_for(sync_engine, "after_cursor_execute")
    def done(conn: Connection, *_: Any) -> None:
        _disarm(conn.info)

    @event.listens_for(sync_engine, "handle_error")
    def interrupted(context: Any) -> Exception | None:
        if context.connection is not None:
            _disarm(context.connection.info)
            if isinstance(context.original_exception, asyncio.CancelledError):
                # The caller gave up on the statement, but the driver's thread is still running it, holding the file's
                # write lock and the turn, and the rollback queues behind it: with its timer disarmed it would run to
                # its end, however long. Stop it now (a connection with nothing running ignores the interrupt).
                _interrupt(context.connection.connection.driver_connection)
        if (getattr(context.original_exception, "sqlite_errorcode", None) or 0) & 0xFF == SQLITE_INTERRUPT:
            return Busy("a statement ran past its time limit and was stopped; nothing was changed, call again in a "
                        "few seconds")
        return None


async def read_only(session: AsyncSession) -> None:
    """Make `session` a reader's: its transaction takes no turn and begins a plain `BEGIN` (a snapshot of the file as it
    was last committed, which a writer holding the turn does not disturb). For a session that reads and never writes
    (/health, a page that only shows); a write in it is the bug it was, and may fail as SQLITE_BUSY_SNAPSHOT. It holds
    for the session's current transaction (its next, after a commit, is a writer's again). Called before the session's
    first statement: its transaction has begun otherwise, and `RuntimeError` says so."""
    if session.in_transaction():
        raise RuntimeError("read_only is said before a session's first statement: its transaction has begun")
    await session.connection(execution_options=READ_ONLY)


def is_reader(session: AsyncSession) -> bool:
    """Whether `session` came from `read_session_factory` (`ReadSession`): every transaction of it is a reader's. The
    board reads the stored order as it stands when it is handed one of these, and settles it first, in the session it
    was handed, when it is not (`row._settle_order`)."""
    return bool(session.get_bind().get_execution_options().get("db_read_only"))


# --- open transactions: how long each is held, and no network I/O inside one ------------------------------------

_BEGAN = "db_began"
#: The sessions this task (and the tasks it started) began a transaction in, as weak references.
_open: ContextVar[tuple[weakref.ref, ...]] = ContextVar("db_open_sessions", default=())
#: Called with (seconds held, session) when a session's transaction ends: the tests' watch on long transactions.
watchers: list[Callable[[float, Any], None]] = []
#: Network I/O inside an open transaction raises (`IoInTransaction`) instead of being logged once per place. The app's
#: test suite turns it on for every test (`tests/conftest.py`), so no new network read inside a transaction can land.
strict_io = False
#: Each `IoInTransaction` raised while `strict_io` is on, with where: the tests fail on one even when the code that met
#: it caught it as a site that did not answer.
io_caught: list[str] = []
_io_logged: set[tuple[str, int]] = set()


class IoInTransaction(RuntimeError):
    """Network I/O was begun while this task held a database transaction open (`strict_io`)."""


#: A writer's transaction begun while this task already holds the writer's turn in another session raises
#: (`NestedWriter`) instead of waiting for a turn that only this task's first transaction could give back, and then
#: being told `Busy`. The app's test suite turns it on for every test (`tests/conftest.py`).
strict_nested = False
#: Each `NestedWriter` raised while `strict_nested` is on, with where: the tests fail on one even when the code that met
#: it caught it.
nested_caught: list[str] = []
_nested_logged: set[tuple[str, int]] = set()
#: In `session.info`: the task whose session's transaction holds the writer's turn (it began `BEGIN IMMEDIATE`).
_HOLDS = "db_holds_turn"


class NestedWriter(RuntimeError):
    """A writer's transaction was begun while this task held the writer's turn in another session: the second waits for
    a turn the first (this same task, which is waiting for the second) would have to give back. Commit or release the
    first before the second begins (`release`), or read in a reader's session (`read_session_factory`)."""


def _awaiting_frames() -> list[tuple[Path, int]]:
    """The (file, line) of the coroutines this task is inside, outermost first. SQLAlchemy runs its sync code in a
    greenlet, whose own stack holds only SQLAlchemy's frames; the task's are those of the greenlet it switches back
    to."""
    from greenlet import getcurrent

    parent = getcurrent().parent
    frame = parent.gr_frame if parent is not None else None
    found: list[tuple[Path, int]] = []
    while frame is not None:
        found.append((Path(frame.f_code.co_filename), frame.f_lineno))
        frame = frame.f_back
    return found[::-1]


def _nested_writer() -> None:
    """A writer is about to ask for its turn: say so when this very task holds it already in another session. (A task
    that inherited the transaction's context from the one that started it is not this task: it waits for the first to
    end, which is not waiting for it, unless the first awaits it, which is not seen here.)"""
    here = asyncio.current_task()
    if here is None:
        return
    holders = [s for r in _open.get() if (s := r()) is not None and s.info.get(_HOLDS) is here]
    if not holders:
        return
    inside = [(name, line) for name, line in _awaiting_frames()
              if BACKEND_DIR in name.parents and name.name != "db.py"]  # outermost first
    site = ("?", 0)
    if inside:
        name, line = inside[-1]
        site = (name.relative_to(BACKEND_DIR).as_posix(), line)
    chain = [f"{name.name}:{line}" for name, line in inside]
    said = f"a second writer inside a writer's transaction (at {site[0]}:{site[1]}; via {' < '.join(chain[::-1])})"
    if strict_nested:
        nested_caught.append(said)
        raise NestedWriter(said)
    if site not in _nested_logged:
        _nested_logged.add(site)
        log.warning("%s: it waits for a turn its own task holds, and is told busy; release or commit the first "
                    "transaction before the second begins (db.release)", said)


class AppSession(SyncSession):
    """The app's sessions: every transaction notes when it began (`watchers`, `in_transaction`). Its limits are the
    engine's (`_guard`)."""


@event.listens_for(AppSession, "after_begin")
def _began(session: SyncSession, transaction: Any, connection: Any) -> None:
    if _BEGAN in session.info:
        return
    session.info[_BEGAN] = time.monotonic()
    if connection.info.get(_TURN) is not None:
        session.info[_HOLDS] = asyncio.current_task()
    live = tuple(r for r in _open.get() if (s := r()) is not None and _BEGAN in s.info)
    _open.set((*live, weakref.ref(session)))


@event.listens_for(AppSession, "after_transaction_end")
def _ended(session: SyncSession, transaction: Any) -> None:
    if transaction.parent is not None:
        return
    session.info.pop(_HOLDS, None)
    began = session.info.pop(_BEGAN, None)
    if began is None:
        return
    held = time.monotonic() - began
    for watch in list(watchers):
        watch(held, session)


def detached() -> contextvars.Context:
    """A copy of this task's context in which it holds no transaction: for a read started to run beside this task's own
    database work (`postings.finished_first`), which holds none of this task's transactions open however long it
    takes. The task that starts it must not wait for it inside a transaction (it releases first: `release`)."""
    context = contextvars.copy_context()
    context.run(_open.set, ())
    return context


def in_transaction() -> bool:
    """Whether this task (or the task that started it) holds a database transaction open now."""
    return any((s := r()) is not None and _BEGAN in s.info for r in _open.get())


#: Why network I/O inside this task's transaction is held knowingly (`io_held_knowingly`), or None.
_known_io: ContextVar[str | None] = ContextVar("db_known_io", default=None)


@contextlib.contextmanager
def io_held_knowingly(why: str) -> Iterator[None]:
    """Inside the block a network read inside the transaction is allowed, for `why`: a path whose transaction must stay
    whole across a read it cannot do first (each use says why, and is a debt). Logged once for each place, never
    raised."""
    token = _known_io.set(why)
    try:
        yield
    finally:
        _known_io.reset(token)


def outside_transaction(what: str) -> None:
    """Network I/O is about to begin (`what`): it must not be inside an open transaction, which would hold its row locks
    and its pooled connection for as long as the far side takes. Logged once for each place in the code that does it
    (with where), or raised when `strict_io` is on. Use `read_fetch_write` to keep the rule."""
    if not in_transaction():
        return
    site = ("?", 0)
    for frame in reversed(traceback.extract_stack()[:-1]):  # the first caller outside this module and the fetcher
        name = Path(frame.filename)
        if name.name not in ("db.py", "polite.py"):
            shown = name.relative_to(BACKEND_DIR) if BACKEND_DIR in name.parents else name
            site = (shown.as_posix(), frame.lineno or 0)
            break
    if strict_io and _known_io.get() is None:
        chain = [f"{Path(f.filename).name}:{f.lineno}" for f in traceback.extract_stack()[:-1]
                 if (BACKEND_DIR / "app") in Path(f.filename).parents and Path(f.filename).name != "db.py"]
        said = f"{what} inside an open database transaction (at {site[0]}:{site[1]}; via {' < '.join(chain[::-1])})"
        io_caught.append(said)
        raise IoInTransaction(said)
    if site not in _io_logged:
        _io_logged.add(site)
        log.warning("network I/O inside an open database transaction (%s, at %s:%s): read, close the session, fetch, "
                    "then write in a new one (db.read_fetch_write)", what, *site)


def after_commit(session: AsyncSession, then: Callable[[], object]) -> None:
    """Call `then()` once this session's transaction has committed (to start work that must see what it wrote, outside
    it). A rollback calls nothing, and nor does a later commit of the same session."""
    done = False

    def committed(_session: SyncSession) -> None:
        nonlocal done
        if not done:
            done = True
            then()

    def ended(_session: SyncSession, transaction: Any) -> None:
        nonlocal done
        if transaction.parent is None:
            done = True

    event.listen(session.sync_session, "after_commit", committed)
    event.listen(session.sync_session, "after_transaction_end", ended)


#: What the task is inside that keeps its transaction whole (a process engine's call: `holding`), or None.
_holding: ContextVar[str | None] = ContextVar("db_holding", default=None)


@contextlib.contextmanager
def holding(what: str) -> Iterator[None]:
    """Inside the block the transaction must stay whole (`what`: a call of the process engine, which locks the
    instance's row and moves its token around the step's action): `release` refuses, and `can_release` says no."""
    token = _holding.set(what)
    try:
        yield
    finally:
        _holding.reset(token)


def can_release(session: AsyncSession) -> bool:
    """Whether `release` may end `session`'s transaction here: not inside a savepoint, nor inside `holding`."""
    return _holding.get() is None and not session.in_nested_transaction()


async def release(session: AsyncSession) -> None:
    """End `session`'s transaction before slow work (a page read, a mail read, a wait on the browser): what it has done
    so far is committed, the writer's turn and its locks are let go and its pooled connection is handed back. The
    objects it loaded stay readable (`expire_on_commit` is off) and its next statement begins a fresh, short
    transaction, which must look again at a row it changes (another transaction may have changed it meanwhile). Nothing
    when no transaction is open.

    The owner of a request or a pass may call it between its steps, and so may a service that does slow work (the rule
    in the project rules: a transaction is held only for its database work). Never inside a savepoint (`begin_nested`)
    or a
    process engine's call (`holding`): each is part of a bigger whole, which raises here rather than being committed by
    half. Work that runs inside one (a process step) has its reads done before it begins (`polite.read_ahead`)."""
    if not can_release(session):
        raise RuntimeError(f"a transaction is released inside {_holding.get() or 'a savepoint'}: read ahead of it "
                           "instead (polite.read_ahead)")
    if session.in_transaction():
        await session.commit()


async def release_if_free(session: AsyncSession) -> bool:
    """`release` where it may be (`can_release`), for a loop that reads the network and is called both by the owner of
    its transaction and from inside a process step. True when the transaction was released (or none was open)."""
    if not can_release(session):
        return False
    await release(session)
    return True


async def read_fetch_write[A, B, C](
    read: Callable[[AsyncSession], Awaitable[A]], fetch: Callable[[A], Awaitable[B]],
    write: Callable[[AsyncSession, B], Awaitable[C]],
) -> C:
    """The shape for work that reads the network between database work: `read` in a reader's session that is closed
    (`read_session_factory`: it takes no turn of the file's one writer, where a writer's session began `BEGIN
    IMMEDIATE` at its first statement and held the turn for the whole read), then `fetch` with no transaction and no
    pooled connection held, then `write` in a new writer's session that is committed. `read` therefore only reads, and
    what it returns is plain values or rows whose loaded columns are used (a reader's rows are detached when it closes).
    `write` must look again at what it changes (the rows may have moved while the far side answered)."""
    async with read_session_factory()() as session:
        planned = await read(session)
        await session.commit()
    fetched = await fetch(planned)
    async with session_factory()() as session:
        done = await write(session, fetched)
        await session.commit()
    return done


def _upgrade(url: str, revision: str) -> None:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    cfg.attributes["url"] = url
    command.upgrade(cfg, revision)


def is_empty(connection: Connection) -> bool:
    """Whether the database has no table at all: a file just made."""
    return not inspect(connection).get_table_names()


def create_schema(connection: Connection) -> None:
    """Make every table this app owns from the models, as the schema at head, then the triggers that keep the stored
    order current (`pipeline/queue_triggers.py`: they name tables, so they come after). The engine's tables first: the
    app's refer to them. The process engine's shadow, mode and gate tables are not this app's, so they are not made."""
    from process_engine import models as proc_models  # noqa: F401 - registers the engine's tables on its metadata
    from process_engine.base import Base as EngineBase

    from app.database import Base
    from app.jobsearch import models  # noqa: F401 - registers the tables
    from app.jobsearch.pipeline import queue_triggers
    from app.models import agent, handout, work  # noqa: F401
    from app.sites import models as site_models  # noqa: F401

    for metadata in (EngineBase.metadata, Base.metadata):
        metadata.create_all(connection, tables=[t for t in metadata.sorted_tables if t.name in OWNED_TABLES])
    queue_triggers.make(connection)


async def init_schema(bind: AsyncEngine | None = None, revision: str = "head") -> None:
    """Bring the database to the current schema (Alembic `upgrade head`). Safe to run on every start: on a database
    `create_all` made before Alembic, the baseline creates only what is missing and stamps it."""
    from app.jobsearch import models  # noqa: F401 - registers the tables
    from app.models import agent, handout, work  # noqa: F401
    from app.sites import models as site_models  # noqa: F401

    target = bind or engine()
    url = target.url.render_as_string(hide_password=False)
    # Alembic's env runs its own event loop; do that off this one.
    await asyncio.to_thread(_upgrade, url, revision)
