"""The Agents page's routes: who is here (the app's version), the roster, one agent, pause and resume, and Activity (the
trail). The roster and the trail keep what this app needs (a roster, a detail,
Pause, a trail); there is one owner, so no account or member id is in an address. Connecting, token and wake-secret
management, skills and boundaries are not here."""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents import roster
from app.db import ReadSession, Session
from app.errors import NotFound
from app.jobsearch.actors import actor_name
from app.settings import VERSION, settings
from app.trail import service as trail

router = APIRouter(prefix="/api/v1", tags=["agents"])

Filter = Literal["all", "needs_look", "highlights"]


class EditAgent(BaseModel):
    paused: bool | None = None


def _limit(limit: int) -> int:
    return max(1, min(limit, 100))


@router.get("/me")
async def me() -> dict:
    # The owner's zone rides along, so the page writes every time on the clock the server's own words use.
    return {"version": VERSION, "timezone": settings.timezone or "UTC"}


@router.get("/agents")
async def list_agents(session: ReadSession):
    return await roster.listing(session, settings.owner)


async def _conn(session: AsyncSession, connection_id: uuid.UUID):
    conn = await roster.get(session, settings.owner, connection_id)
    if conn is None:
        raise NotFound("no such agent")
    return conn


@router.get("/agents/{connection_id}")
async def get_agent(connection_id: uuid.UUID, session: ReadSession):
    conn = await _conn(session, connection_id)
    return await roster.detail(session, settings.owner, conn, await roster.role_titles(session))


@router.patch("/agents/{connection_id}")
async def edit_agent(connection_id: uuid.UUID, body: EditAgent, session: Session, reader: ReadSession):
    conn = await _conn(session, connection_id)
    if body.paused is not None and body.paused != conn.paused:
        conn.paused = body.paused
        await trail.record(
            session, settings.owner, event="agent_paused" if body.paused else "agent_resumed", actor="person",
            who=await actor_name(session, settings.owner), agent_slug=conn.slug,
            sentence=f"{conn.name} was paused: it is handed no work until you resume it." if body.paused
            else f"{conn.name} was resumed.",
        )
    await session.commit()
    # The reply is read on a reader once the write is committed: the writer's turn is for writing.
    shown = await _conn(reader, connection_id)
    return await roster.detail(reader, settings.owner, shown, await roster.role_titles(reader))


@router.get("/agents/{connection_id}/trail")
async def agent_trail(
    connection_id: uuid.UUID, session: ReadSession, filter: Filter = "all", before: str | None = None, limit: int = 20,
):
    conn = await _conn(session, connection_id)
    return await trail.page(
        session, settings.owner, agent_slug=conn.slug, filter=filter, before=before, limit=_limit(limit)
    )


@router.get("/trail")
async def all_trail(session: ReadSession, filter: Filter = "all", before: str | None = None, limit: int = 20):
    return await trail.page(session, settings.owner, filter=filter, before=before, limit=_limit(limit))
