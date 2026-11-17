"""The engine's side of Run now on the agents (`POST /agent-kinds/run-all` and `/agent-kinds/{kind}/run`,
`pool/routes.py`). The page saves the ask (`engine_calls.ask`) and the engine does what the routes did in one program:
it queues the waiting work of the kind (`pool.run_kind`: research and employer jobs read the network to start, so the
page must not), keeps the owner's ask as the rows `owner_asked:<kind>` and `asked:<kind>`, commits, and wakes the
lanes when anything was queued or a browser job was asked for. The answer is what the route used to return.
Importing this module registers the asks `pool.run_all` and `pool.run_kind` (`engine_asks.register`); only the
engine's start-up (`jobsearch/main.py`) imports it, never the page."""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.jobsearch.pool import core as pool
from app.jobsearch.work import engine_asks, engine_calls


async def run_all(session: AsyncSession, owner: str, args: dict[str, Any]) -> dict[str, Any]:
    """Run now for every kind (`pool.run_all`). The knock goes out after the commit, and only when something was queued
    or a browser job was asked for."""
    out = await pool.run_all(session, owner)
    # The ask is a row: read before the commit, in the transaction that wrote it, and only when nothing was queued.
    wake = any(out["queued"].values()) or await pool.browser_asked(session, owner)
    await session.commit()
    if wake:
        await engine_calls.knock("run-all", reason=engine_calls.RUN_NOW)
    return out


async def run_kind(session: AsyncSession, owner: str, args: dict[str, Any]) -> dict[str, Any]:
    """Run now for the kind `args["kind"]`, a name the page has already checked (`pool.check_kind`)."""
    kind = args["kind"]
    out = await pool.run_kind(session, owner, kind)
    wake = out["queued"] or await pool.browser_asked(session, owner)  # read before the commit, as `run_all`
    await session.commit()
    if wake:
        # the lanes check in every 5 minutes: this one starts at once
        await engine_calls.knock(kind, reason=engine_calls.RUN_NOW)
    return out


engine_asks.register("pool.run_all", run_all)
engine_asks.register("pool.run_kind", run_kind)
