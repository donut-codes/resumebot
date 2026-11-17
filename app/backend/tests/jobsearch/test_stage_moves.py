"""Stage moves (docs/PRD.md path 24): every new role and every status change is written down, whichever code made it.
Synthetic roles only."""

from sqlalchemy import select

from app import db
from app.jobsearch import stages
from app.jobsearch.models import Opportunity, StageMove
from tests.support import OWNER

READY = stages.READY_TO_LAUNCH


def role(slug, **extra):
    return Opportunity(
        owner=OWNER, slug=slug, company=f"Co {slug}", title=f"Role {slug}", fit=5, origin="scout",
        match_key=f"co {slug}|role {slug}", **extra,
    )


async def test_a_new_role_and_each_status_change_are_written_down(js):
    async with db.session_factory()() as session:
        a = role("a")
        session.add_all([a, role("b", status="applied")])
        await session.commit()
        a.status = READY
        await session.commit()
        a.title = "Renamed"  # not a status change: nothing written
        await session.commit()
        moves = (await session.scalars(select(StageMove).order_by(StageMove.to_status))).all()
    got = sorted(((m.from_status or ""), m.to_status) for m in moves)
    default = Opportunity.__table__.c.status.default.arg
    assert got == sorted([("", default), ("", "applied"), (default, READY)])
    assert all(m.owner == OWNER for m in moves)
