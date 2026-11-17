"""Employer research is part of going on deck (the owner, 2026-10-21): a role on deck whose employer was never
researched, or not within the again-after days, queues that employer's research at once and at any hour, ahead of the
deck's read; the read waits for it a short while and goes on without it. The night researches no employer without a
role on deck or later. Everything here is made up."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app import db
from app.jobsearch import stages
from app.jobsearch.employers import core as employers
from app.jobsearch.models import Employer, Opportunity
from app.jobsearch.pipeline import deck as pipeline
from app.jobsearch.research import analysis
from app.jobsearch.research import employer as employer_research
from app.jobsearch.work import hours as night
from app.jobsearch.work import session_kinds
from app.timeutil import owner_today
from tests.support import OWNER, load_profile

TODAY = owner_today()


@pytest.fixture(autouse=True)
def deck_research_on(monkeypatch):
    monkeypatch.setattr(employer_research, "DECK_RESEARCH", True)


async def _deck_role(session, name: str, status: str = stages.SECOND_IMPRESSION, **employer_fields) -> Employer:
    employer = await employers.find_or_make(session, OWNER, name)
    for key, value in employer_fields.items():
        setattr(employer, key, value)
    session.add(Opportunity(owner=OWNER, slug=name.lower(), company=name, title="Programs Lead", origin="scout",
                            match_key=f"{name.lower()}|programs lead", status=status, found=TODAY, posted=TODAY,
                            employer_id=employer.id, sourced_at=datetime.now(UTC)))
    await session.flush()
    return employer


async def test_a_role_on_deck_queues_its_unresearched_or_stale_employer_and_no_other(js):
    now = datetime.now(UTC)
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        await _deck_role(session, "Never")
        await _deck_role(session, "Stale", researched_at=now - timedelta(days=100), research_state="done")
        await _deck_role(session, "Fresh", researched_at=now - timedelta(days=10), research_state="done")
        await _deck_role(session, "Hardpass", gated=True)
        await _deck_role(session, "Failed", research_state=employer_research.FAILED)
        await _deck_role(session, "Lowrank", status=stages.FIRST_IMPRESSION)
        assert await pipeline.employers_for_deck(session, OWNER) == 2
        await session.commit()
    async with db.session_factory()() as session:
        rows = {e.name: e for e in await session.scalars(select(Employer))}
        queued = {n for n, e in rows.items() if e.research_state == employer_research.WAITING}
        assert queued == {"Never", "Stale"}, "a role below the deck, a gated employer and a failed one are left alone"
        assert rows["Never"].research[employer_research.FOR_DECK] is True
        assert await pipeline.employers_for_deck(session, OWNER) == 0, "queued once"


async def test_research_queued_for_the_deck_is_handed_out_at_any_hour_before_the_deck_read(js, monkeypatch):
    monkeypatch.setattr(night, "in_off_hours", lambda s, now=None: False)  # by day
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        await _deck_role(session, "Acme")
        await employer_research.queue_for_deck(session, OWNER, 90)
        await session.commit()
    async with db.session_factory()() as session:
        assert await session_kinds.next_kind(session, OWNER) == session_kinds.RESEARCHER, (
            "by day, as the deck's own reads are")


async def test_the_deck_read_waits_a_short_while_for_its_employer_then_goes_on_without_it(js):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        employer = await _deck_role(session, "Acme")
        await employer_research.queue_for_deck(session, OWNER, 90)
        role = await session.scalar(select(Opportunity).where(Opportunity.slug == "acme"))
        role.analysis_state, role.analysis_asked_at = analysis.WAITING, datetime.now(UTC)
        await session.commit()
        asked = employer.research_asked_at
    async with db.session_factory()() as session:
        assert not bool(await analysis.free(session, OWNER, asked + timedelta(minutes=2))), "its research comes first"
        late = asked + employer_research.EMPLOYER_WAIT + timedelta(minutes=1)
        assert bool(await analysis.free(session, OWNER, late)), "research that is late does not hold the read"
