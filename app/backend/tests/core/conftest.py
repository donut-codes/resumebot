"""The SQLite file the port's tests (`test_sqlite_*.py`) work on: made once for each module from the models, copied for
each test. `test_sqlite_schema.py` has its own, and a module's own fixture of the same name is the one it uses."""

import contextlib
import shutil
import sqlite3

import pytest

from app import db


async def let_go() -> None:
    """Close the app's engines and forget them."""
    await db.engine().dispose()
    if db._lifeline_engine is not None:
        await db._lifeline_engine.dispose()
    db._engine = db._sessions = db._lifeline_engine = db._lifeline_sessions = None


@pytest.fixture(scope="module")
async def template(tmp_path_factory):
    """A file `init_schema` made once for the module (making the schema takes seconds); each test works on a copy."""
    path = tmp_path_factory.mktemp("template") / "app.db"
    engine = db.configure(db.sqlite_url(path))
    await db.init_schema(engine)
    await let_go()
    with contextlib.closing(sqlite3.connect(path)) as raw:  # a checkpoint cannot run inside the engine's transaction
        raw.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return path


@pytest.fixture
async def lite(template, tmp_path, monkeypatch):
    """The app's engine on its own copy of the made file. `db.among` makes ids one argument on SQLite;
    here the reads of rows by id run with a plain `IN`."""
    monkeypatch.setattr(db, "among", lambda column, ids: column.in_(list(ids)))
    path = tmp_path / "app.db"
    shutil.copyfile(template, path)
    engine = db.configure(db.sqlite_url(path))
    yield engine
    await let_go()
