"""The owner's limit on active sessions (Agents page): a lane past it is told nothing is waiting, so the change
applies at its next check-in; raising the limit resumes it. The limit holds by day (in the off hours every lane works:
`test_off_hours`), so these run by day, on a packet the owner asked for. Everything here is made up."""

from sqlalchemy import select

from app import db
from app.jobsearch import stages
from app.jobsearch.models import Opportunity
from app.jobsearch.work import hours as night
from app.jobsearch.work import session_kinds
from app.jobsearch.writing import packets
from tests.builders import analysis_setup
from tests.jobsearch.test_pool_queue import KINDS, _drafted
from tests.support import OWNER


async def _ask(runner: str) -> str | None:
    async with db.session_factory()() as session:
        out = await session_kinds.next_kind(session, OWNER, runner)
        await session.commit()
    return out


async def _by_day(monkeypatch) -> None:
    """Outside the off hours, with the owner's own ask for the role's packet, so it is theirs to hand out by day."""
    monkeypatch.setattr(night, "in_off_hours", lambda s, now=None: False)
    async with db.session_factory()() as session:
        role = (await session.scalars(select(Opportunity).where(Opportunity.status == stages.READY_TO_LAUNCH))).one()
        await packets.start(session, OWNER, role.id)
        await session.commit()


async def test_default_limit_is_one_session(js, client, monkeypatch) -> None:
    await analysis_setup()
    await _drafted(1)
    await _by_day(monkeypatch)
    assert (await client.get(f"{KINDS}/sessions")).json() == {"count": 1, "max": 5, "budget": None, "budget_line": None}
    assert await _ask("session-1") is not None
    assert await _ask("session-2") is None


async def test_a_lane_past_the_limit_gets_no_work_and_raising_it_resumes(js, client, scout_token, monkeypatch) -> None:
    await analysis_setup()
    await _drafted(1)
    await _by_day(monkeypatch)
    resp = await client.put(f"{KINDS}/sessions", json={"count": 2})
    assert resp.status_code == 200 and resp.json() == {"count": 2, "max": 5, "budget": None, "budget_line": None}
    assert await _ask("session-3") is None
    assert await _ask("session-5") is None
    assert await _ask("session-2") is not None
    assert await _ask("scout") is not None, "a runner that is no session lane is not limited"
    http = await client.get("/api/v1/agent/work?only=any&runner=session-4", headers=scout_token)
    assert http.status_code == 200 and http.json() == {"next": None}
    await client.put(f"{KINDS}/sessions", json={"count": 5})
    assert await _ask("session-5") is not None


async def test_the_limit_stays_within_the_lanes(js, client) -> None:
    assert (await client.put(f"{KINDS}/sessions", json={"count": 6})).status_code == 422
    assert (await client.put(f"{KINDS}/sessions", json={"count": 0})).status_code == 422
