"""The Inbox and Tasks screens' API (all under `/api/v1`, one owner, so no account id in the address).

    GET  /inbox                        what needs you (at most three), what waits, the notifications, the badge count
    GET  /inbox/history                past decisions (a record, never a to-do)
    GET  /tasks                        the task list: for you, for the agents, finished lately
    GET  /tasks/{id}
    POST /tasks/{id}/done              {note?}
    POST /tasks/{id}/skip              {reason?}
    POST /tasks/{id}/answer            {option, note?}   (a decision; "Search again" starts a search; a site project's
                                       plan, stuck planner or held round is carried out; "Use the shipped version" saves
                                       the file a release shipped as the next version; "Merge" merges two employer
                                       records, "Split them out" splits roles out of one; `note` is the owner's words,
                                       with no limit; a site answer that an agent could not be sent whole with its
                                       instruction file is refused 413)
    POST /tasks/{id}/snooze            {until: "YYYY-MM-DD" | null}
    GET  /notifications                the latest, unread first
    POST /notifications/{id}/read      and POST /notifications/read-all
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents import edit_process
from app.db import ReadSession, Session
from app.errors import Conflict
from app.inbox import service
from app.jobsearch.employers import pairs as employer_pairs
from app.jobsearch.mail import act as mail_act
from app.jobsearch.missions import search_run as run
from app.jobsearch.work import engine_calls
from app.models.agent import AgentConnection
from app.models.work import TASK_OPEN
from app.settings import settings
from app.sites import inbox_asks
from app.sites.base import PlanEngineError

router = APIRouter(prefix="/api/v1", tags=["inbox"])


class DoneIn(BaseModel):
    note: str | None = None


class SkipIn(BaseModel):
    reason: str | None = None


class AnswerIn(BaseModel):
    option: str = Field(max_length=40)
    #: The owner's own words with the answer (what to change in a plan sent back). No limit: kept as typed.
    note: str | None = None


class SnoozeIn(BaseModel):
    until: date | None = None


async def _agent_names(session: AsyncSession) -> dict[str, str]:
    rows = (await session.execute(
        select(AgentConnection.slug, AgentConnection.name).where(AgentConnection.owner == settings.owner)
    )).all()
    return {slug: name for slug, name in rows}


@router.get("/inbox")
async def inbox(session: ReadSession):
    return await service.inbox(session, settings.owner)


@router.get("/inbox/history")
async def history(session: ReadSession, before: datetime | None = None, limit: int = 20):
    return await service.history(session, settings.owner, before, max(1, min(limit, 100)))


@router.get("/tasks")
async def tasks(session: ReadSession):
    return await service.task_list(session, settings.owner, await _agent_names(session))


@router.get("/tasks/{task_id}")
async def task(task_id: uuid.UUID, session: ReadSession):
    found = await service.get_task(session, settings.owner, task_id)
    return service.task_view(found, await _agent_names(session))


async def _refuse_site_question(session: AsyncSession, task_id: uuid.UUID) -> None:
    """A site loop's question with choices is answered with one of them: Done and Set aside would close the card and
    leave the work it asked about waiting (`inbox_asks.apply_answer` never ran). A question with no choices (asked
    before it had any) can still be closed."""
    found = await service.get_task(session, settings.owner, task_id)
    if found.state == TASK_OPEN and found.kind in inbox_asks.KINDS and found.options:
        raise Conflict("answer this one with one of its choices")


@router.post("/tasks/{task_id}/done")
async def done(task_id: uuid.UUID, body: DoneIn, session: Session):
    await _refuse_site_question(session, task_id)
    found = await service.mark_done(session, settings.owner, task_id, body.note)
    await session.commit()
    return service.task_view(found)


@router.post("/tasks/{task_id}/skip")
async def skip(task_id: uuid.UUID, body: SkipIn, session: Session):
    await _refuse_site_question(session, task_id)
    found = await service.skip(session, settings.owner, task_id, body.reason)
    await session.commit()
    return service.task_view(found)


@router.post("/tasks/{task_id}/answer")
async def answer(task_id: uuid.UUID, body: AnswerIn, session: Session):
    """Answer a decision. "Search again" on a failed search starts one first: if it can't start (a search is open, the
    scout is paused) the decision stays open and the reason is the answer. A site project's question (its plan, a
    planner that could not finish, a round that was held) is carried out too (`sites.inbox_asks.apply_answer`): one the
    round could not carry out stays open (it is held, and the Inbox says why), and a refusal leaves it as it was."""
    wakes: list = []
    try:
        found = await service.get_task(session, settings.owner, task_id)
        if found.kind in inbox_asks.KINDS and found.state == TASK_OPEN:
            # The words go to an agent whole, beside its instruction file: refused (413, in words) before anything is
            # changed when the two would not fit in one reply.
            await inbox_asks.refuse_over_room(session, found, body.note)
        started = None
        if found.kind == "search_failed" and body.option == "search_again" and found.state == TASK_OPEN:
            started = await run.start_round(session, settings.owner, started_by="manual")
        found, _label = await service.answer(session, settings.owner, task_id, body.option, body.note)
        if found.kind in inbox_asks.KINDS:
            applied = await inbox_asks.apply_answer(session, found, body.option, body.note)
            if not applied.carried:
                inbox_asks.reopen(found)
                await session.commit()  # what the hold wrote (the Inbox's "couldn't ...") is kept
                raise Conflict("resumebot couldn't carry that out just now; it is held, and Notifications says why")
            wakes = applied.wakes
        elif found.kind in employer_pairs.KINDS:
            # "Merge" merges the two employer records, "Split them out" splits the roles out (docs/PRD.md 36.5, 36.6); a
            # refusal is the employer error's own (422) and leaves the decision open.
            await employer_pairs.apply_answer(session, settings.owner, found, body.option)
        elif found.kind == mail_act.TASK_KIND:
            # The owner chose which role a mail is about: its kind is carried out for that role (docs/PRD.md 14.5).
            await mail_act.apply_answer(session, settings.owner, found, body.option)
        await session.commit()
    except (run.RunError, PlanEngineError, edit_process.EditError) as exc:
        # Whatever status the error has elsewhere, a decision the app could not carry out is a conflict here.
        raise Conflict(str(exc)) from exc
    if started is not None:
        await engine_calls.knock(str(started.id))
    await engine_calls.send_wakes(wakes)
    return service.task_view(found)


@router.post("/tasks/{task_id}/snooze")
async def snooze(task_id: uuid.UUID, body: SnoozeIn, session: Session):
    found = await service.snooze(session, settings.owner, task_id, body.until)
    await session.commit()
    return service.task_view(found)


@router.get("/notifications")
async def notifications(session: ReadSession):
    shown = await service.inbox(session, settings.owner)
    return {"notifications": shown["notifications"], "unread_count": shown["unread_count"]}


@router.post("/notifications/read-all")
async def read_all(session: Session):
    changed = await service.mark_read(session, settings.owner, None)
    await session.commit()
    return {"changed": changed}


@router.post("/notifications/{note_id}/read")
async def read_one(note_id: uuid.UUID, session: Session):
    changed = await service.mark_read(session, settings.owner, note_id)
    await session.commit()
    return {"changed": changed}
