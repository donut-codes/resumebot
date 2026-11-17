"""The database is SQLite: the column types keep what goes
in, the partial indexes keep their WHERE, every connection is told its PRAGMAs, and an empty file is made from the
models and stamped at head. These tests make their own file in a temporary folder and use none of the shared database
fixtures.
"""

import contextlib
import shutil
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta, timezone

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from process_engine.models import ProcInstance, ProcToken
from process_engine.types import UtcDateTime
from sqlalchemy import JSON, Boolean, Column, DateTime, Integer, MetaData, Table, Uuid, false, insert, select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError

from app import db
from app.database import Base
from app.jobsearch.models import SearchRun
from app.models import agent, handout, work  # noqa: F401 - registers the tables
from app.sites import models as site_models  # noqa: F401


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
async def lite(template, tmp_path):
    """The app's engine on its own copy of the made file."""
    path = tmp_path / "app.db"
    shutil.copyfile(template, path)
    engine = db.configure(db.sqlite_url(path))
    yield engine
    await let_go()


def scratch(*columns: Column) -> Table:
    return Table(f"scratch_{uuid.uuid4().hex}", MetaData(), Column("id", Integer, primary_key=True), *columns)


async def make(engine, table: Table) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(table.metadata.create_all)


# --- B1: the types ---------------------------------------------------------------------------------------------


async def test_a_uuid_of_digits_only_reads_back_as_the_same_uuid(lite):
    table = scratch(Column("ident", Uuid))
    await make(lite, table)
    digits = uuid.UUID("12345678-1234-5678-1234-567812345678")  # its hex is all digits: a number to a naive column
    leading_zero = uuid.UUID("00000000-0000-0000-0000-000000000042")
    async with lite.begin() as conn:
        await conn.execute(insert(table), [{"ident": digits}, {"ident": leading_zero}])
        rows = (await conn.execute(select(table.c.ident).order_by(table.c.id))).scalars().all()
        kinds = (await conn.execute(text(f"SELECT DISTINCT typeof(ident) FROM {table.name}"))).scalars().all()
    assert rows == [digits, leading_zero]
    assert all(isinstance(row, uuid.UUID) for row in rows)
    assert kinds == ["text"]


async def test_a_json_null_is_not_a_sql_null(lite):
    table = scratch(Column("data", JSON))
    await make(lite, table)
    async with lite.begin() as conn:
        await conn.execute(insert(table).values(id=1))  # the column left out: SQL NULL
        await conn.execute(insert(table).values(id=2, data=None))  # None given: the JSON value null
        await conn.execute(insert(table).values(id=3, data={"a": [1, None]}))
        sql_null = (await conn.execute(text(f"SELECT id FROM {table.name} WHERE data IS NULL"))).scalars().all()
        json_null = (await conn.execute(
            text(f"SELECT id FROM {table.name} WHERE json_type(data) = 'null'"))).scalars().all()
        read = dict((await conn.execute(select(table.c.id, table.c.data))).all())
    assert sql_null == [1]
    assert json_null == [2]
    assert read == {1: None, 2: None, 3: {"a": [1, None]}}


async def test_an_aware_time_is_stored_as_utc_and_read_back_aware(lite):
    table = scratch(Column("at", UtcDateTime))
    await make(lite, table)
    zone = timezone(timedelta(hours=-5))
    local = datetime(2026, 11, 4, 12, 0, 0, 123456, tzinfo=zone)
    async with lite.begin() as conn:
        await conn.execute(insert(table).values(id=1, at=local))
        await conn.execute(insert(table).values(id=2, at=None))
        stored = (await conn.execute(text(f"SELECT at FROM {table.name} WHERE id = 1"))).scalar_one()
        read = (await conn.execute(select(table.c.at).where(table.c.id == 1))).scalar_one()
        missing = (await conn.execute(select(table.c.at).where(table.c.id == 2))).scalar_one()
    assert stored == "2026-11-04 17:00:00.123456"
    assert read == local
    assert read.tzinfo is not None and read.utcoffset() == timedelta(0)
    assert missing is None


async def test_times_in_different_zones_compare_and_sort_as_moments(lite):
    table = scratch(Column("at", UtcDateTime))
    await make(lite, table)
    east = timezone(timedelta(hours=9))
    west = timezone(timedelta(hours=-8))
    first = datetime(2026, 11, 4, 9, 0, tzinfo=east)  # 00:00 UTC
    second = datetime(2026, 11, 4, 3, 0, tzinfo=west)  # 11:00 UTC
    third = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
    async with lite.begin() as conn:
        await conn.execute(insert(table), [{"id": 1, "at": third}, {"id": 2, "at": first}, {"id": 3, "at": second}])
        order = (await conn.execute(select(table.c.id).order_by(table.c.at))).scalars().all()
        before_noon = (await conn.execute(
            select(table.c.id).where(table.c.at < third).order_by(table.c.at))).scalars().all()
    assert order == [2, 3, 1]
    assert before_noon == [2, 3]


async def test_a_naive_time_is_taken_as_utc(lite):
    table = scratch(Column("at", UtcDateTime))
    await make(lite, table)
    async with lite.begin() as conn:
        await conn.execute(insert(table).values(id=1, at=datetime(2026, 11, 4, 12, 0)))
        read = (await conn.execute(select(table.c.at))).scalar_one()
    assert read == datetime(2026, 11, 4, 12, 0, tzinfo=UTC)


async def test_a_text_false_default_reads_back_as_false(lite):
    table = scratch(Column("flag", Boolean, server_default=false(), nullable=False),
                    Column("other", Boolean, server_default="false", nullable=False))
    await make(lite, table)
    async with lite.begin() as conn:
        await conn.execute(text(f"INSERT INTO {table.name} (id) VALUES (1)"))
        flag = (await conn.execute(select(table.c.flag))).scalar_one()
    assert flag is False  # the `sa.false()` default; the text one beside it is the trap this test keeps out


async def test_a_row_gets_its_time_from_the_app_and_an_update_moves_updated_at(lite):
    async with db.session_factory()() as session:
        instance = ProcInstance(process_key="p", file_hash="h")
        session.add(instance)
        await session.commit()
        made = instance.created_at
        assert made.tzinfo is not None and abs(datetime.now(UTC) - made) < timedelta(minutes=1)
        before = instance.updated_at
        instance.status = "completed"
        await session.commit()
        assert instance.updated_at > before
        assert instance.created_at == made


def test_no_column_keeps_a_postgres_type_or_a_database_clock():
    """The metadata holds only what SQLite stores faithfully: no JSONB or Postgres UUID, no time without
    `UtcDateTime`, no text boolean default, no `now()` server default."""
    from process_engine.base import Base as EngineBase

    wrong = []
    for metadata in (Base.metadata, EngineBase.metadata):
        for table in metadata.tables.values():
            for column in table.columns:
                kind = column.type
                where = f"{table.name}.{column.name}"
                if isinstance(kind, (postgresql.JSONB, postgresql.UUID)):
                    wrong.append(f"{where}: {kind}")
                if isinstance(kind, DateTime):
                    wrong.append(f"{where}: a DateTime that is not UtcDateTime")
                default = column.server_default
                text_of = str(getattr(default, "arg", "")).lower()
                if isinstance(kind, Boolean) and text_of in {"true", "false"} and isinstance(default.arg, str):
                    wrong.append(f"{where}: a text boolean default")
                if "now()" in text_of or "current_timestamp" in text_of:
                    wrong.append(f"{where}: a database clock default")
    assert not wrong, wrong


# --- B3: the indexes -------------------------------------------------------------------------------------------


async def test_a_unique_partial_index_refuses_a_second_open_row_and_allows_a_closed_one(lite):
    async with db.session_factory()() as session:
        session.add(SearchRun(owner="o", status="open"))
        await session.commit()
        session.add(SearchRun(owner="o", status="done"))
        session.add(SearchRun(owner="p", status="open"))
        await session.commit()  # the other owner's open run, and a closed one of this owner's: both allowed
        session.add(SearchRun(owner="o", status="open"))
        with pytest.raises(IntegrityError):
            await session.commit()


def partial_indexes() -> list[tuple[str, str, str]]:
    """(index, table, WHERE) of each partial index the models declare."""
    return sorted((index.name or "", table.name, str(index.dialect_options["sqlite"]["where"]))
                  for table in Base.metadata.tables.values() for index in table.indexes
                  if index.dialect_options["sqlite"]["where"] is not None)


@pytest.mark.parametrize(("index", "table", "where"), partial_indexes())
async def test_the_planner_can_use_each_partial_index(lite, index, table, where):
    """`INDEXED BY` is refused ("no query solution") when the planner cannot use the index for the WHERE."""
    query = f"EXPLAIN QUERY PLAN SELECT 1 FROM {table} INDEXED BY {index} WHERE {where}"
    async with lite.connect() as conn:
        plan = " ".join(str(row[3]) for row in (await conn.execute(text(query))).all())
    assert index in plan


async def test_the_facts_pending_index_is_made_on_the_json_state(lite):
    async with lite.connect() as conn:
        sql = (await conn.execute(text(
            "SELECT sql FROM sqlite_master WHERE name = 'ix_js_opportunities_owner_facts_pending'"))).scalar_one()
    assert "json_extract(gate_facts, '$.state') = 'pending'" in sql


async def test_the_browser_items_role_index_is_made_from_the_models(lite):
    async with lite.connect() as conn:
        columns = (await conn.execute(text("PRAGMA index_info('ix_js_browser_items_role_id')"))).all()
    assert [row[2] for row in columns] == ["role_id"]


async def test_every_partial_index_keeps_its_where(lite):
    async with lite.connect() as conn:
        sqls = (await conn.execute(text(
            "SELECT name, sql FROM sqlite_master WHERE type = 'index' AND sql IS NOT NULL"))).all()
    partial = {name for name, sql in sqls if " WHERE " in sql}
    declared = {index.name for table in Base.metadata.tables.values() for index in table.indexes
                if index.dialect_options["sqlite"]["where"] is not None}
    assert declared and declared <= partial
    assert len(declared) >= 14


# --- B5: the bootstrap -----------------------------------------------------------------------------------------


async def test_every_connection_runs_with_the_pragmas(lite):
    for factory in (db.session_factory(), db.lifeline_session_factory()):
        async with factory() as session:
            mode = (await session.execute(text("PRAGMA journal_mode"))).scalar_one()
            keys = (await session.execute(text("PRAGMA foreign_keys"))).scalar_one()
            sync = (await session.execute(text("PRAGMA synchronous"))).scalar_one()
            wait = (await session.execute(text("PRAGMA busy_timeout"))).scalar_one()
        assert (mode, keys, sync) == ("wal", 1, 1)
        assert wait in {db.LOCK_TIMEOUT_MS, db.LIFELINE_LOCK_TIMEOUT_MS}


async def test_a_foreign_key_is_enforced(lite):
    async with db.session_factory()() as session:
        session.add(ProcToken(instance_id=uuid.uuid4(), element_id="e"))
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_an_empty_file_is_made_from_the_models_and_stamped_at_head(lite):
    async with lite.connect() as conn:
        tables = set((await conn.execute(text("SELECT name FROM sqlite_master WHERE type = 'table'"))).scalars().all())
        stamped = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalars().all()
    config = Config(str(db.BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(db.BACKEND_DIR / "migrations"))
    head = ScriptDirectory.from_config(config).get_current_head()
    assert db.OWNED_TABLES <= tables
    assert stamped == [head]
    from process_engine.base import Base as EngineBase

    not_ours = set(EngineBase.metadata.tables) - db.OWNED_TABLES
    assert not not_ours & tables  # the engine's own tables that are not this app's are not made


async def test_the_start_on_a_made_file_changes_nothing(lite):
    async with db.session_factory()() as session:
        session.add(SearchRun(owner="kept", status="done"))
        await session.commit()
    await db.init_schema(lite)
    async with db.session_factory()() as session:
        owners = (await session.execute(select(SearchRun.owner))).scalars().all()
    assert owners == ["kept"]


def test_the_app_refuses_a_database_that_is_not_sqlite():
    with pytest.raises(RuntimeError, match="SQLite"):
        db.configure("postgresql+asyncpg://u:p@localhost/x")

