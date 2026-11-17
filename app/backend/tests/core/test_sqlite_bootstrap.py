"""The start on a SQLite file: a missing or empty file is made
from the models and stamped at head, every connection is told its PRAGMAs, foreign keys are enforced (a cascade works
only when they are), and the triggers that keep the stored order current fire inside the transaction of the write.
These replace what the tests of the Postgres era's migrations pinned about the schema. The file is the module's `lite`
(`tests/core/conftest.py`: a copy of one made by `init_schema`), except where a test makes its own to start from
nothing. Everything here is made up.
"""

import re
import sqlite3
import uuid

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import select, text

from app import db
from app.jobsearch import stages
from app.jobsearch.models import Opportunity, StageMove
from app.timeutil import owner_today
from tests.support import OWNER

LINES = text("SELECT owner, role_id FROM js_queue_dirty ORDER BY owner, role_id")


async def let_go() -> None:
    """Close the app's engines and forget them."""
    await db.engine().dispose()
    if db._lifeline_engine is not None:
        await db._lifeline_engine.dispose()
    db._engine = db._sessions = db._lifeline_engine = db._lifeline_sessions = None


def _head() -> str:
    config = Config(str(db.BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(db.BACKEND_DIR / "migrations"))
    return ScriptDirectory.from_config(config).get_current_head()


async def _role(session, slug: str) -> Opportunity:
    role = Opportunity(owner=OWNER, slug=slug, company=f"Firm {slug}", title=f"Lead {slug}", origin="scout",
                       match_key=f"firm {slug}|lead {slug}", status=stages.LANDING_ZONE, found=owner_today())
    session.add(role)
    await session.flush()
    return role


# --- the file ------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("before", ["no file", "a file of no bytes"])
async def test_a_start_on_no_file_or_an_empty_one_makes_the_file_from_the_models_and_stamps_it_at_head(tmp_path,
                                                                                                       before):
    path = tmp_path / "app.db"
    if before == "a file of no bytes":
        path.touch()
    engine = db.configure(db.sqlite_url(path))
    try:
        async with engine.connect() as conn:
            assert await conn.run_sync(db.is_empty), "nothing there to keep"
        await db.init_schema(engine)
        async with engine.connect() as conn:
            assert not await conn.run_sync(db.is_empty)
            tables = set((await conn.execute(text("SELECT name FROM sqlite_master WHERE type = 'table'"))).scalars())
            stamped = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalars().all()
    finally:
        await let_go()
    assert path.stat().st_size > 0
    assert db.OWNED_TABLES <= tables
    assert stamped == [_head()], "one row: the head, as a start that migrates would find it"


async def test_the_file_is_in_write_ahead_log_mode_once_the_app_has_opened_it_for_any_program_that_opens_it_after(
        lite):
    """The mode is the file's: the app's first connection asks for it (its PRAGMAs), and a program that opens the file
    later, with no app code, finds it kept (what `init_schema` makes is not in it yet: Alembic's own engine asks for
    nothing)."""
    path = lite.url.database
    async with lite.connect() as conn:
        await conn.execute(text("SELECT 1"))
    kept = sqlite3.connect(path)
    try:
        assert kept.execute("PRAGMA journal_mode").fetchone() == ("wal",)
    finally:
        kept.close()


# --- the PRAGMAs ---------------------------------------------------------------------------------------------------


async def test_each_connection_of_a_pool_in_use_has_foreign_keys_on_wal_and_its_own_engines_wait(lite):
    """Three sessions open at once hold three connections only as readers: a session that may write takes the file's
    one turn at its first statement (the writer turn, `db._guard`), and the second would wait for the first. So each
    is a reader (`db.read_only`), which takes no turn."""
    for factory, wait in ((db.session_factory(), db.LOCK_TIMEOUT_MS),
                          (db.lifeline_session_factory(), db.LIFELINE_LOCK_TIMEOUT_MS)):
        sessions = [factory() for _ in range(3)]
        try:
            seen = []
            for session in sessions:
                await db.read_only(session)
                row = []
                for name in ("foreign_keys", "journal_mode", "busy_timeout"):
                    row.append((await session.execute(text(f"PRAGMA {name}"))).scalar_one())
                seen.append(tuple(row))
        finally:
            for session in sessions:
                await session.close()
        assert seen == [(1, "wal", wait)] * 3


# --- foreign keys --------------------------------------------------------------------------------------------------


async def test_a_role_deleted_takes_the_moves_that_point_at_it_with_it_and_no_row_is_left_pointing_at_nothing(lite):
    async with db.session_factory()() as session:
        role = await _role(session, "gone")
        await session.commit()
        role_id = role.id
        assert (await session.scalars(select(StageMove).where(StageMove.role_id == role_id))).all(), "made with it"
    count = text("SELECT count(*) FROM js_stage_moves WHERE role_id = :id")
    async with lite.begin() as conn:
        await conn.execute(text("DELETE FROM js_opportunities WHERE id = :id"), {"id": role_id.hex})
        assert (await conn.execute(count, {"id": role_id.hex})).scalar_one() == 0, "ON DELETE CASCADE ran"
        assert (await conn.execute(text("PRAGMA foreign_key_check"))).all() == []


# --- the triggers --------------------------------------------------------------------------------------------------


async def test_the_triggers_name_a_role_in_the_transaction_of_the_write_and_a_rolled_back_write_names_none(lite):
    async with db.session_factory()() as session:
        role = await _role(session, "traced")
        await session.commit()
        await session.execute(text("DELETE FROM js_queue_dirty"))
        await session.commit()
        role_id = role.id
    update = text("UPDATE js_opportunities SET posted = :day WHERE id = :id")
    async with lite.connect() as conn:
        transaction = await conn.begin()
        await conn.execute(update, {"day": "2026-01-01", "id": role_id.hex})
        assert [(o, uuid.UUID(hex=r)) for o, r in (await conn.execute(LINES)).all()] == [(OWNER, role_id)], (
            "named at once, inside the transaction of the write")
        await transaction.rollback()
        assert (await conn.execute(LINES)).all() == [], "a write that did not happen names nobody"
        await conn.execute(update, {"day": "2026-01-01", "id": role_id.hex})
        await conn.commit()
        assert len((await conn.execute(LINES)).all()) == 1, "a write that did happen names its role"


def test_the_ranked_lists_partial_index_names_exactly_the_stages_the_lanes_rank_by():
    """A lane reads the ranked stages with `status IN (...)`: the partial index serves it only if its WHERE is that list
    (moved from the test of migration 0151, which left with the Postgres era's migrations)."""
    (index,) = [i for i in Opportunity.__table__.indexes if i.name == "ix_js_opportunities_owner_ranked_number"]
    where = str(index.dialect_options["sqlite"]["where"])
    assert sorted(re.findall(r"'([^']*)'", where)) == sorted(stages.RANKED_LIST)
    assert where.count("'") == 2 * len(stages.RANKED_LIST), "nothing else is quoted in it"
