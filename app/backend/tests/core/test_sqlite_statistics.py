"""The planner's statistics (`sqlite_stat1`): a file without them is planned by the owner index (a call takes a
second instead of
milliseconds on a large database), so a connection says `PRAGMA optimize` as SQLite advises an application: when it
opens, and just
before it closes. Everything here is made up."""

import sqlite3
import time

from app import db


def _file_without_statistics(path):
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, owner TEXT, kind TEXT)")
    connection.execute("CREATE INDEX t_owner ON t (owner)")
    connection.executemany("INSERT INTO t (owner, kind) VALUES (?, ?)", [("a", str(n % 5)) for n in range(2000)])
    connection.commit()
    connection.close()


def _has_statistics(path) -> bool:
    connection = sqlite3.connect(path)
    try:
        return connection.execute("SELECT count(*) FROM sqlite_stat1").fetchone()[0] > 0
    except sqlite3.OperationalError:
        return False
    finally:
        connection.close()


async def test_a_connection_that_opens_on_a_file_without_statistics_makes_them(tmp_path):
    path = tmp_path / "x.db"
    _file_without_statistics(path)
    assert not _has_statistics(path)
    engine = db._new_engine(db.sqlite_url(path), 1, 0, 5, 1000)
    async with engine.connect() as connection:
        await connection.exec_driver_sql("SELECT 1")
    await engine.dispose()
    assert _has_statistics(path)


async def test_a_connection_says_optimize_when_it_opens_and_again_before_it_closes(monkeypatch, tmp_path):
    said = []
    monkeypatch.setattr(db, "optimize", lambda _cursor, statement: said.append(statement))
    path = tmp_path / "x.db"
    _file_without_statistics(path)
    engine = db._new_engine(db.sqlite_url(path), 1, 0, 5, 1000)
    async with engine.connect() as connection:
        await connection.exec_driver_sql("SELECT 1")
    assert said == [db.OPTIMIZE_OPEN]
    await engine.dispose()
    assert said == [db.OPTIMIZE_OPEN, db.OPTIMIZE_CLOSE]


async def test_optimize_that_cannot_run_is_not_an_error_for_the_connection(tmp_path):
    path = tmp_path / "x.db"
    _file_without_statistics(path)
    holder = sqlite3.connect(path, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    engine = db._new_engine(db.sqlite_url(path), 1, 0, 5, 1000)
    try:
        async with engine.execution_options(**db.READ_ONLY).connect() as connection:
            assert (await connection.exec_driver_sql("SELECT 1")).scalar() == 1
            assert not _has_statistics(path)  # a writer held the file: nothing was waited for, nothing made
    finally:
        holder.execute("ROLLBACK")
        holder.close()
        await engine.dispose()


async def test_optimize_before_closing_while_a_writer_holds_the_file_is_not_an_error(tmp_path):
    path = tmp_path / "x.db"
    _file_without_statistics(path)
    engine = db._new_engine(db.sqlite_url(path), 1, 0, 5, 1000)
    async with engine.connect() as connection:
        await connection.exec_driver_sql("SELECT 1")
    holder = sqlite3.connect(path, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    try:
        started = time.monotonic()
        await engine.dispose()  # the pooled connection closes: its optimize finds the file held and does not wait
        assert time.monotonic() - started < 0.5  # the connection's own busy wait is 1 s
    finally:
        holder.execute("ROLLBACK")
        holder.close()


def test_a_connection_that_is_broken_at_close_does_not_raise_from_the_close_listener():
    class Broken:
        def cursor(self):
            raise sqlite3.ProgrammingError("Cannot operate on a closed database.")

    db._optimize_at_close(Broken(), None)  # raised before: the cursor was made outside the try
