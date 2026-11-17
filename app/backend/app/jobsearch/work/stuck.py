"""What closing stuck work shares (searches, research, packets, and the finish-time release): one way to cancel the
process behind it, and one way to sweep. Services flush; the caller commits.

A cancel that fails is logged, never swallowed, and never poisons the pass: it runs in its own savepoint, so a database
error inside it rolls back that savepoint and the rest of the pass carries on. A process instance left running after
its run is closed is an orphan (one waited on because nothing said so), so every failure is on the log.
"""

import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from process_engine.models import RUNNING, WAITING, ProcInstance
from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import jsonsql
from app.jobsearch import process
from app.jobsearch.models import Employer, Opportunity
from app.settings import settings

log = logging.getLogger(__name__)


async def cancel_instance(session: AsyncSession, instance_id: uuid.UUID | None, why: str, what: str) -> bool:
    """Cancel a process instance in its own savepoint; True when it is cancelled (or there was none). A failure is
    logged with its traceback and does not stop the caller: closing the work matters more than closing its process."""
    if instance_id is None:
        return True
    try:
        async with session.begin_nested():
            await process.engine().cancel(session, instance_id, why[:200])
    except Exception:
        log.exception("couldn't cancel the process %s of %s", instance_id, what)
        return False
    return True


async def settle_stuck[Row](
    session: AsyncSession, *, now: datetime | None, since: datetime | None, what: str,
    find: Callable[[datetime], Awaitable[list[Row]]], close: Callable[[Row], Awaitable[None]],
) -> int:
    """Close the work `find` names as stuck: what has shown no sign of life since `stuck_minutes` ago (counted from
    `since` when that is later: the end of peak hours, when work was held). Each is closed in its own savepoint, so one
    that fails is logged and the rest are still closed. Returns how many were closed."""
    cutoff = (now or datetime.now(UTC)) - timedelta(minutes=settings.stuck_minutes)
    if since is not None and since >= cutoff:
        return 0
    closed = 0
    for row in await find(cutoff):
        try:
            async with session.begin_nested():
                await close(row)
        except Exception:
            log.exception("couldn't close stuck %s", what)
            continue
        closed += 1
    await session.flush()
    return closed


async def cancel_orphans(session: AsyncSession, owner: str) -> int:
    """Cancel the research runs nothing holds any more: a role's analysis (`process.ANALYSE`) or an employer's research
    (`process.RESEARCH_EMPLOYER`) still waiting while no role or employer names it, or while the one that names it is in
    no line (its research state is cleared). Every step that takes a role or an employer out of the line cancels its run
    (`cancel_instance`); these are the runs earlier releases and migrations left. Returns how many were cancelled."""
    held_by_role = exists().where(Opportunity.analysis_instance_id == ProcInstance.id,
                                  Opportunity.analysis_state.is_not(None))
    held_by_employer = exists().where(Employer.research_instance_id == ProcInstance.id,
                                      Employer.research_state.is_not(None))
    ids = list(await session.scalars(select(ProcInstance.id).where(
        ProcInstance.status.in_((RUNNING, WAITING)), jsonsql.field(ProcInstance.data, "owner") == owner,
        ((ProcInstance.process_key == process.ANALYSE) & ~held_by_role)
        | ((ProcInstance.process_key == process.RESEARCH_EMPLOYER) & ~held_by_employer))))
    cancelled = 0
    for instance_id in ids:
        cancelled += await cancel_instance(session, instance_id, "nothing holds this run any more",
                                           f"the orphaned run {instance_id}")
    await session.flush()
    return cancelled
