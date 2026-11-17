"""Reads that build many rows give the event loop back between slices, and answer what a plain read answers.
Everything here is made up."""

from datetime import date

import pytest
from sqlalchemy import select

from app import db
from app.jobsearch import off_turn, stages
from app.jobsearch.models import Opportunity
from app.jobsearch.pipeline import lifecycle
from tests.builders import counted
from tests.support import OWNER, load_profile


def _counting_sleep(monkeypatch) -> list[int]:
    real, calls = off_turn.asyncio.sleep, []

    async def sleep(delay, *args):
        calls.append(1)
        await real(delay, *args)

    monkeypatch.setattr(off_turn.asyncio, "sleep", sleep)
    return calls


async def _roles(n: int) -> None:
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        session.add_all(Opportunity(
            owner=OWNER, slug=f"r{i}", company=f"Firm {i}", title=f"Title {i}", origin="scout",
            status=stages.FIRST_IMPRESSION, match_key=f"firm {i}|r{i}", found=date.today(), **counted())
            for i in range(n))
        await session.commit()


@pytest.mark.asyncio
async def test_scalars_in_slices_gives_the_loop_back_between_slices(js, monkeypatch):
    await _roles(7)
    stmt = select(Opportunity).order_by(Opportunity.slug)
    async with db.session_factory()() as session:
        plain = [r.id for r in (await session.scalars(stmt)).all()]
    calls = _counting_sleep(monkeypatch)
    async with db.session_factory()() as session:
        sliced = await off_turn.scalars_in_slices(session, stmt, rows=3)
    assert [r.id for r in sliced] == plain
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_lifecycle_settle_reads_roles_in_slices(js, monkeypatch):
    await _roles(2)
    seen = []
    real = off_turn.scalars_in_slices

    async def spy(session, stmt, *args, **kwargs):
        seen.append(stmt)
        return await real(session, stmt, *args, **kwargs)

    monkeypatch.setattr(off_turn, "scalars_in_slices", spy)
    async with db.session_factory()() as session:
        await lifecycle.settle(session, OWNER, set())
    assert len(seen) == 1


async def _selects(body) -> list[str]:
    """Every SELECT statement the engine runs while `body` does."""
    from sqlalchemy import event

    engine = db.engine().sync_engine
    seen: list[str] = []

    def note(conn, cursor, statement, *rest):
        seen.append(statement)

    event.listen(engine, "before_cursor_execute", note)
    try:
        await body()
    finally:
        event.remove(engine, "before_cursor_execute", note)
    return [s for s in seen if s.lstrip().upper().startswith("SELECT")]


def _columns(select_list: str) -> set[str]:
    return {part.strip().rsplit(".", 1)[-1] for part in select_list.removeprefix("SELECT").split(",")}


def _list_of(statement: str) -> str:
    return " ".join(statement.split()).split(" FROM ", 1)[0]


@pytest.mark.asyncio
async def test_a_settle_reads_only_the_columns_it_needs_and_still_moves_a_role(js):
    await _roles(1)
    async with db.session_factory()() as session:
        role = (await session.scalars(select(Opportunity))).one()
        role.analysis, role.analysis_state = None, None  # no read, no text: it leaves for Followup needed
        await session.commit()

    async def body():
        async with db.session_factory()() as session:
            assert await lifecycle.settle(session, OWNER, set()) == 1
            await session.commit()

    every = await _selects(body)
    roles_select = [s for s in every if "status IN" in s.replace("\n", " ") and
                    "FROM js_opportunities" in s.replace("\n", " ")]
    assert roles_select, [s[:150] for s in every]
    names = _list_of(roles_select[0])
    for needed in ("status", "analysis_state", "analysis_claimed_at"):
        assert needed in _columns(names)
    for unread in ("gate_facts", "title", "company", "location", "comp", "employer_id"):
        assert unread not in _columns(names), unread
    async with db.session_factory()() as session:
        assert (await session.scalars(select(Opportunity.status))).one() == stages.FOLLOWUP_NEEDED


@pytest.mark.asyncio
async def test_a_settle_in_the_callers_own_session_leaves_every_column_readable(js):
    await _roles(1)
    async with db.session_factory()() as session:
        role = (await session.scalars(select(Opportunity))).one()
        role.found = date.today()  # dirty: the reader is the session itself
        await lifecycle.settle(session, OWNER, set())
        assert role.title == "Title 0"


@pytest.mark.asyncio
async def test_the_employer_limit_reads_only_the_columns_it_needs(js):
    from app.jobsearch.pipeline import employer_limit

    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        session.add_all(Opportunity(
            owner=OWNER, slug=slug, company="Fabrikam", title=f"Title {slug}", origin="scout", status=status,
            match_key=f"fabrikam|{slug}", found=date.today(), **counted())
            for slug, status in (("a", stages.APPLIED), ("b", stages.ACTIVE_DISCUSSION),
                                 ("c", stages.SECOND_IMPRESSION)))
        await session.commit()
    done: list[employer_limit.Settled] = []

    async def body():
        async with db.session_factory()() as session:
            done.append(await employer_limit.settle(session, OWNER))
            await session.commit()

    every = await _selects(body)
    assert done[0].held == 1
    roles_select = [s for s in every if "FROM js_opportunities" in s.replace("\n", " ") and "ORDER BY" in s]
    assert roles_select, [s[:150] for s in every]
    names = _list_of(roles_select[0])
    for needed in ("employer_id", "company", "title", "screened_out_by", "screened_out_reason"):
        assert needed in _columns(names), needed
    for unread in ("gate_facts", "location", "comp", "fit_breakdown"):
        assert unread not in _columns(names), unread



@pytest.mark.asyncio
async def test_held_postings_reads_in_slices_and_answers_the_same(js, monkeypatch):
    import uuid
    from datetime import UTC, datetime

    from app.jobsearch.fit import freshness
    from app.jobsearch.models import JobPosting

    await _roles(3)
    now = datetime.now(UTC)
    async with db.session_factory()() as session:
        roles = (await session.scalars(select(Opportunity).order_by(Opportunity.slug))).all()
        source = uuid.uuid4()
        session.add_all(JobPosting(
            owner=OWNER, employer_id=source, ats="scout", ext_id=f"p{i}", company="Firm", title="Lead",
            match_key=f"p{i}", first_seen=now, last_seen=now, links=[], opportunity_id=roles[i % 3].id)
            for i in range(5))
        await session.commit()
    async with db.session_factory()() as session:
        whole = await freshness.held_postings(session, OWNER, boards=frozenset())
    monkeypatch.setattr(freshness, "SLICE", 2)
    calls = []
    real = freshness.asyncio.sleep

    async def sleep(delay, *args):
        calls.append(1)
        await real(delay, *args)

    monkeypatch.setattr(freshness.asyncio, "sleep", sleep)
    async with db.session_factory()() as session:
        sliced = await freshness.held_postings(session, OWNER, boards=frozenset())
    assert {k: sorted(v, key=repr) for k, v in sliced.items()} == {k: sorted(v, key=repr) for k, v in whole.items()}
    assert sum(len(v) for v in sliced.values()) == 5 and len(calls) == 3
