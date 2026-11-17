"""The browser queue's seed (the browser work spec): `browser_queue.refresh`, run at every start, makes a `source`
item for each Found role not yet researched whose source is an Exboard or Exthird address, a `read` item for each role
waiting for its text, and a scan or map item for each site read in the browser. It runs again safely. Every name and
address is made up.

Moved from the test of migration 0082, which left with the Postgres era's migrations; the seed is the app's."""

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import select

from app import db
from app.jobsearch.browser import pace as bp
from app.jobsearch.browser import queue as bq
from app.jobsearch.loops import startup
from app.jobsearch.measures.pay_place import match_key
from app.jobsearch.models import BrowserItem, Opportunity
from app.sites.models import Site
from tests.builders import browser_world, in_session
from tests.jobsearch.example_world import example_world  # noqa: F401  (the fixture)
from tests.support import DEFAULT_OWNER

pytestmark = pytest.mark.usefixtures("example_world")


async def test_the_start_seeds_the_queue_from_fact_checking_needs_analysis_and_the_browser_sites(js):
    await browser_world(learned=False, extra_sites=False)
    async with db.session_factory()() as session:
        # Exboard, read in the browser: a board the app knows by name, whose roles only the browser can source
        session.add(Site(owner=DEFAULT_OWNER, url="https://www.board.test/jobs/", kind="job_board", read_by="browser",
                         created_at=datetime.now(UTC)))
        for n, fields in ((1, {"sourced_at": None}), (2, {"url": "https://careers.northwind2.test/jobs/2",
                                                          "source_url": "https://careers.northwind2.test/jobs/2",
                                                          "source_kind": "employer", "sourced_at": datetime.now(UTC),
                                                          "analysis_state": "no_text"})):
            session.add(Opportunity(
                owner=DEFAULT_OWNER, slug=f"role-{n}", company=f"Initech {n}", title=f"Programs Director {n}",
                match_key=match_key(f"Initech {n}", f"Programs Director {n}"), origin="scout",
                status="second_impression", found=date(2026, 10, 9),
                url=fields.pop("url", f"https://www.board.test/jobs/view/{1000 + n}"),
                **fields))
        await session.commit()
    async with db.session_factory()() as session:
        await startup.run_startup_fixups(session, DEFAULT_OWNER)
        await session.commit()
    async with db.session_factory()() as session:
        kinds = sorted((r.kind, r.state) for r in (await session.scalars(select(BrowserItem))).all())
    assert kinds == [("map", "waiting"), ("map", "waiting"), ("read", "waiting"), ("source", "waiting")]
    async with db.session_factory()() as session:  # every start runs it again, safely
        await startup.run_startup_fixups(session, DEFAULT_OWNER)
        await session.commit()
    waiting = await in_session("browser_reader-x", lambda s: bq.waiting_count(s, DEFAULT_OWNER, bp.WORK[bp.WORKER]),
                               commit=False)
    assert waiting == 2, "the same two role items, no more"


async def test_the_queues_plan_reads_as_a_reader_and_its_write_takes_the_turn(js, monkeypatch):
    """`refresh` reads every role, site and item to plan (20 seconds and more on a large database), which holds no turn:
    the plan runs in a reader's transaction, and the write after it in a writer's."""
    await browser_world(learned=False, extra_sites=False)
    seen: dict[str, bool] = {}
    wanted, apply = bq._wanted, bq._apply

    async def reads_as(session) -> bool:
        return bool((await session.connection()).sync_connection.get_execution_options().get("db_read_only"))

    async def watched_wanted(session, owner, sites):
        found = await wanted(session, owner, sites)
        seen["plan"] = await reads_as(session)
        return found

    async def watched_apply(session, owner, made, now):
        seen["apply"] = await reads_as(session)
        return await apply(session, owner, made, now)

    monkeypatch.setattr(bq, "_wanted", watched_wanted)
    monkeypatch.setattr(bq, "_apply", watched_apply)
    async with db.session_factory()() as session:
        await bq.refresh(session, DEFAULT_OWNER)
        await session.commit()
    assert seen == {"plan": True, "apply": False}
