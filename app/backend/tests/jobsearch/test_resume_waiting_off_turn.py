"""The schedule's sweep for analyses left waiting looks at them without the writer's turn (The look
held the file's one turn for 1 to 1.6 s at startup)."""

import asyncio

import httpx
from sqlalchemy import select, update

from app import db
from app.jobsearch.fetch import polite
from app.jobsearch.fetch.polite import Fetcher
from app.jobsearch.models import Opportunity
from app.jobsearch.postings import text as posting_text
from app.jobsearch.research import analysis
from tests.builders import analysis_setup, put_in_line
from tests.support import OWNER, POSTING_WORDS


async def _three_waiting_roles_holding_their_text(monkeypatch) -> None:
    monkeypatch.setattr(polite, "make", lambda: Fetcher(
        httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(503))), per_host_seconds=0))
    ids = await analysis_setup(text=None)
    for company in ("Fabrikam", "Initech", "Hooli"):
        await put_in_line(ids[company], description=None)
    async with db.session_factory()() as session:
        for role in (await session.scalars(select(Opportunity).where(Opportunity.owner == OWNER))).all():
            assert role.analysis_state == analysis.NO_TEXT
            role.description = POSTING_WORDS
        await session.commit()


async def test_the_look_at_waiting_roles_is_a_readers_and_a_second_writer_gets_through_meanwhile(js, monkeypatch):
    await _three_waiting_roles_holding_their_text(monkeypatch)
    real = posting_text.holds_without_read
    seen: list[tuple[bool, bool]] = []  # (read in a reader's session, a second writer got through)

    async def holds_without_read(session, role):
        got = False
        if db.is_reader(session):
            async with db.session_factory()() as other:
                try:
                    await asyncio.wait_for(other.execute(update(Opportunity).where(Opportunity.id == role.id)
                                                         .values(slug=role.slug)), 1)
                    got = True
                except TimeoutError:  # queued behind the look: it still holds the writer's turn
                    pass
                await other.rollback()
        seen.append((db.is_reader(session), got))
        return await real(session, role)

    monkeypatch.setattr(posting_text, "holds_without_read", holds_without_read)
    async with db.session_factory()() as session:
        assert await analysis.resume_waiting(session, OWNER) == 3
        await session.commit()
    looks = seen[:3]
    assert looks == [(True, True)] * 3


async def test_a_session_with_a_transaction_open_looks_in_itself_and_starts_the_same_roles(js, monkeypatch):
    await _three_waiting_roles_holding_their_text(monkeypatch)
    real = posting_text.holds_without_read
    seen: list[bool] = []

    async def holds_without_read(session, role):
        seen.append(db.is_reader(session))
        return await real(session, role)

    monkeypatch.setattr(posting_text, "holds_without_read", holds_without_read)
    async with db.session_factory()() as session:
        (await session.scalars(select(Opportunity).limit(1))).all()  # a transaction is open
        assert await analysis.resume_waiting(session, OWNER) == 3
        await session.commit()
    assert seen[:3] == [False] * 3
