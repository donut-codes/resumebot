"""Whether a posting is fresh: went up within the last so many days (the owner, 2026-10-13: getting in in the first 24
hours can be huge for the odds of a response).

"Within the last N days" means under N x 24 hours old, so "within 1 day" is under 24 hours. Three things say how old a
posting is, and the app uses what it has:

- **The time the app first saw it** (`JobPosting.first_seen`, which a posting read from an employer's board or a job
  site has): the hours since then. With a time, a posting is within N days when it is under N x 24 hours since.
- **The employer's own date for it** (`posted`), and failing that **the day the role was found** (`found`): a date and
  no hour. With only a date, a posting is within N days when that date is today or up to N days back, so "within 1 day"
  is today or yesterday. A date cannot say the hour, so a posting dated yesterday is given the benefit, though it may be
  nearly two days old.
- **Both**: the date can still say no where the time says yes. A posting the employer dated more than N days ago is not
  within N days, whenever the app first saw it (an employer's board read for the first time shows every posting on it
  as first seen now).

With neither a time nor a date the app cannot tell, and says so (None).

**The day a role was posted** (`posted_basis`, the owner, 2026-11-03) is one number for the hard pass, the re-gate, the
take-time check, the role page, the score and the sentence that says why a role was stopped, and (the owner, 2026-11-04)
for a card's Age, the stale penalty, the `new` tag, the golden bonus (`posting_day_hours`: whole days, so the tag never
says new where the Age says 3 days) and the order of the lanes (`posting_hours`; the hour only there, and only where a
day count cannot decide, a day that is itself the moment the app first saw a posting: `PostedDay.at`): the newest
employer date among the role's own and its postings' (a date held to the day the app first saw the posting it is of,
since a board that dates a posting again on every read has not made it new; the role's own date, which it copied from a
posting, to the day the app first saw the earliest of them), else the earliest day a posting was first seen or the role
was found; a job board's date never makes a role newer than the employer's (the owner, 2026-11-05: a board re-lists a
req under a new date), so it counts only when no employer date exists (`posted_basis`). It reads the postings
themselves and never the role's cleansed copy, which is of one posting
(`gate_copy`: the employer's own, else the newest) and so says one posting's date, not the role's.

Two things read these facts, so they cannot disagree: the ask "Posted within the last N days" (an `Ask` with `days`
and `above` false, `scoring.py`; the app ships it unused, the owner adds their own if they want one) and the order the
research goes in (`analysis.picks`, `catch_up.in_order`), youngest first (the owner, 2026-10-13: getting in in the first
24 hours can be huge for the odds of a response). For that order the same facts give one number, the posting's age in
whole hours (`age_hours`): the later of the hours since the app first saw it and the days its date says, so the date
still says older where the time says new. Services flush; nothing here commits.
"""

import asyncio
import uuid
from collections.abc import Collection, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import NamedTuple

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import db
from app.jobsearch import ats
from app.jobsearch.address import is_board_link
from app.jobsearch.employers import follow as employer_posting
from app.jobsearch.fit import role_order as queue
from app.jobsearch.models import ORIGIN_SCOUT, JobPosting, Opportunity
from app.timeutil import aware, owner_today, utc_now

HOURS_A_DAY = 24

#: Where a role's posted day is counted from (`PostedDay.basis`): the employer's own date, the day the app first saw a
#: posting, the day the role was found.
POSTED, SEEN, FOUND = "posted", "seen", "found"
#: The word an answer's evidence starts with, by what the days were counted from ("posted 5 days ago", "first seen 5
#: days ago", "found yesterday").
AGE_FROM = {POSTED: "posted", SEEN: "first seen", FOUND: "found"}


def hours_since(seen: datetime | None, now: datetime | None = None) -> float | None:
    """The hours since the app first saw a posting, or None when it has no time for it. A time a moment ahead of now
    (two clocks that disagree) is 0."""
    when = aware(seen)
    if when is None:
        return None
    return max(((now or utc_now()) - when).total_seconds() / 3600, 0.0)


def within(days: int, age_days: int | None, seen_hours: float | None) -> bool | None:
    """Whether a posting is within the last `days` days, or None when the app can't tell. `age_days` counts the whole
    days since the employer's date, else since the day the role was found (`gates.age_in_days`); `seen_hours` is the
    hours since the app first saw the posting (None: it has no time for it). With a time: under `days` x 24 hours.
    With only a date: today or up to `days` days back. A date more than `days` days back says no whatever the time
    says."""
    if age_days is None and seen_hours is None:
        return None
    if age_days is not None and age_days > days:
        return False
    return seen_hours is None or seen_hours < days * HOURS_A_DAY


def age_hours(age_days: int | None, seen_hours: float | None) -> int | None:
    """How old a posting is, in whole hours, for putting postings in order; None when the app can't tell. `age_days` and
    `seen_hours` are as in `within`. With a time and a date, the later of the two: the whole hours since the app first
    saw it and the date's days as hours (today 0, yesterday 24), so a posting the employer dated last week is a week
    old whenever the app first saw it, as `within` has it. With one of them, that one. Whole hours on purpose: the
    finds of one search are first seen minutes apart, and that is not an age; roles of the same hour (or, for a date,
    the same day) tie, and whatever else orders the roles (the queue rule) breaks the tie."""
    ages = [int(seen_hours)] if seen_hours is not None else []
    if age_days is not None:
        ages.append(age_days * HOURS_A_DAY)
    return max(ages) if ages else None


def youngest_first(posted: date | None, found: date | None, seen: datetime | None, today: date,
                   now: datetime | None = None) -> tuple[bool, int]:
    """Sort ascending by this: the youngest posting first, a posting the app can't date last. The age is from the time
    the app first saw the posting when it has one, else the employer's date, else the day the role was found, and the
    date can still make it older (`age_hours`)."""
    hours = age_hours(queue.age_of(posted, found, today), hours_since(seen, now))
    return hours is None, hours or 0


class OrderRole(NamedTuple):
    """A role as the stored order is worked out from it (`stored_order.work_out`): the columns the age and the number
    need and nothing else, as plain values (an `Opportunity` for each role of a pass over all of them
    costs far more than these do): the age (the posted and found days, the day it was made
    and whether the scout did, whether a board gave its date), the stage and Closed reason, and the fit. A role with a
    Second Impression is also scored by its breakdown and target role, so it is read as an `Opportunity` with those
    columns instead: `Dated` is what `posted_by_role`, `posting_hours` and `pipeline.order` ask of either."""

    id: uuid.UUID
    created_at: datetime
    origin: str
    posted: date | None
    found: date | None
    board_url: str | None
    status: str
    ended_reason: str | None
    fit: int | None


Dated = Opportunity | OrderRole


class AgeBasis(NamedTuple):
    """What a role's age in hours is counted from (`age_basis`): the day it was posted, whole days as hours, or a
    moment, counted in hours, and never both. Neither is a role no day dates (`posting_hours` is then None, and the role
    is last in an order by age). The two columns of the stored order (`Opportunity.queue_day`, `queue_moment`) hold
    exactly this, and `pipeline/stored_order.py` orders by them with the same sums in SQL."""

    day: date | None
    moment: datetime | None

    def hours(self, on: date, now: datetime) -> int | None:
        """The whole hours of the age at `on` and `now`: from a moment, the hours since it (a moment ahead of now is 0);
        from a day, the days since it (a day still ahead is 0) as hours: today 0, yesterday 24."""
        if self.moment is not None:
            return int(hours_since(self.moment, now) or 0)
        if self.day is not None:
            return max((on - self.day).days, 0) * HOURS_A_DAY
        return None


def age_basis(role: Dated, posted: "PostedDay | None" = None) -> AgeBasis:
    """What `posting_hours` counts a role's age from: its posted day (`posted_basis`, the day the gates count) unless
    the day cannot decide, where the day is itself a time: a posted day that is the day the app first saw a posting
    (`SEEN`) is counted from that moment, and a scout's role counted from its found day, with no posting in the index,
    from when the app made it (`created_at`, when that is the found day). A role no day dates is counted from when the
    app made it if it is a scout's, else from nothing. `posted` is the role's `PostedDay` (`posted_by_role`); without
    it, the one the role's own columns give."""
    day = posted or posted_basis(role.posted, role.found, ())
    made = aware(role.created_at) if role.origin == ORIGIN_SCOUT else None
    if day.day is None:
        return AgeBasis(None, made)
    moment = day.at if day.basis == SEEN else None
    if day.basis == FOUND and made is not None and owner_today(made) == day.day:
        moment = made
    return AgeBasis(None, moment) if moment is not None else AgeBasis(day.day, None)


def posting_hours(role: Dated, on: date, now: datetime, posted: "PostedDay | None" = None) -> int | None:
    """How old a role is, in whole hours, for the order of the lanes: the days since its posted day (`posted_basis`, the
    day the gates count) as hours, so the order counts the days the gates do (the owner, 2026-11-04). A date says no
    hour, so whole days as hours: today 0, yesterday 24. A day count cannot decide only where the day is itself a time
    (`age_basis`): the hours since that moment. Whole hours on purpose, as `age_hours` has it: roles found minutes apart
    are not an age apart, and fit orders them. `posted` is the role's `PostedDay` (`posted_by_role`); without it, the
    one the role's own columns give. The one number the order of the lanes reads (`pipeline.age_key`); the first hours
    and a row's `new` flag read `posting_day_hours`, which has no moment in it."""
    return age_basis(role, posted).hours(on, now)


def posting_day_hours(role: Dated, on: date, now: datetime, posted: "PostedDay | None" = None) -> int | None:
    """How old a role is for its first hours, in whole hours: the days since its posted day (`posted_basis`) as hours,
    with no moment in them (today 0, yesterday 24), so the New tag and the golden bonus say what the card's Age says: a
    card that says 3 days is 72 hours old here, never 70 because the app first saw the posting at 2 am (the owner,
    2026-11-04: the hour counts only in the order of the lanes, `posting_hours`). A role no day dates (its Age is
    unknown) is counted as `posting_hours` has it, from when the app made it if it is a scout's, else None. The one
    number the first hours (`pipeline.in_first_hours`) and a row's `new` flag read."""
    day = posted or posted_basis(role.posted, role.found, ())
    if day.day is None:
        return posting_hours(role, on, now, day)
    return (day.days(on) or 0) * HOURS_A_DAY


def facts(posted: date | None, seen: datetime | None, now: datetime | None = None,
          basis: str | None = None) -> dict:
    """What scoring needs of a posting's age besides `age_days`: `seen_hours`, the hours since the app first saw it
    (None: no time), and `age_from`, what `age_days` counts from: the employer's date ("posted"), the day the app first
    saw a posting ("first seen") or the day the role was found ("found"). `basis` is a role's `PostedDay.basis`; without
    it the age is the employer's date when there is one (`posted`), else the day found."""
    return {"seen_hours": hours_since(seen, now),
            "age_from": AGE_FROM.get(basis or "") or ("posted" if posted else "found")}


# --- the words the role panel shows ---------------------------------------------------------------------------------


def _days_ago(days: int) -> str:
    return "today" if days == 0 else "yesterday" if days == 1 else f"{days} days ago"


def _hours_ago(hours: float) -> str:
    if hours < 1:
        return "under an hour ago"
    if hours < 2:
        return "1 hour ago"
    return f"{int(hours)} hours ago" if hours < 48 else f"{int(hours // HOURS_A_DAY)} days ago"


def evidence(days: int, age_days: int | None, seen_hours: float | None, age_from: str = "posted") -> str:
    """The fact an answer to "within the last `days` days" rests on, in a few words: the date when it says the posting
    is older or is all the app has, else the time the app first saw the posting ("posted 5 days ago", "found
    yesterday", "first seen 5 hours ago"). Empty when the app has neither (it gave no answer)."""
    if age_days is not None and (age_days > days or seen_hours is None):
        return f"{age_from} {_days_ago(age_days)}"
    if seen_hours is not None:
        return f"first seen {_hours_ago(seen_hours)}"
    return ""


# --- when the app first saw a posting ---------------------------------------------------------------------------------


async def first_seen_by_key(session: AsyncSession, owner: str, keys: list[str]) -> dict[str, datetime]:
    """When the app first saw the postings that are not yet a role, by match key (the earliest, if several carry one):
    the time a role about to be made from a posting is counted from. A role with no posting in the index has none."""
    if not keys:
        return {}
    rows = await session.execute(
        select(JobPosting.match_key, func.min(JobPosting.first_seen)).where(
            JobPosting.owner == owner, JobPosting.opportunity_id.is_(None), JobPosting.match_key.in_(keys)
        ).group_by(JobPosting.match_key))
    out: dict[str, datetime] = {}
    for key, when in rows.all():
        seen = aware(when)
        if seen is not None:
            out[key] = seen
    return out


async def first_seen_by_role(session: AsyncSession, owner: str,
                             role_ids: Collection[uuid.UUID] | None = None) -> dict[uuid.UUID, datetime]:
    """When the app first saw the posting of each role that was made from one (the earliest, if several are linked to
    it), by the role's id. A role with no posting in the index (the scout's, a tracker's, one pasted in) is not here.
    `role_ids` narrows it to these roles (a list of none reads nothing)."""
    if role_ids is not None and not role_ids:
        return {}
    query = select(JobPosting.opportunity_id, func.min(JobPosting.first_seen)).where(
        JobPosting.owner == owner, JobPosting.opportunity_id.is_not(None))
    if role_ids is not None:
        query = query.where(JobPosting.opportunity_id.in_(role_ids))
    rows = await session.execute(query.group_by(JobPosting.opportunity_id))
    out: dict[uuid.UUID, datetime] = {}
    for role, when in rows.all():
        seen = aware(when)
        if role is not None and seen is not None:
            out[role] = seen
    return out


async def first_seen_of(session: AsyncSession, role_id: uuid.UUID) -> datetime | None:
    """When the app first saw the posting a role was made from (the earliest, if several are linked), or None."""
    return aware(await session.scalar(
        select(func.min(JobPosting.first_seen)).where(JobPosting.opportunity_id == role_id)))


# --- the day a role was posted ----------------------------------------------------------------------------------------

#: How many roles are read by their ids; more are read for the whole owner and picked out. The ids go as one argument
#: (`db.among`), so a long list is no long statement; what is left is the cost: the read by ids grows with the roles
#: asked about (a short list is quick, a long one slower) and the owner's whole set costs the same however few ask, so
#: the whole read pays only for a list of most of the owner's roles (as `chase.WHOLE_AT` has it). A list is narrow only
#: up to `NARROW_AT`, so a long one does not read the whole set again and again in a turn.
NARROW_AT = 20_000


@dataclass(frozen=True)
class PostedDay:
    """The day a role was posted, as `posted_basis` works it out, and what it was counted from (`POSTED`, `SEEN` or
    `FOUND`; None with no day). `posted` and `found` are that day as the two columns the gates and the score count an
    age from: the employer's date, or the day the app saw or found the role. `at` is, for a `SEEN` day, the time the app
    first saw the posting it is of (the hour a day count cannot say); it is no part of what two days are equal by."""

    day: date | None
    basis: str | None
    at: datetime | None = field(default=None, compare=False)

    @property
    def posted(self) -> date | None:
        return self.day if self.basis == POSTED else None

    @property
    def found(self) -> date | None:
        return self.day if self.basis in (SEEN, FOUND) else None

    def days(self, on: date) -> int | None:
        """The whole days from the posted day to `on` (a day still ahead is 0), or None with no day: the one count a
        card's Age, the stale penalty and every gate read."""
        return max((on - self.day).days, 0) if self.day is not None else None


#: A posting as `posted_basis` is handed it: the date it carries, when the app first saw it and, as a third item, True
#: when a job board gave it (`held_postings`); with two items it is the employer's.
HeldPosting = tuple[date | None, datetime | None] | tuple[date | None, datetime | None, bool]


def posted_basis(posted: date | None, found: date | None, postings: Iterable[HeldPosting], *,
                 posted_from_board: bool = False) -> PostedDay:
    """The day a role was posted: the one meaning of "the days since it was posted", worked out one way wherever it is
    asked. `posted` and `found` are the role's own columns and `postings` the (date, first seen) of each of its
    postings, each with a third item True when a job board gave it (`HeldPosting`); `posted_from_board` says the role's
    own date is a board's: the role was found on a board (`Opportunity.board_url`), so it copied its date from a board's
    posting (`pipeline/from_posting.py`).

    - The newest employer date among the role's own and its postings'. A posting's date is held to the local day the app
      first saw it: a board that dates a posting again on every read does not make it new (that day is counted from
      `SEEN`). The role's own date is held to the earliest local day the app first saw any of its postings, for the same
      reason: a role takes its date from a posting (`pipeline/from_posting.py`), so a board's later date is on the role
      too, and a posting is as old at the hard pass as the role made from it is at the re-gate. An undated posting is
      left out whenever any date exists, so a role is no younger for a copy of its posting on a job site that carries no
      date.
    - A job board's date never makes a role newer than the employer's (the owner, 2026-11-05: a board re-lists one req
      under a new id and a new date, so a role looks newer on its card than it is by the employer's posting). When any
      employer date exists, the board's dates are left out of the newest: the role is counted from the employer's, and a
      board's older date does not make it older (a newer req of the employer's is still the newest, as above). With no
      employer date the board's stand, the newest of them, as they always did.
    - With no date at all, the earliest local day a posting was first seen or the role was found (the evidence nearest
      the first time the role was up). The day the app first saw a posting says only that the role is at least that old,
      never that it was posted then, so it stands in for a date only when no posting of the role has one, and then the
      earliest is the nearest.
    - With none of these, no day: the age is unknown, and a gate on it waits rather than stops.

    A `SEEN` day carries the moment it is of (`PostedDay.at`; the earliest, if several are of that day)."""
    held: list[tuple[date | None, date | None, datetime | None, bool]] = []
    for when, first, *by_board in postings:
        moment = aware(first) if first is not None else None
        held.append((when, owner_today(moment) if moment is not None else None, moment, bool(by_board and by_board[0])))
    moments = [moment for _, _, moment, _ in held if moment is not None]
    earliest_at = min(moments, default=None)
    earliest = owner_today(earliest_at) if earliest_at is not None else None
    dated: list[tuple[date, str, datetime | None, bool]] = []
    if posted is not None:
        dated.append((earliest, SEEN, earliest_at, posted_from_board) if earliest is not None and posted > earliest
                     else (posted, POSTED, None, posted_from_board))
    for when, seen, moment, board in held:
        if when is not None:
            dated.append((seen, SEEN, moment, board) if seen is not None and when > seen
                         else (when, POSTED, None, board))
    if dated:
        counted = [(d, basis, at) for d, basis, at, board in dated if not board] or [
            (d, basis, at) for d, basis, at, _ in dated]  # a board's dates count only when there is no employer's
        newest = max(d for d, _, _ in counted)
        if any(d == newest and basis == POSTED for d, basis, _ in counted):
            return PostedDay(newest, POSTED)
        return PostedDay(newest, SEEN, min(at for d, _, at in counted if d == newest and at is not None))
    if found is not None and (earliest is None or found < earliest):
        return PostedDay(found, FOUND)
    return PostedDay(earliest, SEEN if earliest is not None else None, earliest_at)


#: The (date, first seen, from a job board) of each role's postings, by the role's id: what `posted_basis` reads.
Held = dict[uuid.UUID, list[HeldPosting]]
#: How many postings `held_postings` converts before it gives the loop back.
SLICE = 2000


async def held_postings(session: AsyncSession, owner: str, ids: Collection[uuid.UUID] | None = None,
                        boards: frozenset[str] | None = None) -> Held:
    """The dates of the postings of the roles `ids` names, or, with none named, of every role of the owner's: one read,
    whatever the number, and which of them a job board gave. A caller that asks for several lanes of roles reads the
    owner's once and hands it to `posted_by_role` for each. `boards`: the job board hosts, when the caller has read
    them (`follow.job_board_hosts`) already; without them they are read here."""
    if boards is None:
        boards = await employer_posting.job_board_hosts(session, owner)
    query = select(JobPosting.opportunity_id, JobPosting.posted, JobPosting.first_seen, JobPosting.ats, JobPosting.url,
                   JobPosting.apply_url).where(JobPosting.owner == owner, JobPosting.opportunity_id.is_not(None))
    if ids is not None:
        query = query.where(db.among(JobPosting.opportunity_id, ids))
    held: Held = {}
    # In slices of `SLICE` rows, the loop given back after each: the conversion of thousands of rows and the test of
    # each one's link are not made in one stretch.
    res = await session.stream(query.execution_options(yield_per=SLICE))
    try:
        async for part in res.partitions(SLICE):
            for role_id, when, first, system, url, apply_url in part:
                if role_id is not None:
                    # A job board's, as a link is (`address.is_board_link`): never one read from a job system or the
                    # employer's own site (`ats.from_source`), and not one of a job site's whose address is an
                    # employer's.
                    by_board = not ats.from_source(system) and is_board_link(url or apply_url, boards)
                    held.setdefault(role_id, []).append((when, first, by_board))
            await asyncio.sleep(0)
    finally:
        await res.close()
    return held


async def posted_by_role(session: AsyncSession, owner: str, roles: Iterable[Dated],
                         held: Held | None = None, boards: frozenset[str] | None = None) -> dict[uuid.UUID, PostedDay]:
    """The posted day of each of these roles (`posted_basis`), by the role's id: one read of their postings, by their
    ids, or the owner's whole set for a list of more than `NARROW_AT` roles, whatever the number (`held`: the read, when
    the caller has it already; `boards`: the job board hosts, likewise). A role with no posting is counted from its own
    columns."""
    roles = list(roles)
    if not roles:
        return {}
    if held is None:
        held = await held_postings(session, owner, [r.id for r in roles] if len(roles) <= NARROW_AT else None,
                                   boards)
    return {r.id: posted_basis(r.posted, r.found, held.get(r.id, ()), posted_from_board=bool(r.board_url))
            for r in roles}


async def posted_of(session: AsyncSession, role: Opportunity) -> PostedDay:
    """The posted day of one role (`posted_by_role`)."""
    return (await posted_by_role(session, role.owner, [role]))[role.id]
