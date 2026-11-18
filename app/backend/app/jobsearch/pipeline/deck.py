"""How a role moves through the statuses: code moves a role on when its stage
finishes, and this module is the one place that says when.

**Getting ready** (`advance`, `gate`): Followup needed (the posting has not been read, or a read failed) → Landing Zone
(its raw copy is held) → Cleansed Data (made plain) → First Impression (the Haiku rank, `rank.py`). A role only ever
moves forward here, and one the owner moved by hand stays where they put it.

**One list, one number per role** (`rebalance`, the owner, 2026-10-21 and 2026-11-06, docs/PRD.md 24.15): First
Impression, Second Impression and Ready to Launch are cuts of one list sorted by one number. A role's number is 0 until
it is ranked, its newest Haiku rank as the ranker wrote it once it is ranked, and its queue score from Sonnet's read
(`scoring.queue_percent`: the owner's share of the graded requirements' match, the rest their other asks) once it is
read, so a newer read replaces the rank even when it is lower and the role drops. There is no map between the two: the
rank is the rank. Second Impression holds only roles Sonnet has read, the next N after Ready to Launch by that number
(N is the owner's Second Impression size, `Settings.second_impression_size`, else `DAILY_DEFAULT`). Sonnet reads from
the top of First Impression (`line.startable`: a role marked `line.READ_NEXT`, which stays in First Impression
while it is read): the best unread roles by the number that beat the weakest on deck, or any while the deck is not full.
A read role enters Second Impression when it beats the weakest or there is room, and the lowest goes back to First
Impression showing its number. Ready to Launch is the top M read roles (M is the owner's Ready to Launch size,
`Settings.ready_to_launch_size`, else `DAILY_DEFAULT`): a newly read role that beats the weakest takes its place and
the displaced role drops to Second Impression. The two sizes are the owner's, each its own (2026-10-28). Unread roles
at the top of the list hold no Ready place until Sonnet has read them. There is no lowest score for Ready to Launch: it
is the top M read roles, whatever they score (the owner, 2026-11-06). No role without a Second Impression that graded
its posting holds a Ready place, whoever put it there (the owner, 2026-11-03, docs/PRD.md 24.13): a read that graded
nothing never takes one on its number (`reaches`), and a role the owner asked for there before it was read takes its
place once it is (`favorites.place_asked`, `_Plan.placed`).
A role a gate closes, at any stage, leaves; a role at an employer applied to within the gap takes no place; a role Go
opened keeps its Ready place, and a favorite (`favorites.py`) takes one once it is read and holds it while it is
starred. A role is never closed on its score. Nothing builds a packet for a Ready role until the owner presses Go
(`apply_go`) or Build now (`packets`), or stars it (a favorite runs First and Second Impression first, and its packet
is asked once it is read).

**A board role is looked up first** (the owner, 2026-11-05, docs/PRD.md 32.3): an unread board role holds
the board's text, which ranks it, and no employer posting, and Sonnet reads the employer's. So the walk down the best
estimates (`_decide`) gives a board role with no source no read place: it marks the ones it passes before the places
are full, at most the deck's size at once, for their employer's posting to be looked up (`models.SOURCE_NEXT`, which
makes the browser's source item: `models.wants_sourcing`), and the read places go to the roles below them. A role
becomes read next (`READ_NEXT`) only once it is Sourced. A role the browser gave up on (its note is set) is out of the
walk and the count until the note clears, and no new lookup starts while the board is stopped. Roles from other boards
are marked to be read as before. A board role whose employer's posting could not be read (its site refused the app,
the posting is gone, the lookup found none) is given the board's copy as its description by the step before the stages
(the board's copy hook, the owner, 2026-11-05) and Sourced to the board's posting, so it is no lookup but an
ordinary Sourced role, read like any other.

**A Haiku gate zero is zero** (the owner, 2026-11-04, docs/PRD.md 24.14): the ranker scores a posting that clearly fails
a hard ask 0 and starts its why with "Gate:" (`rank.gate_zero`). That role's number is Haiku's own 0 (`ranked`), the
first hours' bonus does not lift it (`points`), and its card says the ranker's line (`row._by_stage`). Sonnet never
picks it (`_decide`: `Ranked.gate_zeros` is the bar, since while Second Impression is not full any ranked unread role
is a challenger; a role still marked to be read when the ranker's gate came in loses its mark), and it is closed by
nothing: it keeps its place in First Impression. The owner's hand lifts a role out of the bar: one the owner starred,
asked for in Ready to Launch, had Launch open or ever moved by hand is an ordinary role again, its Haiku rank as it
is, and is read because the owner asked, not by the walk; so is a role that is applied or closed.

**Stopping the old wait** (`stop_waiting`): a role waiting for the analyst that is not on deck, and that the owner
did not
ask for, is taken out of the line.

**A role that comes into Ready to Launch is checked at once** (the owner, 2026-11-06, docs/PRD.md 24.16;
`check_ready_entries`, the step after the queue is balanced): whichever way it came in (the deck, a star, a hand move),
its posting is checked as Go's check does it, at any hour (is it still open, and is the employer's own page still the
posting held), the result shows on the role, and a role whose posting is gone leaves Ready to Launch with its reason.

**The order** (`order`) is the number (`points`), plus the owner's golden bonus in a role's first golden hours, less
the owner's stale penalty once the posting is more than their stale days old (counted from the day the gates count,
`freshness.posted_basis`; 20 points, 72 hours, 21 points and 30 days until they set them, `QueueRules`), so an old
posting drops below fresher roles of a similar number. Ties go to the younger. The card prints the same number
(`shown`: the order's number as it is), so the list is sorted by exactly the number the card shows (the owner,
2026-11-06): a new role scored 70 shows 90, and one scored 95 shows 115.
A role with no score is 0 and takes no seat. Every stage works the newest roles first, and the order is the same in the
lanes the page shows.

Services flush; the caller commits."""

import contextlib
import math
import uuid
from collections.abc import Awaitable, Callable, Collection, Iterator
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, NamedTuple

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from app import db
from app.jobsearch import as_read, off_turn, stages
from app.jobsearch.employers import core as employers
from app.jobsearch.fetch import host_stops
from app.jobsearch.fit import freshness, scoring
from app.jobsearch.gating import check as gating
from app.jobsearch.gating import sift, stated_place
from app.jobsearch.models import (
    SOURCE_NEXT,
    Opportunity,
    RoleRank,
    is_on_followed_board,
    is_read_first,
    source_next_marked,
)
from app.jobsearch.pipeline import built_out, favorites, lifecycle, placement
from app.jobsearch.pipeline import from_posting as role_from_posting
from app.jobsearch.postings import cleansed
from app.jobsearch.postings import text as posting_text
from app.jobsearch.postings.coop import Cooperate
from app.jobsearch.profile import store as profile_store
from app.jobsearch.profile.schema import (
    GOLDEN_BONUS,
    GOLDEN_HOURS,
    RESEARCH_START_MULTIPLE,
    STALE_DAYS,
    STALE_PENALTY,
    Settings,
    employer_places_of,
)
from app.jobsearch.research import analysis, line, staleness
from app.jobsearch.research import rank as ranker
from app.jobsearch.work import queue
from app.jobsearch.work.queue import coverage
from app.sites import registry
from app.timeutil import aware, owner_today, utc_now
from app.trail import service as trail

#: How many roles Second Impression and Ready to Launch each hold when the owner sets none (`Settings`).
DAILY_DEFAULT = 15
# The queue's numbers are the owner's settings (`Settings.stale_days`, `stale_penalty`, `golden_hours`, `golden_bonus`
# and `research_start_multiple`, the owner, 2026-10-28; docs/PRD.md 39.32). `STALE_DAYS` and the others, imported above
# from `profile`, are only what each is until the owner sets it: 30 days, 21 points, 72 hours, 20 points and 3 times.
# A posting older than `stale_days` (since its posted day, the one the gates count: `freshness.posted_basis`) is stale
# and loses `stale_penalty` points in the order; the default is one point more than the 20 within which a fresh role
# wins, so a stale role sorts below every fresher role within 20 points of it. `GOLDEN_HOURS` and `GOLDEN_BONUS` are
# the first-hours pair (`in_first_hours`).


class QueueRules(NamedTuple):
    """The numbers the order and the reading line run with, each the owner's setting (`queue_rules`)."""

    stale_days: int = STALE_DAYS
    stale_penalty: int = STALE_PENALTY
    golden_hours: int = GOLDEN_HOURS
    golden_bonus: int = GOLDEN_BONUS
    research_start_multiple: int = RESEARCH_START_MULTIPLE
    #: The most roles of an employer that hold a place on the deck and in Ready to Launch together, by employer key
    #: (`Settings.employer_places`); an employer with no pair has no such limit.
    employer_places: tuple[tuple[str, int], ...] = ()


#: The numbers as they are until the owner sets any.
DEFAULT_RULES = QueueRules()


def queue_rules_of(settings: Settings) -> QueueRules:
    """The queue's numbers out of the owner's Settings."""
    places = {employers.key_of(name): n for name, n in employer_places_of(settings.employer_places)}
    return QueueRules(settings.stale_days, settings.stale_penalty, settings.golden_hours, settings.golden_bonus,
                      settings.research_start_multiple, tuple(sorted((k, n) for k, n in places.items() if k)))


async def queue_rules(session: AsyncSession, owner: str) -> QueueRules:
    """The owner's queue numbers, as of their newest Settings version."""
    return queue_rules_of((await profile_store.current(session, owner)).settings)


#: What a role's score is: the Second Impression's queue score, once its line-by-line read is done, else the Haiku rank.
FIT, RANK = "fit", "rank"


class StageSizes(NamedTuple):
    """How many roles the deck (Second Impression) and Ready to Launch each hold."""

    deck: int
    ready: int


def sized(value: int | None) -> int:
    """A stage size the owner set, or `DAILY_DEFAULT` when they set none (blank, 0 or less)."""
    return value if value is not None and value > 0 else DAILY_DEFAULT


async def stage_sizes(session: AsyncSession, owner: str) -> StageSizes:
    """The owner's two sizes (`Settings.second_impression_size`, `ready_to_launch_size`), each `DAILY_DEFAULT` when
    unset. A version saved with the one daily number it used to be reads as that number for both."""
    settings = (await profile_store.current(session, owner)).settings
    return StageSizes(sized(settings.second_impression_size), sized(settings.ready_to_launch_size))


class _Newest(NamedTuple):
    """A role's newest Haiku rank: its fit, whether it is a gate zero (`rank.gate_zero`), and whether the role is an
    ordinary one that a gate zero speaks for: still being got ready (`stages.PRE_READY`) and not one the owner has
    starred, asked for in Ready to Launch or had Launch open."""

    role_id: uuid.UUID
    fit: int
    gate: bool
    ordinary: bool


def _of_newest_version(*where):
    """The join that keeps a role's ranks for the newest short profile: each role's highest `profile_version` among the
    ranks `where` names (a `max()` grouped by role, which the engine reads off `ix_js_role_ranks_owner_role`), joined
    back to the ranks. A role ranked on two copies under that version still has two rows: `_one_a_role` takes the
    latest."""
    latest = (select(RoleRank.role_id, func.max(RoleRank.profile_version).label("version")).where(*where)
              .group_by(RoleRank.role_id).subquery("latest"))
    return latest, and_(latest.c.role_id == RoleRank.role_id, latest.c.version == RoleRank.profile_version)


def _one_a_role(rows) -> list:
    """The first row of each role, in the order the rows came (the caller sorts each role's newest first): the newest
    of a role's ranks for the newest profile. A tie on the time is broken by the rank's id, so the answer never shifts
    between two reads."""
    first: dict[uuid.UUID, Any] = {}
    for row in rows:
        first.setdefault(row[0], row)
    return list(first.values())


async def _newest(session: AsyncSession, owner: str, ids: Collection[uuid.UUID] | None = None) -> list[_Newest]:
    """Each role's newest Haiku rank (0 to 100): the one for the newest short profile, the latest of those. `ids`
    narrows it to these roles (a row asks for its own one)."""
    ordinary = and_(Opportunity.status.in_(stages.PRE_READY), Opportunity.favorited_at.is_(None),
                    Opportunity.ready_asked_at.is_(None), Opportunity.go_opened_at.is_(None))
    where = [RoleRank.owner == owner]
    if ids is not None:
        where.append(db.among(RoleRank.role_id, ids))
    latest, newest_version = _of_newest_version(*where)
    rows = await off_turn.rows_in_slices(session, (
        select(RoleRank.role_id, RoleRank.fit, ranker.gate_zero(), ordinary)
        .join(Opportunity, Opportunity.id == RoleRank.role_id).join(latest, newest_version).where(*where)
        .order_by(RoleRank.role_id, RoleRank.created_at.desc(), RoleRank.id)))
    return [_Newest(*row) for row in _one_a_role(rows)]


class Ranked(NamedTuple):
    """What the Haiku ranks say of the roles asked about: each ranked role's estimate, and the roles the ranker ruled
    out on a gate (a gate zero, `rank.gate_zero`) that count as zero."""

    estimates: dict[uuid.UUID, int]
    gate_zeros: set[uuid.UUID]


async def ranked(session: AsyncSession, owner: str, ids: Collection[uuid.UUID] | None = None) -> Ranked:
    """Each ranked role's estimate: its newest Haiku rank, as the ranker wrote it (the owner, 2026-11-06, docs/PRD.md
    24.15: there is no map between the ranker's scale and Sonnet's), the number of a role not read yet. A **gate zero**
    is Haiku's own 0 (the owner, 2026-11-04, docs/PRD.md 24.14: the ranker said the posting fails a hard ask), and
    `gate_zeros` names the ones that stay barred from a read: an ordinary role, still being got ready that the owner has
    not touched. A role the owner starred, asked for in Ready to Launch, had Launch open or ever moved by hand
    (`placement.moved_by_hand`: they chose to pursue it, whatever the ranker thought) and a role that is applied or
    closed are not in it, so they are read like any other (their number is still the rank's 0). `ids` narrows it to
    these roles."""
    newest = await _newest(session, owner, ids)
    gate_zeros = {n.role_id for n in newest if n.gate and n.ordinary}
    if gate_zeros:
        gate_zeros -= await placement.moved_by_hand(session, owner)
    return Ranked({n.role_id: n.fit for n in newest}, gate_zeros)


async def ranks(session: AsyncSession, owner: str, ids: Collection[uuid.UUID] | None = None) -> dict[uuid.UUID, int]:
    """Each ranked role's estimate (`ranked`), without the question of which ones a gate zero bars."""
    return {n.role_id: n.fit for n in await _newest(session, owner, ids)}


async def ranked_ids(session: AsyncSession, owner: str) -> set[uuid.UUID]:
    """The roles that have a Haiku rank: the keys of `ranks` for the owner, without the rank's number (which needs the
    newest version of each role's ranks to be found). A rank row of a role that is gone has no role to move."""
    return set(await off_turn.scalars_in_slices(session, select(RoleRank.role_id).join(
        Opportunity, Opportunity.id == RoleRank.role_id).where(RoleRank.owner == owner).distinct()))


async def gate_reasons(session: AsyncSession, owner: str, ids: Collection[uuid.UUID]) -> dict[uuid.UUID, str]:
    """The line of why in which the ranker named the gate, for each of these roles whose newest rank is a gate zero
    (`rank.gate_line`): what a card says of a role the ranker ruled out. Only the why of those is read."""
    if not ids:
        return {}
    where = (RoleRank.owner == owner, RoleRank.role_id.in_(ids))
    latest, newest_version = _of_newest_version(*where)
    rows = await session.execute(
        select(RoleRank.role_id, RoleRank.why, ranker.gate_zero()).join(latest, newest_version).where(*where)
        .order_by(RoleRank.role_id, RoleRank.created_at.desc(), RoleRank.id))
    return {role_id: ranker.gate_line(why) for role_id, why, gate in _one_a_role(rows.all()) if gate}


def is_stale(role: freshness.Dated, on: date, rules: QueueRules = DEFAULT_RULES,
             posted: freshness.PostedDay | None = None) -> bool:
    """Whether the role's posting is more than `rules.stale_days` old, counted from the day the gates count
    (`freshness.posted_basis`: `posted` is the role's `PostedDay`, read by `posted_by_role`; without it, the one the
    role's own columns give). A role no day dates is not stale."""
    days = (posted or freshness.posted_basis(role.posted, role.found, ())).days(on)
    return days is not None and days > rules.stale_days


def score(role: freshness.Dated, rank: int | None, span: tuple[int, int] | None,
          read: bool) -> tuple[int | None, str | None]:
    """The number the order sorts a role by and what it is: its Second Impression's queue score
    (`scoring.queue_percent`: the owner's share of match, the rest their other asks) when `read` says it has one, else
    its Haiku rank as it stands, else none. A role that `read` says has a Second Impression is an `Opportunity` loaded
    with its fit, breakdown and target role: no other role's score reads them."""
    if read:
        pct = scoring.queue_percent(role, span)
        if pct is not None:
            return pct, FIT
    if rank is not None:
        return rank, RANK
    return None, None


def has_score(role: freshness.Dated, rank: int | None, span: tuple[int, int] | None, read: bool) -> bool:
    """Whether the role has a number of its own (`score`): a queue score or a rank. A role with none is 0 in the order
    and takes no seat (a read that has no score and no rank is not a role to seat)."""
    return score(role, rank, span, read)[0] is not None


def in_first_hours(hours: int | None, rules: QueueRules = DEFAULT_RULES) -> bool:
    """Whether a role is under the owner's golden hours old (`rules.golden_hours`), as `freshness.posting_day_hours`
    counts them (whole days, the Age the card says). A role nothing dates is not. The one rule the number's bonus
    (`points`) and a row's `new` flag read."""
    return hours is not None and hours < rules.golden_hours


def _stale_loss(role: freshness.Dated, on: date, rules: QueueRules, posted: freshness.PostedDay | None) -> int:
    """The points the owner's stale penalty takes off a role: `rules.stale_penalty` for a stale posting, else none."""
    return rules.stale_penalty if is_stale(role, on, rules, posted) else 0


def _first_hours_bonus(role: freshness.Dated, source: str | None, number: int, on: date, now: datetime,
                       posted: freshness.PostedDay | None, rules: QueueRules) -> int:
    """The points the owner's golden bonus adds to a role's number: `rules.golden_bonus` in its first golden hours, none
    for a rank of 0 (a gate zero is Haiku's own 0, and a hard no is not lifted off the bottom by a new posting)."""
    lifted = (in_first_hours(freshness.posting_day_hours(role, on, now, posted), rules)
              and not (source == RANK and number == 0))
    return rules.golden_bonus if lifted else 0


def first_hours_bonus(role: freshness.Dated, rank: int | None, span: tuple[int, int] | None, read: bool, on: date,
                      now: datetime, posted: freshness.PostedDay | None = None,
                      rules: QueueRules = DEFAULT_RULES) -> int:
    """How many points of a role's number (`points`) are its first hours' bonus: 0 once the hours are over, for a rank
    of 0 and for a role with no score. What the New tag says of it, New (+20) (the row's `first_hours_bonus`), so the
    number on a card and the tag beside it agree."""
    number, source = score(role, rank, span, read)
    if number is None:
        return 0
    return _first_hours_bonus(role, source, number, on, now, posted, rules)


def points(role: freshness.Dated, rank: int | None, span: tuple[int, int] | None, read: bool, on: date, now: datetime,
           posted: freshness.PostedDay | None = None, rules: QueueRules = DEFAULT_RULES) -> int:
    """A role's number, the one the order sorts by and the card shows (`shown`, as it is): its score (`score`), plus the
    first hours' bonus (`first_hours_bonus`), less the stale penalty, never below 0. 0 with no score. The bonus and the
    penalty both count from the role's posted day (`posted`: `freshness.posted_basis`). Not capped at 100: a role over
    it is sorted by the number its card prints, and two over it are apart."""
    number, source = score(role, rank, span, read)
    if number is None:
        return 0
    bonus = _first_hours_bonus(role, source, number, on, now, posted, rules)
    return max(0, number + bonus - _stale_loss(role, on, rules, posted))


def shown(role: freshness.Dated, rank: int | None, span: tuple[int, int] | None, read: bool, on: date, now: datetime,
          posted: freshness.PostedDay | None = None, rules: QueueRules = DEFAULT_RULES) -> int | None:
    """The number a role's card and page show (docs/PRD.md 24.11, 24.15): the order's number (`points`: the score, the
    first hours' bonus and the stale penalty), as it is, so the list is sorted by exactly the number the card shows and
    a new role scored 70 shows 90 (the owner, 2026-11-06; the first build kept it to 100 on the card and left two roles
    over 100 apart by a number the cards did not show). None with no score: the card then has the role's fit as a share
    of what the owner's asks allow, if it has one."""
    if not has_score(role, rank, span, read):
        return None
    return points(role, rank, span, read, on, now, posted, rules)


def age_key(role: freshness.Dated, on: date, now: datetime, posted: freshness.PostedDay | None = None) -> tuple:
    """A role's place by its age alone: the younger posting first (`freshness.posting_hours`: the days since its posted
    day, the one the gates count, as hours; the hour only where a day count cannot decide, a day that is the day the
    app first saw a posting; a role nothing dates is last), then the more recently found, then by id so the order never
    shifts between two reads. The tail of the queue's key (`order`) and the whole key of a lane where the work is not
    ahead (`role_row`), with no first-hours bonus."""
    hours = freshness.posting_hours(role, on, now, posted)
    found = -(aware(role.created_at) or datetime.min.replace(tzinfo=UTC)).timestamp()
    return (hours if hours is not None else math.inf, found, str(role.id))


def order[R: freshness.Dated](roles: list[R], rank_of: dict[uuid.UUID, int], on: date, now: datetime,
                              posted: dict[uuid.UUID, freshness.PostedDay] | None = None,
                              span: tuple[int, int] | None = None, read: set[uuid.UUID] | None = None,
                              rules: QueueRules = DEFAULT_RULES) -> list[R]:
    """`roles`, best first (`read`: the ids with a Second Impression, from `analysis.reads`): by `points`, then by
    `age_key`. `posted` is each role's posted day (`freshness.posted_by_role`)."""
    def key(r: R) -> tuple:
        day = (posted or {}).get(r.id)
        return (-points(r, rank_of.get(r.id), span, r.id in (read or ()), on, now, day, rules),
                *age_key(r, on, now, day))

    return sorted(roles, key=key)


async def ordered[R: freshness.Dated](session: AsyncSession, owner: str, roles: list[R],
                                      posted: dict[uuid.UUID, freshness.PostedDay] | None = None) -> list[R]:
    """`roles` in the order (`order`), with the ranks, reads and posted days read for these roles alone (`posted`: the
    caller's, when it has read them): a few roles are never ordered by reading every rank of a large library, as
    Go's list of Ready to Launch and the analyst's line were. The one place the order's inputs are read for a list of
    roles: Go's list (`board.ready_line`) and the analyst's line ask here, so a new input to the order is added once;
    the lanes of the board are read in the same order from what is stored (`stored_order`, docs/PRD.md 24.19)."""
    if not roles:
        return []
    ids = [r.id for r in roles]
    if posted is None:
        posted = await freshness.posted_by_role(session, owner, roles)
    current = await profile_store.current(session, owner)
    return order(roles, await ranks(session, owner, ids), owner_today(), utc_now(), posted, scoring.bounds(current),
                 await analysis.reads(session, ids), queue_rules_of(current.settings))


# --- getting ready -------------------------------------------------------------------------------------------------


async def advance(session: AsyncSession, owner: str) -> int:
    """Put every role being got ready in the stage its facts show (`lifecycle.stage_of`, the one rule): its cleansed
    copy is made first when it has text and no copy, and a rank or a Second Impression makes it First Impression. A
    role the owner moved by hand stays while it is where they put it, as does one Launch opened (`placement`).
    Returns how many moved."""
    async with off_turn.reading(session) as reader:  # the ranks of every role: read before `settle` takes any turn
        has_rank = await ranked_ids(reader, owner)
    return await lifecycle.settle(session, owner, has_rank)


async def gate(session: AsyncSession, owner: str) -> int:
    """The hard gates on stated facts, at Cleansed Data: every role there is checked on its cleansed copy (the
    copy of the posting the owner's rule picks, `gate_copy`: the employer, the places and way of working and the pay
    band: `gating.role_subject`; the days since the role was posted:
    `freshness.posted_by_role`), and one that clearly fails a gate is closed, Gated out with the gate's reason
    (`gating.stop_role`). An unknown passes. Only a gate closes a role here, never its score. A role the owner moved by
    hand stays (`placement`, `Purpose.GATES`). A role in Ready to Launch whose copy states a place or a way of working
    other than the role's own is judged on its place the same turn (`stated_place.decide_on`): a changed copy of its
    posting is a place input like any other, and the turn runs when a copy was made. Returns how many were closed."""
    # Read, decide, then write (as `lifecycle.settle`): every read and the whole judgement are made apart from the
    # writer's turn (`off_turn.reading`; the reads are long), and
    # only the roles to be closed or judged on their place are locked, as they were read (`lifecycle.STANDS`), and
    # written.
    async with off_turn.reading(session) as reader:
        roles = (await reader.scalars(select(Opportunity).where(
            Opportunity.owner == owner, Opportunity.status.in_((stages.CLEANSED_DATA, *stated_place.DECIDED_STAGES)))
            .options(defer(Opportunity.description)))).all()
        if not roles:
            return 0
        places = await placement.load(reader, owner, roles, placement.Purpose.GATES)
        # Only the copies of the roles at Cleansed Data, not every role's (loading every whole text each turn to gate a
        # handful is slow), and the choice of which copy to read is made for those roles only (not
        # for every role that has one: a second a round); a role in Ready
        # to Launch reads its own (a few, one small query each). The same roles' posted days
        # (`freshness.posted_by_role`), which only the gates on a Cleansed Data role read.
        new = [r for r in roles if r.status == stages.CLEANSED_DATA]
        copies = {}
        days = {}
        if new:
            current = await cleansed.gate_copy_ids_for(reader, owner, [r.id for r in new])
            held = await cleansed.copies(reader, owner, [current[r.id] for r in new if r.id in current])
            copies = {r.id: held[current[r.id]] for r in new if current.get(r.id) in held}
            days = await freshness.posted_by_role(reader, owner, new)
        rules: gating.Rules | None = None
        # In role order: a role to close on a gate (with its stop), or a Ready to Launch role whose copy moves its place
        # (with the copy, and no stop).
        plan: list[tuple[Opportunity, gating.Stop | None, Any]] = []
        coop = Cooperate()
        for role in roles:
            await coop()
            if places[role.id].held:
                continue
            if role.status in stated_place.DECIDED_STAGES:
                copy = await cleansed.gate_copy_of(reader, owner, role.id)
                if stated_place.copy_moves_place(role, copy):
                    plan.append((role, None, copy))
                continue
            rules = rules or await gating.rules(reader, owner)
            employer = rules.index.of(role.employer_id, role.company)
            stop = gating.check(gating.role_subject(role, employer, copies.get(role.id), days.get(role.id)), rules)
            if stop is not None:
                plan.append((role, stop, None))
        if any(stop is None for _, stop, _ in plan):
            rules = rules or await gating.rules(reader, owner)
    if not plan or rules is None:
        return 0
    locked = await as_read.lock(session, Opportunity, [read for read, _, _ in plan], stands=lifecycle.STANDS,
                                defer=(Opportunity.description,))
    closed = 0
    for read, stop, copy in plan:
        if (role := locked.rows.get(read.id)) is None:
            continue
        if stop is None:
            closed += await stated_place.decide_on(session, owner, role, copy, rules,
                                                   cause="The posting's copy changed")
            continue
        why = gating.stop_role(role, stop, rules)
        await trail.record(session, owner, event="gated_at_cleansed", actor="app", who=trail.APP_WHO,
                           link=("opportunity", role.id),
                           sentence=f"{role.title} at {role.company} failed a gate on what its posting states: {why}")
        closed += 1
    await session.flush()
    return closed


# --- the deck and Ready to Launch ----------------------------------------------------------------------------------


def _in_progress(role: Opportunity) -> bool:
    """Whether a session has taken the role's read: it keeps its deck place until it finishes."""
    return role.analysis_state == analysis.WAITING and role.analysis_claimed_at is not None


async def marked_ids(session: AsyncSession, owner: str) -> set[uuid.UUID]:
    """The roles carrying the mark that says the analyst reads them next (`line.READ_NEXT`)."""
    return set(await session.scalars(select(Opportunity.id).where(Opportunity.owner == owner,
                                                                  line.marked_next())))


async def source_marked_ids(session: AsyncSession, owner: str) -> set[uuid.UUID]:
    """The roles carrying the mark that says their employer's posting is looked up next (`SOURCE_NEXT`)."""
    return set(await session.scalars(select(Opportunity.id).where(Opportunity.owner == owner, source_next_marked())))


async def _marked_again(session: AsyncSession, roles: Collection[Opportunity]) -> set[uuid.UUID]:
    """Which of these roles hold a done research already marked to be researched again, read without loading their
    research."""
    done = [r.id for r in roles if r.analysis_state == analysis.DONE]
    if not done:
        return set()
    return set(await session.scalars(select(Opportunity.id).where(Opportunity.id.in_(done), line.marked_again())))


def _mark_key(state: str | None, again: bool) -> str | None:
    """What the queue puts on a role's analysis to have it read, or None where it has nowhere to go: a role not
    researched or waiting is marked to be read next (`line.READ_NEXT`), a done older research is marked to be read
    again (`analysis.REANALYSE`; `again`: it already is), and a role whose read failed or that holds no text takes none.
    `_decide` plans a mark only where this names a key and `_apply` writes the same key, so a queue turn never locks a
    role it has nothing to put on."""
    if state == analysis.DONE:
        return None if again else analysis.REANALYSE
    return line.READ_NEXT if state in (None, analysis.WAITING) else None


@dataclass
class _Snapshot:
    """What `rebalance` read, to decide from with no transaction open."""

    deck_size: int
    ready_size: int
    qrules: QueueRules
    span: tuple[int, int] | None
    rank_of: dict[uuid.UUID, int]
    #: The roles the ranker ruled out on a gate (`ranked`): Sonnet reads none of them unless the owner asked.
    gate_zeros: set[uuid.UUID]
    roles: list[Opportunity]
    today: date
    now: datetime
    stands: placement.Inputs
    #: The day each role was posted (`freshness.posted_by_role`), the day the gates count.
    posted: dict[uuid.UUID, freshness.PostedDay]
    #: How many roles wait at Cleansed Data for their rank.
    unranked: int
    #: The roles that carry the mark that says the analyst reads them next.
    marks: set[uuid.UUID]
    #: The done roles already marked to be researched again (`analysis.REANALYSE`).
    again: set[uuid.UUID]
    #: The roles that carry the mark that says their employer's posting is looked up next (`SOURCE_NEXT`).
    source_marks: set[uuid.UUID]
    #: Whether the board is stopped (`host_stops`): no lookup starts there while it is.
    board_stopped: bool


class _Move(NamedTuple):
    """A role the queue puts in another place, and the history line that says so."""

    role: Opportunity
    was: str
    target: str
    event: str
    sentence: str


class _Mark(NamedTuple):
    """A role the queue puts on the analyst's line, or on the browser's to have its employer's posting looked up (`on`,
    with the history line that says so), or takes off it. `key` is the mark: `line.READ_NEXT` (put on as the role
    is when it is locked: `_mark_key`) or `SOURCE_NEXT`. Putting one on takes the other off, so a board role is
    never in both lines."""

    role: Opportunity
    on: bool
    sentence: str
    key: str = line.READ_NEXT


@dataclass
class _Plan:
    """What the queue does: the favorites it lifts, the roles it moves, and the marks it puts on and takes off. Each
    role is the instance as it was read: `_apply` locks them on `lifecycle.STANDS`."""

    lifts: list[Opportunity]
    #: The roles the owner asked for in Ready to Launch before they were read, that are read now: their move is made
    #: (`favorites.place_asked`).
    placed: list[Opportunity]
    moves: list[_Move]
    marks: list[_Mark]


async def _deck_roles(session: AsyncSession, owner: str, light: bool = False) -> list[Opportunity]:
    """The roles in First Impression, Second Impression and Ready to Launch (not their research or text), built a slice
    at a time with the loop given back between. `light` also leaves out the reader's answers and what the analysis was
    made under, which the queue's decision never reads (a reader's roles are detached after the snapshot); the roles a
    caller's own transaction goes on to settle places for are read whole."""
    skipped = [defer(Opportunity.gate_answers), defer(Opportunity.analysis_made_under)] if light else []
    return await off_turn.scalars_in_slices(session, select(Opportunity).where(
        Opportunity.owner == owner,
        Opportunity.status.in_((stages.FIRST_IMPRESSION, stages.SECOND_IMPRESSION, stages.READY_TO_LAUNCH)))
        .options(defer(Opportunity.analysis), defer(Opportunity.description), *skipped))


async def _snapshot(session: AsyncSession, owner: str) -> _Snapshot | None:
    """Everything the queue is decided from, read, and the transaction ended: None with no role in First Impression,
    Second Impression or Ready to Launch. A role that holds only a count of places takes the places its postings name
    (and is judged on them at once): that is written here, ahead of the reads that need it.

    Read apart from the writer's turn (`off_turn.reading`; the reads would hold it for seconds). The places the postings
    name are written first, a batch of roles to a transaction (`stated_place.PLACES_BATCH`: the first turn after a
    restore may write thousands of them in one), and the
    queue is then read apart. In a caller's open transaction everything is read and written in it, as it always was."""
    async with off_turn.reading(session) as reader:
        if reader is not session:
            roles = await _deck_roles(reader, owner, light=True)
            if not roles:
                return None
            naming = await _naming_places(reader, owner, roles)
            if not naming:
                return await _read_snapshot(reader, owner, roles)
    if reader is not session:
        await _take_places(session, owner, naming)
        async with off_turn.reading(session) as reader:
            roles = await _deck_roles(reader, owner, light=reader is not session)  # a role gated out on its place
            # has left these stages
            return await _read_snapshot(reader, owner, roles) if roles else None
    snapshot = await _read_snapshot(session, owner, None)
    await db.release_if_free(session)
    return snapshot


async def _take_places(session: AsyncSession, owner: str, role_ids: list[uuid.UUID]) -> None:
    """`stated_place.settle_places` for these roles, which writes them a batch to a transaction (`PLACES_BATCH`)."""
    step = as_read.STATEMENT_ROWS
    roles: list[Opportunity] = []
    for at in range(0, len(role_ids), step):
        roles += (await session.scalars(select(Opportunity).where(
            Opportunity.owner == owner, Opportunity.id.in_(role_ids[at:at + step]))
            .options(defer(Opportunity.analysis), defer(Opportunity.description)))).all()
    await stated_place.settle_places(session, owner, roles)
    await db.release_if_free(session)


async def _naming_places(session: AsyncSession, owner: str, roles: list[Opportunity]) -> list[uuid.UUID]:
    """The ids of the roles `stated_place.settle_places` would give the places their postings name (it asks what that
    function asks, `stated_place.naming_places`, so a turn that has nothing to write is read apart from the writer's
    turn)."""
    return list(await stated_place.naming_places(session, owner, roles))


async def _read_snapshot(session: AsyncSession, owner: str, known: list[Opportunity] | None) -> _Snapshot | None:
    """The reads of `_snapshot`, from `session` (a reader's, or the caller's with its writes). `known` are the roles
    already read, else they are read here."""
    deck_size, ready_size = await stage_sizes(session, owner)
    qrules = await queue_rules(session, owner)
    span = scoring.bounds(await profile_store.current(session, owner))
    ranks_now = await ranked(session, owner)
    roles = known if known is not None else await _deck_roles(session, owner)
    if not roles:
        return None
    if known is None:  # read in the caller's transaction, where the places are written
        # A role that holds only a count of places takes the places its postings name, and is judged on them at once.
        for placed in await stated_place.settle_places(session, owner, roles):
            if placed.status == stages.GATED_OUT:
                roles.remove(placed)
    today, now = owner_today(), utc_now()
    stands = await placement.gather(session, owner, roles, placement.Purpose.QUEUE, today=today)
    posted = await freshness.posted_by_role(session, owner, roles)
    unranked = await session.scalar(select(func.count()).select_from(Opportunity).where(
        Opportunity.owner == owner, Opportunity.status == stages.CLEANSED_DATA)) or 0
    marks = await marked_ids(session, owner)
    again = await _marked_again(session, roles)
    source_marks = await source_marked_ids(session, owner)
    # Asked of the record each turn, so a stop another process made (the hand-run command's) holds it too
    board_stopped = False
    for domain in _followed_board_domains():
        board_stopped = await host_stops.refresh(session, owner, domain, now) is not None or board_stopped
    return _Snapshot(deck_size, ready_size, qrules, span, ranks_now.estimates, ranks_now.gate_zeros, roles, today, now,
                     stands, posted, unranked, marks, again, source_marks, board_stopped)


def _followed_board_domains() -> tuple[str, ...]:
    """The domains a followed board's lookup is stopped under (`host_stops.refresh`): the boards whose roles are
    followed to the employer's own site (a runbook's `follow_to_employer`)."""
    return tuple(dict.fromkeys(host_stops.domain_of(h) for r in registry.current().runbooks
                               if r.kind == "board" and r.follow_to_employer for h in r.hosts))


def _is_lookup(role: Opportunity) -> bool:
    """Whether the role is one whose employer's posting is looked up before it is read: in First Impression, found on
    the board and not Sourced. Its text is the board's, which ranks it, and there is no employer posting for Sonnet to
    read yet."""
    return role.status == stages.FIRST_IMPRESSION and role.sourced_at is None and is_on_followed_board(role)


def _given_up(role: Opportunity) -> bool:
    """Whether the browser gave up looking for the role's employer posting (three tries used, a stop, a time-out: each
    leaves a note on the role, `Opportunity.browser_note`, and its card says it). Such a role is neither a challenger
    nor counted among the lookups: it stays in First Impression with its estimate and the note, as a Haiku gate zero
    does, and rejoins when the note clears (its stuck item dropped, or the role Sourced)."""
    return _is_lookup(role) and bool(role.browser_note)


def _may_look_up(role: Opportunity) -> bool:
    """Whether the deck may mark the role for its lookup: nobody has it in the analyst's line, and the owner has not
    asked for it (a star, a move to Ready to Launch before it was read: its sourcing is theirs, handed out at any
    hour, `queue._wanted`), so it is not in the walk."""
    return role.analysis_state is None and not is_read_first(role)


def _over_places(snap: _Snapshot, contenders: list[Opportunity], seated: list[Opportunity], read_ids: set[uuid.UUID],
                 pts: Callable[[Opportunity], int], scored: Callable[[Opportunity], bool]) -> dict[uuid.UUID, str]:
    """The contenders that compete for no place because their employer is at its own limit
    (`Settings.employer_places`, the owner, 2026-11-17), each with the words its history line says. An employer the
    owner named has at most that many roles on the deck and in Ready to Launch together: the roles fixed there
    (`seated`: starred, opened, placed by hand) count first, and the best read roles by number take what is left. An
    unread role is read only while it could take one of those places (one is open, or its estimate beats the weakest
    read role that holds one), the limit's number of them at a time, so the analyst goes on to other employers. A
    role a session is reading is left to it. No role is closed: the rest wait in First Impression."""
    limits = dict(snap.qrules.employer_places)
    if not limits:
        return {}
    held: dict[str, int] = {}
    for r in seated:
        key = employers.key_of(r.company)
        held[key] = held.get(key, 0) + 1
    groups: dict[str, list[Opportunity]] = {}
    for r in contenders:
        key = employers.key_of(r.company)
        if key in limits:
            groups.setdefault(key, []).append(r)
    out: dict[uuid.UUID, str] = {}
    for key, group in groups.items():
        limit = limits[key]
        room = max(limit - held.get(key, 0), 0)
        read = order([r for r in group if r.id in read_ids and scored(r)], snap.rank_of, snap.today, snap.now,
                     snap.posted, snap.span, read_ids, snap.qrules)
        keep = {r.id for r in read[:room]}
        floor = min((pts(r) for r in read[:room]), default=None) if len(read) >= room else None
        reading = [r for r in group if r.id not in read_ids and _in_progress(r)]
        keep |= {r.id for r in reading}
        if room:
            waiting = order([r for r in group if r.id not in read_ids and not _in_progress(r) and r.id in snap.rank_of
                             and (floor is None or pts(r) > floor)],
                            snap.rank_of, snap.today, snap.now, snap.posted, snap.span, rules=snap.qrules)
            keep |= {r.id for r in waiting[:max(limit - len(reading), 0)]}
        words = f"your limit of {limit} at {group[0].company} is held by better roles there"
        out.update({r.id: words for r in group if r.id not in keep})
    return out


def _decide(snap: _Snapshot, places: dict[uuid.UUID, placement.Standing] | None = None) -> _Plan:
    """The queue worked out from what `_snapshot` read, and nothing asked of the database: the order, the read pool,
    the Ready and deck cuts, the reading line and the marks, as the moves to make. See `rebalance` for the rules.
    `places` are the roles' standings when the caller worked them out in slices (`placement.standing_async`)."""
    deck_size, ready_size, qrules, span, rank_of = snap.deck_size, snap.ready_size, snap.qrules, snap.span, snap.rank_of
    today, now, posted, roles = snap.today, snap.now, snap.posted, snap.roles
    if places is None:
        places = placement.standing(snap.stands, roles, placement.Purpose.QUEUE)
    read_ids = {i for i, p in places.items() if p.read}
    graded_ids = {i for i, p in places.items() if p.graded}

    def pts(r: Opportunity) -> int:
        return points(r, rank_of.get(r.id), span, r.id in read_ids, today, now, posted.get(r.id), qrules)

    def scored(r: Opportunity) -> bool:
        return has_score(r, rank_of.get(r.id), span, r.id in read_ids)

    # A favorite holds a Ready to Launch place while it is starred and read (`favorites.py`): one still below it goes up
    # now. One not read yet runs its impressions first (the owner, 2026-10-28) and competes like any other role.
    starred = [r for r in roles if places[r.id].held is placement.Hold.STAR]
    # A role the owner asked for in Ready to Launch before it was read takes its place as soon as it has a read that
    # graded its posting (`favorites.place_asked`), and holds it there as any role the owner moved by hand does.
    asked = [r for r in roles if r.ready_asked_at is not None and r.status in stages.PRE_READY
             and r.id in graded_ids and places[r.id].held is not placement.Hold.STAR]
    asked_ids = {r.id for r in asked}
    # The places that are taken: a star, a role Go opened and a role the owner placed at Ready count toward the Ready
    # size, once each; an opened role keeps its Ready place and its seat until it is Applied (by the owner, or by the
    # mail sync's confirmation) or closes (PRD 24.20); a role the owner placed on deck counts toward the deck's. A role
    # Launch skipped stays in Ready to Launch with its reason but holds no place: the next role moves up in its stead
    # (the owner, 2026-10-22).
    fixed_ready = [r for r in roles if places[r.id].seat == stages.READY_TO_LAUNCH or r.id in asked_ids]
    fixed_deck = [r for r in roles if places[r.id].seat == stages.SECOND_IMPRESSION]
    free = [r for r in roles if places[r.id].free and r.id not in asked_ids]
    # A place the owner's own data gave (an imported tracker's Ready or on-deck role), or one whose read is under way,
    # stays until that read is done (`stays`): it has nothing to compete on yet. A role with no place to take
    # (`off_deck`: its employer was applied to within the gap, a Must waits for a fact, its work location cannot be
    # decided) competes for none, whatever its score, and is kept, not stopped; that outranks a weak hold.
    contenders = [r for r in free if places[r.id].competes]
    # An employer the owner gave a limit of its own: the roles there past it compete for no place and are not read.
    over = _over_places(snap, contenders, [*fixed_ready, *fixed_deck], read_ids, pts, scored)
    contenders = [r for r in contenders if r.id not in over]

    # The read roles, best first: the best that are current take Ready (a Ready role that is beaten gives way), the next
    # N are on deck. A read role with no score and no rank (`scored`) is not a role to seat.
    read_pool = order([r for r in contenders if r.id in read_ids and scored(r)],
                      rank_of, today, now, posted, span, read_ids, qrules)
    ready_cap = max(ready_size - len(fixed_ready), 0)
    # A read whose inputs have changed since still stands on its number until the new read replaces it
    # (`mark_for_reads`). A role fixed in Ready (starred, opened, placed by the owner) is not in this pool. No role
    # takes a Ready place on a read that graded nothing of its posting (the owner, 2026-11-03): it waits on deck for a
    # read that did. Unread roles, whatever they rank, are not in the pool: they hold no place until Sonnet has read
    # them.
    reaches = [r for r in read_pool if r.id in graded_ids]
    ready = reaches[:ready_cap]
    ready_ids = {r.id for r in ready}
    read_pool = [r for r in read_pool if r.id not in ready_ids]
    deck_cap = max(deck_size - len(fixed_deck), 0)
    deck = read_pool[:deck_cap]
    contenders = [r for r in contenders if r.id not in ready_ids]
    full = len(deck) >= deck_cap
    weakest_fit = min((pts(r) for r in deck), default=None) if full else None

    # Sonnet reads from the top of First Impression: the best numbers that beat the weakest on deck, N at a time.
    # While most roles still wait for their rank no new read starts: the best of the few ranked so far is not yet the
    # best of all, so no Sonnet read is spent early.
    first = sum(1 for r in roles if r.status == stages.FIRST_IMPRESSION)
    waiting = bool(snap.unranked) and first < qrules.research_start_multiple * deck_size
    taken = [r for r in contenders if _in_progress(r) and r.id not in read_ids]  # a session has them: they stay queued
    # A role the ranker ruled out on a gate is never picked for a read (`ranked`): the owner's hand, a star or a move,
    # lifts it out of the rule and it competes like any other. A role with a rank has a number (`scored`).
    challengers = order([r for r in contenders if r.id in rank_of and r.id not in read_ids and not _in_progress(r)
                         and (weakest_fit is None or pts(r) > weakest_fit)
                         and r.id not in snap.gate_zeros and not _given_up(r)],
                        rank_of, today, now, posted, span, rules=qrules)
    # The walk down the best estimates. A role with an employer posting to read (Sourced, or from another board) takes a
    # read place; a board role with none takes none and, if the walk passes it before the places are full, is marked
    # for its lookup, at most the deck's size of them at once (a role the browser gave up on is out of the walk and out
    # of the count; one Sourced is a read like any other). No new lookup starts while most roles wait for their rank,
    # as no read does, or while the board is stopped; the ones already marked go on.
    room = 0 if waiting else max(deck_size - len(taken), 0)
    queue: list[Opportunity] = []
    looking: list[Opportunity] = []
    for r in challengers:
        if len(queue) >= room and not waiting:
            break
        if not _is_lookup(r):
            if not waiting:
                queue.append(r)
        elif (len(looking) < deck_size and _may_look_up(r)
              and (r.id in snap.source_marks or not (waiting or snap.board_stopped))):
            looking.append(r)
    queued = {r.id for r in (*taken, *queue)}
    looking_ids = {r.id for r in looking}
    # A lookup the browser gave up on keeps its mark: its item is stuck with the note, and taking the mark off would
    # drop the item and clear the note, and the role would be looked up again for ever.
    parked = {r.id for r in roles if r.id in snap.source_marks and _given_up(r)}
    taken_ids = {x.id for x in taken}
    deck_ids = {r.id for r in deck}

    plan = _Plan(lifts=[r for r in starred if r.status in stages.PRE_READY], placed=asked, moves=[], marks=[])
    for r in free:
        if places[r.id].stays:
            continue
        if r.id in ready_ids:
            target = stages.READY_TO_LAUNCH
        elif r.id in deck_ids:
            target = stages.SECOND_IMPRESSION
        else:
            target = stages.FIRST_IMPRESSION
        if r.status == target:
            continue
        if target == stages.READY_TO_LAUNCH:
            event = "ready_to_launch"
            sentence = (f"{r.title} at {r.company} is read line by line and among the best "
                        f"{ready_size} read roles by number ({pts(r)}): Ready to Launch.")
        elif target == stages.SECOND_IMPRESSION:
            event = "on_deck"
            sentence = (f"{r.title} at {r.company} is on deck: its number of {pts(r)} is among the next "
                        f"{deck_size} read roles.")
        else:
            why = places[r.id].off_deck or over.get(r.id) or (
                "it has no Second Impression that graded its posting"
                if r.status == stages.READY_TO_LAUNCH and r.id not in graded_ids
                else "better read roles hold the places above it")
            event = "off_deck" if r.status == stages.SECOND_IMPRESSION else "back_on_deck"
            sentence = (f"{r.title} at {r.company} is back in First Impression"
                        f"{' showing its number' if r.id in read_ids else ''}: {why}.")
        plan.moves.append(_Move(r, r.status, target, event, sentence))

    # The mark that says the analyst reads a role next goes on the queue and off every other role. A role that holds it
    # and is marked for its lookup instead loses it as that mark goes on.
    new_lookups = {r.id for r in looking if r.id not in snap.source_marks}
    for r in free:
        if r.id in queued and r.id not in snap.marks and r.id not in taken_ids:
            if _mark_key(r.analysis_state, r.id in snap.again) is not None:
                plan.marks.append(_Mark(r, True, f"{r.title} at {r.company} is next to be read: its estimate of "
                                                 f"{pts(r)} is among the best waiting in First Impression."))
        elif r.id in snap.marks and r.id not in queued and r.id not in new_lookups:
            plan.marks.append(_Mark(r, False, ""))
    # The mark that says a board role's employer posting is looked up next goes on the roles the walk passed and off
    # every other (a role Sourced meanwhile takes the read mark above, which takes this one off as it goes on).
    read_on = {m.role.id for m in plan.marks if m.on}
    for r in looking:
        if r.id not in snap.source_marks:
            plan.marks.append(_Mark(r, True, f"{r.title} at {r.company}: its employer's posting is being looked up "
                                             f"first. Its estimate of {pts(r)} is among the best waiting in First "
                                             "Impression, and it is read once the posting is found.", SOURCE_NEXT))
    for r in roles:
        if r.id in snap.source_marks and r.id not in looking_ids and r.id not in parked and r.id not in read_on:
            plan.marks.append(_Mark(r, False, "", SOURCE_NEXT))
    return plan


async def _apply(session: AsyncSession, owner: str, plan: _Plan) -> tuple[int, int]:
    """Write the plan: the roles it changes locked and found as they were read, the favorites lifted, the roles moved
    with their history lines, the marks put on and taken off. Nothing at all for an empty plan. Returns what
    `rebalance` returns."""
    roles = {r.id: r for r in (*plan.lifts, *plan.placed, *(m.role for m in plan.moves), *(m.role for m in plan.marks))}
    if not roles:
        return 0, 0
    held = await as_read.lock(session, Opportunity, list(roles.values()), stands=lifecycle.STANDS,
                              defer=(Opportunity.analysis, Opportunity.description))
    to_ready = moved = 0
    # The roles were read apart (`_snapshot`), so they are written as the lock gave them: the rows of `held`.
    for read in plan.lifts:
        if (r := held.rows.get(read.id)) is not None:
            to_ready += await favorites.to_ready(session, owner, r)
    for read in plan.placed:
        if (r := held.rows.get(read.id)) is not None:
            to_ready += await favorites.place_asked(session, owner, r)
    for move in plan.moves:
        if (row := held.rows.get(move.role.id)) is None:
            continue
        row.status = move.target
        to_ready += stages.READY_TO_LAUNCH in (move.was, move.target)
        moved += stages.READY_TO_LAUNCH not in (move.was, move.target)
        await trail.record(session, owner, event=move.event, actor="app", who=trail.APP_WHO,
                           link=("opportunity", row.id), sentence=move.sentence)
    for mark in plan.marks:
        if (r := held.rows.get(mark.role.id)) is None:
            continue
        await session.refresh(r, ["analysis"])
        kept = r.analysis or {}
        if not mark.on:
            r.analysis = {k: v for k, v in kept.items() if k != mark.key}
            moved += 1
            continue
        if mark.key == SOURCE_NEXT:
            # Sourced meanwhile, or taken by a session or asked for by the owner: nothing to look up.
            if r.sourced_at is not None or r.analysis_state is not None or is_read_first(r):
                continue
            line.put_mark(r, line.Mark.SOURCE_NEXT)
            moved += 1
            await trail.record(session, owner, event="source_next", actor="app", who=trail.APP_WHO,
                               link=("opportunity", r.id), sentence=mark.sentence)
            continue
        # A done older research that is no Second Impression is read again; the mark may have gone on it since the read.
        key = _mark_key(r.analysis_state, analysis.REANALYSE in kept)
        if key is None:
            continue
        line.put_mark(r, line.Mark(key))
        moved += 1
        await trail.record(session, owner, event="read_next", actor="app", who=trail.APP_WHO,
                           link=("opportunity", r.id), sentence=mark.sentence)
    await session.flush()
    if to_ready or moved:
        # The moves are in: end the transaction, so the writer's turn is free when `stop_waiting` begins. It runs in a
        # transaction of its own, and a turn that finds it busy leaves nothing behind that the next turn, which runs
        # it first, does not take.
        await db.release_if_free(session)
        await stop_waiting(session, owner)  # a role no longer to be read leaves the analyst's line
    return to_ready, moved


async def rebalance(session: AsyncSession, owner: str) -> tuple[int, int]:
    """One list, one number per role (the owner, 2026-10-21, 2026-11-06; docs/PRD.md 24.15): First Impression, Second
    Impression and Ready to Launch are cuts of one list sorted by one number (`points`: 0 until ranked, the newest Haiku
    rank as written, Sonnet's queue score once read, with the golden-hours bonus and the stale penalty), and every run
    puts each role where that number says.

    The statuses are buckets of how much diligence a role has had: unread (First Impression), read, and the top read
    roles. **Ready to Launch** is the top M read roles (the owner's Ready to Launch size): a newly read role that beats
    the weakest takes its place and the displaced role drops to Second Impression. There is no lowest score for it. A
    read that graded nothing of its posting takes no Ready place however it scores (`analysis.graded_read`), and unread
    roles at the top of the list wait for their read. **Second Impression** is the next N read roles (the owner's Second
    Impression size), the on-deck list; anything below goes to First Impression, showing its number when it was read.
    **Sonnet reads from the top of First Impression**: the unread roles with the highest numbers that beat the weakest
    Second Impression number (any while it is not full), at most N at a time; a role marked
    to be read (`line.READ_NEXT`) or being read stays in First Impression.
    A role Go opened keeps its Ready place and its seat until it is Applied (by the owner, or by the mail sync's
    confirmation) or closes (24.20); a favorite that is read keeps its place and counts toward M, one that is not
    read yet runs its impressions first (`favorites.advance`); a role the owner moved by hand stays while it is where
    they put it, and one code has moved
    on since competes like any other (the owner,
    2026-10-23); which roles are fixed, which stay, and which take no place and why (an employer applied to within the
    gap, a Must that waits for a fact, a work location that cannot be decided) is `placement`'s (`Purpose.QUEUE`); a
    gate closes a role at any stage (`gate_facts`), never a score.

    **Three parts, so no transaction is open while the queue is worked out** (a turn that read, worked the queue out in
    Python in the same transaction and then wrote would hold it for seconds): `_snapshot` reads everything the decision
    needs (and writes the one thing it must have first, the places a role's postings name) and ends the transaction;
    `_decide` works out the queue from that alone, with no query; `_apply` reads the roles it changes again in id order
    (`as_read.lock`: a plain read, there is no row lock; the writer's turn holds the database) and writes only a role
    still as it was read (`lifecycle.STANDS`: its status, and whether a session has taken its read):
    a role the owner moved or an analyst took meanwhile is left for the next turn.

    Returns how many roles changed place at Ready to Launch, and how many in the rest of the queue."""
    snapshot = await _snapshot(session, owner)
    if snapshot is None:
        return 0, 0
    places = await placement.standing_async(snapshot.stands, snapshot.roles, placement.Purpose.QUEUE)
    return await _apply(session, owner, _decide(snapshot, places))


async def _current(session: AsyncSession, role: Opportunity, now: staleness.Now) -> bool:
    """Whether the role's research is a Second Impression made under the inputs in force now: it counts
    (`analysis.counts`) and no master line it cites, sentence rule, ask or analyst instruction it was made under has
    changed since."""
    return analysis.counts(role) and not await staleness.analysis_reasons(session, role, now)


async def mark_for_reads(session: AsyncSession, owner: str) -> int:
    """A role on deck, or in Ready to Launch and not opened, whose research is not a Second Impression made under the
    inputs in force (the old full analysis, or research under an older master or analyst) is marked to be read again
    (`analysis.REANALYSE`; its old packets are kept); one in Ready to Launch keeps its place on its fit until the new
    read replaces it. Returns how many were marked."""
    roles = (await session.scalars(select(Opportunity).where(
        Opportunity.owner == owner, Opportunity.analysis_state == analysis.DONE,
        Opportunity.status.in_((stages.SECOND_IMPRESSION, stages.READY_TO_LAUNCH)))
        .order_by(Opportunity.id).execution_options(populate_existing=True))).all()
    if not roles:
        return 0
    now = await staleness.Now.load(session, owner)
    places = await placement.load(session, owner, roles, placement.Purpose.REREAD)
    marked = 0
    for role in roles:
        if places[role.id].held or await _current(session, role, now):
            continue
        if analysis.REANALYSE not in (role.analysis or {}):
            role.analysis = {**(role.analysis or {}), analysis.REANALYSE: True}
            await trail.record(session, owner, event="read_again", actor="app", who=trail.APP_WHO,
                               link=("opportunity", role.id),
                               sentence=f"{role.title} at {role.company} is read line by line again on deck: its "
                                        "earlier research is not a Second Impression made under today's inputs.",
                               detail="Its packets are kept.")
            marked += 1
    await session.flush()
    return marked


async def _settle_limit(session: AsyncSession, owner: str) -> tuple[int, int]:
    """Hold the deck roles at an employer with two open applications and return the held ones where room is back
    (`employer_limit.settle`), before the deck is filled. Returns how many were held and how many returned."""
    from app.jobsearch.pipeline import employer_limit  # it reads the stages this module names

    done = await employer_limit.settle(session, owner)
    return done.held, done.returned


async def check_ready_entries(session: AsyncSession, owner: str) -> int:
    """Check, at once and at any hour, the posting of each role that came into Ready to Launch since it was last checked
    (the owner, 2026-11-06, docs/PRD.md 24.16: by the deck, a star or a hand move): is it still open, and is the
    employer's own page still the posting held (`go_checks.check_entered`). A role whose posting is gone leaves Ready
    to Launch with its reason; the result shows on the role. Returns how many were checked."""
    from app.jobsearch.browser import go_checks  # it reads the stages this module names

    return await go_checks.check_entered(session, owner)


async def employers_for_deck(session: AsyncSession, owner: str) -> int:
    """Research, at once and at any hour, the employers of the roles on deck that have none, or none within the Research
    employers mission's again-after days (the owner, 2026-10-21: employer research is part of going on deck)."""
    from app.jobsearch.missions import core as missions  # it reads this module's statuses
    from app.jobsearch.research import employer as employer_research  # they read this module's statuses

    # Planned on a reader (no turn); the turn judges again only the employers it found, so one gated, taken off the deck
    # or researched meanwhile is skipped, and takes no statement at all when none needs research.
    async with off_turn.reading(session) as reader:
        days = int((await missions.standing(reader, owner, missions.RESEARCH)).settings["again_after_days"])
        ids = await employer_research.deck_research_ids(reader, owner, days)
    if not ids:
        return 0
    return await employer_research.queue_for_deck(session, owner, days, only=ids)


async def stop_waiting(session: AsyncSession, owner: str) -> int:
    """Take out of the analyst's line every role that is not on deck and that the owner did not ask for: Sonnet reads
    the top N only. A role a session has taken is left to finish, and so is one that was researched before and sent
    back for new research (taking it out would drop its done state and it would never be researched again). Returns how
    many left the line."""
    roles = (await session.scalars(select(Opportunity).where(
        Opportunity.owner == owner, line.Standing.where(line.Standing.FREE),
        Opportunity.status != stages.SECOND_IMPRESSION,
        ~line.marked_next())
        .order_by(Opportunity.id).execution_options(populate_existing=True))).all()
    # `line.OFF_DECK` spares the roles a Ready to Launch role waits for the analyst because Go sent its stale research
    # back: taking it out would leave it unwritten and asked again at every Go. A role
    # researched before is back for new research (`requeue.reanalyse_stale`), so those are not taken out either.
    return len(await line.leave_many(session, roles, line.OFF_DECK))


#: How many rounds of the steps after the filter a turn makes at most: each round can move roles the one before made
#: movable (a gate lets a role go to the deck, a deck move asks a read), and a round that moves nothing ends the turn.
ROUNDS = 3

#: What a round can move without another round being owed (`_stages`). A round is followed by another to see whether
#: what it moved made something else movable: what a step moves can only be seen by the steps that run after it in the
#: same round, and by the ones before it in the next. `advance` is the first step: every other step of the round saw
#: the roles it placed, and a role it placed is placed by `stage_of` of its facts, which a second `advance` finds
#: already so. The roles the employer limit holds leave the deck (they are Gated out), which no step before the limit
#: reads. So a round whose only moves are these is the last. (A second round after that moved nothing and cost
#: as much as the first.) A role the limit returns to First Impression is not here: `favorites`, which runs before the
#: limit, asks the read of a starred one.
SETTLED_IN_ROUND = ("advanced", "held")

#: The account a turn gives of itself to the caller that asked for one (`accounting`). The queue's handler asks, since a
#: turn it ran stopped short at its cap must post itself again: the old loop's next turn, thirty seconds later, took
#: what was left, and a turn run from to-dos has no clock to do it.
_account: ContextVar[dict[str, bool] | None] = ContextVar("pipeline_account", default=None)


@contextlib.contextmanager
def accounting() -> Iterator[dict[str, bool]]:
    """While the block runs, in this task, the turn (`run_in_steps`, `schedule.read_gate_facts`) says into the account
    whether it stopped short: `more` is set when something the turn does is left undone by a cap, so another turn is
    owed. The signature of the turn is not changed by it."""
    account = {"more": False}
    token = _account.set(account)
    try:
        yield account
    finally:
        _account.reset(token)


def note_more() -> None:
    """The turn stopped short at a cap: say so to the caller that is keeping an account (`accounting`), if any."""
    account = _account.get()
    if account is not None:
        account["more"] = True


async def run(session: AsyncSession, owner: str) -> dict[str, Any]:
    """Every stage's turn, until nothing moves: a posting nobody has filtered meets the code gates first (the night's
    read does it too, but a read cut short before its filter step left them unseen by every stage), then the postings
    that passed the gates become roles (a batch, newest first:
    `role_from_posting`), a role holding only a snippet takes its posting's whole text (`posting_text.take_whole`),
    the roles are advanced, those at Cleansed Data meet the hard gates,
    the old wait is stopped, the best researched roles go up to Ready to Launch and the deck is filled behind them, and
    a role that came into Ready to Launch has its posting checked. Returns what moved. All in the caller's transaction;
    the app's own turn runs it step by step (`run_in_steps`)."""

    async def same(step: Callable[[AsyncSession], Awaitable[Any]]) -> Any:
        return await step(session)

    return await _stages(owner, same)


async def run_in_steps(owner: str, excluded: frozenset[str] = frozenset()) -> dict[str, Any]:
    """`run`, each step in a transaction of its own, committed before the next begins: the pipeline's own turn
    (`schedule.run_turn`). One transaction for the whole turn held every role it locked (the roles at every stage,
    `FOR UPDATE`) until its last step was done, and the hand-outs and tool calls that needed one of them queued behind
    it. Each step is whole on its own, so a turn cut short is finished by the next. `excluded` names the steps that
    something else does (`sift`, `make_roles`: the work queue's dispatcher does them from their own to-dos,
    `schedule.QUEUE_STEPS`); with none named every step runs."""
    async def fresh(step: Callable[[AsyncSession], Awaitable[Any]]) -> Any:
        async with db.session_factory()() as session:
            out = await step(session)
            await session.commit()
        return out

    return await _stages(owner, fresh, recorded=True, excluded=excluded)


async def _stages(owner: str, within: Callable[[Callable[[AsyncSession], Awaitable[Any]]], Awaitable[Any]],
                  recorded: bool = False, excluded: frozenset[str] = frozenset()) -> dict[str, Any]:
    """The steps of `run`, each run `within` a session (the caller's, or one of its own). With `recorded` (the app's own
    turn) each step is also compared with the to-dos posted for its topic (`queue.coverage`): a change made with no
    to-do posted is logged, which is how a writer nobody wired is found.
    `excluded` names the steps left out (`sift`, `make_roles`)."""
    done = {"made": 0, "advanced": 0, "gated": 0, "employers": 0, "deck": 0, "ready": 0, "stopped": 0, "marked": 0,
            "held": 0, "starred": 0, "built_out": 0, "board_copy": 0, "ready_check": 0}
    left_to_queue = excluded  # the steps something else does (the work queue's dispatcher) are left out

    async def covered(step: str, topic: str, call: Callable[[AsyncSession], Awaitable[Any]]) -> Any:
        window = coverage.begin(owner, step) if recorded else None
        out = await within(call)
        if window is not None:
            await coverage.judge(owner, step, topic, window, coverage.counted(out))
        return out

    done["stopped"] = await covered("stop_waiting", queue.DECK_DIRTY, lambda s: stop_waiting(s, owner))

    async def sift_all(s: AsyncSession) -> dict:
        return await sift.sift(s, owner, await profile_store.current(s, owner))

    if "sift" not in left_to_queue:
        await covered("sift", queue.POSTING_SIFT, sift_all)
    if "make_roles" not in left_to_queue:
        done["made"] = await covered(
            "make_roles", queue.POSTING_ROLES, lambda s: role_from_posting.make_roles(s, owner))
    await covered("take_whole", queue.ROLE_SETTLE, lambda s: posting_text.take_whole(s, owner))
    # A role whose employer's own posting cannot be read is given the board's copy of it where its board page was read
    # (the owner, 2026-11-05): the board's copy hook. Before the next step, which marks a role that holds no text.
    board_copy = registry.board_reader("copy")
    if board_copy is not None:
        done["board_copy"] = await covered(
            "board_copy", queue.ROLE_SETTLE, lambda s: board_copy.advance(s, owner))
    # A role the owner applied to or is in talks about waits for its posting's text if it holds none (the owner,
    # 2026-11-05): `pipeline/built_out.py`.
    done["built_out"] = await covered("built_out", queue.ROLE_SETTLE, lambda s: built_out.advance(s, owner))
    for _ in range(ROUNDS):
        moved = {"advanced": await covered("advance", queue.ROLE_SETTLE, lambda s: advance(s, owner)),
                 "gated": await covered("gate", queue.ROLE_SETTLE, lambda s: gate(s, owner)),
                 "employers": await covered("employers_for_deck", queue.EMPLOYER_RESEARCH,
                                            lambda s: employers_for_deck(s, owner)),
                 "starred": await covered("favorites", queue.ROLE_SETTLE, lambda s: favorites.advance(s, owner)),
                 "marked": await covered("mark_for_reads", queue.DECK_DIRTY, lambda s: mark_for_reads(s, owner)),
                 }
        held, returned = await covered("employer_limit", queue.EMPLOYER_LIMIT, lambda s: _settle_limit(s, owner))
        moved["held"] = held + returned
        moved["ready"], moved["deck"] = await covered("rebalance", queue.DECK_DIRTY, lambda s: rebalance(s, owner))
        # A role that just came into Ready to Launch (here, by a star, by the owner's hand) has its posting checked.
        moved["ready_check"] = await covered("ready_check", queue.DECK_DIRTY, lambda s: check_ready_entries(s, owner))
        for key, count in moved.items():
            done[key] += count
        if not returned and not any(count for key, count in moved.items() if key not in SETTLED_IN_ROUND):
            break
    else:
        note_more()  # the last round moved something that is followed by a round: it is owed to the next turn
    return done
