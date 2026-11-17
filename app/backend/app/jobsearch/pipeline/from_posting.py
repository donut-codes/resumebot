"""A posting that passes the hard gates becomes a role at once:
code makes it, in Landing Zone, and the stages move it on (cleansing, the Haiku rank, the deck). No session judges its
title first: the rank replaces that guess. The role's target role is matched in passes (`work/role_match.py`): the
free one is here, when the role is made (its title holds one target role's example title as a whole phrase), and a
later pass overwrites it.

`make_roles` takes the postings the filter let through (`sift.py` sets the route `roles` on them), newest first by the
date they were posted, else first seen, a batch at a time. A posting is checked once more against the gates in force
now: one that fails is stopped, as the filter would stop it, and never a role. A posting that is the owner's role
already in another writing is linked to that role. A job the owner added by its link that nothing has named yet waits
for its read (`waiting_added_where`): it has no title to make a role of. A batch of more than one slice (`SLICE`) is
saved slice by slice (`db.release_if_free`, which does nothing inside a savepoint or a process step), the postings of
the next slice and the roles they may match read again after each save; a batch of one slice, such as the owner's added
link, is never saved by this module, except that a batch not narrowed by `only` (the queue's and the deck's) saves its
slice before the employers' watch list is settled. Services flush; the caller commits."""

import uuid
from collections.abc import Collection
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import and_, func, literal, not_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import db
from app.jobsearch import ats, off_turn
from app.jobsearch.address import is_board_link
from app.jobsearch.employers import core as employers
from app.jobsearch.employers import follow as employer_posting
from app.jobsearch.gating import check as gating
from app.jobsearch.imports.tracker import slugify
from app.jobsearch.judged import ROLES
from app.jobsearch.measures.pay_place import as_date, match_key
from app.jobsearch.models import ORIGIN_OWNER, ORIGIN_SCOUT, Employer, JobPosting, Opportunity
from app.jobsearch.postings import sourced as sourcing
from app.jobsearch.postings import whole
from app.jobsearch.profile import store as profile_store
from app.jobsearch.research import analysis
from app.jobsearch.stages import LANDING_ZONE
from app.jobsearch.work import role_match, titles
from app.sites import where
from app.timeutil import owner_today

#: How many postings one turn of the pipeline makes roles of: the rest follow on the next turns, newest first.
BATCH = 300
#: How many postings one transaction makes roles of when the batch has more than one slice: each slice is saved before
#: the next, so the rows of a slice are held for its own work and not for the batch's (a whole batch in one transaction
#: holds them for a long time). Making a role costs a few milliseconds a posting, so a slice of 40 stays inside the 250
#: ms write bar. The shape's constant, not a setting; the tests patch it.
SLICE = 40


def waiting_where(owner: str, *, probing: bool = False) -> tuple:
    """What makes a posting wait to become a role: through the filter, not stopped, open, not yet a role, and routed to
    the roles.

    `probing` is for the queue's probe, which reads through the partial index `ix_js_postings_owner_roles_waiting`
    (migration 0141) and must keep to it. SQLite uses a partial index only where the statement names the very route the
    index was made for, so the route is written into the statement, not bound; and once the table has statistics
    (`ANALYZE` counts a NULL as one of many values, so `opportunity_id IS NULL` looks like four rows) it chooses the
    `opportunity_id` index and walks every posting that has no role (slow when cold). `unlikely()` tells it that term
    matches few of them, which is true of this predicate as a whole; the partial index still holds and uses it."""
    no_role = JobPosting.opportunity_id.is_(None)
    route = JobPosting.route == ROLES
    if probing:
        no_role, route = func.unlikely(no_role), JobPosting.route == literal(ROLES, literal_execute=True)
    return (JobPosting.owner == owner, JobPosting.filtered_at.is_not(None), JobPosting.filtered_by.is_(None), no_role,
            JobPosting.is_open, route)


def makeable_where(owner: str, *, probing: bool = False) -> tuple:
    """What `make_roles` takes: the postings that wait for a role, less the added links nobody has named yet. The
    queue's probe reads exactly these (`work/queue/handlers.py`, with `probing`), and the partial index
    `ix_js_postings_owner_roles_waiting` (migration 0141) holds the postings that wait."""
    return (*waiting_where(owner, probing=probing), not_(titles.unnamed()))


def waiting_added_where(owner: str) -> tuple:
    """The jobs the owner added by their link that nothing has named yet: they wait to be read, and the board's
    Added links column holds only these."""
    return (*waiting_where(owner), titles.unnamed())


def _comp_text(posting: JobPosting) -> str | None:
    low, high = posting.comp_min, posting.comp_max
    if low and high:
        return f"${int(low):,}-${int(high):,}"
    single = low or high
    if single:
        return f"${int(single):,}"
    return None


def newest_first() -> tuple[Any, ...]:
    """The order postings are made into roles in: the newest day first (the employer's date, else the day the app first
    saw the posting, `seen_day`: both are days, stored), then the one seen later, then by id so that the order never
    shifts between two reads."""
    return (func.coalesce(JobPosting.posted, JobPosting.seen_day).desc(), JobPosting.first_seen.desc(), JobPosting.id)


async def make_roles(session: AsyncSession, owner: str, limit: int | None = None,
                     only: Collection[uuid.UUID] | None = None, owners_own: bool = False) -> int:
    """Make a role of each of the next `limit` postings that wait for one, newest first; returns how many roles were
    made (a posting that is a role already, or fails a gate now, is linked or stopped and not counted). `limit`
    defaults to `BATCH`, read when called. `only` narrows it to those postings (a link the owner just added);
    `owners_own` says the owner asked for them (a link pasted for a posting the scout already held).

    A posting the owner added by its link (`ats.OWNER`, or `owners_own`) makes a role the owner added
    (`ORIGIN_OWNER`), not a scout's: its link is the role's source when it is one posting that is no job
    board's (`sourcing.source_given`), and the role is asked for
    research as the owner's ask, so it is read ahead of the roles nobody asked for."""
    # The reads that decide what to make (the waiting postings, newest first, the profile, the gates and the job
    # boards' hosts) are made apart from the writer's turn (`off_turn.reading`: long before the first
    # role). Each slice then reads its own postings again, with the conditions that
    # made them wait, in the transaction that writes it.
    query = select(JobPosting.id).where(*makeable_where(owner))
    if only is not None:
        query = query.where(JobPosting.id.in_(list(only)))
    async with off_turn.reading(session) as reader:
        ids = list((await reader.scalars(query.order_by(*newest_first()).limit(limit or BATCH))).all())
        if not ids:
            return 0
        profile = await profile_store.current(reader, owner)
        rules = await gating.rules(reader, owner, profile)
        targets = list(profile.criteria.target_roles)
        boards = await employer_posting.job_board_hosts(reader, owner)
    now = datetime.now(UTC)
    on = owner_today(now)
    order = {ident: place for place, ident in enumerate(ids)}
    made = 0
    for at in range(0, len(ids), SLICE):
        if at:
            await db.release_if_free(session)  # the slices before are saved
        # Read as they are now: another transaction may have changed these postings, or made roles of the same writing,
        # since they were found. Each is fetched by its key, and the database says whether it still waits (the
        # conditions in the select list, not the where: with `id IN (...)` beside them SQLite walks the index of the
        # postings with no role, most of them, a slice at a time).
        found = (await session.execute(
            select(JobPosting, and_(*makeable_where(owner)).label("waits")).where(JobPosting.id.in_(ids[at:at + SLICE]))
            .execution_options(populate_existing=True))).all()
        chunk = sorted((posting for posting, waits in found if waits), key=lambda p: order[p.id])
        made += await _make_slice(session, owner, chunk, rules=rules, targets=targets, boards=boards, now=now, on=on,
                                  owners_own=owners_own, release=len(ids) > SLICE)
    await session.flush()
    if made:  # a role in play: its employer is watched now when the owner asked for that (docs/PRD.md 29.9)
        # Once, at the end: a failure partway leaves the slices before it saved and this not run for them. It settles
        # the owner's whole watch list, not these roles alone, so the next role made or moved catches it up. After a
        # batch of slices the last slice is saved first: the watch list is a pass of its own, which the last slice's
        # roles would otherwise be held for. A batch of one slice is saved there
        # too when it is not narrowed by `only`: the queue's turn is the watch list on top of its slice; a link the
        # owner added (`only`) is saved by its caller.
        if len(ids) > SLICE or only is None:
            await db.release_if_free(session)
        await where.settle_watch(session, owner)
    return made


async def _make_slice(session: AsyncSession, owner: str, rows: Collection[JobPosting], *, rules: gating.Rules,
                      targets: Collection[Any], boards: frozenset[str], now: datetime, on: date, owners_own: bool,
                      release: bool = False) -> int:
    """`make_roles` for these postings, which are one slice of its batch: how many roles were made. The owner's roles of
    the same writing are read for this slice alone, so a role made since the last slice (by it or by another
    transaction) is found."""
    # The employers first (a read, or an insert of a new one: saved at once when the batch has other slices, so the
    # compute below does not hold the writer), then every judgement, with no statement between them, then the one
    # write: a role a flush each was a round trip and a turn for the other tasks each, and this is one.
    employer_of: dict[uuid.UUID, Employer | None] = {}
    new_employer = False
    resolved = await employers.find_many(session, owner, [p.company for p in rows])
    made_by_key: dict[str, Employer] = {}  # a company new to the owner, made once for the slice
    for posting in rows:
        employer = resolved[posting.company]
        if employer is None:
            ekey = employers.key_of(posting.company)
            if ekey and ekey != employers.PLACEHOLDER_KEY:
                employer = made_by_key.get(ekey)
                if employer is None:
                    employer = made_by_key[ekey] = await employers.make(session, owner, posting.company)
                    new_employer = True
        employer_of[posting.id] = employer
    if new_employer and release:
        await db.release_if_free(session)
    # After that save, which is a moment for another transaction to make a role of this writing: one made meanwhile is
    # found here and linked, not made twice (`uq_js_opportunities_owner_match_key`).
    known = {k: i for k, i in (await session.execute(select(Opportunity.match_key, Opportunity.id).where(
        Opportunity.owner == owner, Opportunity.match_key.in_([p.match_key or match_key(p.company, p.title)
                                                               for p in rows])))).all()}
    made: list[tuple[Opportunity, JobPosting]] = []
    settled: list[tuple[JobPosting, dict]] = []  # what each posting is given once the roles are written
    for posting in rows:
        key = posting.match_key or match_key(posting.company, posting.title)
        employer = employer_of[posting.id]
        subject = gating.posting_subject(posting, rules.index.of(posting.employer_id, posting.company))
        stop = gating.check(subject, rules)
        if stop is not None:
            settled.append((posting, gating.posting_stop_values(stop, rules)))
            continue
        if key in known:  # the owner has this role already, in the same writing
            settled.append((posting, {"opportunity_id": known[key]}))
            continue
        # only a source's own whole text is a description: a list's snippet and a page read whole are not
        text = whole.of_posting(posting)
        comp = posting.comp or _comp_text(posting)
        added = owners_own or posting.ats == ats.OWNER
        matched = role_match.by_title(posting.title, targets)  # the free pass (docs/PRD.md 30.5)
        role = Opportunity(
            id=uuid.uuid4(), owner=owner, slug=slugify(posting.company, posting.title), company=posting.company,
            title=posting.title,
            status=LANDING_ZONE, level=None, comp=comp, comp_source="posting" if comp else None,
            location=posting.location, move_type=posting.work_mode, found=on, url=posting.url,
            apply_url=posting.apply_url,
            board_url=posting.url if is_board_link(posting.url, boards) else None,
            ats=posting.ats, match_key=key, origin=ORIGIN_OWNER if added else ORIGIN_SCOUT,
            posted=as_date(posting.posted),
            employer_id=employer.id if employer else None, fit_breakdown={}, gate_answers=[],
            description=text or None, description_at=now if text else None,
            notes=None if posting.posted else "posting date unverified",
            target_role=matched, matched_by=role_match.CODE if matched else None,
            matched_by_version=rules.criteria if matched else None)
        session.add(role)
        settled.append((posting, {"opportunity_id": role.id}))
        known[key] = role.id
        made.append((role, posting))
    # The roles first and the postings after, in a flush each: a posting's link names a role the database must hold.
    await session.flush()
    for posting, values in settled:
        for column, value in values.items():
            setattr(posting, column, value)
    await session.flush()
    for role, posting in made:
        if owners_own or posting.ats == ats.OWNER:
            await _added_role(session, owner, role, posting)
    await session.flush()
    return len(made)


async def _added_role(session: AsyncSession, owner: str, role: Opportunity, posting: JobPosting) -> None:
    """What a role the owner added by its link carries from the start: its link as its source (when the link is the
    employer's own posting), and the owner's ask, which puts it ahead in the analyst's line."""
    sourced = bool(posting.url) and await sourcing.source_given(session, role, posting.url or "")
    role.analysis = {**(role.analysis or {}), analysis.ASKED_BY: analysis.ASKED_BY_OWNER}
    if sourced:
        await analysis.start(session, owner, role)
