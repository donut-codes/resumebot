"""`process_engine.portable`: the statements, the locks and the counting key mean the same thing on SQLite and on
Postgres. The tests that read and write take the `session` fixture (SQLite, see conftest.py); the ones that only compile
SQL say what Postgres would be sent, with no server."""

import ast
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select, update
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.schema import CreateTable

from process_engine import portable
from process_engine.models import COMPLETED, Base, ProcFile, ProcHistory, ProcInstance, ProcVersion


def instance(**more) -> ProcInstance:
    return ProcInstance(process_key="p", file_hash="h", **more)


# --- the counting key --------------------------------------------------------------------------------------------


async def test_the_history_counts_up_by_itself(session):
    """Found with the port to SQLite: `proc_history.id` was a BigInteger, which SQLite does not count for, so the first
    move of every run failed with a NOT NULL error."""
    row = instance()
    session.add(row)
    await session.flush()
    first, second = (ProcHistory(instance_id=row.id, seq=n, kind="move") for n in (1, 2))
    session.add_all([first, second])
    await session.flush()
    assert isinstance(first.id, int) and second.id > first.id


def ddl(dialect, name: str) -> str:
    return str(CreateTable(Base.metadata.tables[name]).compile(dialect=dialect))


def test_the_history_key_is_the_column_each_database_counts_for():
    assert "id BIGSERIAL NOT NULL" in ddl(postgresql.dialect(), "proc_history")
    on_sqlite = ddl(sqlite.dialect(), "proc_history")
    assert "id INTEGER NOT NULL" in on_sqlite, "the one column SQLite counts for itself"
    assert "PRIMARY KEY (id)" in on_sqlite


# --- the upsert --------------------------------------------------------------------------------------------------


async def test_insert_ignore_adds_a_row_once_and_a_second_time_does_nothing(session):
    values = {"file_hash": "abc", "process_key": "p", "xml": "<a/>"}
    await portable.insert_ignore(session, ProcFile, values, conflict=("file_hash",))
    await portable.insert_ignore(session, ProcFile, {**values, "xml": "<changed/>"}, conflict=("file_hash",))
    await portable.insert_ignore(session, ProcFile, {**values, "file_hash": "def"}, conflict=("file_hash",))
    rows = (await session.execute(select(ProcFile.file_hash, ProcFile.xml).order_by(ProcFile.file_hash))).all()
    assert rows == [("abc", "<a/>"), ("def", "<a/>")], "the second insert of 'abc' left the first row as it was"


async def test_insert_ignore_by_the_columns_of_a_unique_constraint(session):
    entity = uuid.uuid4()
    values = {"entity_id": entity, "process_key": "p", "number": 1, "file_hash": "h", "xml": "<a/>"}
    on = ("entity_id", "process_key", "number")
    for _ in range(2):
        await portable.insert_ignore(session, ProcVersion, values, conflict=on)
    await portable.insert_ignore(session, ProcVersion, {**values, "number": 2}, conflict=on)
    found = (await session.execute(select(ProcVersion.number).where(ProcVersion.entity_id == entity))).scalars().all()
    assert sorted(found) == [1, 2]
    row = (await session.execute(select(ProcVersion).where(ProcVersion.number == 1))).scalar_one()
    assert isinstance(row.id, uuid.UUID) and row.created_at is not None, "the defaults are filled in as for any insert"


class Asked:
    """A session that only says which statement it was handed, on the dialect it is given."""

    def __init__(self, dialect):
        self.dialect, self.sent = dialect, []

    def get_bind(self):
        return SimpleNamespace(dialect=self.dialect)

    async def execute(self, statement):
        self.sent.append(statement)


@pytest.mark.parametrize("dialect", [postgresql.dialect(), sqlite.dialect()], ids=["postgres", "sqlite"])
async def test_insert_ignore_is_on_conflict_do_nothing_on_both_engines(dialect):
    asked = Asked(dialect)
    await portable.insert_ignore(asked, ProcFile, {"file_hash": "x", "process_key": "p", "xml": "<a/>"},
                                 conflict=("file_hash",))
    [statement] = asked.sent
    assert str(statement.compile(dialect=dialect)).endswith("ON CONFLICT (file_hash) DO NOTHING")


async def test_insert_ignore_knows_two_engines_and_no_others():
    elsewhere = SimpleNamespace(get_bind=lambda: SimpleNamespace(dialect=SimpleNamespace(name="mysql")))
    with pytest.raises(ValueError, match="SQLite or Postgres, not 'mysql'"):
        await portable.insert_ignore(elsewhere, ProcFile, {"file_hash": "x"}, conflict=("file_hash",))


# --- the locks ---------------------------------------------------------------------------------------------------


async def test_a_locked_get_reads_the_row_afresh_and_says_none_for_a_row_that_is_not_there(session):
    row = instance()
    session.add(row)
    await session.flush()
    await session.execute(update(ProcInstance).where(ProcInstance.id == row.id).values(status=COMPLETED)
                          .execution_options(synchronize_session=False))
    assert row.status != COMPLETED, "the session still remembers the copy it had"
    got = await portable.get_locked(session, ProcInstance, row.id)
    assert got is row and got.status == COMPLETED, "a locked read is a fresh read"
    assert await portable.get_locked(session, ProcInstance, uuid.uuid4()) is None


def test_a_lock_is_asked_of_postgres_as_for_update_and_of_sqlite_as_nothing():
    query = select(ProcInstance).where(ProcInstance.id == uuid.uuid4())
    as_postgres, as_sqlite = postgresql.dialect(), sqlite.dialect()
    assert str(portable.for_update(query).compile(dialect=as_postgres)).endswith("FOR UPDATE")
    assert str(portable.for_update(query, skip_locked=True).compile(dialect=as_postgres)).endswith(
        "FOR UPDATE SKIP LOCKED")
    for skip in (False, True):
        assert "FOR UPDATE" not in str(portable.for_update(query, skip_locked=skip).compile(dialect=as_sqlite))
    assert str(query.compile(dialect=as_sqlite)) == str(portable.for_update(query).compile(dialect=as_sqlite))


async def test_a_savepoint_opened_after_reads_alone_is_undone_with_the_transaction(committed):
    """What a host owes the engine on SQLite (the module's words): with the driver's own transaction handling, no
    transaction has begun after a read, so the savepoint is the outermost and its release commits everything. `tick`
    and `live.attempt` work in savepoints, and the caller's rollback has to undo them."""
    one = committed()
    await one.execute(select(ProcFile.file_hash))  # reads only
    async with one.begin_nested():
        await portable.insert_ignore(one, ProcFile, {"file_hash": "a", "process_key": "p", "xml": "<a/>"},
                                     conflict=("file_hash",))
    await one.rollback()
    assert (await committed().execute(select(ProcFile.file_hash))).all() == []


# --- the one place -----------------------------------------------------------------------------------------------

SOURCE = Path(__file__).resolve().parents[1] / "src" / "process_engine"
ENGINE_IMPORTS = ("sqlalchemy.dialects", "asyncpg", "aiosqlite", "sqlite3", "psycopg")
ENGINE_NAMES = {"JSONB", "with_for_update", "on_conflict_do_nothing", "on_conflict_do_update", "astext", "has_key"}
ENGINE_KEYWORDS = ("postgresql_", "sqlite_", "with_for_update")


def test_nothing_but_the_portable_module_names_an_engine():
    """Every other module speaks to the database in SQLAlchemy's own words. What one engine has and the other lacks is
    in `portable.py`."""
    found = []
    for path in sorted(SOURCE.rglob("*.py")):
        if path.name == "portable.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            where = f"{path.relative_to(SOURCE).as_posix()}:{getattr(node, 'lineno', 0)}"
            if isinstance(node, ast.Import):
                found += [f"{where} imports {a.name}" for a in node.names if a.name.startswith(ENGINE_IMPORTS)]
            elif isinstance(node, ast.ImportFrom) and (node.module or "").startswith(ENGINE_IMPORTS):
                found.append(f"{where} imports {node.module}")
            elif isinstance(node, ast.Name | ast.Attribute):
                name = node.id if isinstance(node, ast.Name) else node.attr
                if name in ENGINE_NAMES:
                    found.append(f"{where} names {name}")
            elif isinstance(node, ast.keyword) and node.arg and node.arg.startswith(ENGINE_KEYWORDS):
                found.append(f"{where} passes {node.arg}")
    assert found == [], "an engine is named outside process_engine/portable.py:\n  " + "\n  ".join(found)
