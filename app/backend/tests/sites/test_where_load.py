"""Where to look reads only the postings it can use (docs/PRD.md 30.3): the postings of a role that is gated out are
not read at all, and one site's or one job system's list reads the postings of that source alone. The answer is the
same as when every posting of the owner was read: the old posting statement stands here as the reference. Every
employer, host and role is made up (example.test); no network."""

from sqlalchemy import select

from app import db
from app.jobsearch.models import JobPosting, Opportunity
from app.jobsearch.stages import GATED_OUT
from app.sites import job_systems, where_view
from tests.sites.builders import SYSTEM_A, SYSTEM_B, SYSTEM_C, UNREAD
from tests.sites.test_where_sources import START, _linked, _role, _world  # noqa: F401  (the shared made-up world)
from tests.support import OWNER


def _every_posting(_owner, _sites, _systems):
    """The posting statement as it was: every posting of the owner that became a role, gated out or not."""
    return (select(JobPosting.opportunity_id, JobPosting.employer_id, JobPosting.ats)
            .where(JobPosting.owner == OWNER, JobPosting.opportunity_id.is_not(None)))


async def _fill(session) -> dict:
    world = await _world(session)
    acme, delta = world["employers"]["acme"], world["employers"]["delta"]
    board, borealis = world["sites"]["board"], world["employers"]["borealis"]
    for hours in range(6):
        await _role(session, acme.id, SYSTEM_A, hours=hours, title=f"Acme {hours}", postings=1 + hours % 3)
    await _role(session, delta.id, SYSTEM_A, hours=7, title="Delta one")
    await _role(session, borealis.id, "own_site", hours=8, title="Borealis own")
    await _role(session, world["employers"]["cirrus"].id, SYSTEM_B, hours=9, title="Cirrus one")
    await _role(session, board.id, "job_site", hours=10, title="Board one", postings=2)
    await _role(session, board.id, "job_site", hours=11, title="Board stopped", status=GATED_OUT, postings=3)
    await _role(session, acme.id, SYSTEM_A, hours=12, title="Acme stopped", status=GATED_OUT, postings=4)
    await _role(session, acme.id, SYSTEM_A, hours=13, title="Never a role", status=None)
    await _linked(session, "https://board.example.test/jobs/77", "Found by link", hours=14)
    await session.flush()
    return world


async def test_every_source_has_the_same_roles_in_the_same_order_as_when_every_posting_was_read(js, monkeypatch):
    async with db.session_factory()() as session:
        world = await _fill(session)
        sites = list(world["sites"].values())
        keys = [SYSTEM_A, SYSTEM_B, UNREAD, SYSTEM_C]
        new_all = await where_view.roles_by_source(session, OWNER, sites=sites, systems=keys)
        new_one = {s.id: (await where_view.roles_by_source(session, OWNER, sites=[s]))[str(s.id)] for s in sites}
        new_sys = {k: (await where_view.roles_by_source(session, OWNER, systems=[k]))[k] for k in keys}
        new_pages = {k: await job_systems.roles_of(session, OWNER, k, 0, 50) for k in (SYSTEM_A, SYSTEM_B)}
        new_site_pages = {s.id: await where_view.roles_of_site(session, OWNER, s.id, 0, 50) for s in sites}
        monkeypatch.setattr(where_view, "_postings_of", _every_posting)
        assert new_all == await where_view.roles_by_source(session, OWNER, sites=sites, systems=keys)
        for s in sites:
            old = (await where_view.roles_by_source(session, OWNER, sites=[s]))[str(s.id)]
            assert new_one[s.id] == old, s.url
            assert new_site_pages[s.id] == await where_view.roles_of_site(session, OWNER, s.id, 0, 50)
        for k in keys:
            assert new_sys[k] == (await where_view.roles_by_source(session, OWNER, systems=[k]))[k], k
        for k, page in new_pages.items():
            assert page == await job_systems.roles_of(session, OWNER, k, 0, 50), k
        assert len(new_sys[SYSTEM_A]) == 7 == new_pages[SYSTEM_A]["total"], "a fixture with something in it"


async def test_the_posting_statement_reads_no_posting_of_a_gated_out_role_and_none_of_another_source(js):
    async with db.session_factory()() as session:
        world = await _fill(session)
        gated = set((await session.scalars(select(Opportunity.id).where(Opportunity.status == GATED_OUT))).all())
        assert gated
        everyone = (await session.execute(_every_posting(OWNER, [], []))).all()
        assert any(role_id in gated for role_id, _, _ in everyone)
        board = world["sites"]["board"]
        rows = (await session.execute(where_view._postings_of(OWNER, [board], []))).all()
        assert rows and not any(role_id in gated for role_id, _, _ in rows), "a gated-out role's postings are not read"
        assert {source for _, source, _ in rows} == {board.id}, "one site reads the postings under its own id only"
        rows = (await session.execute(where_view._postings_of(OWNER, [], [SYSTEM_B]))).all()
        assert rows and {system for _, _, system in rows} == {SYSTEM_B}, "one job system reads its own postings only"
        assert len(rows) < len(everyone)


async def test_a_list_of_one_source_sends_two_statements_whatever_the_world_holds(js, transactions):
    async with db.session_factory()() as session:
        world = await _fill(session)
        site = world["sites"]["board"]
        transactions.clear()
        await where_view.roles_by_source(session, OWNER, sites=[site])
        await where_view.roles_by_source(session, OWNER, systems=[SYSTEM_A])
        sent = [s for s in transactions.open if s.lstrip().upper().startswith("SELECT")]
        assert len(sent) == 4, "the roles, then the postings, for each of the two lists"
        assert sum("js_postings" in s for s in sent) == 2
        assert all("js_opportunities" in s for s in sent[1::2]), "the posting statement joins to the role"
