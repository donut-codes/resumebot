"""Check my roles and Fits none of my roles, over HTTP (docs/PRD.md 31.2 and 31.3), under What to look for:

- `POST /profile/check` asks for a dry run of the title check (`jobs/check.py`) over the roles the owner pursued, a
  sample of the postings the title check stopped, or one pasted posting; `GET /profile/check/{id}` says
  how it is going and, when done, what it found. A small Claude session judges, woken by the request
  (`wake.CHECK_ASKED`); whether an agent can take it now is `GET /agent/ready`.
- `GET /profile/fits-none` counts and lists the roles the owner pursued that fit none of their target roles and the
  postings found this week that fit none (`fits_none.py`, counted from the database); a role or posting can be marked
  fine as it is, and a draft target role can be made from one (not saved).
"""

from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from app.db import ReadSession, Session
from app.errors import NotFound
from app.jobsearch.jobs import check as role_check
from app.jobsearch.jobs import fits_none
from app.jobsearch.work import engine_calls
from app.settings import settings

router = APIRouter(prefix="/api/v1/profile")


class CheckBody(BaseModel):
    #: pursued | gated | posting
    scope: str = Field(max_length=10)
    #: The pasted posting (scope posting).
    text: str | None = None
    #: How many stopped postings to sample (scope gated), within the bounds.
    sample: int | None = Field(default=None, ge=1, le=role_check.MAX_SAMPLE * 10)


@router.post("/check")
async def check_start(body: CheckBody, session: Session):
    """Ask for a check of the owner's role tabs. The runner is woken for it. 409 with `why` when no agent can take it
    now; 422 in words when it cannot be asked for (no role yet, nothing to check, a posting too short)."""
    try:
        job, new = await role_check.start(session, settings.owner, body.scope, body.text, body.sample)
    except role_check.CheckError:  # a NotReady is answered 409 with `why`, any other 422
        await session.commit()  # the jobs that took too long were failed on the way
        raise
    await session.commit()
    if new or job.state == role_check.WAITING:
        await engine_calls.knock(str(job.id), reason=engine_calls.CHECK_ASKED)
    return {"id": str(job.id)}


@router.get("/check/{job_id}")
async def check_read(job_id: str, session: ReadSession) -> dict:
    """Where a check is: waiting for an agent, working (with how far along), done (with its result) or failed (with
    why). A result is kept for a day."""
    told = await role_check.told(session, settings.owner, job_id)  # on a reader: a late job is told failed
    if told is None:
        raise NotFound("That check is gone. Ask for it again.")
    return told


@router.get("/fits-none")
async def fits_none_read(
    session: ReadSession,
    limit: Annotated[int, Query(ge=1, le=fits_none.MOST_PER_PAGE)] = fits_none.PAGE,
    pursued_offset: Annotated[int, Query(ge=0)] = 0,
    this_week_offset: Annotated[int, Query(ge=0)] = 0,
) -> dict:
    """What the role tabs miss: `{pursued: {count, items}, this_week: {count, items}}`. The roles the owner pursued with
    no target role, and the postings found in the last 7 days that fit none, each with a page of items (`limit` of them
    from its offset), newest first: `{id, kind, title, company, stage, stage_label, found_at}`."""
    return await fits_none.view(session, settings.owner, pursued_offset, this_week_offset, limit)


@router.post("/fits-none/{subject_id}/dismiss")
async def fits_none_dismiss(subject_id: str, session: Session, reader: ReadSession) -> dict:
    """Mark a role (or a posting from this week's list) fine as it is: it is kept out of what fits none of the owner's
    roles. Answers with the new counts."""
    done = await fits_none.dismiss(session, settings.owner, subject_id)
    await session.commit()
    # The new counts are read on a reader once the mark is committed: the writer's turn is for writing.
    return {**done, **await fits_none.counts(reader, settings.owner)}


@router.post("/roles/from-role/{subject_id}")
async def role_from_role(subject_id: str, session: ReadSession) -> dict:
    """A target role drafted from a role (or a posting): name, an example title, a description from its text. Nothing is
    saved: the page adds it as a new tab, and it is kept when the owner saves the page."""
    return await fits_none.draft(session, settings.owner, subject_id)
