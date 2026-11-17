"""Fill from a posting, over HTTP (What to look for, inside a target role): whether an agent can take the work now, a
pasted posting to split into the role's fields, and how that is going. The work itself is `jobs/parse.py`; a small
Claude session does the splitting, woken by the request (`wake.PARSE_ASKED`).

Import your resume (Me, the master resume) is the same job in its resume mode: a resume's text, pasted or read from a
chosen file, to divide into the master resume's fields (`resume/parse.py`, `resume/text.py`). How either is going is
asked the same way, with the job's id."""

import asyncio
from typing import Annotated

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import ReadSession, Session
from app.errors import NotFound, TooLarge
from app.jobsearch.jobs import parse as role_parse
from app.jobsearch.resume import parse as resume_parse
from app.jobsearch.resume import text as resume_text
from app.jobsearch.work import engine_calls
from app.settings import settings

router = APIRouter(prefix="/api/v1")


class ParseBody(BaseModel):
    #: The posting's text as the owner pasted it.
    text: str
    #: The target role's tab it was asked from; the answer carries it back.
    role_id: str | None = Field(default=None, max_length=80)

    @field_validator("text")
    @classmethod
    def _enough(cls, v: str) -> str:
        v = v.strip()
        if len(v) < role_parse.MIN_CHARS:
            raise ValueError(f"paste the whole posting: at least {role_parse.MIN_CHARS} characters")
        return v


class ResumeBody(BaseModel):
    #: The resume's text as the owner pasted it. It has no most of its own: what bounds it is what an agent can be sent
    #: with the answer shape (`_start_resume`).
    text: str

    @field_validator("text")
    @classmethod
    def _enough(cls, v: str) -> str:
        v = v.strip()
        if len(v) < resume_parse.MIN_CHARS:
            raise ValueError(f"paste the whole resume: at least {resume_parse.MIN_CHARS} characters")
        return v


async def _start(session: AsyncSession, text: str, role_id: str | None = None, kind: str = role_parse.POSTING):
    """The text as a waiting job, the runner woken for it once it is saved; 409 with `why` when no agent can take it."""
    try:
        job = await role_parse.start(session, settings.owner, text, role_id, kind=kind)
    except role_parse.NotReady:  # answered 409 with `why`
        await session.commit()  # the jobs that took too long were failed on the way
        raise
    await session.commit()
    await engine_calls.knock(str(job.id), reason=engine_calls.PARSE_ASKED)
    return {"id": str(job.id)}


async def _start_resume(session: AsyncSession, text: str, *, what: str, fix: str):
    """A resume's text as a waiting job, once an agent can be sent it whole (`role_parse.refuse_resume_over_room`: over
    the room, 413 in words with both sizes, before a job is made; the text is never cut)."""
    await role_parse.refuse_resume_over_room(session, settings.owner, text, what=what, fix=fix)
    return await _start(session, text, kind=role_parse.RESUME)


@router.post("/profile/parse")
async def parse_start(body: ParseBody, session: Session):
    """Ask for a pasted posting to be split into a role's fields. The runner is woken for it. 409 with `why` when no
    agent can take it now."""
    return await _start(session, body.text, body.role_id)


@router.post("/profile/parse/resume")
async def parse_resume_text(body: ResumeBody, session: Session):
    """Ask for a pasted resume to be divided into the master resume's fields. The runner is woken for it. 413 in words
    when an agent could not be sent it whole; 409 with `why` when no agent can take it now."""
    return await _start_resume(session, body.text, what="Your resume text", fix="Check that it is your resume.")


@router.post("/profile/parse/resume/file")
async def parse_resume_file(request: Request, session: Session, name: Annotated[str, Query(max_length=300)]):
    """Ask for a resume file to be divided into the master resume's fields: the file's bytes are the body and `name` is
    its name (a `.docx`, `.pdf` or `.txt`). The server reads its text, which is all that is kept. 422, in words, when
    the file cannot be read or has no text; 413 in words when the file is over 5 MB or an agent could not be sent its
    text whole; 409 with `why` when no agent can take it now."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > resume_text.MAX_BYTES:
        raise TooLarge(resume_text.TOO_BIG)
    data = await request.body()
    text = await asyncio.to_thread(resume_text.extract, name, data)  # a ReadError is answered 422, in words
    return await _start_resume(session, text, what="This file's text", fix="Check that it is the right file.")


@router.get("/profile/parse/{job_id}")
async def parse_read(job_id: str, session: ReadSession) -> dict:
    """Where a parse is: waiting for an agent (queued, saying so, while a session is at work), reading, done (with the
    fields) or failed (with why)."""
    told = await role_parse.told(session, settings.owner, job_id)  # on a reader: a late job is told failed
    if told is None:
        raise NotFound("That example is gone. Paste the posting again.")
    return told
