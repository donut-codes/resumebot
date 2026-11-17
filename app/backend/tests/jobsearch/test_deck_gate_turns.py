"""The deck's gate gives the loop back between roles, and the employers for the deck are planned on a reader: judging
hundreds of cleansed copies in one stretch held the loop (and so the writer's turn) for the whole judgement, and the
research queue's read was made in the turn. Everything here is made up."""

import asyncio
import time

import pytest
from sqlalchemy import select, update

from app import db
from app.jobsearch import stages
from app.jobsearch.employers import core as employers
from app.jobsearch.gating import check as gating
from app.jobsearch.models import CleansedPosting, Employer, Opportunity
from app.jobsearch.pipeline import deck
from app.jobsearch.postings.coop import EVERY
from app.jobsearch.research import employer as employer_research
from app.timeutil import owner_today
from tests.jobsearch.test_deck_employer_research import _deck_role
from tests.jobsearch.test_pipeline_deck import _role, _with_copy
from tests.support import OWNER, load_profile

TODAY = owner_today()
ROLES = 500
TEXT = "The team builds programs for the neighbourhood and plans its work with care. " * 260  # 20 KB


async def test_the_gate_gives_the_loop_back_between_roles_and_closes_the_same_roles(js, monkeypatch):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, gates={"compensation": {"amount": 95_000, "against": "maximum"}})
        read = {"state": gating.FACTS_SETTLED, gating.FACTS_WAY: True}  # the way of working was read, and is silent
        roles = [_role(f"r{n:03}", stages.CLEANSED_DATA, gate_facts=read) for n in range(ROLES)]
        session.add_all(roles)
        await session.flush()
        for role in roles:
            await _with_copy(session, role, pay_min=60_000, pay_max=70_000)
        await session.execute(update(CleansedPosting).values(text_clean=TEXT))
        await session.commit()
    ticks = 0  # the turns the loop got
    judged: list[tuple[float, int]] = []  # when each role was judged, and how many turns the loop had had by then
    real = gating.check

    def check(*args, **kwargs):
        judged.append((time.perf_counter(), ticks))
        return real(*args, **kwargs)

    monkeypatch.setattr(deck.gating, "check", check)

    async def ticker():
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0)

    task = asyncio.create_task(ticker())
    try:
        async with db.session_factory()() as session:
            closed = await deck.gate(session, OWNER)
            await session.commit()
    finally:
        task.cancel()
    assert closed == ROLES
    assert len(judged) == ROLES
    # The stretch in which the roles are judged (reads before it and the write after it are other work). The gate gives
    # the loop back after every EVERY seconds of judging; the ticker gets one turn per give-back. Counting turns, not
    # timing a gap, holds on any timer and any load. A gate that never gave the loop back leaves 0 turns; the bound
    # allows ten times EVERY between give-backs.
    (first, first_ticks), (last, last_ticks) = judged[0], judged[-1]
    turns = last_ticks - first_ticks
    assert turns >= 1, "the loop got no turn while the roles were judged"
    assert turns >= int((last - first) / (10 * EVERY))
    async with db.session_factory()() as session:
        statuses = set(await session.scalars(select(Opportunity.status)))
    assert statuses == {stages.GATED_OUT}


class _Counting:
    """A writer's session that counts the statements it is asked to run."""

    def __init__(self, session):
        self.session, self.calls = session, 0

    def __getattr__(self, name):
        attr = getattr(self.session, name)
        if name in ("execute", "scalars", "scalar", "stream", "stream_scalars", "get", "flush"):
            async def counted(*a, **k):
                self.calls += 1
                return await attr(*a, **k)

            return counted
        return attr


@pytest.fixture
def deck_research_on(monkeypatch):
    monkeypatch.setattr(employer_research, "DECK_RESEARCH", True)


async def test_with_no_employer_needing_research_the_writer_runs_no_statement(js, deck_research_on):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        await _deck_role(session, "Fresh", research_state="done", researched_at=employer_research.utc_now())
        await session.commit()
    async with db.session_factory()() as session:
        counted = _Counting(session)
        assert await deck.employers_for_deck(counted, OWNER) == 0
        assert counted.calls == 0
        assert not session.in_transaction()


async def test_an_employer_needing_research_is_started_exactly_once(js, deck_research_on, monkeypatch):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        await _deck_role(session, "Never")
        await session.commit()
    started: list[str] = []
    real = employer_research.start

    async def start(session, owner, employer, **kw):
        started.append(employer.name)
        return await real(session, owner, employer, **kw)

    monkeypatch.setattr(employer_research, "start", start)
    async with db.session_factory()() as session:
        assert await deck.employers_for_deck(session, OWNER) == 1
        await session.commit()
    assert started == ["Never"]


async def test_an_employer_gated_between_the_read_and_the_turn_is_not_started(js, deck_research_on, monkeypatch):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        await _deck_role(session, "Later")
        await _deck_role(session, "Stays")
        await session.commit()
    real = employer_research.deck_research_ids

    async def ids_then_gate(reader, owner, days, now=None):
        found = await real(reader, owner, days, now)
        assert len(found) == 2
        async with db.session_factory()() as other:  # another writer gates one employer after the read
            (await employers.find_or_make(other, OWNER, "Later")).gated = True
            await other.commit()
        return found

    monkeypatch.setattr(employer_research, "deck_research_ids", ids_then_gate)
    async with db.session_factory()() as session:
        assert await deck.employers_for_deck(session, OWNER) == 1
        await session.commit()
    async with db.session_factory()() as session:
        states = {e.name: e.research_state for e in await session.scalars(select(Employer))}
    assert states == {"Later": None, "Stays": employer_research.WAITING}
