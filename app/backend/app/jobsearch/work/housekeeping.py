"""The app's own housekeeping (docs/PRD.md path 47.5): what a runner's ask for work used to do on the way, so that the
ask only reads what is waiting. Resuming an orphaned search, failing the jobs that took too long
(`role_parse.expire`, `role_check.expire`, the Find Gaps sweeps, the browser's leases), the catch-up's next role, the
one-off tagging that has no role left, starting the next queued job of the pool, and removing the hand-out claims that
lapsed (`hand_outs.py`). One pass (`settle`) does all of it, in the order the ask did; the work queue's ticks run
its parts (`work/queue/handlers.py`).

The work queue's dispatcher runs the parts: the minute tick runs
`MINUTE` (the orphaned search, the jobs that took too long, the tagging job, the catch-up) and `LEASES` (the items
whose lease ran out go back), the hour tick runs `HOUR` (the rows of ended jobs and the retries of the master's
jobs, the lapsed claims, the tab register), and the pool's pull runs from `pool.owed`. `settle` with no `parts`
runs all of `ORDER`, the whole old pass in the order the ask did it, for the tests that need every part.

It is the only settler: no read settles on the way (the kinds' `free` and `waiting`, the walk, the ask), so what a read
answers is what the last pass left, and a caller that needs the pass first runs `settle`.

Apart from the pass, the hour tick clears the stored copies of postings that a newer copy replaced over seven days ago
(`clear_old_copies`, docs/PRD.md 47.9), so that they do not build up."""

import logging
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy import ColumnElement, Select, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased, load_only

from app import db
from app.jobsearch.models import CleansedPosting, JobPosting, RawPosting, RoleRank

log = logging.getLogger(__name__)

#: The parts of a pass, in the order the ask did them.
ORDER = ("search_steps", "jobs", "browser", "tagging", "catch_up", "queued", "claims", "tabs")
#: What the minute tick runs: an open search whose session went silent, the jobs no session took or finished in time
#: (the failing half of `jobs`), the tagging job with no role left, the catch-up's next role.
MINUTE = ("search_steps", "jobs_late", "tagging", "catch_up")
#: What the hour tick runs: the other half of `jobs` (the rows of ended jobs, and the master's jobs made again when they
#: were lost or failed), the lapsed hand-out claims, and what the tab register says is over.
HOUR = ("jobs_old", "claims", "tabs")
#: What the minute tick also runs: the items whose lease ran out go back to the queue. The whole pass does it in its
#: `browser` part. Nothing in the pass makes the queue: `browser.reconcile` does.
LEASES = ("leases",)

#: When the last pass finished in this program; None until the first pass after a start. The minute tick writes it,
#: and the dispatcher's (`dispatcher.last_pass`), to the engine's facts (`handlers.stamp_passes`), and the action
#: door's `health` reads them there (PRD path 50.5).
last_pass: datetime | None = None


async def settle(session: AsyncSession, owner: str, parts: tuple[str, ...] | None = None) -> dict[str, int]:
    """One pass of housekeeping. Each part is in a savepoint and its failure is logged, so one bad part never keeps the
    rest from running, and each part ends its own transaction (`db.release_if_free`): the rows one part writes are not
    held through the reads of the parts after it (inside a savepoint of the caller's, nothing is ended, and the pass is
    the one transaction it was). The caller commits what is left open. Returns what it did, by part, for the log and
    the tests.

    `parts` names the ones to run (the queue's ticks ask for `MINUTE`, `HOUR` or `LEASES`); by default the whole pass
    (`ORDER`)."""
    from app.agents import roster
    from app.jobsearch.browser import leases as browser_leases  # they read this module
    from app.jobsearch.browser import tabs as tab_register  # they read this module
    from app.jobsearch.gaps import coach as gap_coach  # they read this module
    from app.jobsearch.gaps import drafts as gap_drafts  # they read this module
    from app.jobsearch.gaps import runs as find_gaps  # they read this module
    from app.jobsearch.jobs import check as role_check  # they read this module
    from app.jobsearch.jobs import parse as role_parse  # they read this module
    from app.jobsearch.missions import catch_up  # they read this module
    from app.jobsearch.missions import search_run as run  # they read this module
    from app.jobsearch.resume import cleansed_master, structured_profile  # they read this module
    from app.jobsearch.work import hand_outs, session_kinds, tagging  # they read this module

    did: dict[str, int] = {}

    async def part(name: str, step) -> None:
        try:
            async with session.begin_nested():
                did[name] = int(await step() or 0)
        except Exception:
            log.warning("housekeeping: %s failed", name, exc_info=True)
        await db.release_if_free(session)

    async def search_steps() -> int:
        open_run = await run.open_run(session, owner)
        if open_run is None:
            return 0
        roster.resume_if_stale(open_run)  # an orphaned search is the one about to be worked on again
        return 1

    async def expire_jobs() -> int:
        await role_parse.expire(session, owner)
        await cleansed_master.ensure(session, owner)   # the master's claims job, for a Sonnet session
        await structured_profile.ensure(session, owner)  # the master's own job, remade if it was lost or failed
        await role_check.expire(session, owner)
        await find_gaps.expire(session, owner)
        await gap_drafts.expire(session, owner)
        await gap_coach.expire(session, owner)
        return 1

    async def fail_late_jobs() -> int:
        """The failing half of `expire_jobs`, for the minute tick: the jobs no session took or finished in time."""
        await role_parse.expire(session, owner, drop_old=False)
        await role_check.expire(session, owner, drop_old=False)
        await find_gaps.expire(session, owner, drop_old=False)
        await gap_drafts.expire(session, owner, drop_old=False)
        await gap_coach.expire(session, owner, drop_old=False)
        return 1

    async def drop_old_jobs() -> int:
        """The rest of `expire_jobs`, for the hour tick: the rows of ended jobs, and the master's jobs made again."""
        await role_parse.expire(session, owner, fail_late=False)
        await cleansed_master.ensure(session, owner)
        await structured_profile.ensure(session, owner)
        await role_check.expire(session, owner, fail_late=False)
        await find_gaps.expire(session, owner, fail_late=False)
        await gap_drafts.expire(session, owner, fail_late=False)
        await gap_coach.expire(session, owner, fail_late=False)
        return 1

    async def browser() -> int:
        await browser_leases.expire(session, owner, datetime.now(UTC))
        return 1

    async def leases() -> int:
        """The same expiry with its own count, for the minute tick: the items whose lease ran out go back."""
        return await browser_leases.expire(session, owner, datetime.now(UTC))

    async def tagging_job() -> int:
        return int(await tagging.settle(session, owner) is not None)

    async def catch_up_next() -> int:
        await catch_up.hand_on(session, owner)
        return 1

    steps = {
        "search_steps": search_steps,
        "jobs": expire_jobs,
        "jobs_late": fail_late_jobs,
        "jobs_old": drop_old_jobs,
        "browser": browser,
        "leases": leases,
        "tagging": tagging_job,
        "catch_up": catch_up_next,
        "queued": lambda: session_kinds.pull(session, owner),
        "claims": lambda: hand_outs.expire(session, owner),
        "tabs": lambda: tab_register.expired(session, owner),  # what the tab register says is over (PRD 52.3)
    }
    for name in parts if parts is not None else ORDER:
        await part(name, steps[name])
    return did


# --- the stored copies of postings that a newer copy replaced -------------------------------------------------------

#: How long a stored copy of a posting is kept after a newer copy replaced it (docs/PRD.md 47.9).
KEEP_REPLACED = timedelta(days=7)
#: One search reads this many raw rows (by `seq`), one transaction clears the copies of this many, and one pass (an
#: hour tick) clears this many rows at most: the writer's turn is never held long, and what is left goes at the next.
CLEAR_SPAN = 5_000
CLEAR_BATCH = 200
CLEAR_CAP = 5_000


def _at(row: type[RawPosting], other: type[RawPosting]) -> tuple[ColumnElement[bool], ...]:
    """`other` is a raw row at `row`'s address: the rows of one posting (`raw_copies`)."""
    return other.owner == row.owner, other.source_id == row.source_id, other.ext_id == row.ext_id


def _current(copy: type[CleansedPosting], row: type[RawPosting], *of_row: ColumnElement[bool]) -> ColumnElement[bool]:
    """A posting names this cleansed copy of this raw row as its current one: the posting the row names or the one at
    the row's address, each found by a key. A posting names the copy of the newest raw row at its own address
    (`raw_copies.repoint`), so no other can; `_named_by_roles` asks about the rest all the same. `of_row` joins the
    copy to the row where the statement around it does not (each question is one flat subquery: one inside another
    would not be tied to the row of the statement)."""
    named = exists().where(*of_row, JobPosting.id == row.posting_id, JobPosting.current_cleansed_id == copy.id)
    there = exists().where(*of_row, JobPosting.owner == row.owner, JobPosting.employer_id == row.source_id,
                           JobPosting.ext_id == row.ext_id, JobPosting.current_cleansed_id == copy.id)
    return or_(named, there)


def _row_replaced(cutoff: datetime) -> tuple[ColumnElement[bool], ...]:
    """What makes a raw row (`RawPosting`) one to clear. All of: it names a posting; a newer row (a higher `seq`) of
    its kind from its reader is held for the same posting and was itself fetched before the cutoff; it is not a whole
    read of a board page (`raw_copies.board_page()`, which the board readers go back to); it was not fetched with
    the posting's newest detail row (the rows of one answer share their fetch time and are read together); no rank
    names a cleansed copy of it; and no posting names one as its current copy.

    A detail row is replaced only by a newer row of its own link: an answer of several replies writes a row only for
    the reply that changed (`raw_copies._keep_sent`), so the others' rows stay the newest of their links, which a
    re-parse replays by link (`reparse.parsed`)."""
    from app.jobsearch.postings import raw_copies  # it reads this package

    newer, detail, copy = aliased(RawPosting), aliased(RawPosting), aliased(CleansedPosting)
    replaced = exists().where(
        *_at(RawPosting, newer), newer.posting_id == RawPosting.posting_id, newer.seq > RawPosting.seq,
        newer.kind == RawPosting.kind, newer.reader == RawPosting.reader, newer.fetched_at < cutoff,
        or_(RawPosting.kind != raw_copies.DETAIL, newer.url.is_not_distinct_from(RawPosting.url)))
    newest_detail = (select(detail.fetched_at).where(*_at(RawPosting, detail), detail.kind == raw_copies.DETAIL)
                     .order_by(detail.seq.desc()).limit(1).scalar_subquery())
    ranked = exists().where(copy.raw_id == RawPosting.id, RoleRank.cleansed_id == copy.id)
    current = _current(copy, RawPosting, copy.raw_id == RawPosting.id)
    return (RawPosting.posting_id.is_not(None), RawPosting.reader != raw_copies.board_page(), replaced,
            RawPosting.fetched_at.is_distinct_from(newest_detail), ~ranked, ~current)


def _copy_replaced(cutoff: datetime) -> tuple[ColumnElement[bool], ...]:
    """What makes a cleansed copy (`CleansedPosting`, joined to its raw row) one to clear though its raw row stays.
    All of: its raw row names a posting and is not the newest at the posting's address; a newer copy of the same
    posting (one of a later raw row, or of the same row by a later cleaner) was itself cleaned before the cutoff; no
    rank names it; and no posting names it as its current copy. Nothing reads such a copy: a posting's copy is read
    through its pointer, and a raw row is asked for its copy only when it is the posting's newest
    (`raw_copies.current_copies`, `recleanse`, `repoint`)."""
    later, of, newer = aliased(RawPosting), aliased(RawPosting), aliased(CleansedPosting)
    not_newest = exists().where(*_at(RawPosting, later), later.seq > RawPosting.seq)
    replaced = exists().where(
        newer.raw_id == of.id, *_at(RawPosting, of), of.posting_id == RawPosting.posting_id,
        or_(of.seq > RawPosting.seq, newer.cleaner_version > CleansedPosting.cleaner_version),
        of.seq >= RawPosting.seq, newer.cleaned_at < cutoff)
    ranked = exists().where(RoleRank.cleansed_id == CleansedPosting.id)
    return (RawPosting.posting_id.is_not(None), not_newest, replaced, ~ranked,
            ~_current(CleansedPosting, RawPosting))


def _rows_to_clear(cutoff: datetime) -> Select[int]:
    """The `seq` of each raw row to clear."""
    return select(RawPosting.seq).where(*_row_replaced(cutoff))


def _rows_with_copies_to_clear(cutoff: datetime) -> Select[int]:
    """The `seq` of each raw row that stays and holds a cleansed copy to clear."""
    return (select(RawPosting.seq).join(CleansedPosting, CleansedPosting.raw_id == RawPosting.id)
            .where(*_copy_replaced(cutoff)).distinct())


async def _found(statement: Select[int]) -> list[int]:
    """What a search finds, read in a reader's transaction of its own: it takes no turn, and it sees what the
    transactions before it cleared."""
    async with db.read_session_factory()() as reader:
        return list((await reader.scalars(statement)).all())


async def _named_by_roles(session: AsyncSession, copies: Sequence[uuid.UUID]) -> set[uuid.UUID]:
    """Of these cleansed copies, the ones a posting of any role names as its current copy, wherever that posting is:
    one read of the index that holds the roles' postings with their copies (`ix_js_postings_owner_role_copy`), not of
    the table, whose rows carry the postings' text. A role would show no copy if its posting's went."""
    if not copies:
        return set()
    named = await session.scalars(select(JobPosting.current_cleansed_id).where(
        JobPosting.opportunity_id.is_not(None), JobPosting.current_cleansed_id.is_not(None),
        db.among(JobPosting.current_cleansed_id, copies)))
    return {copy for copy in named.all() if copy is not None}


async def _clear_rows(seqs: Sequence[int], cutoff: datetime) -> tuple[int, int]:
    """Clear these raw rows, each looked at again in the transaction that deletes it (a posting's pointer has no
    foreign key, and the search read them in another transaction). The database deletes a row's cleansed copies with
    it. Returns (the rows, the cleansed copies) cleared."""
    async with db.session_factory()() as session:
        rows = (await session.scalars(select(RawPosting).options(load_only(RawPosting.seq, RawPosting.id)).where(
            RawPosting.seq.in_(seqs), *_row_replaced(cutoff)))).all()
        copies = (await session.execute(select(CleansedPosting.id, CleansedPosting.raw_id).where(
            CleansedPosting.raw_id.in_([row.id for row in rows])))).all()
        named = await _named_by_roles(session, [copy.id for copy in copies])
        kept = {copy.raw_id for copy in copies if copy.id in named}
        gone = [row for row in rows if row.id not in kept]
        for row in gone:
            await session.delete(row)
        await session.commit()
    return len(gone), sum(1 for copy in copies if copy.raw_id not in kept)


async def _clear_copies(seqs: Sequence[int], cutoff: datetime, room: int) -> int:
    """Clear the replaced cleansed copies of these raw rows, `room` of them at most, each looked at again in the
    transaction that deletes it. Returns how many were cleared."""
    async with db.session_factory()() as session:
        copies = (await session.scalars(
            select(CleansedPosting).options(load_only(CleansedPosting.id))
            .join(RawPosting, RawPosting.id == CleansedPosting.raw_id)
            .where(RawPosting.seq.in_(seqs), *_copy_replaced(cutoff)).order_by(RawPosting.seq))).all()
        named = await _named_by_roles(session, [copy.id for copy in copies])
        gone = [copy for copy in copies if copy.id not in named][:room]
        for copy in gone:
            await session.delete(copy)
        await session.commit()
    return len(gone)


async def clear_old_copies(now: datetime) -> tuple[int, int]:
    """Clear the stored copies of postings that a newer copy replaced more than `KEEP_REPLACED` ago (docs/PRD.md
    47.9): the raw rows `_row_replaced` describes, each with its cleansed copies, and the cleansed copies
    `_copy_replaced` describes. The newest copies of a posting, the copy it names and every copy a rank names stay,
    whatever their age, and no posting, role, stage move, rank or pointer is written.

    The raw rows are gone through in order, `CLEAR_SPAN` of them to a search, each search in a reader's transaction;
    what a search finds is cleared `CLEAR_BATCH` rows at a time, each batch in a short transaction of its own that
    looks at the rows again. A pass stops at `CLEAR_CAP` rows and the next begins again from the first row, so a
    backlog goes over several passes. It deletes through the session, row by row: neither table is one the work queue
    watches. Returns (the raw rows, the cleansed copies) cleared, and logs them in one line when there were any."""
    cutoff = now - KEEP_REPLACED
    last = (await _found(select(func.coalesce(func.max(RawPosting.seq), 0))))[0]
    rows = with_rows = copies = 0
    try:
        for start in range(0, last, CLEAR_SPAN):
            window = (RawPosting.seq <= start + CLEAR_SPAN,)
            after = start
            while (room := CLEAR_CAP - rows - copies) > 0:
                seqs = await _found(_rows_to_clear(cutoff).where(RawPosting.seq > after, *window)
                                    .order_by(RawPosting.seq).limit(min(room, CLEAR_BATCH)))
                if not seqs:
                    break
                cleared = await _clear_rows(seqs, cutoff)
                rows, with_rows, after = rows + cleared[0], with_rows + cleared[1], seqs[-1]
            after = start
            while (room := CLEAR_CAP - rows - copies) > 0:
                seqs = await _found(_rows_with_copies_to_clear(cutoff).where(RawPosting.seq > after, *window)
                                    .order_by(RawPosting.seq).limit(min(room, CLEAR_BATCH)))
                if not seqs:
                    break
                copies, after = copies + await _clear_copies(seqs, cutoff, room), seqs[-1]
            if CLEAR_CAP - rows - copies <= 0:
                break
    finally:
        if rows or with_rows or copies:
            log.info("housekeeping: cleared %d raw rows and %d cleansed copies of postings (stored copies a newer copy "
                     "replaced over %d days ago)", rows, with_rows + copies, KEEP_REPLACED.days)
    return rows, with_rows + copies
