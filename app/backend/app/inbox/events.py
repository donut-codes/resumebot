"""What a search produces for the owner: a trail line, a notification, and sometimes a task. Plain words, no dates
invented, nothing that nags. Everything here flushes; whoever closes the run commits."""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.roster import SCOUT
from app.inbox import service
from app.jobsearch.actors import actor_name
from app.jobsearch.models import SearchRun
from app.trail import service as trail

SCOUT_NAME = "Scout"


def _plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


async def search_started(session: AsyncSession, owner: str, run: SearchRun) -> None:
    how = "You started a search." if run.started_by == "manual" else "A scheduled search started."
    if run.focus and run.started_by == "manual":
        how = f"You started a search: {run.focus}"
    await trail.record(
        session, owner, event="search_started", actor="person" if run.started_by == "manual" else "app",
        who=await actor_name(session, owner) if run.started_by == "manual" else None, agent_slug=SCOUT, sentence=how,
        link=("search_run", run.id),
    )


async def scout_handed_task(session: AsyncSession, owner: str, run_id: uuid.UUID) -> None:
    """The scout asked for its task and was given it (once per search: the second call is a profile section)."""
    from sqlalchemy import select

    from app.models.work import TrailEvent

    seen = await session.scalar(
        select(TrailEvent.id).where(
            TrailEvent.owner == owner, TrailEvent.event == "scout_handed_task", TrailEvent.sent_run_id == run_id
        ).limit(1)
    )
    if seen is not None:
        return
    await trail.record(
        session, owner, event="scout_handed_task", actor="agent", who=SCOUT_NAME, agent_slug=SCOUT,
        sentence="The scout was handed this search and your match profile.", sent_run_id=run_id,
        link=("search_run", run_id),
    )


async def scout_reported(session: AsyncSession, owner: str, run: SearchRun, staged: int, done: bool) -> None:
    if staged <= 0 and not done:
        return
    sentence = (
        f"The scout reported {_plural(staged, 'role')} and said it was done." if done and staged
        else "The scout said it was done." if done else f"The scout reported {_plural(staged, 'role')}."
    )
    await trail.record(
        session, owner, event="scout_reported", actor="agent", who=SCOUT_NAME, agent_slug=SCOUT, sentence=sentence,
        link=("search_run", run.id),
    )


async def search_finished(
    session: AsyncSession, owner: str, run: SearchRun, reported: int, kept: int, screened: int, duplicates: int,
    best: list[str],
) -> None:
    """A search ended well. It raises nothing for the owner, whether it found roles or not: they are on the
    Pipeline, and the trail line below records the search."""
    link = ("search_run", run.id)
    await trail.record(
        session, owner, event="search_finished", actor="app", agent_slug=SCOUT, tier="result", mark="on",
        sentence=(f"The search finished: {_plural(kept, 'role')} kept, {screened} screened out." if reported
                  else "The search finished: the scout reported no roles."),
        link=link,
    )


async def search_failed(session: AsyncSession, owner: str, run: SearchRun, why: str) -> None:
    """A search ended badly: tell the owner, and ask the one thing they can decide (search again)."""
    link = ("search_run", run.id)
    await service.notify(
        session, owner, key=f"search:{run.id}:failed", kind="search_failed", title="The scout couldn't finish a search",
        body=why[:300], level="attention", link=link,
    )
    await service.raise_task(
        session, owner, key=f"search:{run.id}:again", kind="search_failed", title="Search again?",
        detail="The last search didn't finish, so nothing from it was kept.",
        options=[
            {"id": "search_again", "label": "Search again", "description": "Starts a new search now."},
            {"id": "not_now", "label": "Not now"},
        ],
        recommended="search_again", link=link,
    )
    await trail.record(
        session, owner, event="search_failed", actor="app", agent_slug=SCOUT, tier="needs_you", mark="off",
        sentence="The scout couldn't finish a search, so it was closed.", link=link, detail=why[:500],
    )
