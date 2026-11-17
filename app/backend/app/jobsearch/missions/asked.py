"""The engine's side of Run now on a mission (`POST /missions/{id}/run`, `missions/routes.py`). The page saves the ask
(`engine_calls.ask`) and the engine does what the route did in one program: it runs the mission (`core.run_now`: the
refusals, then the kind's own start, which for research and analysis reads the network to begin), commits, and knocks
for the work the run left. The answer is the run as the page reads it, or the refusal the run met, with its status and
its words, so the page can say it again. Importing this module registers the ask `mission.run`
(`engine_asks.register`); only the engine's start-up (`jobsearch/main.py`) imports it, never the page."""

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import DomainError
from app.jobsearch.actors import YOU
from app.jobsearch.missions import core as missions
from app.jobsearch.work import engine_asks, engine_calls


async def run(session: AsyncSession, owner: str, args: dict[str, Any]) -> dict[str, Any]:
    """Run the mission `args["mission_id"]` now, whatever its schedule and even when paused. Returns `{"run": <the run
    as the page reads it>}`, or `{"refused": {"status", "message"}}` for a refusal (no such mission 404; one running
    already 409; Claude work inside the peak hours 409; a mission the app never starts 422): the transaction is then
    rolled back and nothing is changed. The commit and the knocks are made here, because a knock goes out only after the
    commit."""
    try:
        mission = await missions.get(session, owner, uuid.UUID(args["mission_id"]))
        ran = await missions.run_now(session, owner, mission, by=YOU)
    except DomainError as exc:
        await session.rollback()
        return {"refused": {"status": exc.status, "message": str(exc)}}
    shown = ran.run
    await session.commit()
    await engine_calls.knocks(ran.wake)
    return {"run": shown}


engine_asks.register("mission.run", run)
