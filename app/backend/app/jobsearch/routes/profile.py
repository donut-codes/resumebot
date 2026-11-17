"""The owner's profile over HTTP: read all of it (with each gate and ask as a sentence), save a document as a new
version, read its history or one earlier version, and bring one back. The master resume is also exported here as a
JSON Resume file (docs/PRD.md 8.4)."""

import json
from datetime import datetime
from typing import Annotated, cast

from fastapi import APIRouter, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import db
from app.db import ReadSession, Session
from app.errors import Conflict, NotFound
from app.jobsearch import stages
from app.jobsearch.actors import BY_OWNER
from app.jobsearch.fit import comp, scoring
from app.jobsearch.models import JobPosting, Opportunity
from app.jobsearch.profile import never_write, style_rules, views
from app.jobsearch.profile import store as profile_store
from app.jobsearch.profile.base import KINDS
from app.jobsearch.profile.documents import About, Resume
from app.jobsearch.profile.schema import Settings
from app.jobsearch.profile.store import ALL_KINDS, ProfileError
from app.jobsearch.resume import export as resume_export
from app.jobsearch.work import engine_calls, tagging
from app.jobsearch.writing import library as story_library
from app.jobsearch.writing.routes import attachment
from app.settings import settings

router = APIRouter(prefix="/api/v1/profile")
#: The owner's own documents (resume, application info, writing rules), read together; each is saved through the
#: same routes as a profile part (PUT /api/v1/profile/resume and so on).
documents_router = APIRouter(prefix="/api/v1/documents")


#: How long a save or a restore waits for the engine to say what it did to the roles. It is the owner-action wait
#: (`WAIT_SECONDS` in `owner_actions/runner.py`); that module is the engine's, so the number is written here. Past it
#: page answers with the saved version and no `said`, and the engine does the work when it can.
ASK_WAIT_S = 20.0
#: The parts whose new version the engine follows up: Hard pass (every posting and role checked against the gates
#: again) and Prioritization (every score worked out again). Nothing runs after a save of any other part.
FOLLOWED_UP = ("gates", *profile_store.WORKS_OUT_AGAIN)


class Save(BaseModel):
    data: dict
    why: str = ""
    #: The version of this document the page loaded. A newer one in force refuses the save (409); left out, the save is
    #: stored as it comes (agents, scripts and imports send none).
    base_version: int | None = Field(default=None, ge=0)


def _kind(kind: str) -> str:
    if kind not in ALL_KINDS:
        raise NotFound(f"no part called {kind}; the parts are {', '.join(ALL_KINDS)}")
    return kind


class Why(BaseModel):
    """Why a version was saved, said after it was."""

    why: str = ""


@router.get("")
async def read(session: ReadSession) -> dict:
    return await _view(session)


async def _view(session: AsyncSession) -> dict:
    """The whole profile as the page reads it: each part, whether there is anything to search for, the most a fit can
    earn, the versions, and each gate and ask as its sentence."""
    profile = await profile_store.current(session, settings.owner)
    return {
        **profile.model_dump(mode="json"),
        "is_set": profile.is_set(),
        #: The most the asks that apply to every role can earn now: what a fit is a percent of for a role with no target
        #: role (docs/PRD.md 30.6), and for each target role the most its own asks can earn (31.5), by its id.
        "fit_possible": scoring.possible(profile),
        "fit_possible_by_role": scoring.possible_by_role(profile),
        #: The best positive number the owner set for one required and for one preferred requirement (0 when none is
        #: positive or the requirements are not graded): the most each can add to a role's fit, for the About line.
        "grading_best": scoring.grading_best(profile),
        "versions": {k: v for k, v in (await profile_store.counts(session, settings.owner)).items() if k in KINDS},
        "says": views.says(profile),
    }


@router.get("/comp-example")
async def comp_example(request: Request) -> dict:
    """The starting-comp method worked on a made-up posting with the numbers given in the address (docs/PRD.md 26.6):
    what the settings page shows under the numbers it is editing, before they are saved. The address holds any of the
    method's numbers (`comp.NUMBERS`); one left out is its default. `comp.example` runs the real `comp.compute` on it.
    Read-only, and it reads nothing the owner saved: the posting is the same for everyone and no pay floor or pay ask
    is applied. A number that doesn't fit is a 422 in words, as a save of it would be."""
    given = dict(request.query_params)
    unknown = sorted(set(given) - set(comp.NUMBERS))
    if unknown:
        raise ProfileError(f"not a number of the pay method: {', '.join(unknown)}")
    try:
        draft = Settings.model_validate(given)
    except ValidationError as exc:
        raise ProfileError("the numbers don't fit: " + "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or 'a number'}: {e['msg']}" for e in exc.errors()[:10])) from exc
    return comp.example(draft)


@router.get("/gates/stopped")
async def gates_stopped(session: ReadSession) -> dict:
    """How many each line of Hard pass has stopped (Search Settings: a rule that stops
    nothing is a rule to question), by the line's id as a stop keeps it (`location`, `compensation`, `location:<role>`,
    the id of a sentence or an employer rule, `employer` for the employers gated): the roles Gated out by it, and the
    postings it stopped before a role was made of them. Counted from the rows; a line that stopped nothing is left
    out."""
    stopped: dict[str, int] = {}
    owner = settings.owner
    by_role = (select(Opportunity.screened_out_gate_id, func.count())
               .where(Opportunity.owner == owner, Opportunity.status == stages.GATED_OUT,
                      Opportunity.screened_out_gate_id.is_not(None))
               .group_by(Opportunity.screened_out_gate_id))
    by_posting = (select(JobPosting.stop_gate_id, func.count())
                  .where(JobPosting.owner == owner, JobPosting.stop_gate_id.is_not(None),
                         JobPosting.opportunity_id.is_(None))
                  .group_by(JobPosting.stop_gate_id))
    rows = [*(await session.execute(by_role)).tuples().all(), *(await session.execute(by_posting)).tuples().all()]
    for line, n in rows:
        if line is not None:
            stopped[line] = stopped.get(line, 0) + n
    return {"stopped": stopped}


@router.post("/roles/tag")
async def tag_roles(session: Session) -> dict:
    """Start the one-off job that gives the roles found before target roles the target role each matches (docs/PRD.md
    30.5): a small title session is handed them (`tagging.py`) and the runner is woken for it. `open` is whether a
    job is open now and `count` how many roles it has to settle. A job already open is not started again. It touches
    no mission, and holds off in Claude's peak hours like the rest of Claude's work."""
    job, count = await tagging.start(session, settings.owner)
    await session.commit()
    if job is not None:
        await engine_calls.knock(str(job.id), reason=engine_calls.TAG_ROLES)
    return {"open": job is not None, "count": count}


@router.get("/roles/{role_id}/trend")
async def role_trend(role_id: str, session: ReadSession,
                     days: Annotated[int, Query(ge=1, le=tagging.MAX_TREND_DAYS)] = 30) -> dict:
    """How many roles got past the gates (not gated out) for a target role on each of the last `days` days in the
    owner's time zone, oldest first, with their total: `{days: [{date, found}], total}`. A role id that is not one of
    the owner's target roles is all zeros."""
    return await tagging.trend(session, settings.owner, role_id, days)


@router.put("/{kind}")
async def save(kind: str, body: Save, session: Session) -> dict:
    """Save a part as its next version. `said` is the one line that says what the save did to what was already stored
    (a new Hard pass: how many postings and roles it checked again and how many moved, and how many roles a reader
    answers a rule again for tonight; Prioritization: the scores worked out again; What to look for: nothing, unless
    a tab was deleted, when the postings only it had said yes to go on to become roles); it is there only when the
    save had something to say.

    The writer's turn is for writing: the refusals are read on a reader, the version and what belongs to it are stored
    and, for Hard pass and Prioritization, the engine is asked in the same commit to check the roles against the gates
    again or work the scores out again (`_followed_up`; nothing is held while the page waits for it), and the answer is
    read on a reader. With the engine stopped or slow the save is answered without `said`."""
    said: list[str] = []
    async with db.read_session_factory()() as reader:
        await profile_store.refuse_what_a_save_refuses(reader, settings.owner, _kind(kind), body.data)
    version = await profile_store.save(
        session, settings.owner, kind, body.data, by=BY_OWNER, why=body.why.strip(),
        base_version=body.base_version, said=said, defer_works_out=True)
    await _followed_up(session, kind, version, "was saved", said)
    return {"saved": version is not None, "version": version, **({"said": " ".join(said)} if said else {}),
            **await _whole(kind)}


async def _followed_up(session: AsyncSession, kind: str, version: int | None, how: str, said: list[str]) -> None:
    """Commit what the save stored and see to what follows it. For Hard pass and Prioritization the engine does that
    (`engine_asks`, "profile.after_save": every role checked against the gates again, every score worked out again):
    the ask is saved in the same commit as the version, so it is never lost, nothing is held while the page waits up to
    `ASK_WAIT_S`, and the sentence that says what the engine did goes first in `said`, where a save always put it
    (before what the save said of the employers' facts). With no answer in time the save is answered without it. A
    save that stored nothing, or a part that nothing follows, asks for nothing and only commits."""
    if version is None or kind not in FOLLOWED_UP:
        await session.commit()
        return
    answer = await engine_calls.ask(
        session, settings.owner, "profile.after_save", {"kind": kind, "version": version, "how": how}, ASK_WAIT_S)
    line = answer.get("said") if isinstance(answer, dict) else None
    if line:
        said.insert(0, line)


async def _whole(kind: str) -> dict:
    """For a part of the profile, the whole profile as it now stands (`profile`, as `GET /profile` reads it), so the
    page that saved one field takes the new versions and sentences from the answer instead of asking again. Nothing for
    the owner's other documents. Read on a reader: the answer asks for no turn."""
    if kind not in KINDS:
        return {}
    async with db.read_session_factory()() as reader:
        return {"profile": await _view(reader)}


@router.get("/{kind}/history")
async def history(kind: str, session: ReadSession) -> dict:
    return {"kind": kind, "versions": await profile_store.history(session, settings.owner, _kind(kind))}


@router.get("/{kind}/versions/{number}")
async def one_version(kind: str, number: int, session: ReadSession) -> dict:
    return await profile_store.version(session, settings.owner, _kind(kind), number)


@router.post("/{kind}/versions/{number}/restore")
async def restore(kind: str, number: int, session: Session) -> dict:
    """Bring an earlier version back as the newest, in the same shape as `save`: refused on a reader, stored, the engine
    asked to check or re-score (`_followed_up`), the answer read on a reader."""
    said: list[str] = []
    async with db.read_session_factory()() as reader:
        old = await profile_store.version(reader, settings.owner, _kind(kind), number)
        await profile_store.refuse_what_a_save_refuses(reader, settings.owner, kind, old["data"])
    version = await profile_store.restore(session, settings.owner, kind, number, by=BY_OWNER, said=said,
                                          defer_works_out=True)
    await _followed_up(session, kind, version, f"was made by bringing version {number} back", said)
    return {"restored": number, "version": version, **({"said": " ".join(said)} if said else {}),
            **await _whole(kind)}


@router.put("/{kind}/versions/{number}/why")
async def set_why(kind: str, number: int, body: Why, session: Session) -> dict:
    """Say why the newest version was saved, after it was: the Saved line's "Add a reason". Only the newest version, and
    only one the owner saved; 422 in words otherwise. History shows it."""
    row = await profile_store.set_why(session, settings.owner, _kind(kind), number, body.why, by=BY_OWNER)
    await session.commit()
    return row


@documents_router.get("")
async def read_documents(session: ReadSession) -> dict:
    """The resume, application info and writing rules, with how many versions each has."""
    docs = await profile_store.owner_documents(session, settings.owner)
    counts = await profile_store.counts(session, settings.owner)
    return {**docs.model_dump(mode="json"), "versions": {k: counts[k] for k in ("resume", "about", "writing")}}


#: The highest version number the store can hold (a 32-bit integer): a number past it is no version, and is not asked of
#: the database.
MOST_VERSIONS = 2_147_483_647


async def _export_resume(session: AsyncSession, number: int | None) -> Response:
    """The master resume as a JSON Resume file (`resume/export.py`): version `number` of the resume, or the one in force
    when `number` is None, with the application info in force now. Reads only and commits nothing. 404 in words for a
    version that is not there, 409 when the master has no summary and no role, 422 in words when a stored version no
    longer fits the model. The file's `meta.lastModified` is when the newest of the two documents in it was saved."""
    owner = settings.owner
    if number is None:
        number = await profile_store.newest_version(session, owner, "resume")
        stored = ({"data": {}, "saved_at": None} if number == 0
                  else await profile_store.version(session, owner, "resume", number))
    elif 1 <= number <= MOST_VERSIONS:
        stored = await profile_store.version(session, owner, "resume", number)
    else:
        raise profile_store.NoSuchVersion(f"there is no version {number} of the resume")
    resume = cast(Resume, profile_store.checked("resume", stored["data"]))
    if resume_export.nothing_to_export(resume):
        raise Conflict(resume_export.EMPTY)
    about_version = await profile_store.newest_version(session, owner, "about")
    about_stored = ({"data": None, "saved_at": None} if about_version == 0
                    else await profile_store.version(session, owner, "about", about_version))
    about = cast(About, profile_store.checked("about", about_stored["data"]))
    # The read is done: let go of the connection and its transaction, as a read route that commits nothing does.
    await session.rollback()
    saved = max(datetime.fromisoformat(t) for t in (stored["saved_at"], about_stored["saved_at"]) if t)
    doc = resume_export.json_resume(resume, about, master_version=number, about_version=about_version,
                                    modified_at=saved)
    body = json.dumps(doc, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
    return Response(body, media_type="application/json", headers={"Content-Disposition": attachment("resume.json")})


@documents_router.get("/resume/export.json")
async def export_resume(session: ReadSession) -> Response:
    """The master resume in force as `resume.json`, a JSON Resume v1.0.0 file another resume tool can import
    (docs/PRD.md 8.4). What the standard has no field for (notes, guardrails, evidence, achievements) stays out."""
    return await _export_resume(session, None)


@documents_router.get("/resume/versions/{number}/export.json")
async def export_resume_version(number: int, session: ReadSession) -> Response:
    """An earlier version of the master resume as `resume.json`: its own content, with the application info in force
    now (the file's `meta.resumebot` names the version of each). 404 for a version that is not there."""
    return await _export_resume(session, number)


@documents_router.get("/built-in-checks")
async def read_built_in_checks() -> dict:
    """The checks that ship with the app and run on every draft, the same for every owner: `checks`, the four kinds of
    text, each with its name and the QA check it backs; and `style`, the five style rules, each with its id, name and
    sentence (which of them are off is in the writing document). The three integrity checks are not listed: they are not
    a setting. Read-only."""
    return {"checks": never_write.built_in_checks(), "style": style_rules.shipped()}


@documents_router.get("/story-library")
async def read_story_library(session: ReadSession) -> dict:
    """The newest story library, as the owner reads it on My Resume (`story_library.view`): each role's scope line and
    resume lines, the achievements and the stories with their letter blocks, what was held out, whether it is from the
    master and rules in force, and whether a build is going. Read-only."""
    docs = await profile_store.owner_documents(session, settings.owner)
    row = await story_library.newest(session, settings.owner)
    return story_library.view(row, docs, current=await story_library.is_current(session, settings.owner),
                              going=await story_library.going(session, settings.owner))
