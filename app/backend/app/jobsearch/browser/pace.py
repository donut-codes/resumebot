"""The browser queue's constants, its in-process state and the pace per domain (see `browser/items.py` for the whole
account of the browser's work)."""

import asyncio
import logging
import random
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import db
from app.agents import roster
from app.errors import Busy
from app.jobsearch import as_read
from app.jobsearch.browser import reader as br
from app.jobsearch.fetch import host_stops
from app.jobsearch.models import BrowserDomain, BrowserItem
from app.jobsearch.states import BrowserKind, BrowserState
from app.jobsearch.work.door import caller
from app.settings import settings
from app.sites import registry
from app.timeutil import aware

log = logging.getLogger(__name__)

SOURCE, READ, SCAN, MAP, RECORD, LISTING = (k.value for k in BrowserKind)
WAITING, LEASED, DONE, STUCK, DROPPED = (s.value for s in BrowserState)
#: The two kinds of worker (the pool's agent kinds) and the items each takes, in the order it takes them.
WORKER, PATHFINDER = roster.BROWSER_READER, roster.PATHFINDER
WORK = {WORKER: (SOURCE, READ, SCAN), PATHFINDER: (MAP,)}
#: A session works one track: role work (`source`, `read`), site reading (`scan`) or mapping. Site reading never
#: crowds out role work in one session.
TRACK = {SOURCE: "roles", READ: "roles", SCAN: "sites", MAP: "map", RECORD: "record", LISTING: "roles"}
RANK = {SOURCE: 0, READ: 1, SCAN: 2, MAP: 0, RECORD: 0, LISTING: 0}

#: What a worker may hand back for each kind of item.
FOUND, EASY_APPLY, NOT_FOUND, GONE, BLOCKED = "found", "easy_apply", "not_found", "gone", "blocked"
TEXT, UNREADABLE = "text", "unreadable"
FINISHED, FAILED = "done", "failed"
RESULTS = {SOURCE: (FOUND, EASY_APPLY, NOT_FOUND, GONE, BLOCKED), READ: (TEXT, GONE, UNREADABLE, BLOCKED),
           SCAN: (FINISHED, BLOCKED, FAILED), MAP: (FINISHED, BLOCKED, FAILED),
           RECORD: (FINISHED, BLOCKED, FAILED), LISTING: (TEXT, GONE, UNREADABLE, BLOCKED)}
#: What an attempt says when nothing was handed back (the lease ran out, the session ended or was paused).
NO_RESULT = "no_result"

#: The `priority` of a source item for a role in Followup needed (2026-11-03): after every other item, the roles on deck
#: (1), the starred (0), the reads and the scans, so a backlog of hundreds never holds them back (`_order`).
BACKLOG = 2

#: How long a lease lasts without a call from the session that holds it.
LEASE = timedelta(minutes=10)

#: How long a session may work one item, by kind (the owner, 2026-10-16: a box per item; the owner, 2026-10-19: and a
#: cap of items per session, `MAX_ITEMS_PER_SESSION`). An
#: item that runs past its box is handed back for its worker as timed out (`not_found` for a source, `unreadable` for a
#: read, tried again like any other), and a scan ends as it stands. A map has none: it takes as long as a site needs.
SOURCE_TIMEBOX, READ_TIMEBOX, SCAN_TIMEBOX = timedelta(minutes=5), timedelta(minutes=5), timedelta(minutes=15)
TIMEBOX = {SOURCE: SOURCE_TIMEBOX, READ: READ_TIMEBOX, SCAN: SCAN_TIMEBOX}
TIMED_OUT = "timed out"


def follow_domains() -> tuple[str, ...]:
    """The domains of the boards whose roles must be followed to the employer's own site (a runbook's
    `follow_to_employer`), as registrable domains. The queue holds their page loads for the one reader that keeps its
    own clock there (`takeable`'s hold)."""
    return tuple(dict.fromkeys(host_stops.domain_of(h) for r in registry.current().runbooks
                               if r.kind == "board" and r.follow_to_employer for h in r.hosts))


def site_name(domain: str | None) -> str:
    """What the site on this domain is called, by its runbook; the domain itself when none names it."""
    runbook = registry.for_host(domain)
    return runbook.name if runbook else (domain or "the site")


def code_source_domains() -> tuple[str, ...]:
    """The boards whose `source` items the app's code tries first, over the app's browser (their Apply link's address):
    the followed boards, and the boards whose posting page the code reads as well (a runbook's
    `code_reads_postings`)."""
    return tuple(dict.fromkeys((*follow_domains(), *(host_stops.domain_of(h) for h in registry.code_read_hosts()))))


def reads_postings(domain: str | None) -> bool:
    """Whether the app's code reads the postings of this domain's board (a runbook's `code_reads_postings`)."""
    return bool(domain) and domain in {host_stops.domain_of(h) for h in registry.code_read_hosts()}


#: One page every 5 to 12 seconds on a domain (the owner, 2026-10-16), a different gap each time; a domain whose pages
#: are read at a slower pace (its runbook's `pace_seconds`) is held to that one (`page_gap`).
PAGE_GAP = (5.0, 12.0)
#: How long a domain rests after a rate-limit page (a refusal or bot check is a stop instead, `host_stops.py`: for good,
#: and for a site whose runbook says so until the owner's next off hours start, PRD 48.14 d): an
#: hour, then eight hours if it is limited again before a success.
COOLING = (timedelta(hours=1), timedelta(hours=8))
#: A `not_found`, `unreadable` or failed map waits this long before the next try (the owner, 2026-10-16), and after
#: `TRIES` such tries a role's item is stuck (built as recommended: three daily tries).
RETRY_AFTER, TRIES = timedelta(hours=24), 3

#: An item whose domain is free within this long is waited for (the worker is told to wait), not given up on.
WAIT_UP_TO = timedelta(seconds=15)

#: How long a note may be, and the most items a run's line keeps.
NOTE_MIN, NOTE_MAX, RUN_ITEMS = 3, 300, 500

#: The owners whose code-first worker runs (`browser_code.code_loop`): only then does a session wait for code to try an
#: item first. Without it (a test, an app started without its schedule) sessions take every item, as before.
_code_on: set[str] = set()


class BrowserWorkError(br.BrowserError):
    """The door refuses, in plain words."""


def gap() -> timedelta:
    """The wait before the next page on a domain: a different number of seconds each time, within `PAGE_GAP`."""
    return timedelta(seconds=random.uniform(*PAGE_GAP))


def page_gap(domain: str | None) -> timedelta:
    """The wait before the next page on `domain`: `gap()`, and never less than the pace a host of that domain is read
    at (its runbook's `pace_seconds`: a board's public pages, one every 30 seconds, PRD 48.4 and 48.13), whoever loads
    the page. The sessions, the digest and the app's own code all keep the domain's one pace."""
    every = registry.pace_for_domain(domain)
    return max(gap(), timedelta(seconds=every))


def worker_of(session_id: str | None) -> str:
    """The kind of worker a session is, by its id (the runner names it `<kind>-<hex>`)."""
    return PATHFINDER if (session_id or "").split("-", 1)[0] == PATHFINDER else WORKER


#: The id prefix of the app's own code-first workers (`browser/code.py`): no Claude session, no run of their own.
CODE = "code"


def is_code(session_id: str | None) -> bool:
    return (session_id or "").split("-", 1)[0] == CODE


def code_running(owner: str, on: bool) -> None:
    """The code-first worker started (or stopped) for this owner."""
    if on:
        _code_on.add(owner)
    else:
        _code_on.discard(owner)


#: What a waiting item's last note says while the install limits the browser to some domains and the item is for another
#: (`mark_held`); cleared when the scope is widened.
HELD_PREFIX = "held: browser limited to "


def in_scope(domain: str | None) -> bool:
    """Whether the install lets the browser work on this domain (`settings.browser_domains`; empty: every one)."""
    scope = settings.browser_scope()
    host = (domain or "").lower()
    return not scope or any(host == d or host.endswith("." + d) for d in scope)


def held_note() -> str:
    return HELD_PREFIX + ", ".join(settings.browser_scope())


async def mark_held(session: AsyncSession, owner: str) -> int:
    """Mark the waiting items the scope holds back (`held: browser limited to example.com`) and let go the ones it no
    longer does. They stay waiting, never dropped. Returns how many are held. The caller commits. Only the items whose
    note changes are locked (`as_read.lock`: waiting items only, one another transaction holds, a lease or a hand-back
    under way, is left out), so a pass that changes nothing writes nothing."""
    rows = (await session.execute(select(BrowserItem.id, BrowserItem.domain, BrowserItem.last_note).where(
        BrowserItem.owner == owner, BrowserItem.state == WAITING))).all()
    held = 0
    notes: dict[uuid.UUID, str | None] = {}
    for item_id, domain, note in rows:
        if not in_scope(domain):
            held += 1
            if note != held_note():
                notes[item_id] = held_note()
        elif (note or "").startswith(HELD_PREFIX):
            notes[item_id] = None
    locked = await as_read.lock(session, BrowserItem, list(notes), where=(BrowserItem.state == WAITING,))
    for row in locked.rows.values():
        row.last_note = notes[row.id]
    await session.flush()
    return held


def _asker() -> str:
    sid = caller.get().session_id
    if not sid:
        raise BrowserWorkError("this call names no session (the runner sends X-Session-Id): stop")
    return sid


def _ago(age: timedelta) -> str:
    minutes = max(int(age.total_seconds() // 60), 0)
    if minutes < 60:
        return f"{max(minutes, 1)} minute{'s' if minutes != 1 else ''}"
    if minutes < 48 * 60:
        return f"{minutes // 60} hour{'s' if minutes // 60 != 1 else ''}"
    return f"{minutes // 1440} days"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


#: The in-app turn for the mission's row, one per owner: (the loop it belongs to, its lock). See `lock_mission`.
_mission_turns: dict[str, tuple[asyncio.AbstractEventLoop, asyncio.Lock]] = {}
#: Set on a session whose transaction holds the owner's turn (the session's `info`).
_TURN_HELD = "browser_mission_turn"


def _mission_turn(owner: str) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    held = _mission_turns.get(owner)
    if held is None or held[0] is not loop:  # a new loop (a test's) begins with a free turn
        held = _mission_turns[owner] = (loop, asyncio.Lock())
    return held[1]


async def lock_mission(session: AsyncSession, owner: str) -> None:
    """Take the owner's turn on the "Read in your browser" queue: the first thing a call does that hands out items or
    inserts a session's run, so every call takes it in the same place. There is no row to lock in the database (it has
    one writer): the turn is the app's own, an in-process lock per owner (`_mission_turn`).

    Waited for `lock_timeout` at most: past that it raises `Busy`, and the call says to wait (a hand-out), takes nothing
    (the code-first worker) or is refused to be made again (a hand-back), so no call queues behind a long one.

    The wait is made in the app before the call has a pooled connection: waiting inside the database held one
    connection for each waiting call, and about ten of them (the sessions and the code-first workers) were all the pool
    had, so the night's read could not save a board. The turn is let go when the session's transaction ends. One call
    per transaction: a second call in the same transaction already holds it."""
    why = "the browser's queue is busy for a moment; nothing was changed"
    if session.info.get(_TURN_HELD):
        return
    turn = _mission_turn(owner)
    wait = (db.BACKGROUND_LOCK_TIMEOUT_MS if db.in_background() else db.LOCK_TIMEOUT_MS) / 1000
    try:
        async with asyncio.timeout(wait):
            await turn.acquire()
    except TimeoutError:
        raise Busy(why) from None
    session.info[_TURN_HELD] = True

    def let_go(_session, transaction) -> None:
        # Left listening once it has let go (removing it during the event's own dispatch fails); it does nothing then.
        if transaction.parent is None and session.info.pop(_TURN_HELD, None):
            turn.release()

    event.listen(session.sync_session, "after_transaction_end", let_go)


async def _pace(session: AsyncSession, owner: str, domain: str) -> BrowserDomain:
    row = await session.scalar(select(BrowserDomain).where(
        BrowserDomain.owner == owner, BrowserDomain.domain == domain))
    if row is None:
        row = BrowserDomain(owner=owner, domain=domain, blocks=0)
        session.add(row)
        await session.flush()
    return row


#: How long a domain's "its pages draw themselves" stands before the app's fetch looks again.
SHELL_FOR = timedelta(days=7)


async def shell_domain(session: AsyncSession, owner: str, domain: str, now: datetime) -> bool:
    """Whether the app's fetch found this domain's posting pages to hold no text in their HTML (they draw themselves
    with script) within `SHELL_FOR`: its reads then go straight to the browser, with no fetch first."""
    row = await session.scalar(select(BrowserDomain).where(
        BrowserDomain.owner == owner, BrowserDomain.domain == domain))
    seen = aware(row.page_read_at) if row is not None else None
    return row is not None and row.page_read == "shell" and seen is not None and now - seen < SHELL_FOR


async def note_page_read(session: AsyncSession, owner: str, domain: str, how: str, now: datetime) -> None:
    """Record how the app's fetch of a posting page on this domain went: `text`, `shell` or `refused`. The caller
    commits."""
    pace = await _pace(session, owner, domain)
    pace.page_read, pace.page_read_at = how, now


async def whole(session: AsyncSession, row: Any) -> BrowserItem:
    """The item with every column loaded, from the id of a row `takeable.ask` returned: the caller leases it or
    offers it, and reads and writes the rest of it. Read again in place if the session holds it already."""
    got = await session.get(BrowserItem, row.id, populate_existing=True)
    assert got is not None, f"item {row.id} is gone"
    return got
