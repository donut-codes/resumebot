"""A role's row (docs/PRD.md paths 7 and 11): what the Pipeline list shows for one role, the note of what is wrong with
it or waits on the owner included, finished. The one place a row is made: a column's cards, a role's own page, the
answer to a move and the answer to a star are all this module's, so the row for one role is by construction the row its
column shows. The screen draws it and rewrites nothing.

The interface is three reads. `row(session, owner, role_id)` is one role's row. `columns(session, owner, key, ...)` is
the board: every lane (`stages.LANES`, with Added links first) or one, in the owner's order and a page at a time.
`favorites(session, owner)` is the starred roles, one list that is no lane. All only read: no flush, no network I/O;
they run in the caller's open transaction and see its flushed writes (a move or a star answers with its row).
`columns` is the board's own request. The route hands it a reader's session (`db.ReadSession`), and with one it writes
nothing: the lanes are read from the stored order as it stands, which the engine settles when a write has dirtied it
(the work queue's look, `work/queue/loop.py`), so a read of the board never asks for the writer's turn (the owner,
2026-11-10: the page program runs no work). Only a caller that hands it a writer's session has the order settled
first, in that session (`_settle_order`: the lanes then show the rows it has flushed; a settle that fails is rolled
back and logged, and the lanes are read from what is stored). It ends the transaction after each lane's reads
(`db.release_if_free`), so a lane's cards are made from what the reads brought with no connection held (each lane is
its own snapshot, never the board as one). None reads a role's posting text or research. A fact the app
does not hold shows as None or as the stage's own words, never as an error; the only refusals are `board.NoSuchRole` and
`board.NoSuchColumn` (404).

**The order** is the owner's (docs/PRD.md 24.6 to 24.9). Where the work is ahead (First Impression, Second Impression,
Ready to Launch, and the ranked list of the three): `pipeline.points` (the number with the first golden hours' bonus and
the stale penalty, the owner's settings in `pipeline.QueueRules`, the highest first, ties to the younger), the order
`pipeline.ordered` puts roles in. Go's list calls that function (`board.ready_line`). Everywhere else:
`pipeline.age_key`, the tail of that same key (the younger posting first, then the more recently found, then by id; a
role nothing dates is last). A role's age is counted from the day the gates count (`freshness.posted_basis`, read by
`freshness.posted_by_role`) in all of them: the Age column, the stale penalty, the `new` tag and the order (docs/PRD.md
40.1; the owner, 2026-11-04). A lane is read in that order by the database, from the number and the age the roles hold
(`stored_order`, docs/PRD.md 24.19: the owner, 2026-11-06), and only the roles of the page are read: not every role of
the lane to show fifty. A test holds the stored order to `pipeline.ordered` and `pipeline.age_key` for every role.

**The score** is one number (`pipeline.shown`, docs/PRD.md 24.11, 24.15): the role's number, 0 until it is ranked, its
Haiku rank as written until Sonnet has read it, Sonnet's queue score after, with the first hours' bonus and the stale
penalty (the number the list is sorted by, `pipeline.points`, as it is: it can pass 100); None for a role with no score,
which shows its fit as a share of what the owner's asks allow (`fit_pct`) if it has one. A new role scored 70 shows 90,
and its `new` tag says why: the row carries the bonus as `first_hours_bonus` (20), which the tag prints as New (+20).

**The note** (`next`) is worded here and nothing on the screen rewrites it: the first of the rules in `_next` that
matches. It is there only when the owner acts on it or something is wrong (docs/PRD.md 24.18): a gate that stopped the
role, with its reason; a posting the page did not confirm, or the browser could not check; a packet that failed or is
held; Launch skipping the role; the browser giving up; a read that failed or graded nothing; a pay band mostly under the
floor; the opened role waiting to be marked Applied; the reason a role closed. What a stage does as a matter of course
(waits to be read, is with the analyst, waits for the employer, a packet to build, a posting found open) is not said:
the stage and the number say all there is, and `next` is None.

**Where the rows' facts come from.** Everything one call needs is read once for the call (`_Facts`: the owner's
numbers, then, for the roles being shown, each one's rank, whether it is read, the day it was posted, its
newest packet and its predicted ask), so what a call costs does not grow with the number of roles shown, and no fact is
read for a role a lane holds and does not show. The cards shown are read with a card's columns (`CARD_COLUMNS`). The Row
type is mirrored by the page's `BoardCard` (`api/types/pipeline.ts`) and both are checked against one fixture
(`tests/role_row.json`): a new key is added to all three in one commit. Services flush; the route commits."""

import logging
import math
import re
import uuid
from collections.abc import Collection
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from typing import Any, Literal, NotRequired, TypedDict

from sqlalchemy import ColumnElement, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer, load_only

from app import db
from app.jobsearch import stages
from app.jobsearch.browser import go_checks
from app.jobsearch.employers import follow as employer_posting
from app.jobsearch.fit import freshness, scoring
from app.jobsearch.measures.pay_place import (
    below_floor_words,
    is_part_time,
    mostly_below_floor,
    parse_comp_text,
)
from app.jobsearch.models import JobPosting, Opportunity, Packet
from app.jobsearch.pipeline import board, ended, stored_order
from app.jobsearch.pipeline import deck as pipeline
from app.jobsearch.pipeline import from_posting as role_from_posting
from app.jobsearch.profile import store as profile_store
from app.jobsearch.research import analysis
from app.jobsearch.states import PacketState
from app.timeutil import aware, iso_or_none, owner_today, owner_zone, utc_now

log = logging.getLogger(__name__)

#: How many cards a lane's first load holds (a page, `PAGE_SIZE`), and how many a whole lane or one asked for by its key
#: holds at most. Module globals, read when a call is made.
SHOWN = 50
WHOLE_COLUMN = 1000
#: One page of a lane asked for by `page`, when the caller says no `size`.
PAGE_SIZE = 50
#: The Added links column: the jobs the owner added by their link that nothing has named yet
#: (`role_from_posting.waiting_added_where`), not roles yet. A page is this many.
ADDED_LINKS_PAGE = 50

#: Lanes where the work is ahead: ordered by `pipeline.order`.
_QUEUED = (stages.FIRST_IMPRESSION, stages.SECOND_IMPRESSION, stages.READY_TO_LAUNCH)
#: What a Ready to Launch role says while its research is being made again: it stays in its lane and is passed over
#: (`board.unconfirmed`) until its gates are confirmed.
RECHECKING_WORDS = "Being re-checked: it is offered again once its gates are confirmed"
#: What a role says when its Second Impression graded nothing of the posting: no place in Ready to Launch stands on it.
UNGRADED_WORDS = "Read, but it graded nothing of the posting: it has no place in Ready to Launch until a read does"
#: A US place as "City, ST" inside a longer location line.
_PLACE = re.compile(r"\b([A-Z][A-Za-z.'\- ]{1,40}, [A-Z]{2})\b")
_MODES = {"remote": "Remote", "hybrid": "Hybrid", "onsite": "In person", "unknown": "Not stated"}
#: What a card reads of a role: every column `_shape` and what it calls (`_next`, `_score`, `_age`,
#: `_first_hours_points` (`pipeline.first_hours_bonus`), `ended.words`, `scoring.percent_of`,
#: `pipeline.in_first_hours`) read, and no other. A role is loaded with these and no more: the other columns
#: are not on a card, and among them the posting's text, the research and the gate facts would be received and
#: decoded for every role the board reads, for a few hundred cards. A column a
#: card starts to read is added here, or reading it from the role fails (the loop does not load it for you);
#: `tests/jobsearch/test_board_loads.py` holds the two to each other.
CARD_COLUMNS = (
    Opportunity.id, Opportunity.title, Opportunity.company, Opportunity.status, Opportunity.fit,
    Opportunity.fit_breakdown, Opportunity.target_role, Opportunity.go_opened_at, Opportunity.go_skip,
    Opportunity.favorited_at, Opportunity.analysis_state, Opportunity.browser_note,
    Opportunity.sourced_at, Opportunity.move_type, Opportunity.location, Opportunity.comp, Opportunity.posted,
    Opportunity.found, Opportunity.created_at, Opportunity.origin, Opportunity.board_url, Opportunity.ended_reason,
    Opportunity.apply_url, Opportunity.url, Opportunity.employer_id, Opportunity.screened_out_reason,
    Opportunity.go_check, Opportunity.go_checked_at)


# --- the types: what the page reads ---------------------------------------------------------------------------------

#: How old a role is: its days, in words, and whether counted from the employer's posting date ("posted") or from the
#: day the app saw or found it ("found"). The days are the ones the gates count (`freshness.posted_basis`).
Age = TypedDict("Age", {"days": int | None, "words": str, "from": Literal["posted", "found"] | None})


class Work(TypedDict):
    """How the work happens, as the role's own words say it: Remote, Hybrid · City, In person · City, Not stated."""

    mode: Literal["remote", "hybrid", "onsite", "unknown"]
    place: str | None
    words: str


class NewestPacket(TypedDict):
    """The role's newest packet version; `progress` is where it is on its way (`packets.progress_of`)."""

    id: str
    version: int
    status: str
    progress: dict[str, Any]


class Row(TypedDict):
    """What the page draws of one role, whole. Four keys are drawn by no screen: `favorited_at`, `ended_reason`,
    `ended_words` and `reason` (`next` already carries their words). They stay as facts for the API's other readers:
    the answers to a move and a star, the role page's API and an agent's `role.state`, whose tests pin them."""

    id: str
    title: str
    company: str
    #: A status, or "added_links" for a posting that waits there.
    status: str
    status_label: str
    #: The fit as a percent of what the owner's asks allow.
    fit_pct: int | None
    #: The role's number (`pipeline.shown`: the number the list is sorted by, with the first hours' bonus and less the
    #: stale penalty, so a new role can show more than 100); None for a role with no score.
    score: int | None
    #: What is wrong with the role or waits on the owner, in words (docs/PRD.md 24.18): nothing on the screen rewrites
    #: it. None for the normal run of a stage, and for an added link.
    next: str | None
    packet: NewestPacket | None
    #: When Go opened the role's tab, else None: it stays in Ready to Launch until the mail confirms or the owner marks
    #: it Applied. Whether Launch has opened it is whether this is set.
    opened_at: str | None
    #: Under the owner's golden hours old, whatever its stage.
    new: bool
    #: The points of `score` that are the first hours' bonus (`pipeline.first_hours_bonus`): 0 once the hours are over,
    #: for a rank of 0, for a role with no score and when the owner turned the bonus off. The tag prints it: New (+20).
    first_hours_bonus: int
    favorite: bool
    favorited_at: str | None
    work: Work
    pay: str
    #: The predicted ask, once the role is researched.
    ask: int | None
    age: Age
    ended_reason: str | None
    ended_words: str | None
    #: Why a gate stopped it (Gated out only).
    reason: str | None
    url: str | None
    apply_url: str | None
    #: "tracker", "scout" or "owner" for a role; "posting" for an added link.
    origin: str
    employer_id: str | None


class Column(TypedDict):
    key: str
    label: str
    #: How many roles the lane holds (every one, not only the cards here); for a Closed lane read by a `reason`, the
    #: roles under it.
    count: int
    cards: list[Row]
    #: Set when the lane was asked for by `page`: the page it holds and how many pages there are.
    page: NotRequired[int]
    pages: NotRequired[int]


# --- the interface --------------------------------------------------------------------------------------------------


async def row(session: AsyncSession, owner: str, role_id: uuid.UUID) -> Row:
    """One role's row whole, as its column shows it: what a move or a star answers with, and the top of the role's own
    page. Raises `board.NoSuchRole` for an id that is no role of the owner's."""
    role = await session.get(Opportunity, role_id)
    if role is None or role.owner != owner:
        raise board.NoSuchRole("no such role")
    facts = await _page_facts(session, owner, await _owner_facts(session, owner), [role])
    return _shape(role, facts)


async def favorites(session: AsyncSession, owner: str) -> Column:
    """Every role the owner starred, whatever its stage, as one list (the owner, 2026-10-23): each row as its own lane
    shows it, its stage named by `status_label`, the most recently starred first. A favorite is a role the owner chose,
    so there are few; they are read whole. No lane of the board: `columns` has no such key."""
    rows = list((await session.scalars(select(Opportunity).where(
        Opportunity.owner == owner, Opportunity.favorited_at.is_not(None)).options(load_only(*CARD_COLUMNS))
        .order_by(Opportunity.favorited_at.desc(), Opportunity.id))).all())
    facts = await _page_facts(session, owner, await _owner_facts(session, owner), rows)
    return {"key": board.FAVORITES, "label": board.FAVORITES_LABEL, "count": len(rows),
            "cards": [_shape(r, facts) for r in rows]}


async def columns(session: AsyncSession, owner: str, key: str | None = None, *, target_role: str | None = None,
                  reason: str | None = None, page: int | None = None, size: int | None = None,
                  added_links: int | None = None) -> list[Column]:
    """The board's columns, each with its cards.

    Without `key`: Added links, then each lane of `stages.LANES`, each a first load of `SHOWN` cards (Second
    Impression and Ready to Launch hold up to `WHOLE_COLUMN`: the owner's limits cap them).
    With `key` a lane (or `closed`): that lane, up to `WHOLE_COLUMN` cards; with `page` too, the cards from
    `(page - 1) * size` up to `page * size` (`size`: `PAGE_SIZE` unless given) and the column also says its `page` and
    `pages`. `key` `added_links`: Added links alone, never paged. `key` `work` or `ranked` (`stages.GROUPS`, docs/PRD.md
    24.17): the same roles as their lanes, as one column each: `work` the roles still being got ready (Followup needed,
    Landing Zone, Cleansed Data) in the lanes' own order (the younger posting first), `ranked` First Impression, Second
    Impression and Ready to Launch as one list sorted by the one number (24.15), each card saying its stage in `status`
    and `status_label`; neither is part of the board without a `key`. Any other key raises `board.NoSuchColumn`.

    `added_links` is how many cards Added links holds, on the whole board and on its own (`ADDED_LINKS_PAGE` unless
    given): it counts cards, so no `page` and no `size` reach it. `target_role` (the id of one of the owner's target
    roles) keeps only the roles found for it, counts included; an id that is no target role has none, and Added links
    is then empty (a posting belongs to no target role until the analyst matches it). `reason`
    (`board.closed_reason_key`) keeps only the closed roles under that reason, and the Closed column's count is
    theirs."""
    on = owner_today()
    group = dict(stages.GROUPS).get(key) if key else None
    lanes = [(key, group)] if key and group else [(k, s) for k, s in stages.LANES if key in (None, k)]
    if key is not None and not lanes and key != board.ADDED_LINKS:
        raise board.NoSuchColumn(f"there is no column called {key}")
    out: list[Column] = []
    if key in (None, board.ADDED_LINKS):
        out.append(await _added_links(session, owner, max(added_links or ADDED_LINKS_PAGE, 1), on, target_role))
    if lanes:
        owner_facts = await _owner_facts(session, owner)
        await _settle_order(session, owner, owner_facts.now)
        paged = key is not None and page is not None and key != board.ADDED_LINKS
        step = PAGE_SIZE if size is None else size
        # Plan first, read once: each lane's count and page of ids, then the cards and facts of every lane's page in
        # one go, then the transaction ends and the cards are made.
        plans: list[tuple[str, int, list[uuid.UUID]]] = []
        for lane, statuses in lanes:
            limit = step if paged else WHOLE_COLUMN if key else _shown(lane)
            offset = (page - 1) * step if paged and page else 0
            count, ids = await _lane_ids(session, owner, owner_facts, lane, statuses, limit, offset, target_role,
                                         reason)
            plans.append((lane, count, ids))
        every = list(dict.fromkeys(i for _, _, ids in plans for i in ids))
        cards, asks = await _cards_and_asks(session, every)
        facts = await _page_facts(session, owner, owner_facts, [cards[i] for i in every if i in cards], asks=asks)
        await db.release_if_free(session)  # the reads are done: the cards are made with no transaction open
        for lane, count, ids in plans:
            column: Column = {"key": lane, "label": board.STAGE_LABELS[lane], "count": count,
                              "cards": [_shape(cards[i], facts) for i in ids if i in cards]}
            if paged and page:
                column["page"], column["pages"] = page, max(math.ceil(column["count"] / step), 1)
            out.append(column)
    return out


async def _settle_order(session: AsyncSession, owner: str, now: datetime) -> None:
    """A reader's session (`db.ReadSession`, which the board's route takes) settles nothing: the lanes are read from
    the stored order as it stands, and the engine brings it up to date when a write has dirtied it (the work queue's
    look, `loop.Dispatcher.settle_order`), so a read of the board never asks for the writer's turn.

    A session that is not a reader's (a caller inside a transaction of its own) has the roles the stored order has not
    caught up with worked out first, in itself, so the lanes show the rows it has flushed."""
    if db.is_reader(session):
        return
    try:
        await stored_order.settle(session, owner, now)
        await db.release_if_free(session)
    except Exception:
        # The stored order is only ever a little behind, so a settle that fails (a lock, a bad row, the turn not given
        # in time) must not fail the board with it: as the hour's tick does, roll back and log, and read the lanes from
        # what is stored.
        log.warning("board: the stored order could not be settled; the lanes are read from what is stored",
                    exc_info=True)
        await session.rollback()


def _shown(lane: str) -> int:
    """How many cards a lane's first load holds: a page (`SHOWN`). Second Impression and Ready to Launch show every
    role: the apply limit caps them (the owner, 2026-10-21)."""
    return WHOLE_COLUMN if lane in (stages.SECOND_IMPRESSION, stages.READY_TO_LAUNCH) else SHOWN


# --- what a call reads ----------------------------------------------------------------------------------------------


@dataclass
class _Facts:
    """What the rows of one call are made from. The owner's facts are read once for the call; the rest for the roles
    being shown. The seam inside this module: a test reaches it only through `row` and `columns`."""

    on: date
    now: datetime
    span: tuple[int, int] | None
    rules: pipeline.QueueRules
    #: The lowest pay the owner's gate checks, or 0 when it checks none.
    floor: int
    #: Each shown role's First Impression estimate, if it has one.
    ranks: dict[uuid.UUID, int] = field(default_factory=dict)
    #: The shown roles the ranker ruled out on a gate, with the line in which it named the gate (`pipeline.ranked`).
    gates: dict[uuid.UUID, str] = field(default_factory=dict)
    #: The roles with a Second Impression, and the ones among them whose read graded something of the posting.
    reads: set[uuid.UUID] = field(default_factory=set)
    graded: set[uuid.UUID] = field(default_factory=set)
    #: The day each role was posted (`freshness.posted_by_role`), the day the gates count.
    posted: dict[uuid.UUID, freshness.PostedDay] = field(default_factory=dict)
    packets: dict[uuid.UUID, NewestPacket] = field(default_factory=dict)
    asks: dict[uuid.UUID, int] = field(default_factory=dict)
    #: The owner's job board hosts (`follow.job_board_hosts`), read by the first lane that has cards and kept on the
    #: owner's facts for the lanes after it.
    boards: frozenset[str] | None = None


async def _owner_facts(session: AsyncSession, owner: str) -> _Facts:
    """The owner's numbers: the day, the score's span, the queue's rules and the pay floor."""
    profile = await profile_store.current(session, owner)
    gates = profile.gates
    floor = (gates.compensation.amount or 0) if gates.checks_pay() else 0
    return _Facts(owner_today(), utc_now(), scoring.bounds(profile), pipeline.queue_rules_of(profile.settings), floor)


async def _page_facts(session: AsyncSession, owner: str, facts: _Facts, rows: list[Opportunity], *,
                      asks: dict[uuid.UUID, int] | None = None) -> _Facts:
    """`facts` with what the rows being shown need. `asks` is the rows' predicted asks when the caller read them along
    with the rows (`_cards_and_asks`); without it they are read here."""
    ids = [r.id for r in rows]
    if rows and facts.boards is None:
        facts.boards = await employer_posting.job_board_hosts(session, owner)
    grades = await analysis.reads_graded(session, ids)
    ranked = await pipeline.ranked(session, owner, ids)
    return replace(
        facts,
        ranks=ranked.estimates, gates=await pipeline.gate_reasons(session, owner, ranked.gate_zeros),
        posted=await freshness.posted_by_role(session, owner, rows, boards=facts.boards),
        reads=set(grades), graded={i for i, graded in grades.items() if graded},
        packets=await _newest_packets(session, ids),
        asks=await _asks(session, ids) if asks is None else asks)


async def _newest_packets(session: AsyncSession, ids: list[uuid.UUID]) -> dict[uuid.UUID, NewestPacket]:
    """Each role's newest packet version: its id, version, status and where it is on its way (`packets.progress_of`,
    for a building bar)."""
    from app.jobsearch.writing import packets  # the packets read much of the job search

    if not ids:
        return {}
    newest = (select(Packet.role_id, func.max(Packet.version).label("version"))
              .where(Packet.role_id.in_(ids)).group_by(Packet.role_id).subquery())
    found = (await session.execute(
        select(Packet.role_id, Packet.id, Packet.version, Packet.status, Packet.brief[packets.STEP_KEY])
        .join(newest, (Packet.role_id == newest.c.role_id) & (Packet.version == newest.c.version))
        .order_by(Packet.role_id, Packet.version.desc()))).all()
    out: dict[uuid.UUID, NewestPacket] = {}
    for role_id, packet_id, version, status, step in found:
        if role_id not in out:
            out[role_id] = {"id": str(packet_id), "version": version, "status": status,
                            "progress": packets.progress_of(status, step if isinstance(step, str) else None)}
    return out


async def _asks(session: AsyncSession, ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    """The predicted ask of each of these roles that has one, read without loading the research itself."""
    if not ids:
        return {}
    ask = Opportunity.analysis["ask"]["ask"].as_integer()
    found = await session.execute(select(Opportunity.id, ask).where(Opportunity.id.in_(ids)))
    return {i: a for i, a in found if a is not None}


# --- the lanes ------------------------------------------------------------------------------------------------------


async def _lane_ids(session: AsyncSession, owner: str, owner_facts: _Facts, lane: str, statuses: tuple[str, ...],
                    limit: int, offset: int, target_role: str | None,
                    reason: str | None) -> tuple[int, list[uuid.UUID]]:
    """One lane's count and its `limit` role ids from `offset`, in its order. The database puts the lane's roles in
    order and gives the page (`stored_order.page`: the number where the work is ahead, the age everywhere); the cards
    and facts of every lane's page are read together by `columns`."""
    where = (Opportunity.owner == owner, Opportunity.status.in_(statuses),
             *([Opportunity.target_role == target_role] if target_role else []),
             *([board.closed_reason_where(reason)] if lane == board.CLOSED and reason else []))
    count = await session.scalar(select(func.count()).select_from(Opportunity).where(*where)) or 0
    ids = await stored_order.page(session, where, queued=set(statuses) <= set(_QUEUED), on=owner_facts.on,
                                  now=owner_facts.now, limit=limit, offset=offset)
    return count, list(ids)


async def _cards(session: AsyncSession, ids: Collection[uuid.UUID]) -> dict[uuid.UUID, Opportunity]:
    """These roles read with a card's columns (`CARD_COLUMNS`), by id: one statement for any number of them. A role
    deleted since the lane was read is not among them."""
    return (await _cards_and_asks(session, ids))[0]


async def _cards_and_asks(session: AsyncSession, ids: Collection[uuid.UUID]) -> tuple[
        dict[uuid.UUID, Opportunity], dict[uuid.UUID, int]]:
    """`_cards`, and in the same statement each role's predicted ask (the one integer of its research, which is not
    loaded itself) for the roles that have one: what `_asks` reads with a statement of its own."""
    if not ids:
        return {}, {}
    ask = Opportunity.analysis["ask"]["ask"].as_integer().label("ask")
    found = (await session.execute(
        select(Opportunity, ask).where(db.among(Opportunity.id, ids)).options(load_only(*CARD_COLUMNS)))).all()
    return {r.id: r for r, _ in found}, {r.id: a for r, a in found if a is not None}


def _posting_day() -> ColumnElement:
    """The posted day of one posting, as `freshness.posted_basis` has it: the employer's date held to the owner's local
    day the app first saw it (`seen_day`, stored with `first_seen` when the posting was written), else that day. The
    earlier of the two, or the one that is there; with neither it is null, and a posting no day dates goes last, as in
    the lanes."""
    posted, seen = JobPosting.posted, JobPosting.seen_day
    return case((posted.is_(None), seen), (seen.is_(None), posted), (posted < seen, posted), else_=seen)


async def _added_links(session: AsyncSession, owner: str, size: int, on: date, target_role: str | None) -> Column:
    """The added links waiting to be read, newest first by the day their cards say (`_shape_posting`: the posting's
    posted day, `freshness.posted_basis`), so the ages go up down the column; of one day, the one the app saw later
    first. A posting belongs to no target role until the analyst matches it, so a column narrowed to a role is empty."""
    if target_role:
        return {"key": board.ADDED_LINKS, "label": board.ADDED_LINKS_LABEL, "count": 0, "cards": []}
    where = role_from_posting.waiting_added_where(owner)
    count = await session.scalar(select(func.count()).select_from(JobPosting).where(*where)) or 0
    day = _posting_day()
    rows = (await session.scalars(
        select(JobPosting).where(*where).options(defer(JobPosting.text))
        .order_by(day.desc().nulls_last(), JobPosting.first_seen.desc(), JobPosting.id).limit(size))).all()
    await db.release_if_free(session)
    return {"key": board.ADDED_LINKS, "label": board.ADDED_LINKS_LABEL, "count": count,
            "cards": [_shape_posting(p, on) for p in rows]}


# --- the row --------------------------------------------------------------------------------------------------------


def _shape(role: Opportunity, facts: _Facts) -> Row:
    """The row, the one builder of a role's: every column, page and answer is made here, from the same facts."""
    day = facts.posted.get(role.id)
    hours = freshness.posting_day_hours(role, facts.on, facts.now, day)
    return {
        "id": str(role.id), "title": role.title, "company": role.company, "status": role.status,
        "status_label": board.LABELS.get(role.status, role.status),
        "fit_pct": scoring.percent_of(role, facts.span),
        # The number shown is the number the list is sorted by (`pipeline.shown`).
        "score": _score(role, facts),
        "next": _next(role, facts),
        "packet": facts.packets.get(role.id),
        "opened_at": iso_or_none(role.go_opened_at),
        "new": pipeline.in_first_hours(hours, facts.rules), "first_hours_bonus": _first_hours_points(role, facts),
        "favorite": role.favorited_at is not None, "favorited_at": iso_or_none(role.favorited_at),
        "work": _work(role.move_type, role.location), "pay": _pay_words(role.comp, role.title),
        "ask": facts.asks.get(role.id),
        "age": _age(role, facts.on, day),
        "ended_reason": role.ended_reason if role.status in stages.ENDED else None, "ended_words": ended.words(role),
        "reason": role.screened_out_reason if role.status == stages.GATED_OUT else None,
        # `url` is the employer's own posting once it is known, else the job board's (employers/follow.py).
        "url": role.url, "apply_url": role.apply_url, "origin": role.origin,
        "employer_id": str(role.employer_id) if role.employer_id else None,
    }


def _score(role: Opportunity, facts: _Facts) -> int | None:
    """The number the role's card shows (`pipeline.shown`)."""
    return pipeline.shown(role, facts.ranks.get(role.id), facts.span, role.id in facts.reads, facts.on, facts.now,
                          facts.posted.get(role.id), facts.rules)


def _shape_posting(p: JobPosting, on: date) -> Row:
    """A waiting posting's row: read-only, with no role behind it (`url` is the posting's own link). Its work, pay and
    age are said by the same words as a role's; its age follows the same rule, the posted day the gates count for the
    one posting (`freshness.posted_basis`): its date when it has one, held to the day the app first saw it, else that
    day."""
    comp = p.comp
    if not (comp or "").strip() and (p.comp_min or p.comp_max):
        low, high = p.comp_min or 0, p.comp_max or 0
        comp = f"{_k(low)}–{_k(high)}" if low and high else _k(low or high)
    day = freshness.posted_basis(None, None, [(p.posted, p.first_seen)])
    days = day.days(on)
    return {
        "id": str(p.id), "title": p.title, "company": p.company, "status": board.ADDED_LINKS,
        "status_label": board.ADDED_LINKS_LABEL, "fit_pct": None, "score": None, "next": None, "packet": None,
        "opened_at": None, "new": False, "first_hours_bonus": 0, "favorite": False, "favorited_at": None,
        "work": _work(p.work_mode, p.location), "pay": _pay_words(comp, p.title), "ask": None,
        "age": {"days": days, "words": _age_words(days), "from": _age_from(day)},
        "ended_reason": None, "ended_words": None, "reason": None, "url": p.url or p.apply_url,
        "apply_url": p.apply_url, "origin": "posting", "employer_id": None,
    }


# --- what a row says: the note --------------------------------------------------------------------------------------


def _next(role: Opportunity, facts: _Facts) -> str | None:
    """What is wrong with the role or waits on the owner, in words, from what the app holds (the first rule that
    matches), then, for a role still in play, a note when its posted pay is mostly under the owner's floor (the pay gate
    keeps a role whose top reaches it). None for the normal run of a stage (docs/PRD.md 24.18). Nothing is guessed. A
    Ready to Launch role Launch has opened says it is opened: what Go does next with the others (skips one, waits for
    its research) is not said of a role that holds its place by being open (`pipeline.rebalance` fixes it there)."""
    ready = role.status == stages.READY_TO_LAUNCH and role.go_opened_at is None
    if ready and role.go_skip:
        words = f"Launch in Browser skips it: {role.go_skip}"
    elif ready and role.analysis_state == analysis.WAITING:
        words = RECHECKING_WORDS
    elif role.status in stages.PRE_READY and role.browser_note:
        words = f"The browser gave up after its tries: {role.browser_note}"
    else:
        words = _by_stage(role, facts)
    if role.status in stages.OPEN and facts.floor:
        low, high = parse_comp_text(role.comp, part_time=is_part_time(role.title, role.comp))
        if band := mostly_below_floor(low, high, facts.floor):
            words = " · ".join(x for x in (words, below_floor_words(band)) if x)
    return words


def _first_hours_points(role: Opportunity, facts: _Facts) -> int:
    """How many points of the role's card number are its first hours' bonus (`pipeline.first_hours_bonus`)."""
    return pipeline.first_hours_bonus(role, facts.ranks.get(role.id), facts.span, role.id in facts.reads, facts.on,
                                      facts.now, facts.posted.get(role.id), facts.rules)


def _by_stage(role: Opportunity, facts: _Facts) -> str | None:
    """What a stage says of a role that is not the normal run of it, else None (docs/PRD.md 24.18)."""
    status, state = role.status, role.analysis_state
    if status == stages.FIRST_IMPRESSION and state is None:
        # The ranker ruled it out on a gate (docs/PRD.md 24.14): the gate, with its reason, as the ranker wrote it.
        return facts.gates.get(role.id)
    if status == stages.SECOND_IMPRESSION or (status in stages.PRE_READY and state is not None):
        if state == analysis.FAILED:
            return "Analyst didn't finish: send it on yourself"
        if state == analysis.DONE and role.id in facts.reads and role.id not in facts.graded:
            return UNGRADED_WORDS
        return None  # waiting for the analyst, for its text, to be read again: the normal run
    if status == stages.READY_TO_LAUNCH:
        return _ready_words(role, facts.packets.get(role.id), facts.now)
    if status in stages.CLOSED:
        return (ended.words(role) or (role.screened_out_reason if status == stages.GATED_OUT else None)
                or board.LABELS.get(status, status))
    return None  # a stage that is the normal run, or one the app has no words for


def _ready_words(role: Opportunity, packet: NewestPacket | None, now: datetime) -> str | None:
    """What a Ready to Launch role says: that Launch opened it (the owner marks it Applied), else what is wrong with its
    packet and what the check of its posting found (`_posting_check_words`); None when neither is."""
    if role.go_opened_at is not None:
        return "Opened. Mark Applied when sent"
    return " · ".join(x for x in (_packet_words(packet), _posting_check_words(role, now)) if x) or None


def _packet_words(packet: NewestPacket | None) -> str | None:
    """The packet's step while it is being written (the building bar's words), and that it failed or is held; None for a
    packet to build or one that is ready, which are the normal run."""
    status = packet["status"] if packet else None
    if status == PacketState.WRITING:
        return (packet["progress"].get("label") if packet else None) or "Packet building now"
    if status == PacketState.FAILED:
        return "The packet failed"
    if status == PacketState.HELD:
        return "The packet is held"
    return None


def _posting_check_words(role: Opportunity, now: datetime) -> str | None:
    """What the check of a Ready to Launch role's posting found, when that is not the normal run (docs/PRD.md 24.16,
    24.18): the page said nothing either way (and Launch checks it again), or the browser could not check it. A posting
    found open, a browser still looking and a role with no check say nothing; a posting that is gone is no longer in
    Ready to Launch, and says so as the role's reason."""
    checked = aware(role.go_checked_at)
    if checked is None or role.go_check not in (go_checks.UNCLEAR, *go_checks.BROWSER_LOOKS):
        return None
    if role.go_check in go_checks.BROWSER_LOOKS:
        return None if now - checked < go_checks.BROWSER_LIFE else "The browser could not check the posting"
    day = checked.astimezone(owner_zone()).date()
    today = now.astimezone(owner_zone()).date()
    return f"Posting not confirmed when checked {'today' if day == today else f'{day:%b} {day.day}'}"


# --- what a row says: work, pay, age --------------------------------------------------------------------------------


def _work_mode(text: str | None) -> Literal["remote", "hybrid", "onsite", "unknown"]:
    """remote, hybrid, onsite or unknown, from a work-mode word or a location line."""
    t = (text or "").lower()
    if "hybrid" in t:
        return "hybrid"
    if "remote" in t:
        return "remote"
    if any(w in t for w in ("onsite", "on site", "on-site", "in person", "in-person", "office")):
        return "onsite"
    return "unknown"


def _work(mode_word: str | None, location: str | None) -> Work:
    """How the work happens, the standard answer an application asks for, from the role's own words: its work-mode word,
    else its location line, and the "City, ST" the location names."""
    mode = _work_mode(mode_word)
    if mode == "unknown":
        mode = _work_mode(location)
    found = _PLACE.search(location or "")
    place = found.group(1).strip() if found else None
    words = _MODES[mode] + (f" · {place}" if place and mode in ("hybrid", "onsite") else "")
    return {"mode": mode, "place": place, "words": words}


def _k(n: int) -> str:
    return f"${round(n / 1000)}K" if n >= 1000 else f"${n}"


def _pay_words(comp: str | None, title: str | None = None) -> str:
    """The pay a card shows: a year's range; a fractional or part-time role's hourly pay as it was written."""
    low, high = parse_comp_text(comp, part_time=is_part_time(title, comp))
    if low and high and low != high:
        return f"{_k(low)}–{_k(high)}"
    if low or high:
        return _k(low or high or 0)
    return "Not posted" if not (comp or "").strip() else (comp or "").strip()[:40]


def _age_words(days: int | None) -> str:
    if days is None:
        return "Unknown"
    return "Today" if days == 0 else f"{days} day{'s' if days != 1 else ''}"


def _age_from(day: freshness.PostedDay) -> Literal["posted", "found"] | None:
    """What an age is counted from, as the page says it: the employer's date is "posted"; the day the app saw or found
    the role is "found"; no day says nothing."""
    return None if day.day is None else "posted" if day.basis == freshness.POSTED else "found"


def _age(role: Opportunity, on: date, posted: freshness.PostedDay | None = None) -> Age:
    """The Age column (the owner, 2026-10-15; 2026-11-04): the days since the role was posted, the days the gates count
    (`freshness.posted_basis`, `posted`; without it, the one the role's own columns give): the employer's date when one
    is known (held to the day the app first saw the posting it is of), else the day the app first saw or found the
    role."""
    day = posted or freshness.posted_basis(role.posted, role.found, ())
    days = day.days(on)
    if days is None:
        return {"days": None, "words": "Unknown", "from": None}
    return {"days": days, "words": _age_words(days), "from": _age_from(day)}
