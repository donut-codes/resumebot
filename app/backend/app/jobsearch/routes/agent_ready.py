"""`GET /agent/ready`: whether an agent is there to take a piece of Claude work the owner asks for. It is the page's
route. It reads the database on a reader and imports nothing of the lane doors, which are in `routes/runner.py` and
are the engine's."""

from fastapi import APIRouter

from app.db import ReadSession
from app.jobsearch.work import job_queue
from app.settings import settings

router = APIRouter(prefix="/api/v1/agent")


@router.get("/ready")
async def agent_ready(session: ReadSession) -> dict:
    """Whether an agent is there to take a piece of Claude work the owner asks for, and when not, one plain sentence for
    why (peak hours, no agent that has checked in, a paused one). An agent busy with another session is there: the work
    is queued behind it (`job_queue.ready`)."""
    ok, why = await job_queue.ready(session, settings.owner)
    return {"ready": ok, "why": why}
