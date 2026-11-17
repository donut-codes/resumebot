"""Which target role each role matches (docs/PRD.md 30.5).

A role found from now on is matched to one of the owner's target roles by the analyst when it reads the role
(`analysis.to_match`), and keeps the id (`Opportunity.target_role`). The roles found before target roles have none, so
the owner can start a one-off job that gives each the role it matches:

- `start` opens the job (one at a time) when the owner has target roles and roles Haiku has not answered; the route
  and the catch-up open it for every role a gate did not stop, and `settle` opens it by itself for the live ones;
- a small title session (a Haiku kind) is handed those roles by `jobs.titles_next`, as items of kind `role`, and
  answers each with the target role it matches, or none (`jobs.titles_report`); either answer is kept on the role
  (`matched_by` haiku), so the role is not asked again;
- `settle` closes the job when none is left, and opens the next when live roles wait, except for roles the last job
  handed the most times and never got answered.

The job runs through the runner like other agent work (`session_kinds.next_kind`), holds off in Claude's peak hours and
while the scout is paused, and touches no mission. This module is data only. Services flush; the route or tool that owns
the request commits.
"""

import uuid
from collections import Counter
from collections.abc import Collection
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import ColumnElement, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from app.agents import roster
from app.errors import DomainError
from app.jobsearch.models import ORIGIN_TRACKER, Opportunity, RoleTagJob
from app.jobsearch.profile import store as profile_store
from app.jobsearch.stages import GATED_OUT
from app.jobsearch.work import hand_outs, role_match
from app.timeutil import owner_zone

OPEN, DONE = "open", "done"
#: How long the id a session answers with is: the start of the role's id, enough to tell a batch's apart.
ID_CHARS = 8
#: A role handed to the job this many times and never answered is left (it stays untagged).
HANDED_AT_MOST = 2
#: The most days one target role's trend covers.
MAX_TREND_DAYS = 90


class TagError(DomainError):
    """The job can't start; plain words."""

    status = 422


def reached_found():
    """The roles that got past the gates: every role a gate did not stop."""
    return Opportunity.status != GATED_OUT


def days_window(days: int, now: datetime | None = None) -> tuple[date, date, datetime, datetime]:
    """The last `days` days of the owner's calendar, today last: (first day, last day, the instant the first begins, the
    instant after the last ends). A day ends where the owner's time zone says."""
    zone = owner_zone()
    last = (now or datetime.now(UTC)).astimezone(zone).date()
    first = last - timedelta(days=days - 1)
    return (first, last, datetime.combine(first, time.min, zone).astimezone(UTC),
            datetime.combine(last + timedelta(days=1), time.min, zone).astimezone(UTC))


def found_on(first: date, last: date, since: datetime, until: datetime):
    """The roles found on a day from `first` to `last` (docs/PRD.md 31.8), by the day each was *found*, not the day the
    app made or imported its row: the role's own found date (what the tracker's Found column says for an imported role,
    and the day the search kept it for a role the app found). A role with no found date counts on the day its row was
    made, unless it was imported: an imported role with no date has no day to count on, so it is left out of a count by
    day (`since` to `until` are the instants `first` begins and `last` ends)."""
    return or_(
        and_(Opportunity.found.is_not(None), Opportunity.found >= first, Opportunity.found <= last),
        and_(Opportunity.found.is_(None), Opportunity.origin != ORIGIN_TRACKER,
             Opportunity.created_at >= since, Opportunity.created_at < until))


def found_day(found: date | None, created_at: datetime, zone: ZoneInfo) -> date:
    """The day a role counts on (see `found_on`): its found date, else the day its row was made, in the owner's zone."""
    return found if found is not None else created_at.astimezone(zone).date()


def short_id(role: Opportunity) -> str:
    return str(role.id)[:ID_CHARS]


def handed(job: RoleTagJob | None) -> dict[str, int]:
    return dict(((job.counts if job is not None else None) or {}).get("handed") or {})


def _scope(job: RoleTagJob | None) -> list[str] | None:
    """The stages a job asks about (`start`'s `stages`); None is every role a gate did not stop."""
    return ((job.counts if job is not None else None) or {}).get("stages") or None


def _ids(keys: Collection[str]) -> set[uuid.UUID]:
    out: set[uuid.UUID] = set()
    for key in keys:
        try:
            out.add(uuid.UUID(key))
        except ValueError:
            continue
    return out


def _at_limit(ledger: dict[str, int]) -> set[uuid.UUID]:
    return _ids([key for key, count in ledger.items() if count >= HANDED_AT_MOST])


def needs_haiku(owner: str, stages: Collection[str] | None = None) -> list[ColumnElement[bool]]:
    """The conditions for a role the Haiku pass still has to answer: the code pass's answer or none at all (a role with
    a target role and no pass recorded was answered before passes were recorded). A role Haiku or the analyst answered
    (a role, or none) is never asked again. `stages` None is every role a gate did not stop."""
    return [Opportunity.owner == owner, reached_found(),
            Opportunity.status.in_(list(stages)) if stages is not None else Opportunity.status.is_not(None),
            or_(and_(Opportunity.matched_by.is_(None), Opportunity.target_role.is_(None)),
                Opportunity.matched_by == role_match.CODE)]


async def open_job(session: AsyncSession, owner: str) -> RoleTagJob | None:
    return await session.scalar(
        select(RoleTagJob).where(RoleTagJob.owner == owner, RoleTagJob.status == OPEN)
        .order_by(RoleTagJob.started_at.desc()).limit(1))


async def untagged(session: AsyncSession, owner: str, stages: Collection[str] | None = None, *,
                   limit: int | None = None, leave: Collection[uuid.UUID] = ()) -> list[Opportunity]:
    """The roles Haiku has still to answer (`needs_haiku`), the newest first, each with only what a title session is
    handed (id, title, company) and what an answer is checked against: no description. `leave` ids are left out."""
    query = (select(Opportunity).options(load_only(
        Opportunity.id, Opportunity.title, Opportunity.company, Opportunity.status, Opportunity.target_role,
        Opportunity.matched_by)).where(*needs_haiku(owner, stages))
        .order_by(Opportunity.created_at.desc(), Opportunity.id))
    if leave:
        query = query.where(Opportunity.id.not_in(list(leave)))
    if limit is not None:
        query = query.limit(limit)
    return list((await session.scalars(query)).all())


async def pending(session: AsyncSession, job: RoleTagJob) -> list[Opportunity]:
    """The roles the job still has to settle: those Haiku has not answered, within the job's stages."""
    return await untagged(session, job.owner, _scope(job))


async def waiting(session: AsyncSession, job: RoleTagJob, *, skip_leased: bool = False,
                  limit: int | None = None) -> list[Opportunity]:
    """The roles a title session may be handed now: pending, and not handed `HANDED_AT_MOST` times already. With
    `skip_leased`, not those another session holds on a lease (`hand_outs.lease`). They are left out in the query, so a
    long ledger never starves a session."""
    leave = _at_limit(handed(job))
    if skip_leased:
        held = hand_outs.leased(job, "handed")
        # nor those claimed for a session that has not taken them yet (`hand_outs.py`)
        held = held | await hand_outs.held(session, job.owner, hand_outs.TITLES)
        leave |= _ids(held)
    return await untagged(session, job.owner, _scope(job), limit=limit, leave=leave)


def hand_out(job: RoleTagJob, roles: list[Opportunity], version: int | None = None) -> None:
    """Note on the job that these roles were handed to a title session (see `HANDED_AT_MOST`), and the version of What
    to look for it was handed (`version`), which the roles it matches record (`tag`)."""
    ledger = handed(job)
    for r in roles:
        ledger[str(r.id)] = ledger.get(str(r.id), 0) + 1
    job.counts = {**(job.counts or {}), "handed": ledger, **({"version": version} if version is not None else {})}
    hand_outs.lease(job, "handed", [str(r.id) for r in roles])


def item(role: Opportunity) -> dict:
    """One role as the title session is handed it for tagging: what it is called and where."""
    return {"id": short_id(role), "kind": "role", "title": role.title, "company": role.company}


def tag(job: RoleTagJob, role: Opportunity, role_id: str | None, version: int | None = None) -> None:
    """Apply one answer: the target role the role matches, or none. Either is Haiku's answer, kept on the role with the
    version of What to look for it was made under (the one the session was handed, else `version`), so the role is not
    asked again. An answer a higher pass (the analyst) made since the hand-out is left as it is."""
    if not role_match.may_write(role, role_match.HAIKU):  # pyright: ignore[reportArgumentType]  # role_match's Protocol
        return
    counts = dict(job.counts or {})
    role.target_role = role_id or None
    role.matched_by = role_match.HAIKU
    role.matched_by_version = counts.get("version") or version
    if role_id:
        counts["tagged"] = int(counts.get("tagged") or 0) + 1
    job.counts = counts


async def answerable(session: AsyncSession, job: RoleTagJob, ids: Collection[str]) -> dict[str, Opportunity]:
    """The roles among `ids` (full ids) that were handed to the job and that Haiku has still to answer, whole rows
    (an answer sets them). One read for the batch, not one over every role there is."""
    ledger = handed(job)
    wanted = _ids([i for i in dict.fromkeys(ids) if i in ledger])
    if not wanted:
        return {}
    rows = await session.scalars(select(Opportunity).where(
        *needs_haiku(job.owner, _scope(job)), Opportunity.id.in_(wanted)))
    return {str(r.id): r for r in rows}


async def _count(session: AsyncSession, owner: str, stages: Collection[str] | None) -> int:
    return int(await session.scalar(select(func.count()).select_from(Opportunity).where(
        *needs_haiku(owner, stages))) or 0)


async def start(session: AsyncSession, owner: str, stages: Collection[str] | None = None
                ) -> tuple[RoleTagJob | None, int]:
    """Open the job (or find the one open), and say how many roles it has to settle. No job is made when no role needs
    Haiku. Raises `TagError` when the owner has no target role to match them to. `stages` limits the job to roles in
    those stages (the job that starts by itself asks the live ones)."""
    profile = await profile_store.current(session, owner)
    if not any(r.says_something() for r in profile.criteria.target_roles):
        raise TagError("add a target role first: a role is matched to one of the ones you have")
    job = await open_job(session, owner)
    if job is not None:
        return job, await _count(session, owner, _scope(job))
    count = await _count(session, owner, stages)
    if not count:
        return None, 0
    job = RoleTagJob(owner=owner, status=OPEN, counts={"total": count, **({"stages": list(stages)} if stages else {})})
    session.add(job)
    await session.flush()
    return job, count


async def to_tag(session: AsyncSession, owner: str, *, skip_leased: bool = False,
                 limit: int | None = None) -> list[Opportunity]:
    """The roles the open job still has to hand out (`waiting`), and none when the owner has no target role left to
    match them to. `settle`'s question, asked without closing anything (a runner's ask only reads:
    `housekeeping.py`); with `skip_leased`, what a title session could be handed now (`titles.free`)."""
    job = await open_job(session, owner)
    if job is None:
        return []
    profile = await profile_store.current(session, owner)
    if not any(r.says_something() for r in profile.criteria.target_roles):
        return []
    return await waiting(session, job, skip_leased=skip_leased, limit=limit)


async def _start_by_itself(session: AsyncSession, owner: str) -> RoleTagJob | None:
    """Open the job for the live roles Haiku has not answered, when there are some and it can run: no search is open
    (the same hold `titles.free` applies) and the owner has a target role. A role handed `HANDED_AT_MOST` times in the
    last job and never answered does not count: it would open a job to be handed the same two times again, for ever."""
    if await roster.open_run(session, owner) is not None:
        return None
    last = await session.scalar(select(RoleTagJob).where(RoleTagJob.owner == owner, RoleTagJob.status == DONE)
                                .order_by(RoleTagJob.started_at.desc()).limit(1))
    if not await untagged(session, owner, role_match.LIVE, limit=1, leave=_at_limit(handed(last))):
        return None
    try:
        job, _count_ = await start(session, owner, role_match.LIVE)
    except TagError:
        return None
    return job


async def settle(session: AsyncSession, owner: str) -> RoleTagJob | None:
    """The open job that still has roles to hand out, else None. A job with none left (or with no target role left to
    match them to) is closed here. With no job open, one starts for the live roles Haiku has not answered
    (`_start_by_itself`)."""
    job = await open_job(session, owner)
    if job is None:
        return await _start_by_itself(session, owner)
    if await to_tag(session, owner, limit=1):
        return job
    job.status, job.ended_at = DONE, datetime.now(UTC)
    await session.flush()
    return None


async def trend(session: AsyncSession, owner: str, role_id: str, days: int = 30, now: datetime | None = None) -> dict:
    """How many roles got past the gates for a target role on each of the last `days` days (docs/PRD.md 30.5, 31.8), in
    the owner's days (their time zone says where one ends), oldest first, today last. A role counts on the day it was
    *found* (`found_on`): an imported role on the tracker's found date, not on the day it was imported. The gated ones
    are not counted. A day with none is 0, and an id that is no target role is all zeros."""
    days = min(max(days, 1), MAX_TREND_DAYS)
    zone = owner_zone()
    first, last, since, until = days_window(days, now)
    rows = (await session.execute(select(Opportunity.found, Opportunity.created_at).where(
        Opportunity.owner == owner, Opportunity.target_role == role_id, reached_found(),
        found_on(first, last, since, until)))).all()
    per_day = Counter(found_day(found, created, zone) for found, created in rows)
    run = [first + timedelta(days=i) for i in range(days)]
    entries = [{"date": day.isoformat(), "found": per_day[day]} for day in run]
    return {"days": entries, "total": sum(e["found"] for e in entries)}
