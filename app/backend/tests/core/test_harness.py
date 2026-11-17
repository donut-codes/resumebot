"""The tests' own harness: each test works on a SQLite file of its
own, copied from the template its worker made once, and the owner's zone and the unique-instant mark start the same for
every test (clock pin, step 1). Each of these fails without the part of the harness it names."""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import text

from app import db, timeutil
from app.jobsearch.models import Opportunity
from tests.support import NOON, OWNER, migration_head, pin_clock


def rows_in(path, table: str) -> int:
    """How many rows `table` holds in the file at `path`, read on a connection of its own."""
    held = sqlite3.connect(path)
    try:
        return held.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    finally:
        held.close()


async def test_a_test_writes_to_its_own_copy_and_the_template_stays_empty(js, schema_template):
    path = Path(js.url.database)
    assert path.resolve() != schema_template.resolve()
    async with db.session_factory()() as session:
        session.add(Opportunity(owner=OWNER, slug="a", company="Co a", title="Role a", match_key="co a|role a"))
        await session.commit()
    assert rows_in(path, "js_opportunities") == 1
    assert rows_in(schema_template, "js_opportunities") == 0


async def test_the_copy_holds_every_table_the_app_owns_and_is_set_as_the_app_sets_a_file(js):
    async with js.connect() as conn:
        tables = {row[0] for row in await conn.execute(text("SELECT name FROM sqlite_master WHERE type = 'table'"))}
        mode = (await conn.execute(text("PRAGMA journal_mode"))).scalar()
        keys = (await conn.execute(text("PRAGMA foreign_keys"))).scalar()
    assert db.OWNED_TABLES <= tables
    assert (mode, keys) == ("wal", 1)


async def test_the_copy_is_stamped_at_head_so_a_start_that_migrates_it_has_nothing_to_run(js):
    await db.init_schema(js)  # as the CLI's import does at its start; on an unstamped file it runs 0001 on made tables
    async with js.connect() as conn:
        stamped = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalars().all()
    assert stamped == [migration_head()]


def test_a_test_that_leaves_another_zone_and_a_moved_on_mark_behind_is_the_first_half_of_a_pair():
    """Left as a test of its own: the two below say what the next test finds. (Set directly, not with `monkeypatch`:
    it is what a test that forgot to put things back does.)"""
    timeutil.settings.timezone = "America/New_York"
    timeutil.utc_now_unique()  # the real clock: the mark is now days past the moment a later test pins


def test_the_next_test_finds_the_owners_zone_in_utc():
    assert timeutil.owner_zone().key == "UTC"


def test_the_next_test_finds_the_unique_mark_at_the_beginning_of_time_so_a_pinned_clock_is_what_it_says(monkeypatch):
    assert timeutil._last_unique == datetime.min.replace(tzinfo=UTC)
    pin_clock(monkeypatch, NOON)
    assert timeutil.utc_now_unique() == NOON
