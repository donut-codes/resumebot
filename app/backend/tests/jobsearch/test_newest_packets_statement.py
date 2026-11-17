"""`row._newest_packets`: the board's packet read takes only the newest version of each role, in one statement that
returns one row per role, and gives the same facts as reading every version and keeping the first. Names are made up."""

import uuid
from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession

from app import db
from app.jobsearch import stages
from app.jobsearch.models import Opportunity, Packet
from app.jobsearch.pipeline import row
from tests.support import OWNER


def _role(n: int) -> Opportunity:
    return Opportunity(id=uuid.UUID(int=n), owner=OWNER, slug=f"role-{n}", company="Firm a", title=f"Lead {n}",
                       match_key=f"firm a|lead {n}", origin="scout", status=stages.FIRST_IMPRESSION,
                       found=date(2026, 11, 3))


async def test_the_newest_version_of_each_role_is_read_in_one_row_per_role(js, monkeypatch):
    statuses = {1: ["replaced", "failed", "ready"], 2: ["ready"], 3: ["replaced", "writing"]}
    async with db.session_factory()() as session:
        session.add_all(_role(n) for n in statuses)
        await session.flush()
        for n, kinds in statuses.items():
            for v, status in enumerate(kinds, start=1):
                session.add(Packet(id=uuid.UUID(int=n * 100 + v), owner=OWNER, role_id=uuid.UUID(int=n), version=v,
                                   status=status, brief={}, resume={}, letter={}))
        await session.commit()

    statements: list[str] = []
    returned: list[list] = []
    original = AsyncSession.execute

    class _Rows:
        def __init__(self, rows):
            self._rows = rows

        def all(self):
            return self._rows

    async def spy(self, statement, *args, **kwargs):
        result = await original(self, statement, *args, **kwargs)
        statements.append(str(statement))
        returned.append(result.all())
        return _Rows(returned[-1])

    monkeypatch.setattr(AsyncSession, "execute", spy)
    async with db.session_factory()() as session:
        got = await row._newest_packets(session, [uuid.UUID(int=n) for n in statuses] + [uuid.UUID(int=9)])

    assert {k: (v["id"], v["version"], v["status"]) for k, v in got.items()} == {
        uuid.UUID(int=1): (str(uuid.UUID(int=103)), 3, "ready"),
        uuid.UUID(int=2): (str(uuid.UUID(int=201)), 1, "ready"),
        uuid.UUID(int=3): (str(uuid.UUID(int=302)), 2, "writing")}
    assert len(statements) == 1 and len(returned[0]) == 3, "one statement, one row per role (six versions stored)"
    assert "max(" in statements[0].lower(), "the statement filters on the newest version"
