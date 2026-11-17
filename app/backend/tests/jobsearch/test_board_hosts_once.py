"""The board reads the owner's job board hosts once for a read, and an address is split once.

A board read asked `js_sites` for the hosts once for each lane that had cards, and split every address again each time.
The cards say what they said. Every name here is made up."""

import re

from sqlalchemy import select

from app import db
from app.jobsearch import stages
from app.jobsearch.employers import follow
from app.jobsearch.fit import freshness
from app.jobsearch.models import JobPosting, Opportunity
from app.jobsearch.pipeline import row as role_row
from app.jobsearch.pipeline import stored_order
from app.sites import avoid
from app.sites.models import SEARCH, Site
from app.timeutil import utc_now
from tests.employer_builders import make_employer
from tests.jobsearch.test_board_loads import statements
from tests.jobsearch.test_role_row import _role
from tests.support import OWNER, load_profile


async def _world() -> None:
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        now = utc_now()
        session.add_all([Site(owner=OWNER, url=f"https://board{n}.example.test/jobs", kind="job_board",
                              list_name=SEARCH, created_at=now) for n in range(3)])
        roles = [_role(f"{status}-{n}", status) for status in (stages.FIRST_IMPRESSION, stages.SECOND_IMPRESSION,
                                                              stages.READY_TO_LAUNCH) for n in range(2)]
        session.add_all(roles)
        await session.flush()
        acme = await make_employer(session, "Acme Works")
        session.add_all([
            JobPosting(owner=OWNER, employer_id=acme.id, ats="scout", ext_id=r.slug, match_key=r.match_key,
                       opportunity_id=r.id, url=f"https://board0.example.test/{r.slug}", title=r.title,
                       company=r.company, first_seen=now, last_seen=now)
            for r in roles])
        await stored_order.settle(session, OWNER)  # a board that has been read: the order is stored
        await session.commit()


async def test_a_board_read_asks_for_the_job_sites_once(js):
    await _world()
    async with db.session_factory()() as session:
        with statements() as seen:
            columns = await role_row.columns(session, OWNER)
        await session.rollback()
    lanes = [c["key"] for c in columns if c["cards"]]
    assert len(lanes) >= 3, lanes
    assert len([q for q in seen if re.search(r"FROM js_sites\b", q)]) == 1


async def test_posted_days_are_the_same_with_the_hosts_passed_in(js):
    await _world()
    async with db.session_factory()() as session:
        roles = list((await session.scalars(select(Opportunity))).all())
        boards = await follow.job_board_hosts(session, OWNER)
        assert await freshness.posted_by_role(session, OWNER, roles) == await freshness.posted_by_role(
            session, OWNER, roles, boards=boards)
        with statements() as seen:
            await freshness.posted_by_role(session, OWNER, roles, boards=boards)
        assert not [q for q in seen if "FROM js_sites" in q]
        await session.rollback()


def test_host_of_gives_what_it_gave():
    table = [(None, None), (7, None), ("", None), ("   ", None), ("  https://www.Padded.example.test./x  ",
             "padded.example.test"), ("Bare.example.test/path", "bare.example.test"), ("http://[bad/x", None)]
    for given, said in table:
        assert avoid.host_of(given) == said, given


def test_host_of_splits_an_address_once(monkeypatch):
    calls = []
    real = avoid.urlsplit
    monkeypatch.setattr(avoid, "urlsplit", lambda *a, **k: calls.append(a) or real(*a, **k))
    avoid._host_of_text.cache_clear()
    assert avoid.host_of("https://www.Once-Only.example.test/x") == "once-only.example.test"
    assert avoid.host_of(" https://www.Once-Only.example.test/x ") == "once-only.example.test"
    assert len(calls) == 1
