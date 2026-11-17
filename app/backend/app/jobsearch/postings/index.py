"""The posting index (docs/PRD.md 28.8 and 28.9): every posting read from an employer's board, kept with when it was
first seen, last seen and closed, every link it appeared under, and the system's own id. First seen is the date the app
trusts: a repost resets the employer's date, not ours.

`refresh` reads the due employers' boards through their adapters (the adapters package) and brings the index up to
date; every read is checked (28.7): a refusal, a changed shape, or a board that had roles and suddenly answers none is
a fault on the employer, never a silence. Reads run side by side, `READ_AT_ONCE` of them, and the fetcher keeps each
host to one request at a time at its pace and each job system to `polite.PLATFORM_AT_ONCE` a pace (docs/PRD.md 28.17);
writes run one at a time in the caller's session.

A job system that can be asked for less than its whole board (`Adapter.filters`) is asked for the owner's places and,
between full reads, for what is new (docs/PRD.md 39.5): `plan` makes the question from the owner's work location gate
and the employer's last full read, and the listing says what was asked (`Listing.asked`). The board is read in full,
the owner's places and all that is new or not, once a week and on the first night after the owner's places (or the
employer's board) change (39.24), because a read of only what is new never returns an old posting and so cannot say one
has gone.

`store` is the one place a read's postings are put in the index: the adapters' reads here and the reads of a site's own
saved skill (`site_reads.py`) both end in it. A posting a read did not return is closed only by a read that could have
returned it (39.5)."""

import asyncio
import hashlib
import json
import logging
import math
import uuid
from collections.abc import AsyncIterator, Awaitable, Collection
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, NamedTuple

import httpx
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from app import db
from app.jobsearch import adapters, as_read, ats, judged, off_turn, stages
from app.jobsearch.adapters import Ask, Asked, Wanted
from app.jobsearch.employers import resolve
from app.jobsearch.fetch import polite
from app.jobsearch.fetch.polite import NOT_NOW, Fetcher, Refused, RobotsRefused, tapping, timeup
from app.jobsearch.measures.pay_place import Gates, match_key, says_no_place
from app.jobsearch.models import Employer, JobPosting, Opportunity, posting_of_a_role_given_up
from app.jobsearch.postings import hold, raw_copies, read_progress, this_posting, whole
from app.jobsearch.postings.coop import Cooperate
from app.jobsearch.states import FetchKind
from app.jobsearch.work import queue

log = logging.getLogger(__name__)

#: Boards, descriptions and places read at once (docs/PRD.md 28.17). It sets how many reads are begun side by side and
#: no more: the fetcher decides how many of them are asking at any moment, one request at a time to a host, at a request
#: a second to it, and at most `polite.PLATFORM_AT_ONCE` requests a second (and open at once) to the hosts of one
#: platform. So the width is for the time the reads spend waiting on other sites' answers and on their turns, never for
#: pressing any one site. It was 4; 24 is past where a wider read stopped being faster on a real set of boards (the
#: platforms' own limit is then what a night waits for).
READ_AT_ONCE = 24
#: How many postings' pages `read_role_details` keeps in one writer's turn: a page kept is a posting, a raw row, a
#: cleansed copy and their pointers, a small slice of the one writer's turn each, which adds up over the many a pull
#: takes (more when the other handlers are writing too). The turn is let go between batches.
DETAILS_WRITE_BATCH = 8
#: What `_read` answers for a board, and `read_places` for a page, the night's time ran out before it was begun:
#: neither a listing nor an error.
NOT_BEGUN = object()
#: The fields an adapter gives that the index keeps as they last read.
FIELDS = ("title", "location", "work_mode", "comp", "comp_min", "comp_max", "url", "apply_url", "department")


#: What one slice of a board's save holds (`store` with `sliced`): the postings it looks at, and the rows it writes (new
#: postings, raw copies and cleansed copies), whichever is reached first; and how many postings one slice of its
#: close-out closes. The shape's own constants, not settings: a slice is a few statements, committed before the next
#: begins.
SLICE_SEEN, SLICE_ROWS, CLOSE_SLICE = 40, 20, 200
#: How many postings one page of a read of a source's postings holds: an ORM load is never more than this many rows (a
#: short statement keeps the loop free), a read of bare ids and flags `KNOWN_PAGE`.
PARTITION, KNOWN_PAGE = 250, 2000

#: What an employer's note says when its last read answered none of the roles it had.
EMPTY_NOTE = "had {before} open roles, and this read answered none"


def was_empty(employer: Employer) -> bool:
    """Whether the employer's last read answered none of the roles it had. A second read like it closes them: one empty
    read may be the site failing to show its roles, two are a board with none."""
    return employer.fetch_ok is False and employer.fetch_kind == FetchKind.EMPTY


def _date(value) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


#: What a failed read says about the site's skill (docs/PRD.md 29.4): `refused` (the site asks not to be read), `shape`
#: (it changed the way it answers) and `empty` (it had roles and answered none) mean the skill no longer fits the
#: site, so it is retired and the site gets a project again (`sites/where.py`, `retire_broken`); `gone` (the board is
#: not there: the employer's job system is looked for again, `resolve.forget`) and `network` (the site was
#: unreachable or erred: try again tomorrow) leave the skill alone.
BREAKS = (FetchKind.REFUSED, FetchKind.SHAPE, FetchKind.EMPTY)


def fault_kind(exc: Exception) -> FetchKind:
    """Which of the above a failed read is. One robots.txt that could not be read (the site was failing), one 429 and a
    timeout are `network`: a read tomorrow tries again, and the skill that works is not retired for them."""
    if isinstance(exc, RobotsRefused) and exc.unreachable:
        return FetchKind.NETWORK
    if isinstance(exc, Refused):
        return FetchKind.REFUSED
    if isinstance(exc, adapters.AdapterError):
        status = exc.status
        if status == 404:
            return FetchKind.GONE
        if status is not None and (status >= 500 or status in NOT_NOW):
            return FetchKind.NETWORK
        return FetchKind.REFUSED if status is not None and 400 <= status < 500 else FetchKind.SHAPE
    if isinstance(exc, httpx.HTTPError):
        return FetchKind.NETWORK
    return FetchKind.SHAPE


#: A board that has filters is read in full once a week, and the first night after its places change (docs/PRD.md
#: 39.24). Half a day short of seven, so a night's clock a few minutes ahead of last week's never makes it eight.
FULL_READ_EVERY = timedelta(days=7) - timedelta(hours=12)


def read_key(employer: Employer, wanted: Wanted | None) -> str:
    """A short fingerprint of what a full read of this employer's board asks for: the job system and address of the
    board, and the owner's places and ways of working. It is kept on the employer when a full read ends
    (`Employer.read_asked`), and a night's read is a full one when it no longer matches."""
    # Whether a probe confirmed the address is not part of which board it is (`resolve.board_key`).
    board = {k: v for k, v in resolve.address(employer.board).items() if k != "confirmed"}
    asked = [employer.ats, board, wanted.fingerprint if wanted is not None else ""]
    return hashlib.sha1(json.dumps(asked, sort_keys=True).encode()).hexdigest()[:16]


def plan(employer: Employer, wanted: Wanted | None, now: datetime) -> Ask:
    """What tonight's read of this employer's board asks for (docs/PRD.md 39.5 and 39.24). The owner's places always.
    What is new only between full reads: the board is read in full when it never was, when the owner's places (or the
    board) have changed since its last full read, and once a week; otherwise the question is the postings of the days
    since that read, with half a day to spare."""
    last = employer.read_full_at
    if last is None or employer.read_asked != read_key(employer, wanted):
        return Ask(wanted)
    since = now - last  # a clock that went back is a full read too
    if not timedelta(0) <= since < FULL_READ_EVERY:
        return Ask(wanted)
    return Ask(wanted, new_days=math.ceil(since.total_seconds() / 86400 + 0.5))


async def _read(fetch: Fetcher, employer: Employer, gate: asyncio.Semaphore, ask: Ask | None = None):
    adapter = adapters.ADAPTERS[employer.ats]
    async with gate:
        if timeup(fetch):  # the night's read ran out of time before this board was begun: it waits for the next read
            return employer, NOT_BEGUN, None, None
        try:
            if ask is not None:  # a job system with filters, asked for what the owner's settings want (`plan`)
                return employer, await adapter.read_asked(fetch, employer.board or {}, ask), None, None
            return employer, await adapter.read(fetch, employer.board or {}), None, None
        except (Refused, adapters.AdapterError, httpx.HTTPError, ValueError) as exc:
            return employer, None, str(exc) or exc.__class__.__name__, fault_kind(exc)
        except Exception as exc:  # an adapter doing something unforeseen fails its own board, never the night
            log.warning("reading %s's board failed unexpectedly", employer.name, exc_info=True)
            return employer, None, str(exc) or exc.__class__.__name__, fault_kind(exc)


async def finished_first(reads: list[Awaitable], session: AsyncSession) -> AsyncIterator[tuple[int, Any]]:
    """The results of `reads` as each finishes, each with its place in `reads`: a night's read stores a board as soon as
    it is read, so the listings of every board are not all held in memory at once (docs/PRD.md 28.17): a read's
    result is let go once it is handed over, and only the reads still going are kept, to be cancelled if the caller
    stops first.

    The reads run beside the caller's own database work in `session` and hold none of its transactions
    (`db.detached`). Before each wait for the next one (and once the last is handed over), what the caller wrote is
    saved and its transaction released (`db.release_if_free`): a board's rows are held while that board is written,
    never while the others are read."""

    async def numbered(place: int, read: Awaitable) -> tuple[int, Any]:
        return place, await read

    async def next_done() -> tuple[int, Any]:
        return (await done.get()).result()

    def watch(task: asyncio.Task[tuple[int, Any]]) -> asyncio.Task[tuple[int, Any]]:
        task.add_done_callback(done.put_nowait)
        task.add_done_callback(going.discard)
        return task

    loop = asyncio.get_running_loop()
    # A finished task holds its result, so no list of all the tasks is kept (nor given to `asyncio.as_completed`, which
    # keeps what it is given): the tasks not yet finished, and a queue that hands each over as it finishes.
    done: asyncio.Queue[asyncio.Task[tuple[int, Any]]] = asyncio.Queue()
    going: set[asyncio.Task[tuple[int, Any]]] = set()
    for place, read in enumerate(reads):
        going.add(watch(loop.create_task(numbered(place, read), context=db.detached())))
    count = len(going)
    try:
        for _ in range(count):
            await db.release_if_free(session)
            yield await next_done()
        await db.release_if_free(session)
    finally:
        for task in list(going):
            task.cancel()


def due(employers: list[Employer]) -> list[Employer]:
    """The employers whose board can be read: a system with an adapter, a whole board address, not gated. A partial
    address (a link to one job) waits for the read's first step to complete it, instead of failing every night."""
    return [e for e in employers if e.ats in adapters.ADAPTERS and ats.complete(e.ats, e.board) and not e.gated]


async def open_within(session: AsyncSession, owner: str, source_id: uuid.UUID, asked: Asked) -> int:
    """How many of the source's open postings a read that asked for only some of its board could have returned
    (`Asked.covers`): the postings in the owner's places, which are the ones the read can speak for. It asks `closed_at`
    alone and not `is_open`: it is the baseline the read compares itself with, what the board had in those places
    whether or not it was being read since (as `site_reads` does for a site)."""
    rows = (await session.execute(select(JobPosting.location, JobPosting.work_mode, func.count()).where(
        JobPosting.owner == owner, JobPosting.employer_id == source_id, JobPosting.closed_at.is_(None)
    ).group_by(JobPosting.location, JobPosting.work_mode))).all()
    return sum(row[2] for row in rows if asked.covers(row))


async def refresh(session: AsyncSession, owner: str, fetch: Fetcher, only: list[Employer] | None = None,
                  now: datetime | None = None, gates: Gates | None = None) -> dict:
    """Read every due employer's board and bring the index up to date. Returns what changed and what failed.

    With the owner's `gates`, a board whose job system has filters is asked for the owner's work location and, between
    full reads, for what is new (`plan`); without them every board is read whole."""
    now = now or datetime.now(UTC)
    employers = due(only if only is not None else list((await session.scalars(
        select(Employer).where(Employer.owner == owner).order_by(Employer.name))).all()))
    await read_progress.set_part(session, "built_in", len(employers))
    already = await read_progress.sites_done()  # a read taken up after a restart skips the sites it finished
    total = len(employers)
    employers = [e for e in employers if str(e.id) not in already]
    wanted = Wanted.of(gates)
    asks = {e.id: plan(e, wanted, now) for e in employers
            if gates is not None and getattr(adapters.ADAPTERS.get(e.ats or ""), "filters", False)}
    gate = asyncio.Semaphore(READ_AT_ONCE)
    counts = {"employers": total, "failed": 0, "seen": 0, "new": 0, "closed": 0, "reopened": 0,
              "unread": 0, "cut": 0}
    faults: list[tuple[int, dict]] = []  # with the employer's place, so they are told in the order the employers are
    finished: list[uuid.UUID] = []  # read, to be noted as done once saved (`read_progress`)
    asked_less = 0  # the boards a read asked for less than all of
    # Each board is saved once it is written (`finished_first` releases the session before each wait), so an employer's
    # row is held for the board it is being written for, never while the others are read.
    async for place, (employer, listing, error, kind) in finished_first(
            [_read(fetch, e, gate, asks.get(e.id)) for e in employers], session):
        await read_progress.note_sites(session, finished)
        finished.clear()
        if listing is NOT_BEGUN:  # the night's time ran out before it was begun (`_read`); nothing changes
            counts["unread"] += 1
            continue
        finished.append(employer.id)
        name, board_id, saved = employer.name, str(employer.id), (dict(counts), len(faults))
        # One board's save is its own unit, and a fault in it leaves the other boards alone. The board's postings are
        # saved a slice at a time (`store` with `sliced`: each slice is committed, so its rows are held for a slice and
        # not for the board), so the fault unit is the slice, a savepoint of its own inside `store`; what is written
        # about the employer before and after is a savepoint each. No rollback of the session: it would expire every
        # employer it holds, and the next board's read looks at its employer (`_read`).
        try:
            asked = getattr(listing, "asked", None)  # None: the whole board was read
            before = employer.open_roles
            if error is None and asked is not None:  # a read of some of the board speaks for those postings only
                # Read apart from the writer's turn (`off_turn.reading`); the savepoint below only assigns and flushes.
                async with off_turn.reading(session, apart=True) as reader:
                    before = await open_within(reader, owner, employer.id, asked)
            async with session.begin_nested():
                employer.fetched_at = now
                if error is not None:
                    employer.fetch_ok, employer.fetch_note, employer.fetch_kind = False, error[:500], kind
                    counts["failed"] += 1
                    log.warning("the board of %s (%s) could not be read: %s", employer.name, employer.ats,
                                employer.fetch_note)
                    faults.append((place, {"employer": employer.name, "employer_id": str(employer.id),
                                           "why": employer.fetch_note, "kind": kind}))
                    if kind == FetchKind.GONE:  # the board is gone: find the employer's system again, never this board
                        resolve.forget(employer, f"its board was gone ({error[:120]}); "
                                           "its job system will be looked for again")
                    continue
                if getattr(listing, "board", None):
                    employer.board = {**(employer.board or {}), **listing.board}
                again = was_empty(employer)
                if asked is not None:
                    asked_less += 1
            await db.release_if_free(session)  # the board's first write is let go before its slices are written
            after = await store(session, owner, listing, source_id=employer.id, ats_name=employer.ats,
                                company=employer.name, now=now, counts=counts, close_empty=again, sliced=True)
            # A read of only what is new answers none on most nights: that is not a board that has gone empty.
            emptied = (asked is None or not asked.new_only) and bool(before) and after == 0
            within = after
            if asked is not None and (not emptied or again):
                async with off_turn.reading(session, apart=True) as reader:
                    within = await open_within(reader, owner, employer.id, asked)
            async with session.begin_nested():
                if emptied:
                    employer.fetch_ok, employer.fetch_kind = False, FetchKind.EMPTY
                    employer.fetch_note = EMPTY_NOTE.format(before=before)
                    faults.append((place, {"employer": employer.name, "employer_id": str(employer.id),
                                           "why": employer.fetch_note, "kind": FetchKind.EMPTY}))
                else:
                    employer.fetch_ok, employer.fetch_kind = True, None
                    employer.fetch_note = listing.note or (
                        "stopped before the last page" if listing.truncated else None)
                    if listing.truncated and timeup(fetch):  # cut short by the night's time, not a repeated page
                        counts["cut"] += 1
                if not emptied or again:  # a first empty read closed nothing: the roles it had are still the baseline
                    employer.open_roles = within
                if (employer.id in asks and not emptied and not listing.truncated
                        and (asked is None or not asked.new_only)):
                    # A full read ended whole: the next is a week on, or when the owner's places (or the board) change.
                    employer.read_full_at, employer.read_asked = now, read_key(employer, wanted)
                await session.flush()
        except Exception as exc:
            log.exception("saving the board of %s failed; the slice it failed in is rolled back, the slices before it "
                          "stay saved, and the other boards go on", name)
            # What was counted of the postings stays (it is what was saved); what was counted about the board is undone.
            counts["failed"], counts["cut"] = saved[0]["failed"], saved[0]["cut"]
            del faults[saved[1]:]
            counts["failed"] += 1
            faults.append((place, {"employer": name, "employer_id": board_id,
                                   "why": f"saving its board failed: {type(exc).__name__}: {exc}"[:500],
                                   "kind": "failed"}))
    await read_progress.note_sites(session, finished)
    log.info("read %d boards in %d requests; %d were asked for less than all of the board (docs/PRD.md 39.5)",
             counts["employers"] - counts["unread"], fetch.requests, asked_less)
    return {**counts, "faults": [told for _, told in sorted(faults, key=lambda f: f[0])], "requests": fetch.requests}


def _keeps_places(posting: JobPosting, listed: str) -> bool:
    """Whether the posting keeps the places it holds against the place a list gives: a list that gives only a count of
    places ("26 Locations", or another word that is no place) does not take the place of the places the posting's own
    page named (`read_places`), as long as they are at least as many as the count."""
    return raw_copies.keeps_places(posting.location, listed)


async def store(session: AsyncSession, owner: str, listing, *, source_id: uuid.UUID, ats_name: str, company: str,
                now: datetime, counts: dict, company_from_postings: bool = False, close_empty: bool = False,
                reader: str | None = None, reader_version: int | None = None, sliced: bool = False) -> int:
    """Bring the index up to date with one whole read of one source: an employer's board (`source_id` is the employer)
    or a site's saved skill (`site_reads.py`: the employer when the site is an employer's, else the site itself). A
    posting is new, known (its last seen moves, a closed one reopens, one that was Not checked, because its board was
    not read, is open again: docs/PRD.md 39.8) or gone; the listing's own text and fields win;
    first seen is never moved. A posting is `company`'s unless `company_from_postings` and the posting names its own
    company (a job board lists other employers' roles). Counts what it did in `counts` (seen, new, reopened, closed)
    and returns how many postings the read saw. A read cut short (`Listing.truncated`) closes nothing, and neither does
    a read that saw none, unless `close_empty` (the read before it saw none too): one empty answer is as likely a site
    that failed to show its roles as a board with none. A read that asked for only some of the board
    (`Listing.asked`) closes only what it could have returned: nothing when it asked for only what is new, and when it
    asked for some places, the postings in those places (docs/PRD.md 39.5). Each posting a reader gave with the item it
    was read from is kept as a raw copy and a cleansed copy (`raw_copies.keep_items`) when its content changed: a
    posting whose item is other bytes but gives the cleaner exactly what the posting's last copy was made from (an
    applicant count moved, nothing else) keeps neither, and wakes no role (`raw_copies.unchanged`), while the index row
    is brought up to date as always; the cleansed copy is what the gates and the ranker read (`cleansed.py`), and the
    raw row is for the cleaner alone; `reader` says who read it (default: the job system's adapter) and
    `reader_version` the skill's.

    **One transaction, or slices.** By default the whole read is written in the caller's transaction, as it always was;
    there the loop is given back between postings and between pages of rows, and that is all that changes. The night's
    board read (`refresh`) asks for `sliced`, and so do a site's read by its skill (`site_reads.py`) and a board
    search (the board's guest reader, docs/PRD.md path 48): the postings and raw rows are read with
    nothing locked, what needs no row (the item's hash and bytes, the cleaner's answer: 14 ms a changed posting, most of
    what a board costs) is made with no transaction open, and the board is written `SLICE_SEEN` postings (or
    `SLICE_ROWS` rows) at a time, each slice committed before the next, then the close-out `CLOSE_SLICE` at a time, so
    no row is held for longer than a slice (docs/PRD.md 28.8). The cleaner's answers for a slice are made just before it
    is written and let go once it is, so memory holds a slice's, not the board's. A slice is locked first, in id order,
    and each posting is written as it is then (a detail read or a hand-in may have changed it since the read). A slice
    is a savepoint of its own: one that fails is rolled back alone, the slices before it stay saved, and `counts` say
    what was saved.
    `sliced` must be asked by a caller that holds no row of the board's postings (it commits between slices)."""
    kept = {"source_id": source_id, "ats_name": ats_name, "company": company, "now": now, "counts": counts,
            "company_from_postings": company_from_postings, "close_empty": close_empty, "reader": reader,
            "reader_version": reader_version}
    if sliced:
        return await _store_sliced(session, owner, listing, **kept)
    return await _store_whole(session, owner, listing, **kept)


@dataclass
class _Listed:
    """One posting of a read, as the save writes it: made from the listing alone, with nothing of the database in it."""

    ext_id: str
    title: str
    name: str
    posting: Any  # the reader's posting: the fields, and the item it was read from (`raw`)
    prepared: raw_copies.Prepared | None = None


def _listed(listing, *, company: str, company_from_postings: bool) -> list[_Listed]:
    """The postings of a read worth keeping, once each, in the order the read gave them."""
    out: list[_Listed] = []
    seen: set[str] = set()
    for raw in listing.postings:
        ext_id = str(raw.get("ext_id") or "").strip()
        title = str(raw.get("title") or "").strip()
        if not ext_id or not title or ext_id in seen:
            continue
        seen.add(ext_id)
        name = (str(raw.get("company") or "").strip()[:200] if company_from_postings else "") or company
        if company_from_postings:  # a job site's postings with no company of their own are not the site's host's
            name = ats.employer_from_link(name, raw.get("url"))
        out.append(_Listed(ext_id, title, name, raw))
    return out


def _assign(posting: JobPosting, item: _Listed, now: datetime) -> None:
    """What the read says of a posting, put on the row as it is now: open and checked, the listing's fields and text,
    the title and its key, the date, the day it was last seen and every link it appeared under."""
    raw = item.posting
    posting.not_checked_at = None  # the board is read and lists it: it is open, whatever it was (docs/PRD.md 39.8)
    for field in FIELDS:
        if raw.get(field) is not None and not (field == "location" and _keeps_places(posting, raw[field])):
            setattr(posting, field, raw[field])
    posting.title = item.title[:500]
    posting.match_key = match_key(item.name, item.title)
    read = _date(raw.get("posted"))  # a posting is posted once: a later read never says a later day (docs/PRD.md 28.9)
    posting.posted = min(read, posting.posted) if read and posting.posted else read or posting.posted
    if raw.get("text"):
        posting.text = raw["text"]
    posting.last_seen = now
    link = raw.get("url")
    if link and link not in (posting.links or []):
        posting.links = [*(posting.links or []), link]


def _new_posting(owner: str, source_id: uuid.UUID, ats_name: str, item: _Listed, now: datetime) -> JobPosting:
    return JobPosting(owner=owner, employer_id=source_id, ats=ats_name, ext_id=item.ext_id, company=item.name,
                      first_seen=now, links=[])


async def _store_whole(session: AsyncSession, owner: str, listing, *, source_id: uuid.UUID, ats_name: str,
                       company: str, now: datetime, counts: dict, company_from_postings: bool, close_empty: bool,
                       reader: str | None, reader_version: int | None) -> int:
    """`store`, all of it in the caller's transaction."""
    coop = Cooperate()
    # The text of a posting already known is not read here (a board has hundreds, each a page long); a read that has
    # text for it replaces it. The rows are read a page at a time, the loop given back between pages.
    known: dict[str, JobPosting] = {}
    after = None
    while True:
        query = select(JobPosting).where(JobPosting.owner == owner, JobPosting.employer_id == source_id).options(
            defer(JobPosting.text)).order_by(JobPosting.ext_id).limit(PARTITION)
        page = (await session.scalars(query if after is None else query.where(JobPosting.ext_id > after))).all()
        known.update((p.ext_id, p) for p in page)
        if len(page) < PARTITION:
            break
        after = page[-1].ext_id
        await coop()
    seen_ids = set()
    copies: list[tuple[JobPosting, dict, str | None]] = []
    for item in _listed(listing, company=company, company_from_postings=company_from_postings):
        await coop()
        seen_ids.add(item.ext_id)
        counts["seen"] += 1
        posting = known.get(item.ext_id)
        if posting is None:
            posting = _new_posting(owner, source_id, ats_name, item, now)
            session.add(posting)
            counts["new"] += 1
        elif posting.closed_at is not None:
            posting.closed_at = None
            counts["reopened"] += 1
        _assign(posting, item, now)
        copies.append((posting, item.posting, item.name))
    # Gone from a whole read means closed (a posting that was Not checked is closed too, not left unchecked, when this
    # read could have returned it); a read cut short can't say that, nor can a first empty one, nor a read that could
    # not have returned the posting (it asked for only what is new, or for places the posting is not in).
    asked = getattr(listing, "asked", None)
    if not listing.truncated and (seen_ids or close_empty):
        for ext_id, posting in known.items():
            if ext_id not in seen_ids and posting.closed_at is None and (asked is None or asked.can_close(posting)):
                posting.closed_at, posting.not_checked_at = now, None
                counts["closed"] += 1
    if any(getattr(listed, "raw", None) is not None for _, listed, _ in copies):
        await session.flush()  # the index rows are there before the copies that point at them
        await raw_copies.keep_items(session, owner, source_id, copies, reader=reader or f"adapter:{ats_name}",
                                    reader_version=reader_version, now=now)
    return len(seen_ids)


async def _existing(session: AsyncSession, owner: str, source_id: uuid.UUID, ext_ids: set[str], asked,
                    closing: bool) -> tuple[dict[str, uuid.UUID], list[uuid.UUID]]:
    """What the index holds of a source that a read concerns, as bare ids and flags (no row is loaded): the ids of the
    postings the read names, by posting id, and, when it can close (`closing`), the ids of the open ones it does not
    name that it could have returned. A read that can close looks at every posting of the source; one that cannot (cut
    short, or of only what is new) at the ones it names."""
    ids: dict[str, uuid.UUID] = {}
    closable: list[uuid.UUID] = []
    if not closing:
        named = sorted(ext_ids)
        for at in range(0, len(named), raw_copies.HEADS_AT_ONCE):
            rows = await session.execute(select(JobPosting.ext_id, JobPosting.id).where(
                JobPosting.owner == owner, JobPosting.employer_id == source_id,
                JobPosting.ext_id.in_(named[at:at + raw_copies.HEADS_AT_ONCE])))
            ids.update((ext_id, posting_id) for ext_id, posting_id in rows.all())
        return ids, closable
    coop = Cooperate()
    after = None
    while True:
        query = select(JobPosting.id, JobPosting.ext_id, JobPosting.closed_at, JobPosting.location,
                       JobPosting.work_mode).where(JobPosting.owner == owner, JobPosting.employer_id == source_id
                                                   ).order_by(JobPosting.ext_id).limit(KNOWN_PAGE)
        page = (await session.execute(query if after is None else query.where(JobPosting.ext_id > after))).all()
        for row in page:
            if row.ext_id in ext_ids:
                ids[row.ext_id] = row.id
            elif row.closed_at is None and (asked is None or asked.can_close(row)):
                closable.append(row.id)
        if len(page) < KNOWN_PAGE:
            return ids, closable
        after = page[-1].ext_id
        await coop()


async def _store_sliced(session: AsyncSession, owner: str, listing, *, source_id: uuid.UUID, ats_name: str,
                        company: str, now: datetime, counts: dict, company_from_postings: bool, close_empty: bool,
                        reader: str | None, reader_version: int | None) -> int:
    """`store`, a slice at a time: read with nothing locked, find which items are new, then for each slice prepare it
    with no transaction open, write and commit it (`SLICE_SEEN` postings), and close out last."""
    items = _listed(listing, company=company, company_from_postings=company_from_postings)
    seen_ids = {i.ext_id for i in items}
    asked = getattr(listing, "asked", None)
    closing = not listing.truncated and (bool(seen_ids) or close_empty) and (asked is None or not asked.new_only)
    with_item = [i for i in items if getattr(i.posting, "raw", None) is not None]
    reader = reader or f"adapter:{ats_name}"
    # 1. What the index holds, read with nothing locked and released at once (a savepoint: a read that fails leaves the
    #    session as it was).
    async with off_turn.reading(session) as apart:  # a reader's session where `session` is free: no writer's turn
        existing, closable = await _existing(apart, owner, source_id, seen_ids, asked, closing)
        have = await raw_copies.heads(apart, owner, source_id, [i.ext_id for i in with_item])
    await db.release_if_free(session)
    # 2. Which items are new: each item's bytes and hash. Nothing here touches a row; the loop is given back between
    #    postings. The bytes are kept for the items that are new, until the slice that holds them is prepared.
    coop = Cooperate()
    changed: dict[str, tuple[bytes, str, str]] = {}
    with session.no_autoflush:
        for item in with_item:
            await coop()
            body, content_type, sha = raw_copies.item_bytes(item.posting.raw)
            if raw_copies.is_new(have.get(item.ext_id, []), sha):
                changed[item.ext_id] = (body, content_type, sha)
    # 3. The slices. Each is prepared just before it is written and its answers are let go once it is saved, so what is
    #    held at a time is a slice's, not the board's (a 10,000-posting board's items are four times its listing).
    for chunk in _slices(items, existing, changed):
        await _prepare(session, owner, [i for i in chunk if i.ext_id in changed], changed, have, coop)
        new, reopened = await _write_slice(session, owner, chunk, existing, source_id=source_id, ats_name=ats_name,
                                           now=now, reader=reader, reader_version=reader_version)
        for item in chunk:
            item.prepared = None
            changed.pop(item.ext_id, None)
        counts["seen"] += len(chunk)
        counts["new"] += new
        counts["reopened"] += reopened
        await db.release_if_free(session)
    # 4. Gone from a whole read means closed (see `store`), a slice at a time, last.
    for at in range(0, len(closable), CLOSE_SLICE):
        counts["closed"] += await _close_slice(session, closable[at:at + CLOSE_SLICE], asked, now)
        await db.release_if_free(session)
    return len(seen_ids)


async def _prepare(session: AsyncSession, owner: str, chunk: list[_Listed],
                   changed: dict[str, tuple[bytes, str, str]], have: dict[str, list[raw_copies.Held]],
                   coop: Cooperate) -> None:
    """What needs no row, made for the items of a slice before it takes a lock: for each, whether it changes anything
    the cleaner reads (`raw_copies.unchanged`: a posting that holds an item and a current copy and gives the cleaner
    the same inputs is marked, and nothing more is made of it), else the cleaner's answer (14 ms a changed posting, most
    of what a board costs). The only reads are the parse of the rows a posting's copy folds (a page at a time, then let
    go) and which of the postings' newest rows hold a copy of this cleaner version (one statement a page); both are
    released before the cleaner runs, so no transaction is open while it does."""
    for at in range(0, len(chunk), PARTITION):
        batch = chunk[at:at + PARTITION]
        async with off_turn.reading(session) as reader:
            await raw_copies.with_parse(reader, [have.get(item.ext_id, []) for item in batch])
            current = await raw_copies.current_copies(reader, owner, [have.get(item.ext_id, []) for item in batch])
        await db.release_if_free(session)
        with session.no_autoflush:
            for item in batch:
                await coop()
                body, content_type, sha = changed[item.ext_id]
                item.prepared = raw_copies.prepare(item.ext_id, item.posting, item.name, body, content_type, sha,
                                                   have.get(item.ext_id, []), current)
                raw_copies.forget_parse(have.get(item.ext_id, []))


def _slices(items: list[_Listed], existing: dict[str, uuid.UUID], changed: Collection[str]) -> list[list[_Listed]]:
    """The postings of a read cut into slices: at most `SLICE_SEEN` postings and `SLICE_ROWS` rows to write (a new
    posting, and a raw copy and a cleansed copy for each item that is new), in the order the read gave them. An item
    that `_prepare` finds changed nothing the cleaner reads (`raw_copies.unchanged`) is counted here as written and
    writes none, so its slice is smaller than the budget, never larger."""
    out: list[list[_Listed]] = []
    chunk: list[_Listed] = []
    rows = 0
    for item in items:
        cost = (0 if item.ext_id in existing else 1) + (2 if item.ext_id in changed else 0)
        if chunk and (len(chunk) >= SLICE_SEEN or rows + cost > SLICE_ROWS):
            out.append(chunk)
            chunk, rows = [], 0
        chunk.append(item)
        rows += cost
    if chunk:
        out.append(chunk)
    return out


async def _write_slice(session: AsyncSession, owner: str, chunk: list[_Listed], existing: dict[str, uuid.UUID], *,
                       source_id: uuid.UUID, ats_name: str, now: datetime, reader: str,
                       reader_version: int | None) -> tuple[int, int]:
    """Write one slice, in a savepoint of its own: the known postings of it are locked first (`FOR NO KEY UPDATE`, in id
    order, the text left unread) and written as they are now, so what a read of a posting's page or a hand-in changed
    since the board was read is not undone from a stale copy; then the raw and cleansed copies. Returns how many
    postings were new and how many reopened."""
    new = reopened = 0
    async with session.begin_nested():
        ids = [existing[i.ext_id] for i in chunk if i.ext_id in existing]
        rows = (await as_read.lock(session, JobPosting, ids,
                                   defer=[JobPosting.text])).rows
        pairs: list[tuple[JobPosting, raw_copies.Prepared]] = []
        for item in chunk:
            posting_id = existing.get(item.ext_id)
            if posting_id is None:
                posting = _new_posting(owner, source_id, ats_name, item, now)
                session.add(posting)
                new += 1
            elif (posting := rows.get(posting_id)) is None:
                continue  # gone since it was read: the next read finds it new
            elif posting.closed_at is not None:
                posting.closed_at = None
                reopened += 1
            _assign(posting, item, now)
            if item.prepared is not None:
                pairs.append((posting, item.prepared))
        await session.flush()  # the index rows are there before the copies that point at them
        await raw_copies.keep_prepared(session, owner, source_id, pairs, reader=reader,
                                       reader_version=reader_version, now=now)
    return new, reopened


async def _close_slice(session: AsyncSession, ids: list[uuid.UUID], asked, now: datetime) -> int:
    """Close the postings of a slice of the close-out that are still open and that the read could have returned, as they
    are now (locked first, as `_write_slice` does). Returns how many were closed."""
    closed = 0
    async with session.begin_nested():
        rows = (await as_read.lock(session, JobPosting, ids, defer=[JobPosting.text])).rows
        for posting in rows.values():
            if posting.closed_at is None and (asked is None or asked.can_close(posting)):
                posting.closed_at, posting.not_checked_at = now, None
                closed += 1
        await session.flush()
    return closed


#: A posting whose description was tried and not read is left alone this many days before it is tried again, so the few
#: that cannot be read do not take the night's reads from the others, every night.
DETAIL_RETRY_DAYS = 3


def needing_text(owner: str, now: datetime | None = None) -> tuple:
    """The conditions on a posting for its text to be fetched now (docs/PRD.md 39.6): it is open
    (`JobPosting.is_open`), the code gates let it through, it has no text (or under `cleansed.MIN_TEXT` characters: a
    snippet is not a posting), its job system can give it, and it was not
    tried in the last `DETAIL_RETRY_DAYS`. A posting a code gate stopped needs none, since the gates need none; a
    posting a role tab's title check stopped, or a role it became, passed the code gates and is in the set. So is a
    posting a change to the owner's settings lets through (the stop taken off it is all it takes), whatever night it
    was first read. The posting of a role (`ats.NO_DETAIL`) and one read in the owner's browser have no job system to
    ask: their text is what a reader or the analyst brought back (39.7), or the page's. A posting on an employer's own
    site that has a link is the exception: its own page is read (`read_page`). A posting of a role the app gave up on
    (`analysis.send_back`: findings graded nothing of the text it was sent back for, twice) is not in it: the text the
    read would bring back would only be the page that was let go."""
    now = now or datetime.now(UTC)
    return (
        JobPosting.owner == owner, JobPosting.is_open, JobPosting.filtered_at.is_not(None),
        ~posting_of_a_role_given_up(),
        or_(JobPosting.filtered_by.is_(None), JobPosting.judged_by != judged.GATE),
        whole.thin_sql(JobPosting.text),  # none, or a snippet
        or_(JobPosting.ats.not_in(ats.NO_DETAIL),
            and_(JobPosting.ats == ats.OWN_SITE, JobPosting.url.is_not(None), JobPosting.url != "")),
        or_(JobPosting.detail_at.is_(None), JobPosting.detail_at < now - timedelta(days=DETAIL_RETRY_DAYS)),
    )


def details_where(owner: str) -> tuple:
    """What `fill_role_details` takes, for a query that joins each posting to its role (`Opportunity.id ==
    JobPosting.opportunity_id`): the postings that need text (`needing_text`, which leaves out a role the app gave up
    on) of roles before Ready to Launch."""
    return (*needing_text(owner), Opportunity.status.in_(stages.PRE_READY))


async def _employers_of(session: AsyncSession, rows: list[JobPosting]) -> dict[uuid.UUID, Employer]:
    return {e.id: e for e in (await session.scalars(select(Employer).where(
        Employer.id.in_({p.employer_id for p in rows} or {None})))).all()}


async def _detail_recipes(session: AsyncSession, rows: list[JobPosting]) -> dict[uuid.UUID, Any]:
    """The live recipe of each employer's site, by employer id, for the employers of these own-site postings whose
    recipe has a detail request (`skills.RequestRecipe.detail`): the postings it listed are read by it, as the night's
    read does (`recipe_live.with_details`), with no browser."""
    wanted = {p.employer_id for p in rows if p.ats == ats.OWN_SITE and p.employer_id is not None}
    if not wanted:
        return {}
    from app.sites import skills  # the sites read the job systems, which read this module
    from app.sites.models import Site, SiteRecipe

    found = (await session.execute(
        select(Site.employer_id, SiteRecipe.recipe).join(SiteRecipe, SiteRecipe.site_id == Site.id)
        .where(Site.employer_id.in_(wanted), SiteRecipe.retired_at.is_(None), SiteRecipe.state == "live")
        .order_by(SiteRecipe.version))).all()
    out: dict[uuid.UUID, Any] = {}
    for employer_id, raw in found:
        if employer_id is None:
            continue
        try:
            recipe = skills.parse_recipe(raw)
        except Exception:  # a recipe that no longer parses reads nothing
            continue
        if isinstance(recipe, skills.RequestRecipe) and recipe.detail is not None:
            out[employer_id] = recipe
    return out


async def _read_by_recipe(fetch: Fetcher, recipe: Any, posting: JobPosting) -> dict | None:
    """One posting's description by its site's recipe (`skills.read_detail`); None for a refusal, a block, an answer
    that is not what the recipe reads, or a posting the answer says is gone: nothing is kept (marked tried)."""
    from app.sites import skills

    try:
        got = await skills.read_detail(fetch, recipe, posting.ext_id)
    except Exception:  # one posting's failure, a block among them, is a stop for that posting alone
        return None
    return {"text": got.text} if got.text else None


class Reading(NamedTuple):
    """What one read of one posting said: its text (whole, as the source gave it; None when it gave none), the facts it
    stated besides (work mode, place, posted day), when the source said the posting no longer exists (404 or 410,
    `polite.gone`), the words for the role's history, and, when a page read for a role stated the title of another job
    (`this_posting.same_job`), that title: a page that is another job's gives no text and none of its facts."""

    text: str | None
    facts: dict
    gone: str | None
    other: str | None = None


#: What a read that reached nothing says: no text, no facts, nothing about the posting being gone.
NOTHING = Reading(None, {}, None)
#: The facts a read keeps besides a text.
READ_FACTS = ("work_mode", "location", "posted")


async def read_link(fetch: Fetcher, link: str) -> Reading:
    """What the job system says of the posting a link opens, from the system's own answer for that posting, for every
    system whose reader can read one alone (`ats.parse(link).read`): the text as the system gave it, stripped, and the
    work mode (remote, hybrid, onsite), place and posted day when the answer gives them. Nothing when the link is on no
    such system or the read is refused, blocked or fails; `gone` says so when the system answers 404 or 410 for the
    posting. Never raises for a refusal, an HTTP error or an answer that is not what the reader reads."""
    at = ats.parse(link)
    if at is None or at.read is None:
        return NOTHING
    try:
        got = await at.read.reader.detail(fetch, at.read.board.parts, at.read.posting)
    except adapters.AdapterError as exc:
        return Reading(None, {}, polite.gone(link, exc.status))
    except (Refused, httpx.HTTPError, ValueError):
        return NOTHING
    said = {k: (got.get(k) or "").strip() for k in ("text", *READ_FACTS)}
    said = {k: v for k, v in said.items() if v}
    return Reading(said.pop("text", None), said, None)


async def read_page(fetch: Fetcher, link: str, title: str | None = None) -> Reading:
    """What the page at a link says of the posting, read as a browser shows it to anyone, in one request: the text is
    the JobPosting the page embeds, else the page's own text, and only when it is the whole posting (`whole.why_not`: a
    fragment, a cut-off page or a wall is no text at all); the facts are everything the page states of the job
    (`adapters.page_facts`: title, employer, place, pay, work mode, posted day). With the `title` of the role the page
    is read for, a text is kept only when the page names the role (`this_posting.names_the_role`: its text carries the
    title's words together, or the JobPosting it embeds or a heading has the title): the page of another
    job, or an employer's careers page around a posting that is drawn in the browser, gives no text and none of its
    facts, which are no more the role's than its text; when its embedded JobPosting is another job's, the title it
    states is `Reading.other`, which a link offered as the role's source is refused for (`sourced.accept_source`). A
    page that is not a web page, or a status that is not 200,
    gives nothing; a 404 or 410 is the page saying the posting is gone (`gone`), a block or a 5xx is never that. Polite
    like every read (robots.txt, a request a second to a host); it never raises for a refusal, which is a stop."""
    try:
        reply = await fetch.get(link)
    except (Refused, httpx.HTTPError, ValueError):
        return NOTHING
    if reply.status_code != 200 or "html" not in reply.headers.get("content-type", "html").lower():
        return Reading(None, {}, polite.gone(link, reply.status_code))
    text = (adapters.page_text(reply.text) or "").strip()
    facts = adapters.page_facts(reply.text)
    kept = text if whole.why_not(text) is None else None
    if kept and title and not this_posting.names_the_role(title, kept, reply.text, facts.get("title")):
        return Reading(None, {}, None, _another_job(title, facts))
    return Reading(kept, facts, None, None if kept else _another_job(title, facts))


def _another_job(title: str | None, facts: dict) -> str | None:
    """The title of another job a page states (its embedded JobPosting's), read for the role titled `title`, or None."""
    said = facts.get("title")
    return said if title and said and not this_posting.same_job(title, said) else None


async def read_detail(fetch: Fetcher, employer: Employer | None, posting: JobPosting,
                      gone: list[str] | None = None) -> dict | None:
    """What the posting's own page gives (its job system's `detail`), or None when there is no way to read it or the
    read failed: the posting is then left as it was. The page is given the stored posting, place included, so a system
    can tell a place its list left out from one it gave. A job system that answers 404 or 410 for the posting says it is
    gone: when `gone` is given, the words are added to it."""
    adapter = adapters.ADAPTERS.get(posting.ats)
    if employer is None or adapter is None:
        return None
    try:
        with tapping() as replies:  # the answers it was made from, for the raw copy (`hold.answer`)
            found = await adapter.detail(fetch, employer.board or {}, {
                "ext_id": posting.ext_id, "url": posting.url, "title": posting.title, "location": posting.location})
        more = found if isinstance(found, adapters.Detailed) else adapters.Detailed(found)
        more.replies = tuple(replies)
        return more
    except adapters.AdapterError as exc:
        if gone is not None and (said := polite.gone(posting.url or posting.ext_id, exc.status)):
            gone.append(said)
        return None
    except (Refused, httpx.HTTPError, ValueError):
        return None
    except Exception:  # one posting's unforeseen failure does not stop the others' pages
        log.warning("reading the description of %s (%s) failed unexpectedly", posting.title, posting.ext_id,
                    exc_info=True)
        return None


async def keep_detail(session: AsyncSession, posting: JobPosting, more: dict, now: datetime | None = None) -> bool:
    """Put what the page gave on the posting (the fields the index keeps as they last read, the description and the
    true date) and keep the raw copy of the answer with the cleansed copy that follows, so the copy holds the text as
    soon as the posting does. An answer that came with replies keeps them (`hold.answer`); one that came with
    none and carries a text (a site recipe's detail, an employer's own page, which the app read as plain data) keeps the
    text as a `code:text` row. True when it gave the description."""
    got = bool(more.get("text"))
    posting.posted = _date(more.get("posted")) or posting.posted
    for field in FIELDS:
        if more.get(field) is not None:
            setattr(posting, field, more[field])
    now = now or datetime.now(UTC)
    # the copy holds the answer's own text, which is what the posting now holds (the callers dated the read)
    put = hold.Put(more["text"], hold.KEEP) if got else None
    if getattr(more, "replies", None) or getattr(more, "raw", None) is not None:
        await hold.hold(session, posting, hold.answer(more), put=put, copy_text=more.get("text"), now=now)
    elif got:
        await hold.hold(session, posting, hold.plain("code:text"), put=put, copy_text=more["text"], now=now)
    return got


async def fill_details(session: AsyncSession, fetch: Fetcher, postings: list[JobPosting],
                       now: datetime | None = None) -> int:
    """Read the description and true date for postings whose list entry lacked them (a job system's list that gives
    neither). Returns how many were filled. Up to `READ_AT_ONCE` pages are read at once and the fetcher keeps each host
    to one request at a time at its pace (one request a second to it) and each job system to `polite.PLATFORM_AT_ONCE` a
    pace, so a site is no more pressed than reading them one by one, and many employers' postings take about the time of
    the slowest host's.
    A posting whose detail can't be read (a refusal, a bot check, an error) is left as it was, marked as tried
    (`detail_at`) so the night's list passes it over for `DETAIL_RETRY_DAYS`. A posting no read was begun for (no reader
    for its system or employer, or the time ran out first) is not marked: nothing was tried, so
    nothing waits for a retry. The pages are read with no transaction open (`db.release_if_free`: what the caller did
    so far is saved first) and what they gave is kept in a fresh one; `fill_role_details` holds none at all."""
    now = now or datetime.now(UTC)
    plan = await plan_details(session, postings)
    if plan.todo:
        await db.release_if_free(session)
    return await keep_details(session, plan.todo, await read_details(fetch, plan), now)


@dataclass
class DetailPlan:
    """What reading the details of some postings needs, read from the database first: the postings that lack text, and
    their employers and live site recipes. The read itself (`read_details`) touches no session."""

    todo: list[JobPosting]
    employers: dict[uuid.UUID, Employer]
    recipes: dict[uuid.UUID, Any]


async def plan_details(session: AsyncSession, postings: list[JobPosting]) -> DetailPlan:
    todo = [p for p in postings if whole.thin(p.text)]
    return DetailPlan(todo, await _employers_of(session, todo), await _detail_recipes(session, todo))


async def read_details(fetch: Fetcher, plan: DetailPlan) -> list[object]:
    """Each planned posting's page (or `NOT_BEGUN`), in `plan.todo`'s order, with no database work. The page of a
    posting on an employer's own site gives its text only when it names the posting (`read_page` with its title): the
    text goes on to the posting's role (`posting_text._plan`), so it must be the posting's own page."""
    gate = asyncio.Semaphore(READ_AT_ONCE)

    async def read(posting: JobPosting) -> object:
        employer = plan.employers.get(posting.employer_id)
        recipe = plan.recipes.get(posting.employer_id) if posting.ats == ats.OWN_SITE else None
        if recipe is not None:  # the site's live recipe gives details: its request, not the page
            async with gate:
                if timeup(fetch):
                    return NOT_BEGUN
                return await _read_by_recipe(fetch, recipe, posting)
        own_page = posting.ats == ats.OWN_SITE and bool(posting.url)
        if not own_page and (employer is None or adapters.ADAPTERS.get(posting.ats) is None):
            return NOT_BEGUN
        async with gate:
            if timeup(fetch):
                return NOT_BEGUN
            if not own_page:
                return await read_detail(fetch, employer, posting)
            # an employer's own page, as anyone reads it: its text is the posting's only when it names the posting
            reading = await read_page(fetch, (posting.url or "").strip(), posting.title or None)
            return {"text": reading.text, **{k: reading.facts[k] for k in READ_FACTS if reading.facts.get(k)}
                    } if reading.text else None

    return list(await asyncio.gather(*(read(p) for p in plan.todo)))


async def _lock_to_write(session: AsyncSession, written: list[JobPosting]) -> None:
    """Lock these postings (`FOR NO KEY UPDATE`, in id order, waiting for a row another transaction holds) before the
    first is written, until the caller's transaction ends. A posting's text is written one posting at a time (each
    keeps its raw copy, whose query sends the posting's `UPDATE` before it), and the writers (`keep_details`, for the
    night's descriptions and the pool's, and `read_places`, for the filter) list the postings in orders of their own:
    two over the same postings held one each and asked for the other's ("deadlock detected" on `UPDATE js_postings SET
    text, detail_at`). Taken here in one order, the second waits for the first. `as_read.lock` with `wait`: the caller
    holds no other posting by now (its pages were read with no transaction open, `db.release_if_free`, or its
    transaction is new). `no_key`: the lock the `UPDATE` of the text would take itself, so the two writers still wait
    for each other, and a row that names a posting (a browser item handed out, a raw copy kept) is not held up for the
    writer's whole transaction, as it would be by `FOR UPDATE`."""
    await as_read.lock(session, JobPosting, written, refresh=False)


async def keep_details(session: AsyncSession, postings: list[JobPosting], pages: list[object],
                       now: datetime) -> int:
    """Keep what each page gave on its posting (`postings` and `pages` in step). Returns how many were filled. The
    postings it writes are locked first, in id order (`_lock_to_write`)."""
    filled = 0
    tried: set[str] = set()
    pairs = list(zip(postings, pages, strict=True))
    for start in range(0, len(pairs), DETAILS_WRITE_BATCH):
        if start:  # the turn is let go between groups: a second writer waits for one group, not for all
            await db.release_if_free(session)
        filled += await _write_group(session, pairs[start:start + DETAILS_WRITE_BATCH], now, tried)
    _retry_when_due(session, tried)
    return filled


async def _write_group(session: AsyncSession, pairs: list[tuple[JobPosting, object]], now: datetime,
                       tried: set[str]) -> int:
    """Write what the pages of one group of postings gave, on the postings already in hand: they are locked first
    (`_lock_to_write`), written in order and flushed. The owners of those it wrote are added to `tried`. Returns how
    many were filled."""
    filled = 0
    await _lock_to_write(session, [p for p, more in pairs if more is not NOT_BEGUN])
    for posting, more in pairs:
        if more is NOT_BEGUN:
            continue
        posting.detail_at = now
        tried.add(posting.owner)
        if isinstance(more, dict):
            filled += await keep_detail(session, posting, more, now)
    await session.flush()
    return filled


def _retry_when_due(session: AsyncSession, owners: set[str]) -> None:
    """A posting whose text was tried (`detail_at`) is left alone for `DETAIL_RETRY_DAYS`; no write happens when that
    time is up, so a to-do is posted for it now, held back until then. Its own
    key, so it is not mistaken for the to-do the hook posts at once for the same write."""
    for owner in owners:
        queue.post(session, owner, queue.ROLE_DETAILS, ["retry"], after=timedelta(days=DETAIL_RETRY_DAYS),
                   cause="core:details.retry")


@dataclass(frozen=True)
class DetailsRead:
    """What one read of the descriptions roles lack did: how many postings were `filled` with text, and how many were
    `settled`: every posting the read left no longer owed, the ones a read was begun for (marked tried, whatever the
    page gave) and the ones that got their text meanwhile. A posting no read was begun for (no reader for its system, or
    the time ran out) is neither: it is still owed, and a look that settled nothing has nothing to repeat."""

    filled: int = 0
    settled: int = 0


async def fill_role_details(owner: str, fetch: Fetcher, limit: int, now: datetime | None = None) -> int:
    """`read_role_details`, as the count of postings it filled (what the pipeline's turn and the pull ask)."""
    return (await read_role_details(owner, fetch, limit, now)).filled


async def read_role_details(owner: str, fetch: Fetcher, limit: int, now: datetime | None = None) -> DetailsRead:
    """Read the description of up to `limit` postings of roles still in play that have none (or a snippet: under
    `cleansed.MIN_TEXT`), by code from their job system, the newest first. A role waits in Cleansed Data, unranked,
    until this gives it text; the raw and cleansed copies the read keeps are what it is ranked on. Returns how many were
    filled, and how many left the owed set (`DetailsRead`). Code only, so it runs at any hour (`pool.queue_details`).

    Its own transactions (`db.read_fetch_write`): the postings are chosen in one, the pages are read with none open
    (a turn's reads took up to 40 seconds inside one, holding its connection), and what they gave is kept in another,
    on the postings as they are then (one that got its text meanwhile is left as it is)."""
    now =now or datetime.now(UTC)
    newest = func.coalesce(JobPosting.posted, func.date(JobPosting.first_seen))

    async def choose(session: AsyncSession) -> DetailPlan:
        rows = list((await session.scalars(
            select(JobPosting).join(Opportunity, Opportunity.id == JobPosting.opportunity_id)
            .where(*details_where(owner))
            .order_by(newest.desc(), JobPosting.first_seen.desc(), JobPosting.id).limit(limit))).all())
        return await plan_details(session, rows)

    async def read(plan: DetailPlan) -> tuple[DetailPlan, list[object]]:
        return plan, (await read_details(fetch, plan) if plan.todo else [])

    async def keep(session: AsyncSession, read_out: tuple[DetailPlan, list[object]]) -> DetailsRead:
        plan, pages = read_out
        filled, still_owed = 0, 0
        for start in range(0, len(plan.todo), DETAILS_WRITE_BATCH):
            if start:  # the turn is let go between batches: a second writer waits for one batch, not for all
                await db.release(session)
            batch = list(zip(plan.todo, pages, strict=True))[start:start + DETAILS_WRITE_BATCH]
            now_rows = {p.id: p for p in (await session.scalars(select(JobPosting).where(
                JobPosting.id.in_([p.id for p, _ in batch] or [None])))).all()}  # looked at again, batch by batch
            pairs = [(now_rows[p.id], page) for p, page in batch
                     if p.id in now_rows and whole.thin(now_rows[p.id].text)]
            filled += await keep_details(session, [p for p, _ in pairs], [page for _, page in pairs], now)
            still_owed += sum(1 for _, page in pairs if page is NOT_BEGUN)
        return DetailsRead(filled, len(plan.todo) - still_owed)

    return await db.read_fetch_write(choose, read, keep)


def wants_places(posting: JobPosting) -> bool:
    """Whether the posting's place is unknown (its list gave none, or only a count of places: `says_no_place`) and its
    job system's own page names the places (`Adapter.places`), so reading that page can settle it."""
    return bool(getattr(adapters.ADAPTERS.get(posting.ats), "places", False)) and says_no_place(posting.location)


async def read_places(session: AsyncSession, fetch: Fetcher, rows: list[JobPosting],
                      now: datetime | None = None) -> None:
    """Read the own page of each of these postings (those for which `wants_places`) to learn where the work is, and keep
    what the page gives: the places and, in the same answer, the description and the true date (so the night's
    descriptions need not read it again). A posting whose page can't be read is left as it was, marked as tried like a
    description (`detail_at`), and its place stays unknown. Up to `READ_AT_ONCE` pages are read at once; the fetcher
    keeps each host to one request at a time at its pace (one request a second to it) and each job system to
    `polite.PLATFORM_AT_ONCE` a pace, so a site is no more pressed than by reading them one by one. Once the
    night's time has run out (`polite.timeup`, docs/PRD.md 28.17) no page is begun: a posting not read is left as it
    was, not marked as tried, and its place stays unknown. The postings it writes are locked first, in id order
    (`_lock_to_write`)."""
    now = now or datetime.now(UTC)
    employers = await _employers_of(session, rows)
    gate = asyncio.Semaphore(READ_AT_ONCE)

    async def read(posting: JobPosting) -> object:
        async with gate:
            if timeup(fetch):  # the night's read ran out of time before this page was begun
                return NOT_BEGUN
            return await read_detail(fetch, employers.get(posting.employer_id), posting)

    if rows:
        await db.release_if_free(session)  # the pages are read with no transaction open; what they gave is kept after
    pages = await asyncio.gather(*(read(posting) for posting in rows))
    tried: set[str] = set()
    pairs = list(zip(rows, pages, strict=True))
    for start in range(0, len(pairs), DETAILS_WRITE_BATCH):
        if start:  # the turn is let go between groups, as in `keep_details`
            await db.release_if_free(session)
        await _write_group(session, pairs[start:start + DETAILS_WRITE_BATCH], now, tried)
    _retry_when_due(session, tried)


def pages_left(night: int, spent: int = 0) -> int | None:
    """How many pages of the night's number (`details_per_night`) are left after `spent`: none under 0, and no limit
    (None) when the number is negative."""
    return None if night < 0 else max(night - spent, 0)
