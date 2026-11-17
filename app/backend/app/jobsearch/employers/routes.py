"""The employers over HTTP (docs/PRD.md path 21): every employer the app knows, one employer whole, and facts set by
hand; and, by hand too, merging an employer into another and splitting roles out of one (path 36). An employer there is
none of is 404; a fact that does not fit, or a merge or split that cannot be done, is 422 (`employers.EmployerError`).

    GET   /employers?view=&q=&industry=&cursor=&limit=   a page of the employers in a view, with each view's count
    PUT   /employers/watch-in-play         {on}: watch the job board of every employer with a role in play, or stop
    GET   /employers/{id}                  one whole
    PATCH /employers/{id}                  set facts; each becomes the owner's
    GET   /employers/{id}/merge?into=      what merging this employer into that one would move (changes nothing)
    POST  /employers/{id}/merge            {into}: merge this employer into that one, which stays (and the list rows
                                           it changed, so a page replaces them)
    GET   /employers/{id}/split?roles=&name=   what moving those roles (ids, comma between) to that name would move
    POST  /employers/{id}/split            {roles, name}: move them to the employer of that name (made when none is),
                                           and the list rows it changed

The watch-in-play switch is shown in Where to look (`GET /sites` carries it, docs/PRD.md 45); its route stays here.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import ReadSession, Session
from app.jobsearch import off_turn
from app.jobsearch.actors import BY_OWNER
from app.jobsearch.employers import core as employers
from app.jobsearch.employers import records as employer_records
from app.jobsearch.employers.core import EmployerError
from app.jobsearch.models import Employer
from app.jobsearch.work import engine_calls
from app.settings import settings
from app.sites import auto_watch, where

router = APIRouter(prefix="/api/v1")
#: How long the change of an employer's gate waits for the engine's check, in seconds. Past it the route answers the
#: employer as saved, and the ask stays for the engine.
ENGINE_WAIT_S = 15.0


class WatchInPlay(BaseModel):
    """The owner's setting at the top of Employers: watch the job board of every employer with a role in play
    (`auto_watch.py`, docs/PRD.md 29.9)."""

    on: bool


class MergeIn(BaseModel):
    #: The employer that stays; the one in the address is merged into it.
    into: uuid.UUID


class SplitIn(BaseModel):
    roles: list[uuid.UUID] = Field(min_length=1, max_length=500)
    #: The employer the roles move to: the one with this name, else a new one.
    name: str = Field(min_length=1)


@router.get("/employers")
async def employer_list(session: ReadSession, view: str = "all", q: str = "", industry: str = "",
                        cursor: str | None = None,
                        limit: Annotated[int, Query(ge=1, le=employers.MOST_PER_PAGE)] = employers.PER_PAGE) -> dict:
    """A page of the employers in a view (`all`, `open`, `watched`, `hard_pass`, `local`), by name, narrowed to a name
    and an industry: `employers` (each with how many roles it has, how many are open and how many are in Ready to
    Launch or later), `next` (the cursor of the page after), `total`, `counts` (each view's, of every employer) and
    `industries`."""
    return await employers.listing(session, settings.owner, view=view, q=q, industry=industry, cursor=cursor,
                                   limit=limit)


@router.put("/employers/watch-in-play")
async def watch_in_play(body: WatchInPlay, session: Session) -> dict:
    """Switch watching every employer with a role in play on or off (a field of the Settings profile, so the change is
    its next version). On, every employer with a role in play is watched now (the ones the owner switched off by hand,
    the gated and the unreadable are left alone). Off, nothing new is watched; the employers already watched stay
    watched until their roles leave play."""
    saved = await auto_watch.set_on(session, settings.owner, body.on)
    await session.commit()
    return {"on": saved["on"]}


@router.get("/employers/{employer_id}")
async def employer_detail(employer_id: uuid.UUID, session: ReadSession) -> dict:
    return await employers.detail(session, settings.owner, employer_id)


@router.patch("/employers/{employer_id}")
async def employer_edit(employer_id: uuid.UUID, body: dict, session: Session) -> dict:
    """Set facts by hand; each becomes the owner's, which research leaves alone. Gating or ungating an employer is saved
    here and checked by the engine (the ask `employer.regate`: the log and the roles, the trail, which employers are
    watched), which this waits for at most 15 seconds. `said` is the line the check says of what it changed; it is
    absent when the engine did not answer in time (the check is kept and runs when it can)."""
    edited = await employers.edit(session, settings.owner, employer_id, body, defer_gate_change=True)
    flipped = bool(edited.pop("gate_changed", False))
    said = None
    if flipped:  # the flag is saved with the ask, which commits before it waits
        asked = await engine_calls.ask(session, settings.owner, "employer.regate",
                                       {"employer_id": str(employer_id), "by": BY_OWNER}, ENGINE_WAIT_S)
        said = (asked or {}).get("said")  # what gating or ungating it did to the log and the roles (PRD 39.14)
    else:
        if "gated" in body:  # a blocked employer is not watched for a role in play, and one let back in is (PRD 29.9)
            await where.settle_watch(session, settings.owner)
        await session.commit()
    if "gated" in body:  # the employer as it is now, watch included, read apart from the writer's turn
        async with off_turn.reading(session) as reader:
            if (employer := await reader.get(Employer, employer_id)) is not None:
                edited = {**employers.view(employer), **({"said": said} if said else {})}
    return edited


@router.get("/employers/{employer_id}/merge")
async def employer_merge_preview(employer_id: uuid.UUID, into: uuid.UUID, session: ReadSession) -> dict:
    """What merging this employer into `into` would move, counted: nothing is changed."""
    return await employer_records.preview_merge(session, settings.owner, into, employer_id)


async def _save_and_refresh(session: AsyncSession, refresh: dict | None) -> None:
    """Commit what a merge or a split saved. When roles moved (`refresh`, from `records.merge` or `split` with
    `defer_refresh`) the engine is asked to work their gates and scores out again (the ask `employer.refresh`) and this
    waits `ENGINE_WAIT_S` seconds for it; with no answer in time the merge or split is answered as it is saved, and the
    ask is kept for the engine. Nothing is asked when no role moved."""
    if refresh is None:
        await session.commit()
    else:
        await engine_calls.ask(session, settings.owner, "employer.refresh", refresh, ENGINE_WAIT_S)


@router.post("/employers/{employer_id}/merge")
async def employer_merge(employer_id: uuid.UUID, body: MergeIn, session: Session) -> dict:
    """Merge this employer into the one in the body, which stays: every role, posting, site and name moves over. Asked
    again, it does nothing (`merged` is false). `rows` is the list row of the one that stays, `removed` the one that
    went, and `counts` each view's count now. The page saves the merge; the engine works the gates and scores of the
    moved roles out again (the ask `employer.refresh`), which this waits for at most 15 seconds, and the answer is read
    after."""
    done = await employer_records.merge(session, settings.owner, body.into, employer_id, why="you merged them by hand",
                                        defer_refresh=True)
    kept_id = done.employer.id
    await _save_and_refresh(session, done.refresh)
    async with off_turn.reading(session) as reader:  # read once the merge is committed and the engine has worked
        detail = await employers.detail(reader, settings.owner, kept_id)
        rows = await employers.rows(reader, settings.owner, [kept_id])
        counts = await employers.view_counts(reader, settings.owner)
    return {"merged": done.merged, "employer": detail, "rows": rows, "removed": [str(employer_id)], "counts": counts}


@router.get("/employers/{employer_id}/split")
async def employer_split_preview(employer_id: uuid.UUID, roles: str, name: str, session: ReadSession) -> dict:
    """What splitting these roles (ids, a comma between) out to the employer of that name would move: nothing is
    changed."""
    wanted = [r for r in roles.split(",") if r.strip()]
    if not wanted:
        raise EmployerError("choose the roles to move")
    return await employer_records.preview_split(session, settings.owner, employer_id, role_ids=wanted, new_name=name)


@router.post("/employers/{employer_id}/split")
async def employer_split(employer_id: uuid.UUID, body: SplitIn, session: Session) -> dict:
    """Move the roles (with the postings they came from, and the names only they were found under) to the employer of
    that name, made when there is none. Asked again, it does nothing more. `rows` is the list rows of both employers,
    and `counts` each view's count now. The page saves the split; the engine works the gates and scores of the moved
    roles out again (the ask `employer.refresh`), which this waits for at most 15 seconds, and the answer is read
    after."""
    done = await employer_records.split(session, settings.owner, employer_id, role_ids=body.roles, new_name=body.name,
                                        why="you split them out by hand", defer_refresh=True)
    target_id = done.employer.id
    await _save_and_refresh(session, done.refresh)
    async with off_turn.reading(session) as reader:  # read once the split is committed and the engine has worked
        detail = await employers.detail(reader, settings.owner, target_id)
        rows = await employers.rows(reader, settings.owner, [employer_id, target_id])
        counts = await employers.view_counts(reader, settings.owner)
    return {"created": done.created, "roles": done.roles, "postings": done.postings, "names": list(done.names),
            "still_gated_out": done.still_gated_out, "employer": detail, "rows": rows, "counts": counts}
