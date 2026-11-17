"""The missions over HTTP (docs/PRD.md path 18): the list with each mission's schedule and last run, the settings they
run with, run now, pause, resume, and change.

A mission there is none of is 404, one already running is 409, a schedule or setting that does not fit is 422
(`missions.MissionError`, `mission_numbers.NumbersError`); Claude work asked for inside the peak hours the owner chose
to avoid is 409 (`night.PeakHours`)."""

import uuid
from datetime import datetime

from fastapi import APIRouter
from pydantic import BaseModel

from app import db
from app.db import ReadSession, Session
from app.errors import Busy, DomainError
from app.jobsearch.actors import BY_OWNER, actor_name
from app.jobsearch.missions import core as missions
from app.jobsearch.missions import numbers as mission_numbers
from app.jobsearch.profile import store as profile_store
from app.jobsearch.work import engine_calls
from app.jobsearch.work import hours as night
from app.settings import settings

router = APIRouter(prefix="/api/v1")

#: How long the page waits for the engine to carry out Run now before it says the engine did not answer.
WAIT_SECONDS = 20


class _Refused(DomainError):
    """A refusal the engine's Run now gave (`missions.asked.run`), said again with the status and words it had."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class PauseBody(BaseModel):
    until: datetime | None = None
    why: str = ""


class MissionChangeBody(BaseModel):
    schedule: dict | None = None
    settings: dict | None = None
    why: str = ""


class NumbersBody(BaseModel):
    values: dict
    why: str = ""


def _peak_section(profile_settings) -> dict:
    """Claude's peak hours as the Missions screen draws them: where they stand now, and the settings that set them."""
    return {**night.peak_view(profile_settings),
            "numbers": mission_numbers.fields(mission_numbers.PEAK, profile_settings)}


async def _detail(mission_id: uuid.UUID) -> dict:
    """The mission as the page reads it, after a write was committed: read on a reader, which takes no turn."""
    async with db.read_session_factory()() as reader:
        return await missions.detail(reader, settings.owner, mission_id)


@router.get("/missions")
async def mission_list(session: ReadSession) -> dict:
    """Every mission: what it is for, its schedule, when it next runs, how its last run ended, on or paused, and the
    profile settings it runs with. Claude's peak hours come with their own settings."""
    listed = await missions.listing(session, settings.owner)
    profile = await profile_store.current(session, settings.owner)
    return {"missions": listed, "peak": _peak_section(profile.settings)}


@router.patch("/missions/numbers")
async def mission_numbers_change(body: NumbersBody, session: Session) -> dict:
    """Change the settings missions run with (`missions/numbers.py`: packets' bars, readers a night, when the classifier
    learns, peak hours). Each is a field of the profile's Settings, so the change is the next version of that
    document, with who and why, and every other field kept."""
    version = await mission_numbers.save(session, settings.owner, body.values, by=BY_OWNER, why=body.why)
    await session.commit()
    async with db.read_session_factory()() as reader:
        profile = await profile_store.current(reader, settings.owner)
    return {"saved": version is not None, "version": version,
            "peak": _peak_section(profile.settings)}


@router.get("/missions/{mission_id}")
async def mission_detail(mission_id: uuid.UUID, session: ReadSession) -> dict:
    return await missions.detail(session, settings.owner, mission_id)


@router.post("/missions/{mission_id}/run")
async def mission_run(mission_id: uuid.UUID, session: Session) -> dict:
    """Run now, whatever the schedule and even when paused. The engine does it (`missions.asked.run`): some kinds read
    the network to begin, and the page never does. This saves the ask and waits for the answer: the run as the page
    reads it, or the refusal the run met, said again with its status and words. With the engine stopped the ask is kept
    and the answer is 503 saying so: the run starts when the engine next runs."""
    answer = await engine_calls.ask(
        session, settings.owner, "mission.run", {"mission_id": str(mission_id)}, WAIT_SECONDS)
    if answer is None:
        raise Busy(engine_calls.WORKER_NOT_RUNNING)
    if "refused" in answer:
        raise _Refused(answer["refused"]["status"], answer["refused"]["message"])
    return answer["run"]


@router.post("/missions/{mission_id}/pause")
async def mission_pause(mission_id: uuid.UUID, body: PauseBody, session: Session) -> dict:
    mission = await missions.get(session, settings.owner, mission_id)
    await missions.pause(session, mission, by=await actor_name(session, settings.owner), until=body.until,
                         why=body.why)
    await session.commit()
    return await _detail(mission_id)


@router.post("/missions/{mission_id}/resume")
async def mission_resume(mission_id: uuid.UUID, body: PauseBody, session: Session) -> dict:
    mission = await missions.get(session, settings.owner, mission_id)
    await missions.resume(session, mission, by=await actor_name(session, settings.owner), why=body.why)
    await session.commit()
    return await _detail(mission_id)


@router.patch("/missions/{mission_id}")
async def mission_change(mission_id: uuid.UUID, body: MissionChangeBody, session: Session) -> dict:
    """Change the schedule or the settings; the change is kept with who and why."""
    mission = await missions.get(session, settings.owner, mission_id)
    await missions.change(session, mission, by=await actor_name(session, settings.owner), schedule=body.schedule,
                          values=body.settings, why=body.why)
    await session.commit()
    return await _detail(mission_id)
