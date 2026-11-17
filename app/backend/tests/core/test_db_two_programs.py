"""Two programs, one file: how their writes take turns.

Inside a program the writers queue on one lock and then `BEGIN IMMEDIATE` (`db._guard`). Between the page and the engine
there is no second lock: SQLite's own write lock is the turn-taker, and a writer that finds the file held by the other
program waits in SQLite's busy handler (`busy_timeout`, in the sqlite thread, not the event loop) and then fails busy.
The other program is played here by a raw `sqlite3` connection that holds `BEGIN IMMEDIATE` on a thread of its own.
Everything here is made up; the file is a small table of notes."""

import asyncio
import sqlite3
import threading
import time

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from app import db, errors, program
from tests.core.test_sqlite_writer_turn import ADD, COUNT, let_go, made_on


class OtherProgram:
    """The other program: holds the file's write lock on a thread of its own until `end`."""

    def __init__(self, path):
        self.path = path
        self.holding = threading.Event()
        self.ends = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        other = sqlite3.connect(self.path, isolation_level=None)
        try:
            other.execute("BEGIN IMMEDIATE")
            self.holding.set()
            self.ends.wait(30)
            other.execute("ROLLBACK")
        finally:
            other.close()

    def hold(self) -> None:
        self.thread.start()
        assert self.holding.wait(5), "the other program did not get the file"

    def end(self) -> None:
        self.ends.set()
        if self.thread.ident is not None:
            self.thread.join(5)


@pytest.fixture
async def two_programs(tmp_path, monkeypatch):
    """`open_it(lock_ms, background_ms=None)`: the app's engines on a new file with that lock wait (the connection's
    own busy wait is set from it when the connection opens), and the other program that will hold the file."""
    path = tmp_path / "two.db"
    others: list[OtherProgram] = []

    async def open_it(lock_ms: int, background_ms: int | None = None) -> OtherProgram:
        monkeypatch.setattr(db, "LOCK_TIMEOUT_MS", lock_ms)
        if background_ms is not None:
            monkeypatch.setattr(db, "BACKGROUND_LOCK_TIMEOUT_MS", background_ms)
        await made_on(path)
        others.append(OtherProgram(path))
        return others[-1]

    yield open_it
    for other in others:
        other.end()
    if db._engine is not None:
        await let_go()


def a_little_page() -> FastAPI:
    """One owner click: a route that writes one note, with the app's own handler for a busy database."""
    app = FastAPI()
    errors.install(app)

    @app.post("/note")
    async def note() -> dict:
        async with db.session_factory()() as session:
            await session.execute(ADD, {"body": "from the page"})
            await session.commit()
        return {}

    return app


async def notes_in_file() -> int:
    async with db.session_factory()() as session:
        return (await session.execute(COUNT)).scalar_one()


async def test_a_page_write_waits_for_the_other_program_and_commits_when_it_ends(two_programs):
    other = await two_programs(2_000)
    other.hold()
    asyncio.get_running_loop().call_later(0.3, other.ends.set)
    started = time.monotonic()
    async with db.session_factory()() as session:
        await session.execute(ADD, {"body": "from the page"})
        await session.commit()
    waited = time.monotonic() - started
    other.end()
    assert 0.25 <= waited < 1.5
    assert await notes_in_file() == 1


async def test_a_page_write_held_past_the_lock_wait_is_answered_busy_and_changes_nothing(two_programs):
    other = await two_programs(300)
    other.hold()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=a_little_page()), base_url="http://test") as page:
        started = time.monotonic()
        answered = await page.post("/note")
        waited = time.monotonic() - started
    assert answered.status_code == 503
    assert answered.headers["Retry-After"] == str(errors.RETRY_AFTER_SECONDS)
    assert 0.25 <= waited < 2.0
    assert db.engine().sync_engine.pool.checkedout() == 0, "the failed write gave its connection back"
    other.end()
    assert await notes_in_file() == 0
    async with db.session_factory()() as session:  # the pool and the turn are free: the next write goes through
        await session.execute(ADD, {"body": "after"})
        await session.commit()
    assert await notes_in_file() == 1


async def test_a_reader_answers_while_the_other_program_holds_the_file(two_programs):
    other = await two_programs(2_000)
    async with db.session_factory()() as session:
        await session.execute(ADD, {"body": "before"})
        await session.commit()
    other.hold()
    try:
        async with asyncio.timeout(1):
            async with db.read_session_factory()() as reader:
                assert (await reader.execute(COUNT)).scalar_one() == 1
    finally:
        other.end()


async def test_a_background_write_meeting_the_other_program_fails_busy_after_the_sqlite_wait(two_programs):
    other = await two_programs(500, background_ms=3_000)
    other.hold()
    started = time.monotonic()
    with pytest.raises(OperationalError) as caught:
        with db.background():
            async with db.session_factory()() as session:
                await session.execute(ADD, {"body": "from the engine"})
    waited = time.monotonic() - started
    other.end()
    assert db.is_busy(caught.value)
    assert 0.4 <= waited < 1.5, "the connection's own wait (500 ms), not the background turn wait (3 s)"
    assert await notes_in_file() == 0


async def _optimize_said(monkeypatch, path, role: str) -> list[str]:
    """The `PRAGMA optimize` statements one connection says (open and close) when this process is the `role` program."""
    said: list[str] = []
    monkeypatch.setattr(db, "optimize", lambda _cursor, statement: said.append(statement))
    monkeypatch.setattr(program, "PROGRAM", role)
    engine = db._new_engine(db.sqlite_url(path), 1, 0, 5, 1000)
    async with engine.connect() as connection:
        await connection.exec_driver_sql("SELECT 1")
    await engine.dispose()
    return said


async def test_a_page_connection_issues_no_optimize(monkeypatch, tmp_path):
    assert await _optimize_said(monkeypatch, tmp_path / "page.db", "page") == []


@pytest.mark.parametrize("role", ["engine", "all"])
async def test_the_engine_and_the_whole_app_keep_optimize(monkeypatch, tmp_path, role):
    said = await _optimize_said(monkeypatch, tmp_path / f"{role}.db", role)
    assert said == [db.OPTIMIZE_OPEN, db.OPTIMIZE_CLOSE]


async def test_a_write_that_waited_in_the_program_waits_less_in_sqlite(two_programs):
    """The wait for the turn and the wait for the file are one budget (`LOCK_TIMEOUT_MS`), not two in a row."""
    other = await two_programs(1_000)
    other.hold()
    lock = db._lock_named(f"\0turn:{db.engine().url.database}")
    await lock.acquire()  # a writer of this program has the turn ...
    asyncio.get_running_loop().call_later(0.8, lock.release)  # ... and gives it up after 0.8 s
    started = time.monotonic()
    with pytest.raises(OperationalError) as caught:
        async with db.session_factory()() as session:
            await session.execute(ADD, {"body": "second in line"})
    waited = time.monotonic() - started
    other.end()
    assert db.is_busy(caught.value)
    assert 0.9 <= waited < 1.5, "0.8 s in the program and the 0.2 s that were left, not 0.8 s and another 1 s"


async def test_a_write_that_waited_little_keeps_the_connections_full_wait(two_programs):
    other = await two_programs(500)
    other.hold()
    started = time.monotonic()
    with pytest.raises(OperationalError):
        async with db.session_factory()() as session:
            await session.execute(ADD, {"body": "first in line"})
    assert 0.4 <= time.monotonic() - started < 1.5
    other.end()
    async with db.session_factory()() as session:
        wait = (await session.execute(text("PRAGMA busy_timeout"))).scalar_one()
    assert wait == 500
