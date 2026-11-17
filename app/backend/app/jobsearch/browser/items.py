"""The browser's work, as a queue of small items the app owns (the owner 2026-10-16).

Code handles everything it can (job-system feeds, employer pages, text from job systems); the browser handles the
rest. Its work is five kinds of item (`BrowserItem`), made from the roles and sites as they stand (`refresh`):

- `source`: find a board role's original posting (the employer's page or job system, or the listing for an Easy Apply);
- `read`: get a posting's whole text from its known address (also Go's check that a Ready to apply role's posting is
  still open, where code could not read it: `go_checks.py`, ahead of the rest, for `go_checks.BROWSER_LIFE`);
- `scan`: read one site's new listings, following its runbook, every `SCAN_EVERY`;
- `map`: the Pathfinder writes or rewrites a site's runbook;
- `listing`: the app's own code alone opens a followed board's role's posting page in its Chrome and keeps the text for
  the stage, the gates and the rank, and the work-mode chip (the board's `capture.keep`, docs/PRD.md 48.14): never the
  role's description, and no session takes one, so each outcome is a result the code hands back itself
  (`browser/code.py` `listing_by_code`, `handback._listing`): `text`, `gone`, `unreadable` (no whole text, no raw copy:
  three daily tries, then the role keeps the reason on its card and stays in Followup needed) or `blocked` (a check or a
  refusal stops the board; a slow-down or a sign-in page rests it, with no try used).

A worker never receives a plan. A session of the pool asks for the next item (`next_item`, the door's
`browser.next_item`), does it in its own tab, hands back what came of it (`hand_back`) and asks again, one item at a
time, until there is nothing it can take now; then it ends. The browser worker (`browser_reader`) takes `source`,
`read` and `scan` items, in that order, and a session that started on role work never takes a scan (nor the other way
round); the Pathfinder takes `map` items.

Leases, not claims. Taking an item leases it for `LEASE`; every call of the session that holds it renews the lease
(`touch`: a hand-in, a `browser.heartbeat`, the hand-back). A lease that runs out returns the item, logged as
`no_result` (`expire`, looked at whenever the queue is read: no sweep, no test of whether the session lives). Pausing
a session ending returns that session's items at once (`release_session`). Pausing a kind, the agents or the
mission, or peak hours, lets a worker finish the item it holds; the door then tells it to stop.

Time boxes, not a session cap (the owner, 2026-10-16). A session's lease of an item carries `due_by`: `TIMEBOX` by
kind (source and read 5 minutes, scan 15; a map none). An item a live session holds past it is handed back for it as
timed out (`_time_out`, in `expire`): `not_found` or `unreadable`, retried after `RETRY_AFTER` like any, a scan ended as
it stands. A result the session hands back before anything timed the item out is still taken.

Code first (`browser/code.py`). Every `read`, and a `source` on a board whose posting page the code reads or at an
address no job board owns (`takeable.code_first`), is tried by the app's
own code before any session, while its code-first worker runs (`code_running`): it leases the item (`code_take`) under
the same pace, and hands back what it settled, or passes the item on with nothing used (`code_pass_on`). Until then no
session is given it (`takeable.ask`). A retry clears `code_at`, so code tries first again.

The pace lives here, per registrable domain (`BrowserDomain`), shared by every worker: one worker on a domain at a
time (no item is leased on a domain another item is leased on), one page every `PAGE_GAP` seconds (the runbook's
`pace_seconds` where it has one, `page_gap`; the gap after a
hand-back, and the wait `browser.heartbeat` gives before each page load), and a block cools the domain down:
`COOLING`, an hour, then eight hours if it happens again before a success.

What each result does (`hand_back`): `found` (an employer or job-system address only: a job board's is not a source) and
`easy_apply` (the listing is the source) source the role, and the text comes by code or by a `read` item; `text` keeps
the posting's whole text; `gone` ends the role as posting_gone; `not_found` and `unreadable` wait `RETRY_AFTER`, and
after `TRIES` such tries the item is `stuck`: the role stays where it is with the reason on its card
(`Opportunity.browser_note`); `blocked` uses no try, returns the item and cools the domain. A shortened or placeholder
text, or a placeholder note, is refused.

Every session's work is a run of the "Read in your browser" mission (`session_run`): its line says what was handed
back, item by item (`summary_words`). The pool's workers have no fixed number: a free session takes browser work
while some domain with ready items is unworked (`browser_queue.free`, asked by `session_kinds`). A restart drains them
(`availability.drain`): no item is handed out, and each worker finishes the one it holds. The health of the queue
(`health`) and a stall over `STALL` while work waits (`watch`, raised to the owner only when it lasts `STALL_TELLS`)
are shown on the Agents page. Services flush; the door or the route commits.

The work is split by job, in five modules:

- `browser/pace.py`: the constants, the process's own state, the install's scope, the pace per domain and the order
  items are handed out in (`takeable.ask`);
- `browser/leases.py`: leases (`expire`, `touch`, `release_session`) and a session's run (`session_run`,
  `summary_words`, `end_run`, `settle_run`, `run_as_read`);
- `browser/queue.py`: making the queue from the roles and sites (`refresh`), and what the owner sees of it
  (`doing`, `health`, `watch`);
- `browser/items.py` (this one): handing items out (`next_item`, `heartbeat`, `code_take`) and the door's `postings`
  and `skill`;
- `browser/handback.py`: handing an item back (`hand_back`) and what each result does."""

import math
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import db
from app.errors import Busy
from app.jobsearch import as_read, off_turn
from app.jobsearch.browser import leases as browser_leases
from app.jobsearch.browser import pace as browser_pace
from app.jobsearch.browser import reader as br
from app.jobsearch.browser import takeable
from app.jobsearch.browser.pace import (
    CODE,
    LEASE,
    LEASED,
    MAP,
    NO_RESULT,
    NOTE_MAX,
    PATHFINDER,
    READ,
    RESULTS,
    SCAN,
    SOURCE,
    TIMEBOX,
    TRACK,
    WAIT_UP_TO,
    WAITING,
    WORKER,
    BrowserWorkError,
)
from app.jobsearch.browser.takeable import Taker
from app.jobsearch.missions import record
from app.jobsearch.models import BrowserItem, JobPosting, Mission, MissionRun, Opportunity
from app.jobsearch.profile import store as profile_store
from app.jobsearch.work import availability, hand_outs, instruction_files
from app.planengine.budget import AGENT_PAYLOAD_BUDGET_CHARS, payload_chars
from app.sites import lessons as site_lessons
from app.sites import skills as site_skills
from app.sites.models import Site
from app.timeutil import aware

#: The most items one browser worker session takes. Every page it read stays in its context, so a long session's turns
#: grow dear;
#: after this many it is told to stop, its run closes, and the pool starts a fresh one that continues (nothing is lost:
#: each item is handed back as it is done). The Pathfinder is not capped: a map takes as long as a site needs.
MAX_ITEMS_PER_SESSION = 8
#: How long a session is told to wait when the queue's row is busy past the lock wait (`next_item`).
BUSY_WAIT_SECONDS = 5


def _stop(why: str) -> dict:
    # `end_session` says this is the last word: a session's hook ends it at once, with no closing reply (a stop that
    # asks the session to hand back what it holds first lacks it).
    return {"ok": True, "stop": True, "end_session": True, "why": why,
            "next": f"{why} Reply with one line saying what you handed back, and stop."}


async def _why_not(session: AsyncSession, owner: str, worker: str, now: datetime) -> str | None:
    """Why this kind of worker is handed no item now, or None."""
    from app.jobsearch.pool import core as pool  # the pool reads this module; imported here, never at the top

    if availability.draining(owner, now):
        return "The app is restarting: you have finished."
    mission = await session.scalar(select(Mission).where(Mission.owner == owner, Mission.key == record.BROWSER))
    if mission is None or mission.state != "on":
        return "The owner has paused reading in the browser."
    if worker in await pool.paused_kinds(session, owner):
        return "The owner has paused this kind of work."
    stop = await availability.barred(session, owner, availability.Question.WORKER, now=now)
    return stop.words if stop is not None else None


#: How many of the items a call could be handed it keeps from its read before the lock, to try under the lock (`_pick`).
CANDIDATES = 8


@dataclass(frozen=True)
class _Seen:
    """What a call read, with no lock, before it locked the mission's row: the taker it asked `takeable.ask` for (its
    kinds, and whether only the owner's items: `Taker.decided`) and the first items that were free at once, in the
    order they would be handed. Only
    ever a candidate list for a grant: every wait, stop and empty answer is decided under the lock, by the code that
    always decided it."""

    taker: Taker
    ids: tuple[uuid.UUID, ...]


async def _look(session: AsyncSession, owner: str, sid: str, worker: str, now: datetime) -> _Seen | None:
    """The read of a hand-out that is made with no lock (read only: no row is written, and no run is opened, which
    before the mission's row is locked is the deadlock `lock_mission` describes): None unless this is a call that would
    be handed an item, a plain ask (a claim is taken, a write, under the lock) of a session that may be handed one (it
    is not stopped, holds no item and has not taken its share) and has items free now, so a call that is stopped or sent
    its held item reads no queue. Otherwise the first `CANDIDATES` items free at once."""
    if browser_pace.caller.get().claim:
        return None
    if await _why_not(session, owner, worker, now) is not None:
        return None
    if await browser_leases._held_item(session, owner, sid) is not None:
        return None
    started = ((entry.started if (entry := await browser_leases._run_of(session, owner, sid)) is not None else None)
               or {})
    if worker == WORKER and int(started.get("taken") or 0) >= MAX_ITEMS_PER_SESSION:
        return None
    taker = await Taker.session(owner, worker, track=started.get("track")).decided(session, now)
    ready = await takeable.ask(session, taker, now)
    ids = tuple(row.id for row, at in ready if at <= now)[:CANDIDATES]
    return _Seen(taker, ids) if ids else None


async def _look_for_code(session: AsyncSession, owner: str, now: datetime) -> _Seen | None:
    """`_look` for the app's own code-first worker (`code_take`): None while the mission is paused, as it is paused
    (read again under the lock); else the first items the code has still to try that are free at once."""
    mission = await session.scalar(select(Mission).where(Mission.owner == owner, Mission.key == record.BROWSER))
    if mission is None or mission.state != "on":
        return None
    taker = await Taker.code(owner).decided(session, now)
    ready = await takeable.ask(session, taker, now)
    ids = tuple(row.id for row, at in ready if at <= now)[:CANDIDATES]
    return _Seen(taker, ids) if ids else None


async def _pick(session: AsyncSession, owner: str, seen: _Seen, now: datetime) -> BrowserItem | None:
    """The first of the items a call read before the lock that is still free under it, whole and locked, or None (the
    caller then does what it always did: asks `takeable.ask` and answers). One statement locks the candidates in id
    order
    (`as_read.lock`: an item another transaction holds, or that is no longer waiting, of a kind asked or due, is left
    out), and one more reads the domains the workers are on and the claims, for the candidates' domains: the item is
    judged as `takeable.ask` judges it, by the same code (`takeable.recheck`), so what is leased is what `ask` would
    have handed first among them. The mission's row is held: no other call leases or gives back meanwhile."""
    held = await as_read.lock(session, BrowserItem, seen.ids, where=(
        BrowserItem.state == WAITING, BrowserItem.kind.in_(seen.taker.kinds), BrowserItem.next_at <= now + WAIT_UP_TO))
    if not held.rows:
        return None
    ready = await takeable.recheck(session, seen.taker, held.rows.values(), now)
    return next((row for row, at in ready if at <= now), None)


async def _seen(session: AsyncSession, owner: str, site: Site, now: datetime) -> list[str]:
    return [url for url in (await session.scalars(select(JobPosting.url).where(
        JobPosting.owner == owner, JobPosting.ats == br.ATS, JobPosting.employer_id == site.id,
        JobPosting.url.is_not(None), JobPosting.last_seen >= now - timedelta(days=br.SEEN_DAYS))
        .order_by(JobPosting.last_seen.desc()).limit(br.SEEN_SHOWN))).all() if url]


async def _search(session: AsyncSession, owner: str) -> dict:
    profile = await profile_store.current(session, owner)
    return {"phrases": profile.criteria.search_phrases(), "places": br._places(profile),
            "window_hours": br.WINDOW_HOURS}


async def _offer(session: AsyncSession, owner: str, row: BrowserItem, entry: MissionRun, now: datetime, *,
                 again: bool = False) -> dict:
    """The item as the worker is sent it: what to open, about what, and what it may hand back; the site's runbook the
    first time this session works on that site; the instructions with the session's first item."""
    started = dict(entry.started or {})
    item: dict = {"item_id": str(row.id), "kind": row.kind, "domain": row.domain, "link": row.link,
                  "results": list(RESULTS[row.kind])}
    if (box := TIMEBOX.get(row.kind)) is not None:
        item["timebox_minutes"] = int(box.total_seconds() // 60)
    if row.tries:
        item["tries_before"] = row.tries
        item["last_note"] = row.last_note
    sites = await br.browser_sites(session, owner)
    rows = sites.get(row.site or "") or []
    if row.kind in (SOURCE, READ):
        if row.role_id is not None:
            role = await session.get(Opportunity, row.role_id)
            if role is not None:
                item["role"] = {"title": role.title, "company": role.company, "location": role.location}
        else:
            item["added_by_link"] = True
    if rows:
        item["site"], item["name"] = row.site, br.site_name(rows)
        recipe = await br.browser_recipe(session, rows[0])
        sent = list(started.get("runbooks_sent") or [])
        if recipe is not None and row.kind != MAP:
            if row.site in sent:
                item["runbook"] = "sent with an earlier item of this session: follow it again"
            else:
                item["runbook"] = br._as_sent(recipe)
                started["runbooks_sent"] = [*sent, row.site]
    if row.kind in (SCAN, MAP) and rows:
        item["start_urls"] = [s.url for s in rows]
        if portal := br.recorded_portal(rows):
            item["portal"] = portal
        item["search"] = await _search(session, owner)
        item["seen_recently"] = await _seen(session, owner, rows[0], now)
        item["batch"] = br.MAX_BATCH
    if row.kind == MAP and rows:
        recipes = {h: r for h, rs in sites.items() if (r := await br.browser_recipe(session, rs[0]))}
        item["learn_note"] = rows[0].learn_note
        item["lessons"] = await site_lessons.lessons_for_kind(session, owner, rows[0].kind)
        item.update(br._reuses(rows, row.site or "", recipes))
        item["runbook_template"] = await instruction_files.read(session, owner, "templates", br.RUNBOOK_TEMPLATE)
    reply: dict = {"ok": True, "item": item}
    if again:
        reply["note"] = "You hold this item already: finish it and hand it back before asking for another."
    if not started.get("instructed"):
        learning = browser_pace.worker_of(started.get("session_id")) == PATHFINDER
        reply["instructions"] = await instruction_files.read(
            session, owner, "instructions", br.LEARNER if learning else br.INSTRUCTIONS)
        reply["response_template"] = await instruction_files.read(
            session, owner, "templates", br.SKILL_TEMPLATE if learning else br.TEMPLATE)
        if learning:
            reply["job_systems"] = site_skills.adapter_systems()
        started["instructed"] = True
    entry.started = started
    while payload_chars(reply) > AGENT_PAYLOAD_BUDGET_CHARS and item.get("seen_recently"):
        item["seen_recently"] = item["seen_recently"][: len(item["seen_recently"]) // 2]
    reply["next"] = ("Do this item, then hand it back with browser.hand_back (item_id, result and what the result "
                     "needs). Call browser.heartbeat before each page load and wait what it says. Read a page with "
                     "browser.page_digest first, and take a full snapshot only when the digest lacks what you need.")
    if "timebox_minutes" in item:
        reply["next"] += (f" Hand it back within {item['timebox_minutes']} minutes: past that the app hands it back "
                          "for you as timed out.")
    return reply


async def next_item(session: AsyncSession, owner: str, now: datetime | None = None) -> dict:
    """The next item for the calling session, leased to it (the door's `browser.next_item`). A session that holds one
    is sent it again. The order: for the browser worker `source`, then `read`, then `scan` (a session keeps to the track
    of its first item); for the Pathfinder `map`. An item is handed out only on a domain no other worker is on, after
    its next page's time and out of any cooling. When the next one is free within `WAIT_UP_TO` the session is told to
    wait; when nothing is, it is told to stop, and its run closes."""
    now = now or datetime.now(UTC)
    sid = browser_pace._asker()
    worker = browser_pace.worker_of(sid)
    # The hand-out makes no queue (`browser.reconcile` does), so the mission's row is never held while every role and
    # item of the owner is read.
    await db.release_if_free(session)  # what the caller read is over before the mission's row is locked
    # What the call would be handed is read before the lock, with nothing held (`_look`), so the lock is held for the
    # decision and the writes and not for the read of every waiting item. The read
    # only names candidates: the mission's row, and the items, are locked before one is leased, and any answer but a
    # grant is decided under the lock as it always was.
    seen = await _look(session, owner, sid, worker, now)
    await db.release_if_free(session)  # the read's connection goes back to the pool while the call waits its turn
    # One hand-out at a time (two sessions asking at once must never take the same domain), and the mission's row is
    # locked before anything else is: see `browser_pace.lock_mission`. Busy past the lock wait: the session waits and
    # asks again, as when nothing is free.
    try:
        await browser_pace.lock_mission(session, owner)
    except Busy:
        return {"ok": True, "wait_seconds": BUSY_WAIT_SECONDS,
                "next": "The queue is busy this moment: wait that many seconds (one evaluate_script setTimeout), "
                        "then call browser.next_item again."}
    expired = await browser_leases.expire(session, owner, now)
    if (why := await _why_not(session, owner, worker, now)) is not None:
        held = await browser_leases._held_item(session, owner, sid)
        if held is not None:  # a pause, peak hours or a restart: the item it holds is finished first
            return {"ok": True, "stop": True, "why": why, "next": "Hand back the item you hold first, then stop."}
        await browser_leases.release_session(session, owner, sid, why, now)
        return _stop(why)
    entry = await browser_leases.session_run(session, owner, sid, now)
    held = await browser_leases._held_item(session, owner, sid)
    if held is not None:
        held.lease_until = now + LEASE
        return await _offer(session, owner, held, entry, now, again=True)
    if worker == WORKER and int((entry.started or {}).get("taken") or 0) >= MAX_ITEMS_PER_SESSION:
        browser_leases.end_run(entry, now)
        return _stop(f"You have taken {MAX_ITEMS_PER_SESSION} items, a session's share: a fresh session continues.")
    # The item the app claimed for this session when it started it (`X-Claim`, `hand_outs.py`): taken now, and handed
    # first if it can still be; what is claimed for another session is passed over.
    claimed = frozenset(uuid.UUID(i) for i in await hand_outs.take(
        session, owner, hand_outs.PATHFINDER if worker == PATHFINDER else hand_outs.BROWSER_READER,
        browser_pace.caller.get().claim, sid) or [])
    taker = await Taker.session(owner, worker, track=(entry.started or {}).get("track"), claimed=claimed).decided(
        session, now)
    # A grant from the read before the lock: the first candidate still free. Not after a lease `expire` just returned
    # (an item it freed is not in a read made before it, and may be first in line: the candidates would hand the second,
    # `test_the_first_item_in_line_whose_lease_ran_out_is_handed_before_the_items_the_read_before_the_lock_named`), and
    # never for a call that names a claim. `not claimed` is defence in depth: `_look` already reads nothing for a call
    # that names a claim, so `seen` is None then; the guard keeps a claim's items first if that ever changes.
    row = (await _pick(session, owner, seen, now)
           if seen is not None and not expired and not claimed and seen.taker == taker else None)
    granted = row is not None
    if row is None:
        ready = await takeable.ask(session, taker, now)
        now_ready = [r for r, at in ready if at <= now]
        if not now_ready:
            if ready:
                wait = min(at for _, at in ready) - now
                entry.started = {**(entry.started or {}), "last_call": now.isoformat()}
                return {"ok": True, "wait_seconds": max(math.ceil(wait.total_seconds()), 1),
                        "next": "Nothing is free this moment: wait that many seconds (one evaluate_script setTimeout), "
                                "then call browser.next_item again."}
            browser_leases.end_run(entry, now)
            return _stop("There is nothing more you can take now: you have finished.")
        row = await browser_pace.whole(session, next((r for r in now_ready if r.id in claimed), now_ready[0]))
    row.state, row.leased_by, row.lease_until, row.run_id = LEASED, sid, now + LEASE, entry.id
    box = TIMEBOX.get(row.kind)
    row.due_by = now + box if box is not None else None  # a map has no time box
    pace = await browser_pace._pace(session, owner, row.domain)
    pace.next_page_at = now + browser_pace.page_gap(row.domain)  # the item's first page loads now
    if row.kind == MAP:
        for site in (await br.browser_sites(session, owner)).get(row.site or "", []):
            site.learn_tried_at = now
    entry.started = {**(entry.started or {}), "track": TRACK[row.kind], "last_call": now.isoformat(),
                     "taken": int((entry.started or {}).get("taken") or 0) + 1}
    await session.flush()
    if granted:
        # The lease is whole: commit before the offer is read (the runbook, the role, the instructions: about a dozen
        # statements and no write that cannot wait), so the mission's row is held for the grant and not for the offer.
        # An offer that fails is made good by the session's next call, which is sent the item it holds again.
        await db.release_if_free(session)
    return await _offer(session, owner, row, entry, now)


async def page_turn(session: AsyncSession, owner: str, domain: str, now: datetime) -> float:
    """Reserve the next page load on a domain: how many seconds to wait before it, so the domain sees one page every
    `PAGE_GAP` seconds whoever loads it (a session, the digest, the app's code)."""
    pace = await browser_pace._pace(session, owner, domain)
    start = max(now, aware(pace.next_page_at) or now)
    pace.next_page_at = start + browser_pace.page_gap(domain)
    await session.flush()
    return max((start - now).total_seconds(), 0.0)





async def code_take(session: AsyncSession, owner: str, sid: str, now: datetime | None = None) -> BrowserItem | None:
    """The next item the app's code tries before any session (`takeable.code_first`), leased to the code-first worker
    `sid`: on a domain no worker is on, out of its cooling and past its next page. No time box (the code keeps its
    own) and no
    run. None when there is none now, the owner has paused reading in the browser, or the app is restarting."""
    now = now or datetime.now(UTC)
    if availability.draining(owner, now):
        return None
    await db.release_if_free(session)
    async with off_turn.reading(session) as reader:  # read with no lock and no turn, as in next_item: candidates only
        seen = await _look_for_code(reader, owner, now)
        # An empty poll takes no turn: nothing to try and no lease run out (the predicates of `leases.expire`) means
        # nothing to write; a paused mission also reads None here and is answered None under the lock as before.
        if seen is None and await reader.scalar(select(BrowserItem.id).where(
                BrowserItem.owner == owner, BrowserItem.state == LEASED,
                or_(BrowserItem.lease_until < now, BrowserItem.due_by < now)).limit(1)) is None:
            return None
    await db.release_if_free(session)
    try:
        await browser_pace.lock_mission(session, owner)  # first, as in next_item
    except Busy:  # busy past the lock wait: the code takes nothing this turn and tries again on its next
        return None
    mission = await session.scalar(select(Mission).where(Mission.owner == owner, Mission.key == record.BROWSER))
    if mission is None or mission.state != "on":
        return None
    expired = await browser_leases.expire(session, owner, now)
    # Self-started like any worker: outside the owner's off hours it takes only their items (`Taker.decided`).
    taker = await Taker.code(owner).decided(session, now)
    # Not after a lease `expire` just returned: an item it freed is not in the read before the lock, and may be first.
    row = (await _pick(session, owner, seen, now)
           if seen is not None and not expired and seen.taker == taker else None)
    if row is None:
        ready = [r for r, at in await takeable.ask(session, taker, now) if at <= now]
        if not ready:
            return None
        row = await browser_pace.whole(session, ready[0])  # the caller reads the whole item
    row.state, row.leased_by, row.lease_until, row.due_by = LEASED, sid, now + LEASE, None
    pace = await browser_pace._pace(session, owner, row.domain)
    pace.next_page_at = now + browser_pace.page_gap(row.domain)  # the item's first page loads now
    await session.flush()
    return row


async def code_pass_on(session: AsyncSession, owner: str, sid: str, item_id, why: str,
                       now: datetime | None = None) -> bool:
    """The app's code could not settle the item: it goes to a session, as before, with nothing used (no try, no wait).
    Returns whether the code-first worker held it."""
    now = now or datetime.now(UTC)
    row = await browser_leases._held_item(session, owner, sid)
    if row is None or str(row.id) != str(item_id):
        return False
    browser_leases._log(session, row, sid, NO_RESULT, f"the app's code passed it on: {why}"[:NOTE_MAX], now)
    row.state, row.leased_by, row.lease_until, row.code_at = WAITING, None, None, now
    await session.flush()
    return True


async def code_release_all(session: AsyncSession, owner: str, now: datetime | None = None) -> int:
    """What a code-first worker of an earlier start held goes back (untried by code, so code tries it again)."""
    now = now or datetime.now(UTC)
    rows = (await session.scalars(select(BrowserItem).where(
        BrowserItem.owner == owner, BrowserItem.state == LEASED, BrowserItem.leased_by.like(f"{CODE}-%")))).all()
    for row in rows:
        browser_leases._give_back(session, row, now, "the app restarted while its code tried this item")
    await session.flush()
    return len(rows)


async def heartbeat(session: AsyncSession, owner: str, now: datetime | None = None) -> dict:
    """The worker is about to load a page (the door's `browser.heartbeat`): its lease is renewed, and it is told how
    long to wait first, so its domain sees one page every `PAGE_GAP` seconds whoever loads it."""
    now = now or datetime.now(UTC)
    await browser_leases.expire(session, owner, now)
    row, _ = await browser_leases.touch(session, owner, now)
    if row is None:
        if await browser_leases._timed_out_last(session, owner, browser_pace._asker()):
            return {"ok": True, "holding": False, "next": "Your item ran past its time box and was handed back as "
                                                          "timed out: close what you opened for it and call "
                                                          "browser.next_item."}
        return {"ok": True, "holding": False, "next": "You hold no item: call browser.next_item."}
    wait_ms = int(await page_turn(session, owner, row.domain, now) * 1000)
    reply = {"ok": True, "holding": True, "wait_ms": wait_ms,
             "next": (f"Wait {wait_ms} ms (one evaluate_script setTimeout), then load the page." if wait_ms else
                      "Load the page now.")}
    if availability.draining(owner, now):
        reply["note"] = "The app is restarting: finish this item, hand it back, then stop."
    return reply


async def postings(session: AsyncSession, owner: str, postings_in, portals=None, now: datetime | None = None) -> dict:
    """Postings the session read on the site of the scan or map item it holds (the door's `browser.postings`)."""
    now = now or datetime.now(UTC)
    row, entry = await browser_leases.touch(session, owner, now)
    if row is None or row.kind not in (SCAN, MAP) or entry is None:
        raise BrowserWorkError("postings go with a scan or map item you hold: call browser.next_item")
    return await br.hand_in(session, owner, entry, row.site or "", postings_in, learning=row.kind == MAP,
                            portals=portals, now=now)


async def skill(session: AsyncSession, owner: str, site, recipe, page_says=None, lessons=None,
                now: datetime | None = None) -> dict:
    """The runbook for the site of the map item the session holds (the door's `browser.skill`)."""
    now = now or datetime.now(UTC)
    row, entry = await browser_leases.touch(session, owner, now)
    if row is None or row.kind != MAP or entry is None:
        raise BrowserWorkError("browser.skill is for a map item you hold: call browser.next_item")
    return await br.hand_skill(session, owner, entry, row.site or "", site, recipe, page_says, lessons, now)
