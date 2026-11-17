"""`sourced.source_held` writes the roles it chose in slices of `SLICE`, ending its transaction between them, so the
writer's turn is never held for all of them at once. Every
name here is made up, and no test reaches the network."""

from sqlalchemy import update

from app import db
from app.jobsearch.models import Opportunity
from app.jobsearch.postings import sourced as sourcing
from tests.builders import browser_world
from tests.conftest import Transactions
from tests.jobsearch.made_up_sites_b import example_system_address
from tests.jobsearch.test_browser_work import _get, _role
from tests.support import DEFAULT_OWNER as OWNER

#: The address of one posting on an employer's board at the job system the shared builders use.
POSTING_AT_SYSTEM = example_system_address("initech{n}", "40{n}")


async def _many(count: int) -> list:
    await browser_world()
    return [await _role(n, status="followup_needed", url=POSTING_AT_SYSTEM.format(n=n),
                        apply_url=POSTING_AT_SYSTEM.format(n=n), board_url=None) for n in range(1, count + 1)]


async def _sources(ids) -> list:
    out = []
    for i in ids:
        row = await _get(Opportunity, i)
        out.append((row.source_url, row.source_kind, row.sourced_at is not None))
    return out


async def test_source_held_commits_once_per_slice_of_roles(js, transactions, monkeypatch):
    monkeypatch.setattr(sourcing, "SLICE", 2)
    ids = await _many(5)
    async with db.session_factory()() as session:
        transactions.clear()
        assert await sourcing.source_held(session, OWNER) == 5
        await session.commit()
    writes = [t for t in transactions.ended if Transactions.writes(t)]
    assert len(writes) == 3, "5 roles in slices of 2 are written in 3 turns"
    assert all(source is not None and done for source, _kind, done in await _sources(ids))


async def test_source_held_sources_the_same_roles_in_slices_as_in_one(js, monkeypatch):
    ids = await _many(5)
    monkeypatch.setattr(sourcing, "SLICE", 2)
    async with db.session_factory()() as session:
        assert await sourcing.source_held(session, OWNER) == 5
        await session.commit()
    sliced = await _sources(ids)
    async with db.session_factory()() as session:
        await session.execute(update(Opportunity).where(Opportunity.owner == OWNER).values(
            source_url=None, source_kind=None, sourced_at=None))
        await session.commit()
    monkeypatch.setattr(sourcing, "SLICE", 100)
    async with db.session_factory()() as session:
        assert await sourcing.source_held(session, OWNER) == 5
        await session.commit()
    assert await _sources(ids) == sliced == [(POSTING_AT_SYSTEM.format(n=n), "employer", True) for n in range(1, 6)]


async def test_source_held_in_a_savepoint_sources_every_slice_in_one_transaction(js, transactions, monkeypatch):
    monkeypatch.setattr(sourcing, "SLICE", 2)
    ids = await _many(5)
    async with db.session_factory()() as session:
        transactions.clear()
        async with session.begin_nested():
            assert await sourcing.source_held(session, OWNER) == 5
        await session.commit()
    assert len([t for t in transactions.ended if Transactions.writes(t)]) == 1
    assert all(done for _s, _k, done in await _sources(ids))
