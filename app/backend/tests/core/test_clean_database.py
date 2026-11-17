"""The helper a test uses to run a scenario twice on the one file empties every table the app owns, whatever points at
what (each test starts on a copy of the template's file, `js`: the fixture that emptied the tables
before each test is gone)."""

from sqlalchemy import text

from app import db
from tests.core.test_app import role
from tests.support import OWNER, empty_the_tables, load_profile


async def rows_held(conn) -> dict[str, int]:
    counts = {}
    for table in sorted(db.OWNED_TABLES):
        counts[table] = (await conn.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one()
    return {table: n for table, n in counts.items() if n}


async def test_every_table_with_rows_is_emptied_whatever_points_at_it(js):
    async with db.session_factory()() as session:
        session.add(role("kept-out", "Some Co", "Some Role", 4, 1))
        await load_profile(session, OWNER)
        await session.commit()
    async with js.begin() as conn:
        assert {"js_opportunities", "profile_versions"} <= set(await rows_held(conn))
        await empty_the_tables(conn)
        assert await rows_held(conn) == {}
        await empty_the_tables(conn)  # nothing to do is not an error
        assert await rows_held(conn) == {}
