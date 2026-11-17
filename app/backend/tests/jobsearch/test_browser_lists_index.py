"""The browser read's role lists find a role's postings by an index, never by scanning every posting once per role.

`to_read_at_source` asks, for each Sourced role, whether any posting of it holds a read text, and with no index
on `js_postings.opportunity_id` Postgres would scan all the postings once per role, which on a large database is
slow enough that no session starts a browser read. `GET /agent/work` runs it (`pool.browser_next`,
`browser_reader.read_due`). Each list's query is planned
here (`EXPLAIN QUERY PLAN`): a plan that still scans `js_postings` has no index to use. Every name is made
up, and no test reaches the network."""

import re

from sqlalchemy import event

from app import db
from app.jobsearch.browser import reader as br
from app.jobsearch.models import JobPosting
from tests.builders import ONE, browser_world

OWNER = "owner"


async def _role(n: int, **fields):
    """A Sourced role of the owner's in Needs Drafted with no text, its own link on the made-up board."""
    from datetime import UTC, date, datetime

    from app.jobsearch.measures.pay_place import match_key
    from app.jobsearch.models import Opportunity
    from app.jobsearch.research import analysis

    async with db.session_factory()() as session:
        role = Opportunity(**{
            "owner": OWNER, "slug": f"role-{n}", "company": f"Initech {n}", "title": f"Programs Director {n}",
            "match_key": match_key(f"Initech {n}", f"Programs Director {n}"), "origin": "scout",
            "status": "ready_to_launch", "analysis_state": analysis.NO_TEXT, "found": date(2026, 10, 9),
            "url": f"https://www.{ONE}/jobs/view/{1000 + n}",
            "source_url": f"https://careers.employer{n}.example.test/jobs/{n}", "source_kind": "employer",
            "sourced_at": datetime(2026, 10, 9, tzinfo=UTC), **fields})
        session.add(role)
        await session.commit()
        return role.id


async def _plans(call) -> list[str]:
    """The query plans (SQLite's `EXPLAIN QUERY PLAN`) of the selects `call(session)` sends that read the postings, each
    planned with the parameters it was sent with."""
    sent: list[tuple[str, object]] = []

    def note(_conn, _cursor, statement, parameters, _context, _many) -> None:
        if statement.lstrip().upper().startswith("SELECT") and JobPosting.__tablename__ in statement:
            sent.append((statement, parameters))

    engine = db.engine().sync_engine
    event.listen(engine, "before_cursor_execute", note)
    try:
        async with db.session_factory()() as session:
            await call(session)
            await session.rollback()
    finally:
        event.remove(engine, "before_cursor_execute", note)
    plans = []
    async with db.engine().connect() as conn:
        for statement, parameters in sent:
            rows = (await conn.exec_driver_sql(f"EXPLAIN QUERY PLAN {statement}", parameters)).all()
            plans.append("\n".join(row[3] for row in rows))
    return plans


def _scans_the_postings(plan: str) -> bool:
    """A bare `SCAN js_postings` line: every posting visited (a `SEARCH ... USING` line is an index at work)."""
    return any(re.fullmatch(rf"\s*SCAN {JobPosting.__tablename__}\s*", line) for line in plan.splitlines())


async def test_the_roles_to_read_at_source_find_their_postings_by_an_index(js):
    await browser_world()
    await _role(1, url="https://careers.employer1.example.test/1")
    plans = await _plans(lambda session: br.to_read_at_source(session, OWNER, -1))
    assert plans and all(
        f"SEARCH {JobPosting.__tablename__} USING" in p and not _scans_the_postings(p) for p in plans), plans


async def test_the_roles_to_read_in_full_find_their_postings_by_an_index(js):
    await browser_world()
    await _role(1)

    async def call(session):
        await br.to_read_in_full(session, OWNER, await br.browser_sites(session, OWNER), -1)

    plans = await _plans(call)
    assert plans and all(
        f"SEARCH {JobPosting.__tablename__} USING" in p and not _scans_the_postings(p) for p in plans), plans


async def test_a_cleansed_data_role_with_only_a_snippet_is_read_in_full_by_the_browser_and_one_with_its_text_is_not(js):
    """A role under `cleansed.MIN_TEXT` characters is not ranked, so a role at Cleansed Data on a browser site's link is
    the browser's to read, though it holds a few lines of listing: the whole page is what ranking needs."""
    await browser_world()
    thin = await _role(1, status="cleansed_data", analysis_state=None, description="Programs Director. Remote. $100K.")
    await _role(2, status="cleansed_data", analysis_state=None,
                description=" ".join(f"Line {i} of the whole posting says what the work is." for i in range(12)))
    async with db.session_factory()() as session:
        found = await br.to_read_in_full(session, OWNER, await br.browser_sites(session, OWNER), -1)
    assert [r["role_id"] for r in found] == [str(thin)]
