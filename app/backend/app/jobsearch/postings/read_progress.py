"""Where the night's board read has got to, kept on its mission run (`MissionRun.started`) so that a read the app's
restart cut short carries on with the sites and the job texts it had not reached (`missions.settle_after_restart`)
and the mission page can say how far it is (`label`).

The read's own task sets `READ_RUN` to its run; anywhere else (a test, a request) no run is set and every call here
does nothing. Each note is written in a session of its own, so it never waits on the read's step: the caller hands its
session over and a note first ends that session's transaction (`db.release_if_free`: the writer's turn is one for a
file, so a second writer opened while the step's transaction holds it would wait for the step, which waits for it), so
what is noted as done is done on disk. What the run holds is read in a reader's session, which takes no turn."""

import uuid
from collections.abc import Callable
from contextvars import ContextVar

from sqlalchemy.ext.asyncio import AsyncSession

from app import db
from app.jobsearch.models import MissionRun

READ_RUN: ContextVar[uuid.UUID | None] = ContextVar("read_run", default=None)


async def _started() -> dict:
    run_id = READ_RUN.get()
    if run_id is None:
        return {}
    async with db.read_session_factory()() as session:
        entry = await session.get(MissionRun, run_id)
    return dict(entry.started or {}) if entry is not None else {}


async def _change(session: AsyncSession, change: Callable[[dict], None]) -> None:
    run_id = READ_RUN.get()
    if run_id is None:
        return
    await db.release_if_free(session)  # the step's own transaction holds the turn this note's session asks for
    async with db.session_factory()() as noting:
        entry = await noting.get(MissionRun, run_id)
        if entry is None:
            return
        started = dict(entry.started or {})
        change(started)
        entry.started = started
        await noting.commit()


async def reset(session: AsyncSession) -> None:
    """A read that begins afresh has done nothing yet."""
    def clear(started: dict) -> None:
        for key in ("sites_done", "read_parts", "details_done", "details_total"):
            started.pop(key, None)
    await _change(session, clear)


async def sites_done() -> set[str]:
    """The ids of the sites this run has finished reading (empty when there is no run)."""
    return set((await _started()).get("sites_done") or [])


async def note_sites(session: AsyncSession, ids: list[uuid.UUID]) -> None:
    def add(started: dict) -> None:
        started["sites_done"] = [*dict.fromkeys([*(started.get("sites_done") or []), *map(str, ids)])]
    if ids:
        await _change(session, add)


async def set_part(session: AsyncSession, name: str, count: int) -> None:
    """How many sites one of the read's two passes has to read (the built-in readers, the recipes)."""
    def put(started: dict) -> None:
        started["read_parts"] = {**(started.get("read_parts") or {}), name: count}
    await _change(session, put)


async def details_before() -> int:
    """The job texts this run has already fetched, before this stretch of it."""
    return int((await _started()).get("details_done") or 0)


async def set_details_total(session: AsyncSession, count: int) -> None:
    """The texts the read has to fetch, fixed the first time it is known (a resumed read has fewer waiting)."""
    def put(started: dict) -> None:
        started.setdefault("details_total", count)
    if READ_RUN.get() is not None and (await _started()).get("details_total") is not None:
        await db.release_if_free(session)  # a resumed read has its total: nothing to write, no turn to take for it
        return
    await _change(session, put)


async def note_details(session: AsyncSession, count: int) -> None:
    def add(started: dict) -> None:
        started["details_done"] = int(started.get("details_done") or 0) + count
    if count:
        await _change(session, add)


def label(started: dict) -> dict | None:
    """The run's progress for the page, while it reads: `done` and `target` are its sites (the page shows them), and
    `text` says it all: "Sites 20 of 40 · job texts 100 of 300". None before the first site count is known."""
    total = sum((started.get("read_parts") or {}).values())
    if not started.get("reading") or total <= 0:
        return None
    done = min(len(started.get("sites_done") or []), total)
    text = f"Sites {done:,} of {total:,}"
    if int(started.get("details_total") or 0) > 0:
        text += f" · job texts {int(started.get('details_done') or 0):,} of {int(started['details_total']):,}"
    return {"done": done, "target": total, "text": text}
