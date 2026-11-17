"""Interviews over HTTP (docs/PRD.md 15.2.1, 15.2.2): add one to a role in Active Discussion, correct it, remove it, and
ask for the next version of its report. A role's interviews come with the role (`roles.detail`). Each refusal answers
with its own status (`interviews/core.py`): a role or interview there is none of is 404, a role in another stage 409, a
form
with a
problem 422. Adding one, or asking again, wakes the Coach."""

import uuid

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import select

from app import db
from app.db import Session
from app.jobsearch.interviews import core as interviews
from app.jobsearch.models import InterviewPrep
from app.jobsearch.work import engine_calls
from app.settings import settings

router = APIRouter(prefix="/api/v1")


class Person(BaseModel):
    id: str | None = None
    name: str
    title: str = ""


class NewInterview(BaseModel):
    #: The date and time with its zone, as the browser's `toISOString` gives it.
    when: str
    round_kind: str = Field(max_length=20)
    label: str = ""
    people: list[Person] = []
    notes: str = ""


class InterviewChanges(BaseModel):
    when: str | None = None
    round_kind: str | None = Field(default=None, max_length=20)
    label: str | None = None
    people: list[Person] | None = None
    notes: str | None = None


def _people(people: list[Person] | None) -> list[dict] | None:
    return None if people is None else [p.model_dump() for p in people]


async def _asked_on_a_reader(interview_id: uuid.UUID) -> dict:
    """The interview as the page reads it, after a write was committed: read on a reader, which takes no turn."""
    async with db.read_session_factory()() as reader:
        return await interviews.one(reader, settings.owner, interview_id)


async def _waiting(session: Session, interview_id: uuid.UUID) -> uuid.UUID | None:
    """The interview's prep that waits for the Coach, if there is one (read before the commit, so the knock that follows
    it is outside any transaction)."""
    return await session.scalar(select(InterviewPrep.id).where(
        InterviewPrep.interview_id == interview_id, InterviewPrep.state == "waiting"))


async def _commit_and_knock(session: Session, interview_id: uuid.UUID) -> None:
    prep_id = await _waiting(session, interview_id)
    await session.commit()
    if prep_id is not None:
        await engine_calls.knock(str(prep_id), reason=engine_calls.COACH_ASKED)


@router.post("/roles/{role_id}/interviews")
async def add_interview(role_id: uuid.UUID, body: NewInterview, session: Session) -> dict:
    """Add an interview to a role in Active Discussion; one whose time is ahead starts its prep."""
    made = await interviews.add(session, settings.owner, role_id, when=body.when, round_kind=body.round_kind,
                                label=body.label, people=_people(body.people), notes=body.notes)
    await _commit_and_knock(session, made.id)
    return await _asked_on_a_reader(made.id)


@router.patch("/interviews/{interview_id}")
async def edit_interview(interview_id: uuid.UUID, body: InterviewChanges, session: Session) -> dict:
    """Correct an interview. It starts no report: the card says what changed and offers Prepare again."""
    changes = body.model_dump(exclude_unset=True)
    if "people" in changes:
        changes["people"] = _people(body.people)
    await interviews.edit(session, settings.owner, interview_id, **changes)
    await session.commit()
    return await _asked_on_a_reader(interview_id)


@router.delete("/interviews/{interview_id}")
async def remove_interview(interview_id: uuid.UUID, session: Session) -> dict:
    await interviews.remove(session, settings.owner, interview_id)
    await session.commit()
    return {"ok": True}


@router.post("/interviews/{interview_id}/prepare")
async def prepare_again(interview_id: uuid.UUID, session: Session) -> dict:
    """Make the next version of the report (409 while one is open or when the time has passed)."""
    await interviews.prepare_again(session, settings.owner, interview_id)
    await _commit_and_knock(session, interview_id)
    return await _asked_on_a_reader(interview_id)

