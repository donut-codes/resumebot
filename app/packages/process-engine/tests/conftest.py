"""The package's own tests: a SQLite file of their own (the tables are made with `Base.metadata.create_all`, no app and
no migrations), and the engine's config at its defaults during every test of the package.

The two autouse fixtures at the bottom act on this folder's tests only, and leave what they found. pytest gives a
conftest outside its root folder to every test of the run, so when these files run in one pytest process with an
app's tests (root folder: the app's), the fixtures are asked for by the app's tests too: there they do nothing, and
after a test of the package the config and the owners' versions are the ones the app had set.

Which file: `PROCESS_ENGINE_TEST_DATABASE_URL` names a `sqlite+aiosqlite:///<file>`, else a file in a temporary folder,
so `pytest` in this folder needs nothing running.

Tests that touch the database use the `session` fixture, whose work is rolled back after each test. The few that need
real concurrency use `committed`: sessions on connections of their own, whose rows are emptied afterwards.

SQLite is set the way the app sets it, because that is what the engine's locks rely on (`process_engine.portable`):
every transaction begins `BEGIN IMMEDIATE` (it holds the whole database from its first statement, so a second
request's transaction waits for its turn until the first ends), the driver's own transaction handling is off (so
savepoints work), the write-ahead log, foreign keys on (the cascades), and a busy timeout long enough for the waiting.
"""

import asyncio
import contextlib
import os
from dataclasses import fields
from pathlib import Path

import pytest
from sqlalchemy import delete, event
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from process_engine import config as engine_config
from process_engine import live, loader
from process_engine.models import Base

#: This folder: the tests the autouse fixtures below act on.
_HERE = Path(__file__).resolve().parent

#: How long a write waits for the transaction in front of it (seconds): far past any test's own.
_BUSY_SECONDS = 10


def engine_on(url) -> AsyncEngine:
    """An engine on the test database, set as described at the top."""
    engine = create_async_engine(url)

    @event.listens_for(engine.sync_engine, "connect")
    def _connect(dbapi_connection, record):
        dbapi_connection.isolation_level = None  # no implicit BEGIN: the one below is ours, so savepoints work
        cursor = dbapi_connection.cursor()
        try:
            for pragma in ("journal_mode=WAL", "foreign_keys=ON", f"busy_timeout={_BUSY_SECONDS * 1000}"):
                cursor.execute(f"PRAGMA {pragma}")
        finally:
            cursor.close()

    @event.listens_for(engine.sync_engine, "begin")
    def _begin(connection):
        connection.exec_driver_sql("BEGIN IMMEDIATE")

    return engine


async def _make_tables(url) -> None:
    engine = engine_on(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()


@pytest.fixture(scope="session")
def database_url(tmp_path_factory):
    explicit = os.environ.get("PROCESS_ENGINE_TEST_DATABASE_URL")
    if explicit:
        url = make_url(explicit)
    else:
        url = URL.create("sqlite+aiosqlite", database=str(tmp_path_factory.mktemp("process_engine") / "engine.db"))
    asyncio.run(_make_tables(url))
    return url


@pytest.fixture
def new_engine(database_url):
    """Makes an engine on the test database, set the way SQLite needs (see the top): `engine = new_engine()`. A test
    that opens sessions of its own, outside `session` and `committed`, builds it with this, and disposes of it."""
    return lambda: engine_on(database_url)


@pytest.fixture
async def session(database_url):
    """A session whose work is rolled back after each test."""
    engine = engine_on(database_url)
    async with engine.connect() as conn:
        trans = await conn.begin()
        make = async_sessionmaker(bind=conn, join_transaction_mode="create_savepoint", expire_on_commit=False)
        async with make() as s:
            yield s
        await trans.rollback()
    await engine.dispose()


@pytest.fixture
async def committed(database_url):
    """Sessions that really commit, each on a connection of its own: for the races two requests can have, which one
    transaction rolled back after the test can't show. `committed()` opens one session; when the test ends (however it
    ends) every connection a session still holds is dropped, and every row the test committed is emptied."""
    engine = engine_on(database_url)
    make = async_sessionmaker(engine, expire_on_commit=False)
    opened = []

    def open_session():
        one = make()
        opened.append(one)
        return one

    yield open_session

    for one in opened:  # a session a failed test left mid-transaction holds the database: end it before the cleaning
        with contextlib.suppress(Exception):
            await asyncio.wait_for(one.close(), 5)
    with contextlib.suppress(Exception):
        await asyncio.wait_for(engine.dispose(), 5)
    cleaner = engine_on(database_url)
    async with cleaner.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            await conn.execute(delete(table))
    await cleaner.dispose()


def _of_this_package(request) -> bool:
    """Whether the test is one of this folder's (see the top)."""
    return _HERE in request.node.path.parents


@pytest.fixture(autouse=True)
def restore_drivables(request):
    """The processes registered to run live (`live.register_drivable`) are put back as they were after each test."""
    if not _of_this_package(request):
        yield
        return
    before = dict(live._drivables)  # noqa: SLF001
    yield
    live._drivables.clear()  # noqa: SLF001
    live._drivables.update(before)  # noqa: SLF001


@pytest.fixture(autouse=True)
def default_config(request):
    """Every setting at its default and no owner's versions during a test of the package; afterwards both are what
    they were before it (the defaults, unless an app in the same process had configured the engine)."""
    if not _of_this_package(request):
        yield
        return
    settings = {f.name: getattr(engine_config.config, f.name) for f in fields(engine_config.Config)}
    tables = (loader._owner_versions, loader._owner_active)  # noqa: SLF001
    engine_config.reset()
    loader.set_owner_versions({}, {})
    yield
    engine_config.configure(**settings)
    loader._owner_versions, loader._owner_active = tables  # noqa: SLF001
