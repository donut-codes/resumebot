"""Fits none of my roles (docs/PRD.md 31.3): the roles the owner pursued that were never matched to one of their target
roles, and the postings found this week that no target role claims, so the owner can see what their role tabs miss and
make a new tab from one ("Make this a role") or call it fine as it is. All of it is counted from the database, with no
agent: a role's target role is the id the analyst matched it to, or one a posting carried (`Opportunity.target_role`).

- **Pursued**: the roles the owner moved forward, the way the classifier counts them (`classifier.label`): a role in
  Needs Drafted, Ready to Apply, Applied or Active Discussion, or one that ended after it had reached Applied or Active
  Discussion; a role whose packet is built counts too. Fit none = no target role recorded, and not marked fine.
- **This week**: postings first seen in the last 7 days that got past the filter, so they became roles (a role was made
  from them, and is not Gated out), and that no target role claims (neither the posting nor its role records one). A
  posting the owner marked fine is left out, and so is one whose role they marked fine.

An item is `{id, kind, title, company, stage, stage_label, found_at}`: `kind` says whether `id` is a role's or a
posting's (`role` or `posting`). Services flush; the route commits.
"""

import re
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, func, not_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from app.errors import DomainError
from app.jobsearch import stages
from app.jobsearch.fit import classifier
from app.jobsearch.models import FitsNoneOk, JobPosting, Opportunity, StageMove
from app.jobsearch.profile import store as profile_store
from app.jobsearch.profile.schema import MAX_TITLES
from app.timeutil import iso_utc

ROLE, POSTING = "role", "posting"
#: How far back "this week" reaches.
WEEK = timedelta(days=7)
#: A page of a list, and the most one page holds.
PAGE = 10
MOST_PER_PAGE = 200
#: What a draft's description takes from a posting's text: its first few sentences, for the owner to write on.
DRAFT_DESCRIPTION_CHARS = 1000


#: The columns an item shows (`role_item`, `posting_item`): a page of ten reads these and not the descriptions, the
#: analysis or the text of a posting.
_ROLE_COLUMNS = (Opportunity.id, Opportunity.title, Opportunity.company, Opportunity.status, Opportunity.created_at)
_POSTING_COLUMNS = (JobPosting.id, JobPosting.title, JobPosting.company, JobPosting.first_seen,
                    JobPosting.opportunity_id)


class FitsNoneError(DomainError):
    """A request that can't be met, in words."""

    status = 404


def _reached_exists(owner: str):
    """`classifier.reached`, asked of one role at a time: the role has a stage move into or out of Applied or Active
    Discussion. The same answer as `Opportunity.id IN (classifier.reached(owner))`, but it is read for the roles that
    ended and reach this term, by the role's own index, where the `IN` read every move of the owner's at each call
    (the stage moves are the largest table the count touches)."""
    return select(StageMove.id).where(
        StageMove.role_id == Opportunity.id, StageMove.owner == owner,
        or_(StageMove.to_status.in_(classifier.REACHED), StageMove.from_status.in_(classifier.REACHED))
    ).correlate(Opportunity).exists()


def pursued_where(owner: str):
    """The roles the owner pursued, as SQL conditions (see the module's note). The same set the classifier learns as
    moved forward, and the check's `pursued` scope: one definition, counted here."""
    return (
        Opportunity.owner == owner,
        Opportunity.status != stages.GATED_OUT,
        or_(
            Opportunity.status.in_(classifier.FORWARD),
            and_(Opportunity.status.in_(stages.ENDED), _reached_exists(owner)),
            # An employer saying no ends an application the owner made, whatever history was kept (an imported role has
            # none): the classifier counts it as pursued too (`classifier.lesson`, docs/PRD.md 31.7).
            and_(Opportunity.status.in_(stages.ENDED), Opportunity.ended_reason == stages.THEY_SAID_NO),
            Opportunity.packet_complete.is_(True),
        ),
    )


def _fine(owner: str):
    """The ids the owner called fine as they are."""
    return select(FitsNoneOk.subject_id).where(FitsNoneOk.owner == owner)


def pursued_none_where(owner: str):
    """The pursued roles that fit none of the owner's target roles and were not called fine."""
    return (*pursued_where(owner), Opportunity.target_role.is_(None), Opportunity.id.not_in(_fine(owner)))


def this_week_none_where(owner: str, now: datetime | None = None):
    """The postings found this week that became roles and that no target role claims."""
    since = (now or datetime.now(UTC)) - WEEK
    fine = _fine(owner)
    role_unclaimed = select(Opportunity.id).where(
        Opportunity.id == JobPosting.opportunity_id, Opportunity.status != stages.GATED_OUT,
        Opportunity.target_role.is_(None)).correlate(JobPosting).exists()
    return (
        JobPosting.owner == owner, JobPosting.first_seen >= since, JobPosting.target_role.is_(None),
        # Implied by `role_unclaimed`, said outright so the partial index `ix_js_postings_owner_week_unclaimed` (0163)
        # is one the planner may use.
        JobPosting.opportunity_id.is_not(None),
        role_unclaimed,
        JobPosting.id.not_in(fine),
        not_(and_(JobPosting.opportunity_id.is_not(None), JobPosting.opportunity_id.in_(fine))),
    )


def role_item(role: Opportunity) -> dict:
    return {"id": str(role.id), "kind": ROLE, "title": role.title, "company": role.company, "stage": role.status,
            "stage_label": stages.LABELS.get(role.status, role.status), "found_at": iso_utc(role.created_at)}


def posting_item(posting: JobPosting, role: Opportunity) -> dict:
    return {"id": str(posting.id), "kind": POSTING, "title": posting.title, "company": posting.company,
            "stage": role.status, "stage_label": stages.LABELS.get(role.status, role.status),
            "found_at": iso_utc(posting.first_seen)}


async def _pursued_count(session: AsyncSession, owner: str) -> int:
    return await session.scalar(
        select(func.count()).select_from(Opportunity).where(*pursued_none_where(owner))) or 0


async def _this_week_count(session: AsyncSession, owner: str, now: datetime | None = None) -> int:
    return await session.scalar(
        select(func.count()).select_from(JobPosting).where(*this_week_none_where(owner, now))) or 0


async def pursued(session: AsyncSession, owner: str, offset: int = 0, limit: int = PAGE) -> dict:
    """The roles the owner pursued that fit none of their target roles: how many, and a page of them, newest first.
    The set is the owner's own pursued roles, a few hundred, so it is read once and the page is cut from it, not run
    again as a count over the whole table."""
    rows = (await session.scalars(
        select(Opportunity).options(load_only(*_ROLE_COLUMNS)).where(*pursued_none_where(owner))
        .order_by(Opportunity.created_at.desc(), Opportunity.id))).all()
    start = max(offset, 0)
    page = rows[start:start + min(max(limit, 1), MOST_PER_PAGE)]
    return {"count": len(rows), "items": [role_item(r) for r in page]}


async def this_week(session: AsyncSession, owner: str, offset: int = 0, limit: int = PAGE,
                    now: datetime | None = None) -> dict:
    """The postings found in the last 7 days that fit none of the owner's target roles: how many, and a page of them,
    newest first."""
    where = this_week_none_where(owner, now)
    count = await _this_week_count(session, owner, now)
    rows = (await session.scalars(
        select(JobPosting).options(load_only(*_POSTING_COLUMNS)).where(*where)
        .order_by(JobPosting.first_seen.desc(), JobPosting.id)
        .offset(max(offset, 0)).limit(min(max(limit, 1), MOST_PER_PAGE)))).all()
    roles = {r.id: r for r in (await session.scalars(
        select(Opportunity).options(load_only(Opportunity.id, Opportunity.status))
        .where(Opportunity.id.in_([p.opportunity_id for p in rows])))).all()} if rows else {}
    return {"count": count, "items": [posting_item(p, roles[p.opportunity_id])
                                      for p in rows if p.opportunity_id is not None]}


async def view(session: AsyncSession, owner: str, pursued_offset: int = 0, week_offset: int = 0, limit: int = PAGE,
               now: datetime | None = None) -> dict:
    """What the profile page shows under the role tabs: `{pursued: {count, items}, this_week: {count, items}}`."""
    return {"pursued": await pursued(session, owner, pursued_offset, limit),
            "this_week": await this_week(session, owner, week_offset, limit, now)}


async def _subject(session: AsyncSession, owner: str, subject_id: str) -> tuple[str, Opportunity | JobPosting]:
    """The owner's role or posting with this id: (kind, row). Raises `FitsNoneError` for any other."""
    try:
        key = uuid.UUID(str(subject_id))
    except ValueError:
        raise FitsNoneError("no role has that id") from None
    role = await session.get(Opportunity, key)
    if role is not None and role.owner == owner:
        return ROLE, role
    posting = await session.get(JobPosting, key)
    if posting is not None and posting.owner == owner:
        return POSTING, posting
    raise FitsNoneError("no role has that id")


async def dismiss(session: AsyncSession, owner: str, subject_id: str) -> dict:
    """Mark a role (or a posting from this week's list) "fine as is": it is stored and left out of what fits none of the
    owner's roles. Marking one that is already marked changes nothing. Returns what was marked; `counts` says the new
    counts, which the route reads on a reader once this is committed."""
    kind, row = await _subject(session, owner, subject_id)
    marked = await session.scalar(
        select(FitsNoneOk.id).where(FitsNoneOk.owner == owner, FitsNoneOk.subject_id == row.id))
    if marked is None:
        session.add(FitsNoneOk(owner=owner, subject_id=row.id, kind=kind))
        await session.flush()
    return {"id": str(row.id), "kind": kind, "dismissed": True}


async def counts(session: AsyncSession, owner: str) -> dict:
    """How many roles and how many postings of this week fit none of the owner's target roles now: `{pursued,
    this_week}`. A read: the dismiss route takes it on a reader once the mark is committed."""
    return {"pursued": await _pursued_count(session, owner), "this_week": await _this_week_count(session, owner)}


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60]


def _unique_id(name: str, taken: set[str]) -> str:
    base = _slug(name) or "target-role"
    role_id, n = base, 2
    while role_id in taken:
        role_id, n = f"{base}-{n}", n + 1
    return role_id


def _clip(text: str, limit: int) -> str:
    """The start of a text held to `limit` characters, cut at a space (at the end of a sentence when one is near)."""
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    stop = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    if stop >= limit // 2:
        return cut[: stop + 1]
    return cut.rsplit(" ", 1)[0] if " " in cut[limit // 2:] else cut


async def draft(session: AsyncSession, owner: str, subject_id: str) -> dict:
    """A target role made from a role (or a posting): its title as the name and the first example title, and its text,
    when the app has it, as the description. Not saved: the page adds it as a new tab for the owner to look over and
    save. `id` is a slug of the name that no saved target role has; the page makes its own against the tabs it holds."""
    kind, row = await _subject(session, owner, subject_id)
    title = " ".join((row.title or "").split())
    if kind == ROLE:
        text = row.description
        if not text:
            text = await session.scalar(select(JobPosting.text).where(
                JobPosting.owner == owner, JobPosting.opportunity_id == row.id, JobPosting.text.is_not(None),
                JobPosting.text != "").order_by(JobPosting.first_seen.desc()).limit(1))
    else:
        text = row.text
    saved = {r.id for r in (await profile_store.current(session, owner)).criteria.target_roles}
    name = title
    return {
        "id": _unique_id(name, saved), "name": name, "titles": [name][:MAX_TITLES] if name else [],
        "description": _clip(text or "", DRAFT_DESCRIPTION_CHARS),
        "responsibilities": "", "qualifications": "", "preferred": "", "compensation": "",
    }
