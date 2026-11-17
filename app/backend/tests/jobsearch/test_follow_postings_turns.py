"""`actions.follow_postings` where the session can release (the night's read) runs the follow and the sourcing as
short turns of their own and rolls back only the uncommitted part of a phase that raises; inside a process engine's
call (`db.holding`) it keeps one transaction and one savepoint a phase. Every name here is made up; nothing reaches the
network. The hold is counted in statements (a turn's length in the work it does), not in seconds, so it is the same on
every machine."""

import pytest
from process_engine.models import ProcInstance
from sqlalchemy import select, text

from app import db
from app.jobsearch import actions, stages
from app.jobsearch.employers import follow as employer_posting
from app.jobsearch.models import Opportunity
from app.jobsearch.postings import sourced as sourcing
from app.models.work import TrailEvent
from tests.conftest import Transactions
from tests.jobsearch.example_world import example_world  # noqa: F401  (the fixture)
from tests.jobsearch.test_follow_roles_cheap import _board_only_roles
from tests.jobsearch.test_sourcing_sweep_slices import _found, _reset, _unsourced
from tests.support import OWNER

pytestmark = pytest.mark.usefixtures("example_world")

DATA = {"owner": OWNER}
BOARD_ONLY = 4


async def _fixture() -> None:
    """50 unsourced roles in play, 40 stale Found roles no gate has read, and a few roles found on a board only."""
    await _found(40)
    await _unsourced(50)
    await _board_only_roles(BOARD_ONLY)


async def _snapshot() -> tuple:
    async with db.session_factory()() as session:
        roles = (await session.scalars(select(Opportunity).order_by(Opportunity.slug))).all()
        rows = [(r.slug, r.url, r.board_url, r.source_url, r.source_kind, r.sourced_at is not None, r.status,
                 {k: v for k, v in (r.gate_facts or {}).items() if k != "at"}) for r in roles]
        events = (await session.scalars(select(TrailEvent).order_by(text("rowid")))).all()
        by_id = {r.id: r.slug for r in roles}
        return rows, [(by_id.get(e.link_id), e.event, e.sentence, e.detail) for e in events]


def _statements(transactions) -> list[int]:
    return [len(t) for t in transactions.ended if Transactions.writes(t)]


async def test_the_night_takes_several_turns_and_writes_what_the_one_transaction_wrote(js, transactions):
    await _fixture()
    async with db.session_factory()() as session:
        before = {r.id: {c: getattr(r, c) for c in Opportunity.__table__.columns.keys() if c != "id"}
                  for r in (await session.scalars(select(Opportunity))).all()}
        trail_before = {e.id for e in (await session.scalars(select(TrailEvent))).all()}
    async with db.session_factory()() as session:  # inside an engine's call: the one transaction it always was
        transactions.clear()
        with db.holding("a call of the process engine"):
            old = await actions.follow_postings(session, None, DATA)
        await session.commit()
    one_turn = _statements(transactions)
    assert len(one_turn) == 1
    expected = await _snapshot()
    await _reset(before, trail_before)

    async with db.session_factory()() as session:
        transactions.clear()
        new = await actions.follow_postings(session, None, DATA)
        await session.commit()
    turns = _statements(transactions)
    assert new == old == {"looked": BOARD_ONLY, "followed": BOARD_ONLY}
    assert len(turns) >= 3, "one transaction is not several turns"
    assert max(turns) * 3 < one_turn[0], "no turn holds a third of what the one transaction held"
    assert await _snapshot() == expected, "the same rows, in the same order"


async def test_a_phase_that_raises_leaves_the_slices_already_committed_and_the_step_returns(js, monkeypatch):
    await _fixture()
    real = sourcing.sweep

    async def fails_after_some(session, owner):
        await real(session, owner)  # its slices are committed as it goes
        session.add(Opportunity(owner=OWNER, slug="half-done", company="X", title="Y", match_key="x|y", origin="scout",
                                status=stages.FOLLOWUP_NEEDED))
        await session.flush()  # this part is not
        raise RuntimeError("the sourcing broke")

    monkeypatch.setattr(sourcing, "sweep", fails_after_some)
    async with db.session_factory()() as session:
        result = await actions.follow_postings(session, None, DATA)
        await session.commit()
    assert result == {"looked": BOARD_ONLY, "followed": BOARD_ONLY}, "the follow's result is kept"
    async with db.session_factory()() as session:
        roles = (await session.scalars(select(Opportunity))).all()
    assert not [r for r in roles if r.slug == "half-done"], "the uncommitted part of the phase is rolled back"
    assert len([r for r in roles if r.slug.startswith("role-") and r.sourced_at]) == 50, "the earlier slices stay"
    assert len([r for r in roles if r.slug.startswith("pm-") and "feedsys" in (r.url or "")]) == BOARD_ONLY


async def test_a_follow_that_raises_costs_its_own_part_and_the_sourcing_still_runs(js, monkeypatch):
    await _fixture()

    async def fails(session, owner):
        raise RuntimeError("the matcher broke")

    monkeypatch.setattr(employer_posting, "follow_roles", fails)
    async with db.session_factory()() as session:
        result = await actions.follow_postings(session, None, DATA)
        await session.commit()
    assert result == {"looked": 0, "followed": 0}
    async with db.session_factory()() as session:
        done = (await session.scalars(select(Opportunity).where(Opportunity.slug.like("role-%")))).all()
    assert len(done) == 50 and all(r.sourced_at for r in done)


@pytest.mark.parametrize("which", ["follow_roles", "sweep"])
async def test_inside_a_process_call_a_raising_phase_rolls_back_only_its_savepoint(
    js, transactions, monkeypatch, which,
):
    await _fixture()

    async def fails(session, owner):
        session.add(Opportunity(owner=OWNER, slug="half-done", company="X", title="Y", match_key="x|y",
                                origin="scout", status=stages.FOLLOWUP_NEEDED))
        await session.flush()
        raise RuntimeError("a phase broke")

    target = employer_posting if which == "follow_roles" else sourcing
    monkeypatch.setattr(target, which, fails)
    async with db.session_factory()() as session:
        instance = ProcInstance(process_key="search_round", file_hash="h", status="running", data={"owner": OWNER})
        session.add(instance)
        await session.flush()  # the engine's own uncommitted work, written before the call
        transactions.clear()
        with db.holding("a call of the process engine"):
            result = await actions.follow_postings(session, instance, DATA)
        await session.commit()
    if which == "follow_roles":
        assert result == {"looked": 0, "followed": 0}
    else:
        assert result == {"looked": BOARD_ONLY, "followed": BOARD_ONLY}
    assert len(_statements(transactions)) == 1, "one turn, as before"
    async with db.session_factory()() as session:
        assert await session.get(ProcInstance, instance.id) is not None, "the engine's own row is intact"
        assert not (await session.scalars(select(Opportunity).where(Opportunity.slug == "half-done"))).all()
