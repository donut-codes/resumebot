"""Leases on the browser's items, and a session's run of the "Read in your browser" mission (see `browser/items.py`)."""

from collections import Counter
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.jobsearch.browser import pace as browser_pace
from app.jobsearch.browser import reader as br
from app.jobsearch.browser.pace import (
    BLOCKED,
    DONE,
    EASY_APPLY,
    FAILED,
    FINISHED,
    FOUND,
    GONE,
    LEASE,
    LEASED,
    MAP,
    NO_RESULT,
    NOT_FOUND,
    PATHFINDER,
    READ,
    RETRY_AFTER,
    RUN_ITEMS,
    SCAN,
    SOURCE,
    STUCK,
    TEXT,
    TIMED_OUT,
    TRIES,
    UNREADABLE,
    WAITING,
)
from app.jobsearch.missions import core as missions
from app.jobsearch.missions import record
from app.jobsearch.models import BrowserAttempt, BrowserItem, MissionRun, Opportunity
from app.jobsearch.work import queue
from app.timeutil import aware


async def held_count(session: AsyncSession, owner: str) -> int:
    """How many items workers hold now, read only: every leased item. One whose lease ran out is counted until the
    engine gives it back (`expire`: the minute tick, `work/housekeeping.py` `leases`). A reader's session can call it:
    the runner's two GETs do (`routes/runner.py`: `browser_drain_state`, and `browser_busy` through `open_work`)."""
    return int(await session.scalar(select(func.count(BrowserItem.id)).where(
        BrowserItem.owner == owner, BrowserItem.state == LEASED)) or 0)


async def leased_count(session: AsyncSession, owner: str, now: datetime | None = None) -> int:
    """How many items workers hold now, once the leases that ran out are given back: what a drain's start waits for.
    It writes (`expire`), so only a writer's session calls it (`POST /agent/browser/drain`)."""
    await expire(session, owner, now or datetime.now(UTC))
    return await held_count(session, owner)


def _log(session: AsyncSession, row: BrowserItem, who: str | None, outcome: str, note: str | None,
         now: datetime) -> None:
    session.add(BrowserAttempt(owner=row.owner, item_id=row.id, kind=row.kind, session_id=who, at=now,
                               outcome=outcome, note=(note or None) and note[:2000]))


def _give_back(session: AsyncSession, row: BrowserItem, now: datetime, why: str) -> None:
    """An item its worker no longer holds goes back to the queue, logged as no result. The refresh leaves a leased item
    to its worker, so one that stopped being called for meanwhile (its role got its text, was sourced or left play)
    is dropped only by a look at the queue once it is waiting again: that is asked for here, since the return to
    waiting posts only the kind of session that may take it (the hook)."""
    _log(session, row, row.leased_by, NO_RESULT, why, now)
    row.state, row.leased_by, row.lease_until, row.due_by = WAITING, None, None, None
    row.next_at = max(aware(row.next_at) or now, now)
    queue.post(session, row.owner, queue.BROWSER_RECONCILE, [""], cause="leases.give_back")


#: What a session's item that ran past its time box is handed back as, by kind.
TIMEOUT_RESULT = {SOURCE: NOT_FOUND, READ: UNREADABLE, SCAN: UNREADABLE}


async def _time_out(session: AsyncSession, row: BrowserItem, now: datetime) -> None:
    """A session's item ran past its time box: it is handed back for the session as timed out. A source or read item
    counts a try and is tried again after `RETRY_AFTER` (after `TRIES` the role keeps the reason), as any `not_found`
    or `unreadable` is; a scan ends as it stands (what it handed in is kept) and comes back with the next scan. The
    session's run line says so."""
    sid, result = row.leased_by, TIMEOUT_RESULT.get(row.kind, UNREADABLE)
    if row.kind == SCAN:
        state = DONE
    else:
        state = _retry(row, now)
        if state == STUCK and row.role_id is not None:
            role = await session.get(Opportunity, row.role_id)
            if role is not None:
                role.browser_note = TIMED_OUT
    _log(session, row, sid, result, TIMED_OUT, now)
    row.state, row.leased_by, row.lease_until, row.due_by = state, None, None, None
    row.last_result, row.last_note = result, TIMED_OUT
    if state == DONE:
        row.done_at = now
    entry = await _run_of(session, row.owner, sid) if sid else None
    if entry is not None:
        started = dict(entry.started or {})
        started["items"] = [*(started.get("items") or []), {
            "kind": row.kind, "result": result, "domain": row.domain, "blocked_at": None,
            "what": row.link or row.domain, "note": TIMED_OUT}][-RUN_ITEMS:]
        entry.started = started
        entry.summary = summary_words(started)


async def expire(session: AsyncSession, owner: str, now: datetime) -> int:
    """Return the items whose lease ran out (nothing was heard from their session for `LEASE`), and hand back those a
    live session held past their time box (`TIMEBOX`, `_time_out`). Returns how many."""
    rows = (await session.scalars(select(BrowserItem).where(
        BrowserItem.owner == owner, BrowserItem.state == LEASED, BrowserItem.lease_until < now))).all()
    for row in rows:
        _give_back(session, row, now, "the lease ran out: nothing was heard from the session for 10 minutes")
    late = (await session.scalars(select(BrowserItem).where(
        BrowserItem.owner == owner, BrowserItem.state == LEASED, BrowserItem.due_by < now))).all()
    for row in late:
        await _time_out(session, row, now)
    if rows or late:
        await session.flush()
    return len(rows) + len(late)


async def _held_item(session: AsyncSession, owner: str, sid: str) -> BrowserItem | None:
    return await session.scalar(select(BrowserItem).where(
        BrowserItem.owner == owner, BrowserItem.state == LEASED, BrowserItem.leased_by == sid).limit(1))


async def holds_item(session: AsyncSession, owner: str, sid: str) -> bool:
    """Whether this session holds an item now (an app stop lets it finish that item first)."""
    return await _held_item(session, owner, sid) is not None


async def _run_of(session: AsyncSession, owner: str, sid: str) -> MissionRun | None:
    for entry in await br.open_runs(session, owner):
        if (entry.started or {}).get("session_id") == sid:
            return entry
    return None


async def touch(session: AsyncSession, owner: str, now: datetime | None = None) -> tuple[BrowserItem | None,
                                                                                          MissionRun | None]:
    """A call of the session: its lease and its run's last call are renewed. Returns the item it holds and its run."""
    now = now or datetime.now(UTC)
    sid = browser_pace._asker()
    row = await _held_item(session, owner, sid)
    if row is not None:
        row.lease_until = now + LEASE
    entry = await _run_of(session, owner, sid)
    if entry is not None:
        entry.started = {**(entry.started or {}), "last_call": now.isoformat()}
    await session.flush()
    return row, entry


async def release_session(session: AsyncSession, owner: str, sid: str, why: str,
                          now: datetime | None = None) -> int:
    """A session ended (or was ended): what it held goes back at once, and its run is closed. Returns how many items."""
    now = now or datetime.now(UTC)
    rows = (await session.scalars(select(BrowserItem).where(
        BrowserItem.owner == owner, BrowserItem.state == LEASED, BrowserItem.leased_by == sid))).all()
    for row in rows:
        _give_back(session, row, now, why)
    entry = await _run_of(session, owner, sid)
    if entry is not None:
        end_run(entry, now, "" if not rows else f"The session ended holding an item ({why}).")
    await session.flush()
    return len(rows)


async def session_run(session: AsyncSession, owner: str, sid: str, now: datetime) -> MissionRun:
    """The session's run of the "Read in your browser" mission: open from its first item to its end."""
    entry = await _run_of(session, owner, sid)
    if entry is not None:
        return entry
    learning = browser_pace.worker_of(sid) == PATHFINDER
    return await missions.open_run(
        session, owner, record.BROWSER, started_by=br.BY, at=now,
        summary="Mapping sites in your browser." if learning else "Working in your browser.",
        started={"browser": True, "mode": br.LEARN if learning else br.READ, "session_id": sid,
                 "last_call": now.isoformat(), "handed": 0, "new": 0, "by_site": {}, "portals": {}, "by_portal": {},
                 "learned": {}, "items": [], "runbooks_sent": []})


def summary_words(started: dict) -> str:
    """What a run did, item by item, in a few sentences for the owner: "Sourced 3 roles (1 Easy Apply). Read the whole
    text of 2 postings. Couldn't find the original posting of 1 role: no posting on the employer's site. Scanned
    Example Jobs: handed in 23 postings, 5 new. board.example.test blocked the browser (showed a CAPTCHA); it rests
    for an hour." Never "no postings" when roles were sourced."""
    items = started.get("items") or []
    count = Counter((i.get("kind"), i.get("result")) for i in items)

    def notes(kind: str, result: str) -> str:
        whys = list(dict.fromkeys((i.get("note") or "").rstrip(". ") for i in items
                                  if i.get("kind") == kind and i.get("result") == result and i.get("note")))
        return (": " + "; ".join(whys[:3])) if whys else ""

    words = []
    sourced = count[(SOURCE, FOUND)] + count[(SOURCE, EASY_APPLY)]
    if sourced:
        easy = count[(SOURCE, EASY_APPLY)]
        words.append(f"Sourced {browser_pace._plural(sourced, 'role')}" + (f" ({easy} Easy Apply)" if easy else "")
                     + ".")
    if count[(READ, TEXT)]:
        words.append(f"Read the whole text of {browser_pace._plural(count[(READ, TEXT)], 'posting')}.")
    gone = count[(SOURCE, GONE)] + count[(READ, GONE)]
    if gone:
        words.append(f"{browser_pace._plural(gone, 'posting')} had been taken down; "
                     f"{'its role' if gone == 1 else 'their roles'} ended.")
    if count[(SOURCE, NOT_FOUND)]:
        words.append(f"Couldn't find the original posting of {browser_pace._plural(count[(SOURCE, NOT_FOUND)], 'role')}"
                     f"{notes(SOURCE, NOT_FOUND)}.")
    if count[(READ, UNREADABLE)]:
        words.append(f"Couldn't read {browser_pace._plural(count[(READ, UNREADABLE)], 'posting')} in full"
                     f"{notes(READ, UNREADABLE)}.")
    for i in items:
        what, why = i.get("what") or i.get("domain"), (i.get("note") or "").rstrip(". ")
        if i.get("kind") == SCAN and i.get("result") == FINISHED:
            words.append(f"Scanned {what}.")
        elif i.get("kind") == SCAN and i.get("result") == FAILED:
            words.append(f"{what}'s runbook didn't fit: {why}; it will be mapped again.")
        elif i.get("kind") == MAP and i.get("result") == FINISHED:
            words.append(f"Mapped {what}.")
        elif i.get("kind") == MAP and i.get("result") == FAILED:
            words.append(f"Couldn't map {what}: {why}.")
    handed, new = int(started.get("handed") or 0), int(started.get("new") or 0)
    if handed:
        words.append(f"Handed in {browser_pace._plural(handed, 'posting')}, {new} new.")
    blocked: dict[str, str] = {}
    for i in items:
        if i.get("result") == BLOCKED:
            blocked.setdefault(i.get("blocked_at") or i.get("domain") or "a site", (i.get("note") or "").rstrip(". "))
    for where_, why in blocked.items():
        words.append(f"{where_} blocked the browser ({why}); it rests a while." if why else
                     f"{where_} blocked the browser; it rests a while.")
    return " ".join(words) or "Handed back nothing."


def end_run(entry: MissionRun, now: datetime, note: str = "") -> None:
    """Close a session's run with its line. Done when it handed anything back, or nothing went wrong; failed when it
    handed nothing back and ended with a `note` (it went quiet, was paused, the app restarted)."""
    if entry.outcome != "running":
        return
    started = dict(entry.started or {})
    did = bool(started.get("items")) or int(started.get("handed") or 0) > 0
    entry.outcome = "done" if did or not note else "failed"
    entry.ended_at = now
    entry.summary = (summary_words(started) + (f" {note}" if note else "")).strip()
    entry.started = started


#: Why a session's items go back when its run is closed for silence.
SILENT = "the session stopped without a word"


def _silent(entry: MissionRun, now: datetime) -> bool:
    """Whether a session's run is still open and no call has touched it for three leases."""
    started = entry.started or {}
    if entry.outcome != "running" or not started.get("browser"):
        return False
    last = aware(datetime.fromisoformat(started["last_call"])) if started.get("last_call") else aware(entry.started_at)
    return last is not None and now - last >= 3 * LEASE


async def settle_run(session: AsyncSession, entry: MissionRun, now: datetime | None = None) -> bool:
    """A run no call has touched for three leases ended without the session saying so (the computer slept, the session
    was cut off): what it held goes back and it is closed with what it had. Returns whether it closed."""
    now = now or datetime.now(UTC)
    if not _silent(entry, now):
        return False
    sid = (entry.started or {}).get("session_id")
    if sid:
        await release_session(session, entry.owner, sid, SILENT, now)
    end_run(entry, now, "The session stopped without saying it was done.")
    return True


async def run_as_read(session: AsyncSession, entry: MissionRun, now: datetime | None = None) -> bool:
    """What `settle_run` would leave on the run, for a read of the missions: `entry` is a copy that is in no session
    (`missions.shown`), closed here in the same words. Nothing the session held is given back and no row is written;
    `settle_run` does that in the schedule pass. Returns whether it closed."""
    now = now or datetime.now(UTC)
    if not _silent(entry, now):
        return False
    sid = (entry.started or {}).get("session_id")
    if sid:
        held = int(await session.scalar(select(func.count(BrowserItem.id)).where(
            BrowserItem.owner == entry.owner, BrowserItem.state == LEASED, BrowserItem.leased_by == sid)) or 0)
        end_run(entry, now, f"The session ended holding an item ({SILENT})." if held else "")
    end_run(entry, now, "The session stopped without saying it was done.")
    return True


async def _timed_out_last(session: AsyncSession, owner: str, sid: str) -> bool:
    """Whether the last thing that came of this session's work was an item timed out for it."""
    note = await session.scalar(select(BrowserAttempt.note).where(
        BrowserAttempt.owner == owner, BrowserAttempt.session_id == sid).order_by(BrowserAttempt.at.desc()).limit(1))
    return note == TIMED_OUT


def _retry(row: BrowserItem, now: datetime) -> str:
    """A try that found nothing: the item waits `RETRY_AFTER`, or after `TRIES` tries it is stuck (the role keeps the
    reason). The app's code tries it first again when it comes back. Returns the item's state."""
    row.tries = (row.tries or 0) + 1
    row.code_at = None
    if row.tries >= TRIES and row.kind != MAP:
        row.state = STUCK
    else:
        row.state, row.next_at = WAITING, now + RETRY_AFTER
    return row.state
