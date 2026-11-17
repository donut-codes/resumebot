"""The statements that lock several role rows at once and wait for one another transaction holds take them in id order,
the order `as_read.lock` takes them in (the passes that lock first and read after, which are not `as_read`'s protocol),
so two passes over the same roles are never half of a cycle of waits. Each is asked once for an owner with no roles:
the statement it sends is the one that says in what order. Every name is made up."""

import re
import uuid
from datetime import UTC, date, datetime, timedelta

import pytest

from app import db
from app.jobsearch import stages
from app.jobsearch.missions import catch_up
from app.jobsearch.models import Opportunity
from app.jobsearch.pipeline import built_out, deck, favorites, quiet
from app.jobsearch.research import analysis
from tests.support import OWNER, reads_on

CALLERS = {
    "deck.mark_for_reads": lambda session: deck.mark_for_reads(session, OWNER),
    "deck.stop_waiting": lambda session: deck.stop_waiting(session, OWNER),
    "favorites.advance": lambda session: favorites.advance(session, OWNER),
    "built_out.advance": lambda session: built_out.advance(session, OWNER),
    "analysis.resume_waiting": lambda session: analysis.resume_waiting(session, OWNER, [uuid.UUID(int=1)]),
    "analysis._release": lambda session: analysis._release(session, OWNER),
    "catch_up._roles": lambda session: catch_up._roles(session, [str(uuid.UUID(int=1))], lock=True),
}


@pytest.mark.parametrize("which", sorted(CALLERS))
async def test_a_pass_that_reads_roles_to_lock_them_reads_in_id_order_and_asks_for_no_lock(js, which):
    with reads_on("js_opportunities") as reads:
        async with db.session_factory()() as session:
            await CALLERS[which](session)
            await session.rollback()
    (statement, _), = reads
    assert re.search(r"ORDER BY js_opportunities\.id\b", statement), statement
    assert "FOR UPDATE" not in statement and "SKIP LOCKED" not in statement, statement



async def test_the_quiet_pass_reads_its_roles_the_oldest_first_and_locks_none_of_them(js):
    now = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
    async with db.session_factory()() as session:
        session.add_all(Opportunity(
            id=uuid.UUID(int=n), owner=OWNER, slug=f"role-{n}", company="Firm a", title=f"Lead {n}",
            match_key=f"firm a|lead {n}", origin="scout", status=stages.FIRST_IMPRESSION, found=date(2026, 11, 3),
            created_at=now - timedelta(days=n)) for n in (1, 2, 3))  # the oldest has the highest id
        await session.commit()
    with reads_on("js_opportunities") as reads:
        async with db.session_factory()() as session:
            slugs = [role.slug for role in await quiet._roles(session, OWNER, (stages.FIRST_IMPRESSION,))]
            await session.rollback()
    assert slugs == ["role-3", "role-2", "role-1"]
    (statement, _), = reads
    assert "FOR UPDATE" not in statement, "a rule locks only the roles it moves, once decided (test_quiet_locks.py)"
