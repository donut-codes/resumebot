"""The session pool's per-kind control over HTTP (`pool/core.py`): each kind of agent with what it is doing now, whether
it
is paused and what waits for it; Run now, pause and resume for one kind or for all. Pausing all is the global pause the
Agents page already has. A kind that is none is 404."""

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app import db
from app.db import ReadSession, Session
from app.errors import NotFound
from app.jobsearch.pool import budget, cost_report
from app.jobsearch.pool import core as pool
from app.jobsearch.work import engine_calls
from app.settings import settings

router = APIRouter(prefix="/api/v1")


#: How long the page waits for the engine to queue the work of Run now before it says the engine did not answer.
WAIT_SECONDS = 20


def _kind(kind: str) -> str:
    try:
        return pool.check_kind(kind)
    except pool.NoSuchKind as exc:
        raise NotFound(str(exc)) from exc


async def _listing() -> list[dict]:
    """The kinds as the page reads them, after a write was committed: read on a reader, which takes no turn."""
    async with db.read_session_factory()() as reader:
        return await pool.listing(reader, settings.owner)


@router.get("/agent-kinds")
async def agent_kinds(session: ReadSession) -> list[dict]:
    return await pool.listing(session, settings.owner)


class ActiveSessionsIn(BaseModel):
    count: int | None = Field(default=None, ge=1, le=pool.MAX_SESSIONS)
    #: The nightly budget in dollars at API prices (`budget.py`); sent as null, no limit; left out, unchanged.
    budget: float | None = Field(default=None, ge=0)


async def _sessions_view(session: Session) -> dict:
    return {"count": await pool.active_sessions(session, settings.owner), "max": pool.MAX_SESSIONS,
            "budget": await budget.budget_of(session, settings.owner),
            "budget_line": await pool.spend_line(session, settings.owner)}


@router.get("/agent-kinds/sessions")
async def active_sessions(session: ReadSession) -> dict:
    """How many sessions may work at once, and the most there are lanes for."""
    return await _sessions_view(session)


@router.put("/agent-kinds/sessions")
async def set_active_sessions(body: ActiveSessionsIn, session: Session) -> dict:
    """Set how many sessions may work at once: a lane past it is told nothing is waiting at its next check-in."""
    await pool.set_active_sessions(
        session, settings.owner, body.count, body.budget if "budget" in body.model_fields_set else pool.UNCHANGED)
    await session.commit()
    async with db.read_session_factory()() as reader:
        return await _sessions_view(reader)


@router.post("/agent-kinds/run-all")
async def run_all(session: Session) -> dict:
    """Run now for every kind. The engine queues the work and wakes the lanes (`pool.asked.run_all`): research and
    employer jobs read the network to start, and the page never does. This saves the ask and waits for the answer. With
    the engine stopped the ask is kept and the answer says so in `held`: it is carried out when the engine next runs."""
    answer = await engine_calls.ask(session, settings.owner, "pool.run_all", {}, WAIT_SECONDS)
    return answer if answer is not None else {"queued": {}, "held": engine_calls.WORKER_NOT_RUNNING}


@router.post("/agent-kinds/pause-all")
async def pause_all(session: Session) -> list[dict]:
    await pool.set_global_pause(session, settings.owner, True)
    await session.commit()
    return await _listing()


@router.post("/agent-kinds/resume-all")
async def resume_all(session: Session) -> list[dict]:
    await pool.set_global_pause(session, settings.owner, False)
    await session.commit()
    return await _listing()


@router.post("/agent-kinds/{kind}/run")
async def run_kind(kind: str, session: Session) -> dict:
    """Run now for one kind; a kind that is none is 404, answered here. The engine queues the work and wakes the lane
    (`pool.asked.run_kind`). With the engine stopped the ask is kept and the answer says so in `held`."""
    name = _kind(kind)
    answer = await engine_calls.ask(session, settings.owner, "pool.run_kind", {"kind": name}, WAIT_SECONDS)
    if answer is None:
        return {"kind": name, "queued": 0, "held": engine_calls.WORKER_NOT_RUNNING}
    return answer


@router.post("/agent-kinds/{kind}/pause")
async def pause_kind(kind: str, session: Session) -> list[dict]:
    await pool.pause(session, settings.owner, _kind(kind))
    await session.commit()
    return await _listing()


@router.post("/agent-kinds/{kind}/resume")
async def resume_kind(kind: str, session: Session) -> list[dict]:
    await pool.resume(session, settings.owner, _kind(kind))
    await session.commit()
    return await _listing()


@router.get("/cost-nights")
async def cost_nights(session: ReadSession) -> list[dict]:
    """The nightly cost benchmark (`cost_report.py`): the last seven nights, newest first."""
    return await cost_report.listing(session, settings.owner)
