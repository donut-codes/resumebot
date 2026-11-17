"""Interviews on a role in Active Discussion (docs/PRD.md 15.2.1): when, what round, who is in it and the owner's notes,
added, corrected and removed by hand, each with a line in the role's History. Adding one whose time is ahead puts its
prep in front of the Coach (`coach.py`); editing one starts nothing (the card says what changed since the report was
made and offers Prepare again). A role in another stage takes no new interview; one it has stays if the role moves.
Services flush; the route commits and wakes the Coach."""

import uuid
from collections.abc import Iterable
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import DomainError
from app.jobsearch import stages
from app.jobsearch.actors import actor_name
from app.jobsearch.interviews import coach, coach_view
from app.jobsearch.models import Interview, InterviewPrep, Opportunity
from app.jobsearch.states import RoundKind
from app.jobsearch.work import stuck
from app.timeutil import aware, iso_utc, owner_zone, utc_now
from app.trail import service as trail

ROUNDS = tuple(k.value for k in RoundKind)
NOT_IN_DISCUSSION = "Interviews are added to a role in Active Discussion. Move this role there first."
#: How the History names a round: "You added a hiring manager interview on ...".
KIND_WORDS = {"recruiter_screen": "a recruiter screen", "hiring_manager": "a hiring manager",
              "panel": "a panel", "executive": "an executive", "other": "an"}


class InterviewError(DomainError):
    status = 422


class NotInDiscussion(InterviewError):
    status = 409


class NoSuchInterview(InterviewError):
    status = 404


def _day(when: datetime) -> str:
    local = (aware(when) or when).astimezone(owner_zone())
    return f"{local:%A, %B} {local.day}"


def _people(raw: Any, before: Iterable[dict] = ()) -> list[dict]:
    """The people as stored: each with a name (required) and a title, and an id that stays when the list is edited (a
    person sent with the id they had keeps it)."""
    if raw is None:
        raise InterviewError("people is a list of {name, title}")
    known = {p["id"] for p in before}
    out: list[dict] = []
    for item in raw:
        item = item if isinstance(item, dict) else {}
        name = " ".join(str(item.get("name") or "").split())
        if not name:
            raise InterviewError("Each person needs a name.")
        given = str(item.get("id") or "")
        ident = given if given in known and given not in {p["id"] for p in out} else uuid.uuid4().hex[:8]
        out.append({"id": ident, "name": name, "title": " ".join(str(item.get("title") or "").split())})
    return out


def _when(value: Any) -> datetime:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.strip())
        except ValueError:
            raise InterviewError("when is a date and time, as 2026-11-04T14:00:00-05:00") from None
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise InterviewError("when needs a date, a time and the time zone it is in")
    return value


def _round(kind: Any) -> str:
    if kind not in ROUNDS:
        raise InterviewError(f"the round is one of {', '.join(ROUNDS)}")
    return str(kind)


async def _role(session: AsyncSession, owner: str, role_id: uuid.UUID) -> Opportunity:
    role = await session.get(Opportunity, role_id)
    if role is None or role.owner != owner:
        raise NoSuchInterview("no such role")
    return role


async def get(session: AsyncSession, owner: str, interview_id: uuid.UUID) -> Interview:
    interview = await session.get(Interview, interview_id)
    if interview is None or interview.owner != owner:
        raise NoSuchInterview("no such interview")
    return interview


async def _said(session: AsyncSession, owner: str, interview: Interview, event: str, sentence: str) -> None:
    await trail.record(session, owner, event=event, actor="person", who=await actor_name(session, owner),
                       sentence=sentence, link=("opportunity", interview.role_id))


async def add(session: AsyncSession, owner: str, role_id: uuid.UUID, *, when: Any, round_kind: Any,
              label: str = "", people: Any = (), notes: str = "") -> Interview:
    """Add an interview to a role in Active Discussion. One whose time is ahead starts its prep; one in the past is kept
    for the record and starts none."""
    role = await _role(session, owner, role_id)
    if role.status != stages.ACTIVE_DISCUSSION:
        raise NotInDiscussion(NOT_IN_DISCUSSION)
    interview = Interview(owner=owner, role_id=role.id, when_at=_when(when), round_kind=_round(round_kind),
                          label=" ".join(str(label or "").split()), people=_people(people),
                          notes=str(notes or "").strip())
    session.add(interview)
    await session.flush()
    await _said(session, owner, interview, "interview_added",
                f"You added {KIND_WORDS[interview.round_kind]} interview on {_day(interview.when_at)}.")
    if (aware(interview.when_at) or interview.when_at) > utc_now():
        await coach.start(session, owner, interview)
    return interview


async def add_from_mail(session: AsyncSession, owner: str, role_id: uuid.UUID, *, when: datetime, round_kind: str,
                        people: Iterable[str], place: str, link: str, source: str) -> Interview:
    """Add the interview a mail (a calendar invite read in code, or the mail reader's reading of the text) asks for
    (`mail/act.py`, docs/PRD.md 14.6): the same record as one added by hand, with the History saying it came from the
    mail, not that the owner added it. The people are names; the place and link go in the notes. A round the mail did
    not say is `other`. `source` is where the time came from ("mail"). Prep starts as for any interview whose time is
    ahead."""
    role = await _role(session, owner, role_id)
    if role.status != stages.ACTIVE_DISCUSSION:
        raise NotInDiscussion(NOT_IN_DISCUSSION)
    notes = " ".join(p for p in ("From a mail.", f"Place: {place}." if place else "",
                                 f"Link: {link}" if link else "") if p)
    interview = Interview(owner=owner, role_id=role.id, when_at=_when(when),
                          round_kind=round_kind if round_kind in ROUNDS else "other", label="",
                          people=_people([{"name": name, "title": ""} for name in people]), notes=notes)
    session.add(interview)
    await session.flush()
    await trail.record(session, owner, event="interview_added", sentence=(
        f"Added {KIND_WORDS[interview.round_kind]} interview on {_day(interview.when_at)}, from the "
        f"{'mail' if source == 'mail' else source}."), link=("opportunity", role.id))
    if (aware(interview.when_at) or interview.when_at) > utc_now():
        await coach.start(session, owner, interview)
    return interview


async def edit(session: AsyncSession, owner: str, interview_id: uuid.UUID, **changes: Any) -> Interview:
    """Correct an interview: any of when, round_kind, label, people and notes. It starts no report."""
    interview = await get(session, owner, interview_id)
    if "when" in changes and changes["when"] is not None:
        interview.when_at = _when(changes["when"])
    if "round_kind" in changes and changes["round_kind"] is not None:
        interview.round_kind = _round(changes["round_kind"])
    if changes.get("label") is not None:
        interview.label = " ".join(str(changes["label"]).split())
    if changes.get("people") is not None:
        interview.people = _people(changes["people"], interview.people or [])
    if changes.get("notes") is not None:
        interview.notes = str(changes["notes"]).strip()
    interview.updated_at = utc_now()
    await session.flush()
    await _said(session, owner, interview, "interview_edited",
                f"You changed the interview on {_day(interview.when_at)}.")
    return interview


async def remove(session: AsyncSession, owner: str, interview_id: uuid.UUID) -> None:
    """Remove an interview and its reports; a prep still open has its process cancelled."""
    interview = await get(session, owner, interview_id)
    for prep in (await session.scalars(select(InterviewPrep).where(
            InterviewPrep.interview_id == interview.id, InterviewPrep.state.in_(coach_view.OPEN)))).all():
        await stuck.cancel_instance(session, prep.instance_id, "the interview was removed", f"the prep {prep.id}")
    await _said(session, owner, interview, "interview_removed",
                f"You removed the interview on {_day(interview.when_at)}.")
    await session.delete(interview)
    await session.flush()


async def prepare_again(session: AsyncSession, owner: str, interview_id: uuid.UUID) -> Interview:
    """Make the next version of the report (when none is open and the time is ahead)."""
    interview = await get(session, owner, interview_id)
    await coach.start(session, owner, interview)
    return interview


# --- what the page reads -----------------------------------------------------------------------------------------


def view(interview: Interview, preps: list[InterviewPrep], texts: dict[str, str], comp: dict | None = None) -> dict:
    """An interview as the owner reads it: the record, the newest prep with its report, and the versions."""
    ordered = sorted(preps, key=lambda p: p.version, reverse=True)
    past = (aware(interview.when_at) or interview.when_at) <= utc_now()
    newest = ordered[0] if ordered else None
    return {
        "id": str(interview.id), "role_id": str(interview.role_id), "when_at": iso_utc(interview.when_at),
        "round_kind": interview.round_kind, "round_label": coach_view.round_label(interview.round_kind),
        "label": interview.label or "", "people": list(interview.people or []), "notes": interview.notes or "",
        "past": past,
        "can_prepare_again": not past and not any(p.state in coach_view.OPEN for p in ordered),
        "versions": [{"id": str(p.id), "version": p.version, "state": p.state} for p in ordered],
        "prep": coach_view.prep_view(newest, interview, texts, comp, newest=True) if newest is not None else None,
    }


async def for_role(session: AsyncSession, owner: str, role_id: uuid.UUID, comp: dict | None = None) -> list[dict]:
    """The role's interviews, by time, each with its prep."""
    rows = (await session.scalars(select(Interview).where(Interview.owner == owner, Interview.role_id == role_id)
                                  .order_by(Interview.when_at))).all()
    if not rows:
        return []
    preps = (await session.scalars(select(InterviewPrep).where(
        InterviewPrep.interview_id.in_([i.id for i in rows])))).all()
    texts = await coach.texts_for(session, owner)
    return [view(i, [p for p in preps if p.interview_id == i.id], texts, comp) for i in rows]


async def one(session: AsyncSession, owner: str, interview_id: uuid.UUID, comp: dict | None = None) -> dict:
    interview = await get(session, owner, interview_id)
    return next(v for v in await for_role(session, owner, interview.role_id, comp) if v["id"] == str(interview.id))

