"""Making the browser's queue from the roles and sites, and what the owner sees of it: what each session is on, the
health line, the stall (see `browser/items.py`)."""

import asyncio
import logging
import uuid
from collections import Counter
from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, case, func, or_, select, update
from sqlalchemy.dialects.sqlite import insert as lite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app import db
from app.inbox import service as inbox
from app.jobsearch import as_read, off_turn, stages
from app.jobsearch.browser import go_checks, takeable
from app.jobsearch.browser import leases as browser_leases
from app.jobsearch.browser import pace as browser_pace
from app.jobsearch.browser import reader as br
from app.jobsearch.browser.pace import (
    BACKLOG,
    CODE,
    DONE,
    DROPPED,
    LEASE,
    LEASED,
    LISTING,
    MAP,
    NO_RESULT,
    PATHFINDER,
    READ,
    RECORD,
    SCAN,
    SOURCE,
    STUCK,
    WAITING,
    WORK,
)
from app.jobsearch.browser.takeable import Taker
from app.jobsearch.fetch import host_stops
from app.jobsearch.missions import record
from app.jobsearch.models import (
    OWN_ORIGINS,
    BrowserAttempt,
    BrowserDomain,
    BrowserItem,
    Mission,
    Opportunity,
    read_first,
)
from app.jobsearch.postings import chase
from app.jobsearch.postings import sourced as sourcing
from app.jobsearch.postings.chase import YIELD_EVERY
from app.jobsearch.work import availability, hand_outs, queue
from app.models.agent import AgentKindPause
from app.models.work import TASK_DONE, TASK_OPEN
from app.settings import settings
from app.sites import where
from app.sites.models import Site
from app.timeutil import aware

log = logging.getLogger(__name__)


#: A site is scanned again this long after its last scan.
SCAN_EVERY = timedelta(hours=8)

#: A stall: work waits and nothing has been handed back for this long. Told to the owner when it lasts `STALL_TELLS`.
STALL, STALL_TELLS = timedelta(minutes=30), timedelta(hours=2)
STALL_KEY = "browser-stall"

#: What the strip and the health line call each kind.
LABELS = {SOURCE: "source", READ: "read", SCAN: "scan", MAP: "map", RECORD: "record", LISTING: "listing"}

_stall_logged: dict[tuple[str, str], datetime] = {}


def _spec(**fields) -> dict:
    return {"role_id": None, "posting_id": None, "site": None, "priority": 1, "order_at": None, "code_only": False,
            **fields}


async def _wanted(session: AsyncSession, owner: str, sites: dict[str, list[Site]]) -> dict[tuple[str, str], dict]:
    """Every piece of browser work the roles and sites call for now, by (kind, target)."""
    out: dict[tuple[str, str], dict] = {}
    # Gates first: a role in Fact checking is judged on what its listing shows
    # before a browser item is queued for it, so a role a cheap gate stops (it is gated out) is never opened. The
    # reading is the pipeline's turn's (`schedule.read_gate_facts`, every 30 seconds), in short transactions of
    # its own, never here: the hand-out's transaction holds the browser mission's row lock (`next_item`, `code_take`),
    # and a long read inside it would queue every other hand-out behind it, each holding a connection.
    await sourcing.source_held(session, owner)  # a role that holds its employer's link needs no session to find it
    await br.note_no_link(session, owner)
    if await db.release_if_free(session):  # what those two wrote is whole: the reads below hold no role
        # ... and write nothing, so they are a reader's: no turn is held while they read every role, site and item.
        # Where the transaction cannot be released, they stay in it.
        await db.read_only(session)
    held = chase.HeldOnce()  # the postings' dates, read once for every list below that asks the age of a role
    board = await br.board_roles(session, owner, sites, held=held)
    for r in board:
        # A starred role's sourcing is handed out before every other (`priority` 0, the newest `order_at`); a role in
        # Followup needed is the backlog, after everything else (`BACKLOG`, `takeable._order`).
        # The stamp is the time the item is made or opened again, not a fact to keep current (`stamped`, `_plan`).
        first = ({"priority": 0, "order_at": datetime.now(UTC), "stamped": True} if r.get("first")
                 else {"order_at": r.get("order_at")})
        if r.get("backlog"):
            first["priority"] = BACKLOG
        # Followup needed is sourced by code only (the owner, 2026-11-13): no session is handed the item, and what code
        # cannot get is closed as noise (`postings/noise.py`). Never for a role of the owner's own data.
        first["code_only"] = bool(r.get("backlog")) and r.get("origin") not in OWN_ORIGINS
        out[(SOURCE, r["role_id"])] = _spec(role_id=uuid.UUID(r["role_id"]), site=r["site"], link=r["link"],
                                            domain=br.domain(r["link"]), **first)
    # A followed board's role in Followup needed has its page read by the app's own code (the owner, 2026-11-05,
    # docs/PRD.md 48.14): its text takes the role out of Followup needed, and the employer's posting is looked for at
    # the read-next mark (`models.SOURCE_NEXT`). The backlog: after everything else, in the off hours, the youngest role
    # first. The page of a role the owner is in talks about, whose employer's text is out of reach, is the owner's own
    # (priority 0, any hour: the board-copy module of `postings/`, `reader.listing_roles` says which); every other page
    # is the backlog.
    for r in await br.listing_roles(session, owner, held=held):
        first = {"priority": 0, "order_at": datetime.now(UTC), "stamped": True} if r.get("first") else {
            "priority": BACKLOG, "order_at": r["order_at"]}
        out[(LISTING, r["role_id"])] = _spec(role_id=uuid.UUID(r["role_id"]), posting_id=uuid.UUID(r["posting_id"]),
                                             link=r["link"], domain=br.domain(r["link"]), **first)
    added = await br.to_read_added(session, owner, -1)
    in_full = await br.to_read_in_full(session, owner, sites, -1, skip=[r["role_id"] for r in board])
    at_source = await br.to_read_at_source(session, owner, -1, skip=[r["role_id"] for r in added + in_full])
    for r in added:
        out[(READ, r["role_id"])] = _spec(posting_id=uuid.UUID(r["role_id"]), link=r["link"],
                                          domain=br.domain(r["link"]), priority=0, order_at=r.get("order_at"))
    for r in in_full + at_source:
        out[(READ, r["role_id"])] = _spec(role_id=uuid.UUID(r["role_id"]), link=r["link"], domain=br.domain(r["link"]),
                                          order_at=r.get("order_at"))
    for role_id, link in await go_checks.browser_checks(session, owner):  # Go's check that a posting is still open
        out[(READ, str(role_id))] = _spec(role_id=role_id, link=link, domain=br.domain(link), priority=0)
    from app.jobsearch.browser.recipes import (
        record as recipe_record,  # it reads the browser code, which reads this module
    )

    for ask in await recipe_record.wanted(session, owner):  # a recording the owner or a broken recipe asked for
        out[(RECORD, ask["site"])] = _spec(site=ask["site"], link=ask["link"], domain=ask["domain"],
                                           priority=ask["priority"])
    # A role the owner asked for is theirs, not the backlog: one they starred or moved to Ready to Launch before it was
    # read (`read_first`: its sourcing is already first, its reading is too now), or asked to have researched. Its
    # sourcing and its reading are handed out at any hour (`priority` 0, `takeable.owners_item`). So is a role the
    # owner is in talks about (the owner, 2026-11-05, `postings/unbuilt.py`: it needs no ask), and one the owner applied
    # to is the backlog, after everything else (`BACKLOG`).
    from app.jobsearch.research import line  # it reads the browser code, which reads this module

    asked = set((await session.scalars(select(Opportunity.id).where(
        Opportunity.owner == owner,
        or_(read_first(), line.owners_asks(), Opportunity.status == stages.ACTIVE_DISCUSSION)))).all())
    applied = set((await session.scalars(select(Opportunity.id).where(
        Opportunity.owner == owner, Opportunity.status == stages.APPLIED))).all())
    for (kind, _), spec in out.items():
        if kind in (SOURCE, READ) and spec["role_id"] in asked:
            spec["priority"] = 0
            spec["code_only"] = False
        elif kind in (SOURCE, READ) and spec["role_id"] in applied:
            spec["priority"] = BACKLOG
            spec["code_only"] = False
    learning = await br.to_learn(session, owner, sites)
    for host, rows in sites.items():
        start = br.recorded_portal(rows) or rows[0].url
        if host in learning:
            out[(MAP, host)] = _spec(site=host, link=rows[0].url, domain=br.domain(host),
                                     order_at=rows[0].learn_tried_at)
        if await br.browser_recipe(session, rows[0]) is not None:
            out[(SCAN, host)] = _spec(site=host, link=start, domain=br.domain(start))
    return {key: spec for key, spec in out.items() if spec["domain"]}


#: The facts of a piece of work that a refresh keeps current on its item.
FIELDS = ("role_id", "posting_id", "site", "link", "domain", "priority", "order_at", "code_only")
#: What a plan reads of an item: the key it is found by and its state, and the facts it compares (`FIELDS`), which it
#: compares only for an item that waits, so the others come with none; and when a scan was done (a scan is opened again
#: only `SCAN_EVERY` after it). The item's lease, tries and notes are not read, and not loaded: a plan reads every item
#: the queue has ever made (tens of thousands, most of them done or dropped), as plain rows (it writes nothing:
#: `_apply` takes the rows it changes again, locked). Taken a few hundred at a time (`PLAN_FETCH`).
PLAN_ITEM_COLUMNS = (
    BrowserItem.id, BrowserItem.kind, BrowserItem.target, BrowserItem.state,
    BrowserItem.code_only.label("was_code_only"),
    # What says a stuck item was ended by a stop (`handback`, `host_stops.end_waiting`) and which host's it was.
    case((BrowserItem.state == STUCK, BrowserItem.link)).label("stuck_link"),
    case((BrowserItem.state == STUCK, BrowserItem.domain)).label("stuck_domain"),
    case((BrowserItem.kind == SCAN, BrowserItem.done_at)).label("done_at"),
    *(case((BrowserItem.state == WAITING, getattr(BrowserItem, f))).label(f) for f in FIELDS))
PLAN_FETCH = YIELD_EVERY * 2
#: How many items one statement of a refresh adds.
CHUNK = 500


@dataclass
class _Plan:
    """What a refresh found to change, read with nothing locked: the items to add, the waiting items whose facts
    changed (by id, with the columns that did), the done or dropped items to open again, and the waiting or stuck items
    no longer called for. `_apply` takes each row as it is when it writes."""

    adds: list[dict] = field(default_factory=list)
    fills: dict[uuid.UUID, dict] = field(default_factory=dict)
    reopens: dict[uuid.UUID, dict] = field(default_factory=dict)
    #: The stuck items a stop ended whose host has no stop in force now: they are in `reopens` too, and their roles'
    #: note, the stop's words, is cleared with them.
    thaws: set[uuid.UUID] = field(default_factory=set)
    drops: list[uuid.UUID] = field(default_factory=list)


async def _plan(session: AsyncSession, owner: str, now: datetime) -> _Plan | None:
    """Read the roles, sites and items and say what would change. Nothing is locked: no hand-out waits behind it. None
    while the owner has paused the mission."""
    # The mission and the sites are read in a reader's session: in the writer's they began the turn here, and it lasted
    # to `source_held`'s release (a loaded drain made it long). Where the caller
    # holds a transaction the reader is the session itself, as it was.
    async with off_turn.reading(session) as reader:
        mission = await reader.scalar(select(Mission).where(Mission.owner == owner, Mission.key == record.BROWSER))
        if mission is None or mission.state != "on":
            return None
        stopped = {r.domain for r in (await reader.scalars(select(BrowserDomain).where(
            BrowserDomain.owner == owner, BrowserDomain.stopped_at.is_not(None)))).all() if host_stops.in_force(r, now)}
        sites = await br.browser_sites(reader, owner)  # none left: every open item of a site's is dropped below
    wanted = await _wanted(session, owner, sites)
    rows = {}
    # The stuck items a stop ended (a refusal is the only way an item ends stuck as blocked): read apart, a few rows, so
    # that the plan's wide read of every item carries no result column.
    blocked = set((await session.scalars(select(BrowserItem.id).where(
        BrowserItem.owner == owner, BrowserItem.state == STUCK,
        BrowserItem.last_result == browser_pace.BLOCKED))).all())
    items = await session.stream(
        select(*PLAN_ITEM_COLUMNS).where(BrowserItem.owner == owner).execution_options(yield_per=PLAN_FETCH))
    async for part in items.partitions(PLAN_FETCH):  # a few hundred rows at a time, the loop given back between
        rows.update({(r.kind, r.target): r for r in part})
        await asyncio.sleep(0)
    plan = _Plan()
    for n, ((kind, target), spec) in enumerate(wanted.items(), 1):
        if n % YIELD_EVERY == 0:
            await asyncio.sleep(0)
        row = rows.get((kind, target))
        if row is None:
            plan.adds.append({"id": uuid.uuid4(), "owner": owner, "kind": kind, "target": target, "state": WAITING,
                              "tries": 0, "next_at": now, "created_at": now, **{f: spec[f] for f in FIELDS}})
        elif row.state == WAITING:
            # An owner's first item is stamped with the time of the plan (`_wanted`: the newest goes first), which no
            # stored row ever equals: one that waits at that priority already keeps the stamp it has (otherwise every
            # such item would be filled again at every refresh, and the hourly sweep would log a catch each time). The
            # mark is the plan's own and is on no row (`FIELDS`).
            kept = ("order_at",) if spec.get("stamped") and row.priority == spec["priority"] else ()
            if changed := {f: spec[f] for f in FIELDS if f not in kept and getattr(row, f) != spec[f]}:
                plan.fills[row.id] = changed
        elif row.state in (DONE, DROPPED):
            done = aware(row.done_at)
            if kind == SCAN and row.state == DONE and done is not None and now - done < SCAN_EVERY:
                continue
            values = {f: spec[f] for f in FIELDS}
            if kind == SCAN:
                values["order_at"] = done  # the site scanned longest ago goes first
            # An item opened again starts over: what it last said went with its tries. Left on it, "is stopped" or
            # "not found" would still mark the role's employer text out of reach (the board-copy module of `postings/`)
            # while the new lookup is waiting or made.
            plan.reopens[row.id] = {**values, "state": WAITING, "tries": 0, "next_at": now, "done_at": None,
                                    "code_at": None, "last_result": None, "last_note": None}
        elif (row.state == STUCK and row.id in blocked
              and host_stops.key_for(row.stuck_link, row.stuck_domain) not in stopped):
            # A stop ended it (a refusal is the only way an item ends stuck as blocked) and the host has none in force
            # now, the owner having lifted it in the data: it is read again, as one just made (the owner, 2026-11-15).
            plan.reopens[row.id] = {**{f: spec[f] for f in FIELDS}, "state": WAITING, "tries": 0, "next_at": now,
                                    "done_at": None, "code_at": None, "last_result": None, "last_note": None}
            plan.thaws.add(row.id)
        elif row.state == STUCK and spec["code_only"] and not row.was_code_only:
            # Once, when the rule is new for the item: an item a session gave up on, under the shapes the code did not
            # read, is tried again by code with them, and the role is closed as noise if code cannot get it either.
            plan.reopens[row.id] = {**{f: spec[f] for f in FIELDS}, "state": WAITING, "tries": 0, "next_at": now,
                                    "done_at": None, "code_at": None, "last_result": None, "last_note": None}
    for n, (key, row) in enumerate(rows.items(), 1):
        if key not in wanted and row.state in (WAITING, STUCK):
            plan.drops.append(row.id)
        if n % (YIELD_EVERY * 4) == 0:
            await asyncio.sleep(0)
    return plan


def _chunks[T](items: list[T]) -> list[list[T]]:
    return [items[start:start + CHUNK] for start in range(0, len(items), CHUNK)]


_NO_KEY = uuid.UUID(int=0)


def _by_key(add: dict) -> tuple[bool, uuid.UUID, uuid.UUID]:
    """Where a new item sits in the statements that insert them: the items of a role first, by role id, then those of a
    posting, by posting id (an item of a site has neither). The insert checks each item's `role_id` and `posting_id`
    foreign key by taking that row `FOR KEY SHARE`, in the order the items are listed and held until the transaction
    ends, and the passes that lock roles `FOR UPDATE` and wait take them in id order (`as_read.lock`): the keys must be
    taken in that order too, or the insert holds a later role and asks for an earlier one such a pass holds while it
    asks for the later."""
    role, posting = add["role_id"], add["posting_id"]
    return role is not None, role if role is not None else _NO_KEY, posting if posting is not None else _NO_KEY


async def _apply(session: AsyncSession, owner: str, plan: _Plan, now: datetime) -> None:
    """Write the plan, in a short transaction of its own, to the rows as they are now: each item that changes is locked
    in id order (an item another transaction holds is left for the next refresh) and looked at again, and is written
    only if it is still in the state the plan found it in (a session that leased it meanwhile keeps it). A new item
    that is there already (another refresh was first) is not added twice. The new items are inserted in the order of
    their keys (`_by_key`), across all the statements, whatever order the plan listed them in."""
    for chunk in _chunks(sorted(plan.adds, key=_by_key)):
        chunk = [{**a, "code_only": bool(a.get("code_only"))} for a in chunk]  # a spec with no word is a session's
        await session.execute(lite_insert(BrowserItem).values(chunk).on_conflict_do_nothing(
            index_elements=["owner", "kind", "target"]))
    # An item added is work for a session (the ones reopened are rows the session saves, which post for themselves).
    added = {queue.browser_ready(a["kind"]) for a in plan.adds}
    if plan.adds:
        for topic in (queue.ready("browser_reader"), queue.ready("pathfinder")):
            queue.post(session, owner, topic, [""] if topic in added else [], cause="core:browser.queue._apply")
    changing = [*plan.fills, *plan.reopens, *plan.drops]
    stuck_of: list[uuid.UUID] = []
    thawed: list[uuid.UUID] = []
    held = await as_read.lock(session, BrowserItem, changing,
                              where=(BrowserItem.state.in_((WAITING, STUCK, DONE, DROPPED)),))
    for row_id in changing:
        if (row := held.rows.get(row_id)) is None:
            continue
        if row_id in plan.fills and row.state == WAITING:
            for column, value in plan.fills[row_id].items():
                setattr(row, column, value)
        elif row_id in plan.thaws and row.state == STUCK and row.last_result == browser_pace.BLOCKED:
            for column, value in plan.reopens[row_id].items():
                setattr(row, column, value)
            if row.role_id is not None:
                thawed.append(row.role_id)
        elif row_id in plan.reopens and (row.state in (DONE, DROPPED) or (row.state == STUCK and not row.code_only)):
            for column, value in plan.reopens[row_id].items():
                setattr(row, column, value)
        elif row_id in plan.drops and row.state in (WAITING, STUCK):
            if row.state == STUCK and row.role_id is not None:
                stuck_of.append(row.role_id)  # the role was sourced or read another way, or left play
            row.state = DROPPED
    if thawed or stuck_of:
        # One statement: the note the stop left on a thawed role (another note, a read that failed, is not the stop's to
        # clear), and every note of a role whose stuck item was dropped (the role was sourced or read another way, or
        # left play).
        await session.execute(update(Opportunity).where(or_(
            Opportunity.id.in_(stuck_of),
            and_(Opportunity.id.in_(thawed), Opportunity.browser_note.like("%is stopped:%")),
        )).values(browser_note=None))
        # The note is read by the deck: a followed board's role it parked (`deck._given_up`) rejoins the walk.
        queue.post(session, owner, queue.DECK_DIRTY, [""], cause="core:browser.queue._apply")
    await session.flush()
    await host_stops.end_waiting(session, owner, now)  # a stopped domain's items end with the reason, never wait
    await browser_pace.mark_held(session, owner)


async def refresh(session: AsyncSession, owner: str, now: datetime | None = None) -> int:
    """Make the queue match the roles and sites: an item for each piece of work that has none open (a done or dropped
    one is opened again, a scan only `SCAN_EVERY` after its last), and an open one no longer called for is dropped (a
    stuck one too, its role's note cleared: the role was sourced or read another way, or left play). A leased item is
    left to its worker. While the owner has paused the mission nothing changes. This is also the queue's seed: the app
    runs it at every start.

    It reads first (`_plan`, which locks nothing), ends the transaction (`db.release_if_free`: what the caller had
    written is committed with it, and inside a savepoint it is one transaction, as before), and writes (`_apply`) to the
    rows as they are then. The caller ends the transaction the writes are in.

    Only the work queue makes the queue: a `browser.reconcile` to-do runs this, and so do the start, favourites and the
    owner's Run now. The hand-outs, the pool's counts, the housekeeping pass and the schedule's look read it as it
    is. Returns how many items it added, filled, opened again or dropped (0 when the mission is paused)."""
    now = now or datetime.now(UTC)
    plan = await _plan(session, owner, now)
    if plan is None:
        return 0
    await db.release_if_free(session)
    await _apply(session, owner, plan, now)
    return len(plan.adds) + len(plan.fills) + len(plan.reopens) + len(plan.drops)


async def free(session: AsyncSession, owner: str, worker: str, now: datetime | None = None, *,
               mine: Collection[uuid.UUID] = (), scope: str | None = None) -> list[uuid.UUID]:
    """The items a session of this kind of worker could be handed now (or within `WAIT_UP_TO`), one for each domain no
    worker is on, in the order they are handed out (`takeable._order`): how many more sessions the pool may start
    for it (`session_kinds`' walk), and what a claim holds (its first, whose domain is held for the session). Items and
    domains claimed for another session that has not leased yet are left out (`takeable.ask`), and `mine`, the
    items claimed for the caller, are not. With `scope` "owners", only the items the owner waits on
    (`takeable.owners_item`). None while the mission is paused, no site is read in the browser, or a drain is on."""
    now = now or datetime.now(UTC)
    if availability.draining(owner, now) or await br.pullable_sites(session, owner) is None:
        return []
    own = frozenset(m if isinstance(m, uuid.UUID) else uuid.UUID(str(m)) for m in mine)
    taker = (Taker.owners(owner, worker, claimed=own) if scope == "owners"
             else Taker.session(owner, worker, claimed=own, owners_only=False))
    ready = await takeable.ask(session, taker, now)
    first: dict[str, uuid.UUID] = {}
    for row, _ in ready:
        first.setdefault(row.domain, row.id)
    return list(first.values())


async def waiting_count(session: AsyncSession, owner: str, kinds: tuple[str, ...] | list[str]) -> int:
    """How many items of these kinds wait (ready now or later, not stuck) for a session: an item only the app's code
    takes (`code_only`) is no work for one."""
    return int(await session.scalar(select(func.count(BrowserItem.id)).where(
        BrowserItem.owner == owner, BrowserItem.state == WAITING, BrowserItem.kind.in_(list(kinds)),
        BrowserItem.code_only.is_(False))) or 0)


async def open_work(session: AsyncSession, owner: str, now: datetime | None = None) -> dict[str, int]:
    """What keeps the app's Chrome open (docs/PRD.md 52.9): `leased`, the items workers hold now, and `waiting`, the
    items a session or the app's own code could be handed now or within `WAIT_UP_TO` (what `free` and `code_take` would
    hand out), and the items claimed for a session that has not leased them yet (`hand_outs`, path 47: the runner is
    starting Claude and its tools, which takes twenty seconds or more, and `takeable.ask` leaves a claimed item and its
    domain
    out). Not counted: work waiting for the night, work the pool is held from (peak hours), an item on a domain that is
    cooling, and anything while a drain is on or the mission is paused, as none of it is handed out. The bridge asks
    before it closes Chrome. Reads only: a lease that ran out is counted as leased until the engine's minute tick
    gives it back (`browser_leases.held_count`)."""
    now = now or datetime.now(UTC)
    leased = await browser_leases.held_count(session, owner)
    waiting = 0
    if not availability.draining(owner, now) and await br.pullable_sites(session, owner) is not None:
        claimed = await hand_outs.held_of_kinds(session, owner, hand_outs.BROWSER_KINDS, now)  # not in `ask`'s lists
        if claimed:
            waiting += int(await session.scalar(select(func.count(BrowserItem.id)).where(
                BrowserItem.id.in_(claimed), BrowserItem.state == WAITING)) or 0)
        if not await availability.standing(session, owner, availability.Question.POOL):
            for worker in WORK:
                waiting += len(await takeable.ask(session, Taker.session(owner, worker), now))
        if owner in browser_pace._code_on:
            waiting += len(await takeable.ask(session, Taker.code(owner), now))
    return {"waiting": waiting, "leased": leased}


async def doing(session: AsyncSession, owner: str) -> dict[str, str]:
    """What each browser session is on, by session id, in a few words: "jobs.example.test: Analyst at Initech",
    "scanning Example Jobs", "mapping Example Jobs" (the strip and the Agents page)."""
    rows = (await session.scalars(select(BrowserItem).where(
        BrowserItem.owner == owner, BrowserItem.state == LEASED))).all()
    if not rows:
        return {}
    names = {h: br.site_name(rs) for h, rs in (await br.browser_sites(session, owner)).items()}
    out: dict[str, str] = {}
    for row in rows:
        if not row.leased_by:
            continue
        if row.kind == SCAN:
            words = f"scanning {names.get(row.site or '', row.domain)}"
        elif row.kind == MAP:
            words = f"mapping {names.get(row.site or '', row.domain)}"
        else:
            role = await session.get(Opportunity, row.role_id) if row.role_id is not None else None
            what = f"{role.title} at {role.company}" if role is not None else "a job added by its link"
            words = f"{row.domain}: {'finding the original posting of ' if row.kind == SOURCE else 'reading '}{what}"
        out[row.leased_by] = words
    return out


async def code_doing(session: AsyncSession, owner: str) -> list[dict]:
    """What the app's code checker is on, one entry per item it holds now (none when it holds nothing), in plain words:
    "finding the original posting of a role on jobs.example.test", "reading a posting on example.com". The Agents page
    and the Pipeline show it as the "Code checker" (`pool.listing`)."""
    rows = (await session.scalars(select(BrowserItem).where(
        BrowserItem.owner == owner, BrowserItem.state == LEASED, BrowserItem.leased_by.like(f"{CODE}-%")
    ).order_by(BrowserItem.lease_until))).all()
    return [{"session": row.leased_by or "", "task": (
        f"finding the original posting of a role on {row.domain}" if row.kind == SOURCE
        else f"recording the listings of {row.domain}" if row.kind == RECORD
        else f"reading the page of a posting on {row.domain}" if row.kind == LISTING
        else f"reading a posting on {row.domain}")} for row in rows]


async def _last_handback(session: AsyncSession, owner: str, kinds) -> datetime | None:
    return aware(await session.scalar(select(func.max(BrowserAttempt.at)).where(
        BrowserAttempt.owner == owner, BrowserAttempt.kind.in_(list(kinds)), BrowserAttempt.outcome != NO_RESULT)))


async def stalled(session: AsyncSession, owner: str, worker: str, now: datetime) -> timedelta | None:
    """How long work of this kind has waited ready for a worker with nothing handed back, when that is over `STALL`;
    else None."""
    kinds = WORK[worker]
    oldest = (await takeable.backlog(session, owner, worker, now)).since
    if oldest is None:
        return None
    last = await _last_handback(session, owner, kinds)
    since = max(oldest, last) if last is not None else oldest
    return now - since if now - since > STALL else None


async def health(session: AsyncSession, owner: str, worker: str, now: datetime | None = None) -> str | None:
    """The health line of a kind of worker: items done in the last hour by kind, how many wait and the oldest's age,
    and a stall when there is one. None when it has nothing done or waiting."""
    now = now or datetime.now(UTC)
    kinds = WORK[worker]
    done = Counter((await session.scalars(select(BrowserAttempt.kind).where(
        BrowserAttempt.owner == owner, BrowserAttempt.kind.in_(list(kinds)), BrowserAttempt.outcome != NO_RESULT,
        BrowserAttempt.at >= now - timedelta(hours=1)))).all())
    count, oldest = (backlog := await takeable.backlog(session, owner, worker, now)).count, backlog.since
    if not done and not count:
        return None
    words = "Done in the last hour: " + ", ".join(f"{done[k]} {LABELS[k]}" for k in kinds) + "."
    if settings.browser_scope():
        held = int(await session.scalar(select(func.count(BrowserItem.id)).where(
            BrowserItem.owner == owner, BrowserItem.state == WAITING, BrowserItem.kind.in_(list(kinds)),
            BrowserItem.last_note == browser_pace.held_note())) or 0)
        if held:
            words += f" {held} {browser_pace.held_note()}."
    if count:
        words += (f" {count} waiting, the oldest for {browser_pace._ago(now - oldest)}." if oldest
                  else f" {count} waiting.")
    if (stall := await stalled(session, owner, worker, now)) is not None:
        words += f" Stalled: nothing handed back for {browser_pace._ago(stall)} while work waits."
    return words


async def paced(session: AsyncSession, owner: str, worker: str, now: datetime | None = None) -> tuple[int, str | None]:
    """How many of this kind's waiting items wait on a resting site (a block's cooling), and the site most of them are
    on: why the night left them (`pool.night_left`)."""
    now = now or datetime.now(UTC)
    resting = set((await session.scalars(select(BrowserDomain.domain).where(
        BrowserDomain.owner == owner, BrowserDomain.cool_until > now))).all())
    if not resting:
        return 0, None
    on = Counter((await session.scalars(select(BrowserItem.domain).where(
        BrowserItem.owner == owner, BrowserItem.state == WAITING, BrowserItem.kind.in_(list(WORK[worker])),
        BrowserItem.domain.in_(resting)))).all())
    return sum(on.values()), (on.most_common(1)[0][0] if on else None)


async def watch(session: AsyncSession, owner: str, now: datetime | None = None) -> None:
    """The schedule's look at the queue (every pass): leases that ran out return their items, quiet runs close, and a
    stall is raised. A stall over `STALL` is logged once; one that lasts `STALL_TELLS`
    becomes one Inbox item for the owner, closed when work moves again."""
    now = now or datetime.now(UTC)
    await browser_leases.expire(session, owner, now)
    for entry in await br.open_runs(session, owner):
        await browser_leases.settle_run(session, entry, now)
    # A kind the owner paused hands nothing back because they paused it: that is no stall.
    paused = set((await session.scalars(select(AgentKindPause.kind).where(AgentKindPause.owner == owner))).all())
    for worker in WORK:
        key = f"{STALL_KEY}-{worker}"
        stall = None if worker in paused else await stalled(session, owner, worker, now)
        task = await inbox.get_task_by_key(session, owner, key)
        if stall is None:
            _stall_logged.pop((owner, worker), None)
            if task is not None:
                if task.state == TASK_OPEN:
                    task.state, task.done_at = TASK_DONE, now
                    task.note = "You paused this work." if worker in paused else "Browser work moves again."
                task.key = f"{key}-{task.id.hex[:8]}"  # the key is free for the next stall
            continue
        if (owner, worker) not in _stall_logged:
            _stall_logged[(owner, worker)] = now
            log.warning("browser work (%s) stalled: nothing handed back for %s while work waits", worker,
                        browser_pace._ago(stall))
        if stall >= STALL_TELLS and task is None:
            label = "Pathfinder" if worker == PATHFINDER else "browser"
            await inbox.raise_task(
                session, owner, key=key, kind=where.NOTICE_KIND,
                title=(f"The {label} work has stalled: nothing handed back for {browser_pace._ago(stall)} while "
                       "work waits."),
                detail="Check that the agents run and Chrome opens on the app's own profile.")
    await session.flush()


async def silent(session: AsyncSession, owner: str, session_id: str, now: datetime | None = None) -> bool:
    """Whether a browser session that says it is alive has handed nothing back, and made no call, for `LEASE`: then its
    items go back and the runner is told to end it."""
    now = now or datetime.now(UTC)
    entry = await browser_leases._run_of(session, owner, session_id)
    if entry is None:
        return False
    started = entry.started or {}
    last = aware(datetime.fromisoformat(started["last_call"])) if started.get("last_call") else aware(entry.started_at)
    if last is None or now - last < LEASE:
        return False
    await browser_leases.release_session(session, owner, session_id,
                                         "the session handed nothing back for 10 minutes", now)
    return True
