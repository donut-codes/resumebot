"""A re-gate page's side reads (where the owner put the roles, their packets, their copies, their dates) are a reader's:
they take no writer turn, so another writer can take its turn while they run. The page's result is the same. Everything
here is made up."""

import pytest
from sqlalchemy import select

from app import db
from app.jobsearch import stages
from app.jobsearch.gating import regate
from app.jobsearch.models import Opportunity
from tests.jobsearch.test_regate import TODAY, gates, role
from tests.support import OWNER, load_profile


def _watch(monkeypatch, owner_module, name: str, seen: list) -> None:
    """Wrap `owner_module.name`: note whether the session it was handed holds the writer's turn once it has read."""
    real = getattr(owner_module, name)

    async def wrapped(session, *args, **kwargs):
        result = await real(session, *args, **kwargs)
        seen.append((name, db._HOLDS in session.info))
        return result

    monkeypatch.setattr(owner_module, name, wrapped)


async def _run(monkeypatch) -> tuple[list, dict]:
    seen: list[tuple[str, bool]] = []
    _watch(monkeypatch, regate.placement, "load", seen)
    _watch(monkeypatch, regate.cleansed, "gate_copies_of", seen)
    _watch(monkeypatch, regate.freshness, "posted_by_role", seen)
    _watch(monkeypatch, regate, "_latest_packets", seen)
    _watch(monkeypatch, regate, "_left", seen)
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, gates=gates(95_000))
        session.add_all([role("low", stages.SECOND_IMPRESSION, 60_000),
                         role("high", stages.SECOND_IMPRESSION, 150_000)])
        await session.commit()
    async with db.session_factory()() as session:
        await regate.run(session, OWNER, cause="a test set it up", today=TODAY)
        await session.commit()
    async with db.session_factory()() as session:
        rows = (await session.scalars(select(Opportunity))).all()
        return seen, {r.slug: r.status for r in rows}


@pytest.mark.asyncio
async def test_a_page_of_roles_reads_its_side_data_without_the_writers_turn(js, monkeypatch):
    seen, statuses = await _run(monkeypatch)
    assert {name for name, _ in seen} == {"load", "gate_copies_of", "posted_by_role", "_latest_packets", "_left"}
    assert not [name for name, held in seen if held], "a side read of a page held the writer's turn"
    assert statuses["low"] == stages.GATED_OUT and statuses["high"] == stages.SECOND_IMPRESSION
