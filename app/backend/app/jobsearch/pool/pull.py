"""The pool's pull: which job starts next (`start_next`), the one place that owns the order of the steps, the pause
rule, the reader or same-session choice and the safe start.

Two things are scheduled, and only they keep to the owner's off hours (the owner, 2026-10-22): the lookups that bring
new postings in (a search) and the building of site recipes. Everything downstream runs on need at any hour, when work
waits: the deck's research of employers and its Sonnet reads, the Haiku ranking. Peak hours, when the owner has them
on, hold every model session (`availability.barred`, in the caller's `look`) and so these too. The descriptions roles
lack are read by the pipeline's own turn (`schedule.pipeline_pass`), never here: this runs inside a runner's ask for
work, which must answer in seconds.

The look and the choice are a reader's, so the turn is not held while they read
and a call that starts nothing never takes it. A call that has a job to start takes the turn only to add it, and reads
again under it only what makes the add safe, on every path (another writer may have had the turn in between, or queued
the same job). When that has changed nothing is started: the next call decides again."""

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.jobsearch import off_turn
from app.jobsearch.models import Employer, Opportunity
from app.jobsearch.pool import core
from app.jobsearch.research import analysis, line, rank
from app.jobsearch.research import employer as employer_research
from app.jobsearch.work.session_kinds import RESEARCHER, SEARCH

#: `look(reader)`: the kinds not to start and whether the owner's off hours and nightly budget have room (`spare`), or
#: None when nothing may start (a limit holds the pool, or work for a free session waits already).
Look = Callable[[AsyncSession], Awaitable[tuple[set[str], bool] | None]]

#: The pause kind of the rank step (the other steps answer to a session kind).
_RANK = "rank"


@dataclass(frozen=True)
class _Plan:
    """The one job a call decided to start, in plain values, so it outlives the reader's session it was decided in: the
    step, and what that step starts (the role or employer by `id`, with the employer's last research as it was read;
    the rank job's `batch`; nothing for a search)."""

    step: str
    id: uuid.UUID | None = None
    researched_at: datetime | None = None
    batch: rank.Batch | None = None


async def _plan_research(session: AsyncSession, owner: str) -> _Plan | None:
    roles = await core._research_picks(session, owner, 1, one_ahead=True)
    return _Plan("research", id=roles[0].id) if roles else None


async def _plan_employer(session: AsyncSession, owner: str) -> _Plan | None:
    employers = await core._employer_picks(session, owner, 1, one_ahead=True)
    return _Plan("employer", id=employers[0].id, researched_at=employers[0].researched_at) if employers else None


async def _plan_rank(session: AsyncSession, owner: str) -> _Plan | None:
    batch = await core._rank_plan(session, owner)
    return _Plan("rank", batch=batch) if batch is not None else None


async def _plan_search(session: AsyncSession, owner: str) -> _Plan | None:
    return _Plan("search") if await core._search_due(session, owner) else None


async def _start_research(session: AsyncSession, owner: str, plan: _Plan) -> bool:
    if await core._role_queued_untaken(session, owner):
        return False
    role = await session.scalar(select(Opportunity).where(
        Opportunity.owner == owner, Opportunity.id == plan.id, line.startable()))
    if role is None:
        return False
    await analysis.start(session, owner, role)
    return True


async def _start_employer(session: AsyncSession, owner: str, plan: _Plan) -> bool:
    if await core._employer_queued_untaken(session, owner):
        return False
    employer = await session.scalar(select(Employer).where(
        Employer.owner == owner, Employer.id == plan.id, Employer.gated.is_(False)))
    if employer is None or employer.research_state == employer_research.WAITING or (
            employer.researched_at != plan.researched_at):
        return False
    await employer_research.start(session, owner, employer)
    return True


async def _start_rank(session: AsyncSession, owner: str, plan: _Plan) -> bool:
    return plan.batch is not None and await rank.add(session, owner, plan.batch)


async def _start_search(session: AsyncSession, owner: str, plan: _Plan) -> bool:
    return bool(await core.queue_search(session, owner))  # asks again whether a search is due, as it always did


#: The steps in the pool's order: name, the kind whose pause skips it, whether it runs at any hour (`bounded`), what
#: decides it (reads only) and what starts it (writes, with the facts that make the start safe read again).
_STEPS = (
    ("research", RESEARCHER, True, _plan_research, _start_research),
    ("employer", RESEARCHER, True, _plan_employer, _start_employer),
    ("rank", _RANK, True, _plan_rank, _start_rank),
    ("search", SEARCH, False, _plan_search, _start_search),
)


async def _decide(reader: AsyncSession, owner: str, paused: set[str], bounded: bool) -> _Plan | None:
    for _, kind, any_hour, plan, _start in _STEPS:
        if any_hour is bounded and kind not in paused and (planned := await plan(reader, owner)) is not None:
            return planned
    return None


async def start_next(session: AsyncSession, owner: str, look: Look) -> bool:
    """Start at most one job, in the pool's order: research (one ahead), employer (one ahead) and rank, at any hour;
    then search, only when `look` says there is room and nothing earlier started. A paused kind skips only its own
    steps (`RESEARCHER` research and employer, `"rank"` rank, `SEARCH` search). `look` runs once, on the session this
    decides on (`off_turn.reading`): a reader's when `session` is free, else `session` itself (an open transaction, a
    savepoint, a process step), where the look, the choice and the start share the one transaction. True when a job was
    started. A changed world is False, not an error; services flush and the caller commits."""
    async with off_turn.reading(session) as reader:
        looked = await look(reader)
        if looked is None:
            return False
        paused, spare = looked
        plan = await _decide(reader, owner, paused, True)
        if plan is None and spare:
            plan = await _decide(reader, owner, paused, False)
    if plan is None:
        return False
    for name, _, _, _, start in _STEPS:
        if name == plan.step:
            return await start(session, owner, plan)
    return False
