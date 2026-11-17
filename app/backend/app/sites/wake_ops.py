"""Knocking on the scout's door for a site project: the wake the loop returns (`agent_calls.Wake`), sent after commit.

Resumebot has one scout and one door (`jobsearch/work/wake.py`, the signed wake the runner accepts), so there is no
per-project agent address and no table of kept wakes. What is kept:

- A wake is **data until the commit has landed** (`agent_calls.Wake`: a reason and a mode). The code that commits calls
  `send(project_id, wake)`; the scout is never woken for work a rolled-back commit means isn't there.
- The wake on the wire is the one contract: `jobsearch/work/wake.knock(project_id, reason="site_project")`. Which kind
of
  session to start is the runner's to choose from what it finds waiting (`task.next`), not the wake's.
- `send` never raises. The first knock is awaited; one that could not be delivered is tried again in the background
  after 30 seconds, then 2 minutes, then 10 minutes (the delays), one pending retry for each project: a
  second wake for the same project while one waits changes nothing (a new failure is not a fresh start). A retry ends
  early when it is delivered, when the scout has called the app since the wake failed (it asks for its work itself,
  which is the default way it gets work; waking is optional), or when the work it was for is taken or gone. After the
  last try resumebot gives up and logs it; the runner's own check-in is the backstop.
- Nothing is kept in the database: a restart of the API drops the pending retries, and the runner's check-in finds the
  work. (the scout checks in every few minutes.) With no wake configured (`WAKE_URL` unset) there is nothing to retry:
  the knock does nothing.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime

from sqlalchemy import select

from app import db
from app.agents import roster
from app.jobsearch.work import engine_calls
from app.jobsearch.work import wake as scout_wake
from app.settings import settings
from app.sites.agent_calls import Wake
from app.sites.models import PlanItemAction, PlanTask, PlanTaskStatus, Project

log = logging.getLogger(__name__)

#: The wait before each retry, in seconds: after the failed knock, then after each failed retry. When the last one
#: fails resumebot gives up, so the number of retries is the length of this.
RETRY_DELAYS = (30, 120, 600)
#: The kinds of task the scout takes: a plan to write, an item to do, an act to run, a question to answer.
SCOUT_ACTIONS = (PlanItemAction.do, PlanItemAction.plan, PlanItemAction.act)

#: The pending retry for each project (its id as text): at most one, so wakes for one project never pile up.
_retrying: dict[str, asyncio.Task] = {}


async def _knock(project_id: str) -> bool:
    """One knock through the one door. Never raises."""
    try:
        return bool(await scout_wake.knock(project_id, reason=scout_wake.SITE_PROJECT))
    except Exception:  # a wake must never break the request that made it
        log.warning("couldn't wake the scout for project %s", project_id, exc_info=True)
        return False


async def work_waiting(project_id: str, since: datetime) -> bool:
    """Whether a wake sent at `since` is still needed: the project is open, a task is waiting for the scout, and the
    scout has not called the app since."""
    async with db.session_factory()() as session:
        project = await session.get(Project, uuid.UUID(project_id))
        if project is None or project.stopped_at is not None or project.finished_at is not None:
            return False
        from app.sites import failures  # failures imports the round, which sends wakes

        if project.id in await failures.held_projects(session, project.owner, [project.id]):
            return False  # a failure holds its round: only the owner's Try again wakes it
        conn = await roster.connection_by_slug(session, project.owner, roster.SCOUT)
        seen = max((t for t in (conn.last_seen_at, conn.declared_at) if t is not None), default=None) if conn else None
        if seen is not None and seen > since:
            return False
        return await session.scalar(
            select(PlanTask.id).where(
                PlanTask.project_id == project.id, PlanTask.status == PlanTaskStatus.todo,
                PlanTask.superseded.is_(False), PlanTask.action.in_(SCOUT_ACTIONS),
            ).limit(1)
        ) is not None


async def _retry(project_id: str, since: datetime) -> None:
    try:
        for delay in RETRY_DELAYS:
            await asyncio.sleep(delay)
            if not await work_waiting(project_id, since):
                return
            if await _knock(project_id):
                return
        log.warning("wake: gave up on project %s after %d retries", project_id, len(RETRY_DELAYS))
    except asyncio.CancelledError:
        raise
    except Exception:
        log.warning("wake: retry for project %s failed", project_id, exc_info=True)
    finally:
        _retrying.pop(project_id, None)


def _launch_retry(key: tuple[str, datetime]) -> None:
    """The task itself: `engine_calls.start("wake_retry", key)` runs this in the program that does the work. `key` is
    (the project's id as text, when the failed wake was sent). The pending retry is kept in `_retrying`."""
    project_id, since = key
    _retrying[project_id] = asyncio.ensure_future(_retry(project_id, since))


engine_calls.register("wake_retry", _launch_retry)


async def send(project_id: uuid.UUID | str, wake: Wake | None) -> bool:
    """Wake the scout for a project, once its commit has landed. `wake` is what the loop returned (None: nothing is
    due, nothing is sent). True when the scout took the knock. A knock that could not be delivered is kept for a retry
    (see the module's words); never raises."""
    if wake is None:
        return False
    key = str(project_id)
    since = datetime.now(UTC)
    if await _knock(key):
        pending = _retrying.pop(key, None)  # it has just been knocked for: any wake still waiting is done
        if pending is not None:
            pending.cancel()
        return True
    if settings.wake_url and key not in _retrying:
        engine_calls.start("wake_retry", (key, since))
    return False


async def send_all(wakes: Iterable[tuple[uuid.UUID | str, Wake | None]]) -> list[str]:
    """`send` for each `(project id, wake)`, one knock for each project (a project woken twice is woken once). Returns
    the ids of the projects the scout took a knock for."""
    taken: list[str] = []
    seen: set[str] = set()
    for project_id, wake in wakes:
        if wake is None or str(project_id) in seen:
            continue
        seen.add(str(project_id))
        if await send(project_id, wake):
            taken.append(str(project_id))
    return taken


async def settle() -> None:
    """Wait for the retries now pending (a test, or a clean stop)."""
    pending = list(_retrying.values())
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
