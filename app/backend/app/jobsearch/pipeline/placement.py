"""Where a role is allowed to sit: the one place that says which roles code must leave where they are, and why.

A role can be put somewhere that code leaves alone: the owner moved it by hand, Go opened it, the owner starred it and
it is read, Go skipped it, the owner's own data placed it, a read of it is under way. Each of those used to be restated
by the code that has to honour it, each with its own idea of the statuses it applies at, and the choices drifted. This
module holds them as one table, `HONOURED`, read by the purpose of the question (`Purpose`): the stage rule, the
best-of queue, a read that is out of date, a drop for a Must that waits, the employer limit, the gates. `load` answers
for many roles in a few statements, `one` for a role. `load` is two halves: `gather` reads what the answer needs from
the database and `standing` decides from that with no query, for a caller that ends its transaction between them (the
queue's turn: `deck.rebalance`). It is read-only: it never writes a status, a history line or a
flag, and never flushes or commits (a query autoflushes, so a status written earlier in the same transaction counts).
The answer is about the status the role has when asked: ask again after a status write, never cache across one.

**The table, and the owner decisions in it.** A cell is the statuses at which a purpose honours a hold; no cell, no
honour. The differences between rows are today's behaviour, visible side by side, not accidents to even out.

- `MOVED`: the owner moved the role on the board, at any time (docs/PRD.md 23.8: a move is the owner's hand, and no
  re-run of the gates moves the role from where they put it; 39.14: no re-run moves it again). Never spent. Only the
  gates read it, so a role the owner pulled out of Gated out is not stopped again by the gate that stopped it, even once
  code has moved it on (pursue-anyway).
- `PLACED`: the owner moved the role and it is still where they put it (the owner, 2026-10-23: once code has moved a
  hand-moved role on, the move is spent and the role goes through the stages and the deck like any other). The stages,
  the queue, an old read, a drop and the limit read this one.
- `OPENED`: Go opened the role (`Opportunity.go_opened_at`, which nothing clears): it keeps its Ready to Launch place
  until it is applied or closed, and counts toward the Ready size. A read that is out of date and a drop for a waiting
  Must leave it alone at any status.
- `STAR`: the owner starred it and it has a Second Impression that graded its posting, before the application (the
  owner, 2026-10-23; the owner, 2026-10-28: a star not read yet holds nothing, it runs its impressions first;
  docs/PRD.md 51.3). `starred` is the one definition. In the queue it holds a Ready to Launch place wherever the role
  sits, and the queue lifts it there.
- `SKIPPED`: Go skipped it (`go_skip`) and it sits in Ready to Launch: held with its reason, but it takes no place, the
  next role moves up (the owner, 2026-10-22).
- `OWN`, `READING`: the owner's own data placed it at the top (an imported tracker, a job they added), or a read of it
  is under way; its place stands until that read is done (`catch_up.py`). Weak holds: a bar (below) outranks them.

The statuses are `C` cleansed_data, `F` first_impression, `S` second_impression, `R` ready_to_launch; `ANY` is every
status. `Hold` order is the precedence: the hold a role has is the first row that applies and is honoured for the
purpose, so a star beats Go having opened the role, and that beats a hand move.

**A place in Ready to Launch stands on a read that graded the posting** (the owner, 2026-11-03, docs/PRD.md 24.13): for
the stage rule and the queue, `STAR`, `OPENED`, `PLACED` and `SKIPPED` hold a role at Ready to Launch only while it
has a Second Impression that graded something of its posting (`Standing.graded`, `analysis.graded_read`;
`NEEDS_GRADED_READ`, `READY_NEEDS_READ`). A role with no read, a read that graded nothing, or no text of its posting
holds no place there whatever else holds it, and the stage rule moves it to the stage its facts give. Every other
status, the other purposes and the weak holds (`OWN`, `READING`, which stand until their read is done) are as they were.

**The seat** (the queue only): which status's places the role takes one of. A star and Go having opened a role take
a Ready place (the queue lifts a role that sits lower: `favorites.to_ready`) and keep it until the role is Applied or
closes (PRD 24.20); a hand move takes a place of the status the role is in when that is Second Impression or Ready to
Launch, else none; a skipped role, an own-data or reading hold and a MOVED hold take none.

**The bars** (the queue and the drop only): a role no strong hold keeps (`STAR`, `OPENED`, `PLACED` and `SKIPPED` are
strong: never barred, and a load of nothing but those reads no rules) can still be given no place, and
`Standing.off_deck` says why, as the words of its history line. Three bars, in order, the gap being the only reason
when it applies: the **gap** (the queue only: the role's employer was applied to within the owner's number of days, so
no role there takes a place: `go_checks.gapped_employers`), a **dial** (a Must on a dial waits for a fact the record
or the posting does not state yet: docs/PRD.md 46.7: kept, never stopped), and an **undecided place** (a read role
whose work location gate cannot be decided on its place and way of working: a Must that waits for its fact, as a
dial's does; not when the analyst said its posting does not state a place, and not when the role holds no place and
was never asked). The queue's words stop at the first bar that applies (a role the dial holds back is not asked about
its place, as it never was); the drop's name every reason it finds (`ALL_CAUSES`). A weak hold that is barred yields:
`held` is None and `off_deck` is set (the queue's bar outranks a place that stands only until the read is done). The
stage rule has no bars, so it keeps an own-data or being-read role that the queue then bars. All three are read from
the columns and the stored gate facts, never from the research.

**The queue's verdicts** (`Standing.free`, `stays`, `competes`) are those rules, said once, so the queue never needs to
know which holds are weak: a strong hold leaves a role where it is and nothing else; a weak hold that no bar outranks
stays; a role off the deck is moved and competes for nothing; every other role competes.

Modules: it knows models, the stages and the clock. The research module, the gates, the go checks and the employers are
read inside `load`, never at the top (the gates and research import this module).
"""

import asyncio
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import func, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from app.jobsearch import stages
from app.jobsearch.models import ORIGIN_OWNER, ORIGIN_TRACKER, Opportunity, StageMove
from app.models.work import TrailEvent
from app.timeutil import owner_today

if TYPE_CHECKING:
    from app.jobsearch.gating.check import Rules, Subject

#: The history line the board's move writes, for the owner (`board.move`, actor "person"): the only fact "by hand" is
#: read from. An app line, another event (an ask for a packet is not a stage move: the owner, 2026-10-20) and another
#: owner's line are not one.
MOVED_EVENT = "role_moved"
#: How many roles `load` asks the database about by id; more are read for the whole owner and intersected, so no
#: statement carries a list of thousands.
OWNER_WIDE_AT = 200
#: The roles the owner's own data made (an imported tracker, a job they added).
_OWN_ORIGINS = (ORIGIN_TRACKER, ORIGIN_OWNER)
#: What a bar says in a role's history line (`Standing.off_deck`).
_GAP_WORDS = "a place at an employer applied to within the gap is not offered"
_PLACE_WORDS = "where the work is (the work location gate cannot be decided yet)"


class Purpose(StrEnum):
    """Why the caller asks, which is the column of the table it reads."""

    STAGE = "stage"  # lifecycle.settle: does the stage rule leave the role where it is?
    QUEUE = "queue"  # deck.rebalance: who is fixed, who stays, who competes
    REREAD = "reread"  # deck.mark_for_reads: leave its old research alone?
    DROP = "drop"  # dial_hold.settle: leave it on the deck?
    LIMIT = "limit"  # employer_limit.settle: exempt from a hold?
    GATES = "gates"  # every gate and re-gate, and the analyst's picks: has the owner overruled code on this role?


class Hold(StrEnum):
    """Why code leaves a role alone. A caller reads `Standing` and names a hold only to act on that one (the queue
    lifts a star to Ready to Launch); which holds are weak is the verdicts' business (`Standing.free`)."""

    STAR = "star"
    OPENED = "opened"
    PLACED = "placed"
    SKIPPED = "skipped"
    OWN = "own"
    READING = "reading"
    MOVED = "moved"

    @property
    def weak(self) -> bool:
        """A place that stands only until the read is done: it has nothing to compete on yet."""
        return self in (Hold.OWN, Hold.READING)


@dataclass(frozen=True, slots=True)
class Standing:
    """What `load` says of one role, for one purpose."""

    #: The first row of the table honoured for the purpose that applies to the role; None: nothing keeps it.
    held: Hold | None
    #: The queue only (None for every other purpose): the status whose places the role takes one of.
    seat: str | None
    #: The queue and the drop only: why the role takes no place, as the words of its history line ("waiting for the
    #: years of experience asked"); None when nothing bars it. Never set with a strong hold.
    off_deck: str | None
    #: It has a Second Impression (`analysis.reads`), asked once for the roles passed.
    read: bool
    #: And that read graded something of the posting (`analysis.graded_read`): the only read a Ready to Launch place
    #: stands on. Never true without `read`.
    graded: bool = False

    @property
    def stays(self) -> bool:
        """A weak hold (own data, a read under way) that no bar outranks: the role stays where it is, takes no seat
        and competes for none, until its read is done."""
        return self.held is not None and self.held.weak

    @property
    def free(self) -> bool:
        """No strong hold keeps the role where it is, so the queue may move it, or take the mark that says it is read
        next off it: a role that stays, one off the deck, one that competes."""
        return self.held is None or self.stays

    @property
    def competes(self) -> bool:
        """Nothing holds the role and no bar gives it none: it competes for a place on its score."""
        return self.held is None and self.off_deck is None


_C, _F, _S, _R = (stages.CLEANSED_DATA, stages.FIRST_IMPRESSION, stages.SECOND_IMPRESSION, stages.READY_TO_LAUNCH)
ANY = None  # a cell that is every status

#: Purpose -> hold -> the statuses at which that purpose honours it (ANY: every status). Absent: not honoured.
HONOURED: dict[Purpose, dict[Hold, tuple[str, ...] | None]] = {
    # lifecycle.stage_of: the owner's move stays except at the stages code moves a role through at once (Followup
    # needed, Landing Zone); a Go-opened role and a read favorite keep Ready; the top places stand while read, own or
    # being read.
    Purpose.STAGE: {Hold.STAR: (_R,), Hold.OPENED: (_R,), Hold.PLACED: (_C, _F, _S, _R), Hold.OWN: (_S, _R),
                    Hold.READING: (_S, _R)},
    # deck.rebalance: a star holds a place at any of the three; Go opened and skipped matter at Ready only.
    Purpose.QUEUE: {Hold.STAR: (_F, _S, _R), Hold.OPENED: (_R,), Hold.PLACED: (_F, _S, _R), Hold.SKIPPED: (_R,),
                    Hold.OWN: (_S, _R), Hold.READING: (_S, _R)},
    # deck.mark_for_reads: a Go-opened role is left alone wherever it sits (nothing clears go_opened_at).
    Purpose.REREAD: {Hold.OPENED: ANY, Hold.PLACED: (_S, _R)},
    # dial_hold.settle (docs/PRD.md 46.7): as for an old read.
    Purpose.DROP: {Hold.OPENED: ANY, Hold.PLACED: (_S, _R)},
    # employer_limit.settle (docs/PRD.md 34.2): only a role still where the owner put it is never held.
    Purpose.LIMIT: {Hold.PLACED: (_F, _S, _R)},
    # every gate and re-gate (docs/PRD.md 23.8, 39.14): the owner's move, ever.
    Purpose.GATES: {Hold.MOVED: ANY},
}
#: The order in which the holds are tried.
PRECEDENCE = (Hold.STAR, Hold.OPENED, Hold.PLACED, Hold.SKIPPED, Hold.OWN, Hold.READING, Hold.MOVED)
#: The holds that keep a role in Ready to Launch only while it has a Second Impression that graded its posting (the
#: owner, 2026-11-03: no unread or ungraded role sits in Ready to Launch). A star was always read-gated; a hand move,
#: Go having opened the role and Go having skipped it were not. Not read at any other status, and the weak holds (own
#: data, a read under way) are not among them: they stand until their read is done.
NEEDS_GRADED_READ = frozenset({Hold.STAR, Hold.OPENED, Hold.PLACED, Hold.SKIPPED})
#: The purposes that decide who sits in Ready to Launch: the stage rule and the queue. The rest (an old read, a drop,
#: the limit, the gates) ask about a role wherever it is and read the holds as they were.
READY_NEEDS_READ = frozenset({Purpose.STAGE, Purpose.QUEUE})


class _Bar(StrEnum):
    GAP = "gap"
    DIAL = "dial"
    PLACE_UNDECIDED = "place"


#: The bars each purpose reads, in order. The gap is the queue's alone: a role the drop meets is already on the deck.
BARS: dict[Purpose, tuple[_Bar, ...]] = {
    Purpose.QUEUE: (_Bar.GAP, _Bar.DIAL, _Bar.PLACE_UNDECIDED),
    Purpose.DROP: (_Bar.DIAL, _Bar.PLACE_UNDECIDED),
}
#: The purposes whose words name every bar that applies (the drop at a Hard pass save, always); the others stop at the
#: first (the queue's line has only ever named one cause).
ALL_CAUSES = frozenset({Purpose.DROP})


def starred(role: Opportunity, graded: bool) -> bool:
    """Whether the role holds a Ready to Launch place by its star: a favorite before the application that has a Second
    Impression that graded the posting (`graded`: `analysis.graded_read`, or `Standing.graded`). One not read yet, or
    read and graded nothing, holds none: it runs its impressions first."""
    return role.favorited_at is not None and role.status in stages.PRE_APPLY and graded


async def _read(session: AsyncSession, owner: str, ids: list[uuid.UUID]) -> dict[uuid.UUID, bool]:
    """Which of these roles have a Second Impression (`analysis.reads`), each with whether that read graded something
    of the posting (`analysis.reads_graded`): asked by id for a few, for the whole owner and intersected for many (the
    same answer either way), in one statement."""
    from app.jobsearch.research import analysis  # analysis imports the gates, which import this module

    if len(ids) <= OWNER_WIDE_AT:
        return await analysis.reads_graded(session, ids)
    wanted = set(ids)
    every = await session.execute(select(Opportunity.id, analysis.graded_something()).where(
        Opportunity.owner == owner, Opportunity.analysis_state == analysis.DONE,
        analysis.deck_read_flag().is_(True)))
    return {i: bool(graded) for i, graded in every.all() if i in wanted}


async def _hand_lines(session: AsyncSession, owner: str, role_ids: Sequence[uuid.UUID] | None = None
                      ) -> dict[uuid.UUID, bool]:
    """The owner's moves on the board, from their history: each role the owner has a line for, and whether it is still
    where they put it (`Hold.PLACED`). A role code moved on after that line (a migration that dropped it back to the
    start, the stages since) is not in a place the owner chose. A status change is a `StageMove`, stamped with its
    transaction's start and the line with the app's clock, so a move and its own line written together count as placed.
    One statement, through the history's indexes on the role a line or a move is about (`ix_trail_events_person_lines`,
    `ix_js_stage_moves_role_at`). `role_ids`: only these roles' moves are read, which is the whole owner's answer
    restricted to them (`_applies` looks a role up by its own id and nothing else): a pass settling one role reads one
    role's history, not the owner's; none asks nothing."""
    if role_ids is not None and not role_ids:
        return {}
    where = [TrailEvent.owner == owner, TrailEvent.actor == "person", TrailEvent.event == MOVED_EVENT,
             TrailEvent.link_kind == "opportunity", TrailEvent.link_id.is_not(None)]
    if role_ids is not None:
        where.append(TrailEvent.link_id.in_(list(dict.fromkeys(role_ids))))
    lines = (select(TrailEvent.link_id.label("role_id"), func.max(TrailEvent.at).label("at")).where(*where)
             .group_by(TrailEvent.link_id).cte("hand_lines"))
    # Each line looks up its own role's last move (a correlated subquery, asked once for each line, so the index on the
    # role is how the moves are read whatever the planner guesses of how many lines there are); a role with no move has
    # none to be later than the line, and is where they put it.
    last_move = (select(func.max(StageMove.at)).where(StageMove.owner == owner, StageMove.role_id == lines.c.role_id)
                 .correlate(lines).scalar_subquery())
    rows = await session.execute(select(lines.c.role_id, func.coalesce(last_move <= lines.c.at, true())))
    return {role_id: bool(placed) for role_id, placed in rows if role_id is not None}


async def moved_by_hand(session: AsyncSession, owner: str) -> set[uuid.UUID]:
    """Every role the owner has moved on the board by hand, ever, wherever it is now: the roles with `Hold.MOVED`, the
    owner's decision to pursue a role that nothing since undoes (one statement, `_hand_lines`)."""
    return set(await _hand_lines(session, owner))


def _applies(hold: Hold, role: Opportunity, read: bool, graded: bool, lines: dict[uuid.UUID, bool],
             waiting: str) -> bool:
    """Whether the hold is true of the role, whatever the purpose."""
    if hold is Hold.STAR:
        return starred(role, graded)
    if hold is Hold.OPENED:
        return role.go_opened_at is not None
    if hold is Hold.PLACED:
        return lines.get(role.id) is True
    if hold is Hold.SKIPPED:
        return bool(role.go_skip)
    if hold is Hold.OWN:
        return role.origin in _OWN_ORIGINS and not read
    if hold is Hold.READING:
        return role.analysis_state == waiting and not read
    return role.id in lines  # MOVED


def _held(role: Opportunity, purpose: Purpose, read: bool, graded: bool, lines: dict[uuid.UUID, bool],
          waiting: str) -> Hold | None:
    table = HONOURED[purpose]
    for hold in PRECEDENCE:
        if hold not in table:
            continue
        where = table[hold]
        if where is not None and role.status not in where:  # None is ANY
            continue
        if hold in NEEDS_GRADED_READ and purpose in READY_NEEDS_READ and role.status == _R and not graded:
            continue  # a place in Ready to Launch stands on a read that graded the posting (docs/PRD.md 24.13)
        if _applies(hold, role, read, graded, lines, waiting):
            return hold
    return None


def _seat(role: Opportunity, held: Hold | None) -> str | None:
    if held in (Hold.STAR, Hold.OPENED):
        return _R
    if held is Hold.PLACED and role.status in (_S, _R):
        return role.status
    return None


def _subject(role: Opportunity, rules: "Rules") -> "Subject":
    """The role as the gates read it from its columns and stored gate facts alone (its research is never loaded). A way
    of working it does not state is assumed on site once the gate-facts step has looked for it (`gating.facts_read`:
    the mark the step stored with its read, which says the copy was whole and said nothing of a way when it read, so
    this agrees with the gates as of that read; a read role that holds no mark was read by the analyst, docs/PRD.md
    30.2). The copy is not loaded here, so a copy that has changed since is judged by the gates' own reading."""
    from app.jobsearch.gating import check as gating  # the gates import this module

    return gating.Subject(
        company=role.company, location=role.location, work_mode=role.move_type, comp=role.comp, comp_min=None,
        comp_max=None, posted=role.posted, found=role.found, target_role=role.target_role,
        employer=rules.index.of(role.employer_id, role.company), stored=(role.gate_facts or {}).get("facts"),
        title=role.title, way_read=gating.facts_read(role))


def _dial_facts(role: Opportunity, rules: "Rules") -> list[str]:
    """What the role's Musts on a dial wait for, in words ("the years of experience asked"); empty when none waits."""
    from app.jobsearch.gating import check as gating  # the gates import this module

    return gating.waiting_dials(_subject(role, rules), rules)


def _place_undecided(role: Opportunity, rules: "Rules", said: str | None) -> bool:
    """Whether the owner's work location gate cannot be decided on what a read role holds (its place or way of working
    is missing or does not say enough). `said` is the analyst's answer on the place (`stated_place.PLACE_KEY`). Not
    held: a role the analyst said its posting does not state a place for (it goes on: nothing more will arrive), and a
    role that holds no place and was never asked (its analyst's ask comes first)."""
    from app.jobsearch.gating import check as gating  # the gates import this module
    from app.jobsearch.gating import stated_place  # it reads this module's neighbours

    if said == stated_place.NOT_STATED or not ((role.location or "").strip() or said):
        return False
    return gating.location_undecided(_subject(role, rules), rules)


async def _place_said(session: AsyncSession, owner: str, ids: list[uuid.UUID]) -> dict[uuid.UUID, str | None]:
    """What the analyst said of the place of each of these read roles (`stated_place.PLACE_KEY`), read from the one
    key of the research and never the research itself: by id for a few, for the owner's read roles for many."""
    from app.jobsearch.research import analysis  # analysis imports the gates, which import this module

    if not ids:
        return {}
    place = Opportunity.analysis["place"].as_string()
    if len(ids) <= OWNER_WIDE_AT:
        return dict((await session.execute(select(Opportunity.id, place).where(Opportunity.id.in_(ids)))).all())
    wanted = set(ids)
    return {i: said for i, said in (await session.execute(select(Opportunity.id, place).where(
        Opportunity.owner == owner, Opportunity.analysis_state == analysis.DONE,
        analysis.deck_read_flag().is_(True)))).all() if i in wanted}


def _gap_stops(roles: list[Opportunity], gapped: set[str]) -> dict[uuid.UUID, str]:
    """The roles whose employer was applied to within the gap, with the words that say so."""
    from app.jobsearch.employers import core as employers  # they read the statuses the queue names

    return {r.id: _GAP_WORDS for r in roles if employers.key_of(r.company) in gapped}


async def _bar_inputs(
        session: AsyncSession, owner: str, roles: list[Opportunity], read: dict[uuid.UUID, bool], purpose: Purpose,
        today: date | None) -> tuple[set[str], "Rules | None", dict[uuid.UUID, str | None]]:
    """What the bars of `purpose` ask of the database for these roles (none of them strongly held): the employers
    applied to within the gap (the queue's), the owner's rules and the place the analyst stated of each read role that
    needs it, each read only when a role is left for it to bar."""
    bars = BARS.get(purpose, ())
    if not roles or not bars:
        return set(), None, {}
    gapped: set[str] = set()
    if _Bar.GAP in bars:
        from app.jobsearch.browser import go_checks  # they read the statuses the queue names

        gapped = await go_checks.gapped_employers(session, owner, today or owner_today())
    stopped = _gap_stops(roles, gapped) if _Bar.GAP in bars else {}
    rest = [r for r in roles if r.id not in stopped]
    if not rest:
        return gapped, None, {}
    from app.jobsearch.gating import check as gating  # the gates import this module

    rules = await gating.rules(session, owner)
    return gapped, rules, await _place_said(session, owner, [r.id for r in rest if r.id in read])


def _off_deck(inputs: "Inputs", roles: list[Opportunity], purpose: Purpose) -> dict[uuid.UUID, str]:
    """Why each of these roles (none of them strongly held) takes no place, by the bars of `purpose`; a role nothing
    bars is not in the answer. Decided from `inputs`: no query."""
    bars = BARS.get(purpose, ())
    out: dict[uuid.UUID, str] = {}
    if not roles or not bars:
        return out
    if _Bar.GAP in bars:
        out = _gap_stops(roles, inputs.gapped)
    rest = [r for r in roles if r.id not in out]
    rules = inputs.rules
    if not rest or rules is None:
        return out
    dial = any(g.kind in ("job_fact", "employer_fact") for g in rules.profile.gates.other)  # none set: read nothing
    for role in rest:
        waits = _dial_facts(role, rules) if dial else []
        asked = purpose in ALL_CAUSES or not waits  # a role the dial holds back is not asked about its place
        if asked and role.id in inputs.read and _place_undecided(role, rules, inputs.said.get(role.id)):
            waits.append(_PLACE_WORDS)
        if waits:
            out[role.id] = "waiting for " + ", ".join(waits)
    return out


@dataclass(frozen=True, slots=True)
class Inputs:
    """What `gather` read from the database for some roles and one purpose, and all that `standing` needs to say where
    each stands: so a caller whose transaction should not be open while the answer is worked out (the queue's turn
    works out thousands in Python, seconds) reads, ends the transaction, and decides after. Valid for the roles it was
    gathered for, as they were then."""

    #: What holds each role (the first row of the table honoured for the purpose), before the bars.
    holds: dict[uuid.UUID, Hold | None]
    #: The roles that have a Second Impression (`analysis.reads`), each with whether that read graded something of the
    #: posting (`analysis.graded_read`).
    read: dict[uuid.UUID, bool]
    #: The bars' inputs (`_bar_inputs`): the employers applied to within the gap, the owner's rules (None when no role
    #: was left for a bar to judge), and the place the analyst stated of each read role.
    gapped: set[str]
    rules: "Rules | None"
    said: dict[uuid.UUID, str | None]


async def gather(session: AsyncSession, owner: str, roles: Iterable[Opportunity], purpose: Purpose, *,
                 today: date | None = None) -> Inputs:
    """The reading half of `load`: every statement it asks, and nothing is decided. The statements do not depend on how
    many roles are passed: one for the owner's moves on the board and one for which roles have a Second Impression, and
    for the queue and the drop the rules, the place the analyst stated of the read roles that need it, and for the queue
    the employers applied to."""
    from app.jobsearch.research import analysis  # analysis imports the gates, which import this module

    roles = list(roles)
    if not roles:
        return Inputs({}, {}, set(), None, {})
    read = await _read(session, owner, [r.id for r in roles])
    # By id for a few roles, for the whole owner for many: as `_read` does, so no statement carries a list of thousands.
    lines = await _hand_lines(session, owner, [r.id for r in roles] if len(roles) <= OWNER_WIDE_AT else None)
    holds = {r.id: _held(r, purpose, r.id in read, read.get(r.id, False), lines, analysis.WAITING) for r in roles}
    barrable = [r for r in roles if (h := holds[r.id]) is None or h.weak]
    gapped, rules, said = await _bar_inputs(session, owner, barrable, read, purpose, today)
    return Inputs(holds, read, gapped, rules, said)


def standing(inputs: Inputs, roles: Iterable[Opportunity], purpose: Purpose) -> dict[uuid.UUID, Standing]:
    """The deciding half of `load`, from what `gather` read for these same roles: an entry for every role passed. No
    query, so a caller may run it with no transaction open."""
    roles = list(roles)
    barrable = [r for r in roles if (h := inputs.holds[r.id]) is None or h.weak]
    off = _off_deck(inputs, barrable, purpose)
    out: dict[uuid.UUID, Standing] = {}
    for role in roles:
        why = off.get(role.id)
        held = None if why is not None else inputs.holds[role.id]  # a bar outranks a weak hold
        out[role.id] = Standing(held, _seat(role, held) if purpose is Purpose.QUEUE else None, why,
                                role.id in inputs.read, inputs.read.get(role.id, False))
    return out


async def standing_async(inputs: Inputs, roles: Iterable[Opportunity], purpose: Purpose, *,
                         size: int = 200) -> dict[uuid.UUID, Standing]:
    """`standing`, worked out a slice of `size` roles at a time with the event loop given back between slices: the same
    dict in the same order, and no query. The queue's turn decides thousands of roles in Python, a stretch of seconds
    in which every other request waits; each role is decided alone, so a slice gives the answer the whole would."""
    roles = list(roles)
    out: dict[uuid.UUID, Standing] = {}
    for at in range(0, len(roles), size):
        if at:
            await asyncio.sleep(0)
        out.update(standing(inputs, roles[at:at + size], purpose))
    return out


async def load(session: AsyncSession, owner: str, roles: Iterable[Opportunity], purpose: Purpose, *,
               today: date | None = None) -> dict[uuid.UUID, Standing]:
    """Where each of these roles stands for `purpose`: an entry for every role passed (index freely). The roles are the
    owner's, loaded with every column but `analysis` and `description`, which may stay deferred: they are never loaded
    here (the research is read through SQL alone). `today` is the day the gap is counted to (the queue; else the
    owner's today). Nothing is asked of the database for no roles. `gather` reads and `standing` decides; this is both,
    in the caller's transaction. No errors of its own, database errors only."""
    roles = list(roles)
    if not roles:
        return {}
    return standing(await gather(session, owner, roles, purpose, today=today), roles, purpose)


async def one(session: AsyncSession, owner: str, role: Opportunity, purpose: Purpose) -> Standing:
    """`load` for one role: the same answer the whole owner gets."""
    return (await load(session, owner, [role], purpose))[role.id]
