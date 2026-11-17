"""Where a role stands in the analyst's line (`Standing`), how it leaves the line (`leave`, by a named `Cause`) and the
marks the deck puts on it (`put_mark`): the one home of what a role's research standing means, in Python (`of`) and in
SQL (`where`), so the places that read it and the places that clear it cannot drift apart.

A role's standing is read from its `analysis_state`, its claim and the owner's ask kept on its research:

- `OUT`: not in line (no state).
- `FREE`: waiting for the analyst (`WAITING`) and no session has taken it.
- `TAKEN`: waiting, and a session has taken it (`analysis_claimed_at`).
- `NEEDS_TEXT`: waiting for the posting's text (`NO_TEXT`).
- `DONE`, `FAILED`: researched, or given up on.

`leave` is the one way a role goes back out of the line, by a cause from the closed table below: the cause names what it
acts on, what it spares (the owner's ask, a deck stage), and what it clears. Each cause row is the clearing one writer
did by hand before, difference for difference. What this module does not do: start a role (that needs the process engine
and the briefing, `analysis.start`), take it, finish it, or write its history line; each caller says in its own words
what it did. Services flush, never commit; nothing here reads the network, ends a transaction or locks a row (the lock
order stays the caller's).

A role stopped inside the analysis process's own step (`inside`, or the `in_step` block around a step's work) keeps its
instance id: the engine ends the instance itself, and cancelling it from within would cut the step off.
"""

import contextlib
import contextvars
import enum
from collections.abc import Collection, Iterator
from dataclasses import dataclass

from sqlalchemy import and_, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app import jsonsql
from app.jobsearch import stages
from app.jobsearch.models import SOURCE_NEXT, Opportunity
from app.jobsearch.work import stuck

#: A role's analysis: none yet | waiting for the analyst | done | failed (the analyst never sent it, or it was refused
#: too often). A failed one isn't picked again by itself; the owner can send the role on by hand.
WAITING, DONE, FAILED = "waiting", "done", "failed"
#: A role that holds no text of the posting, after the app fetched what it could and the analyst looked for the
#: employer's own posting (the owner, 2026-10-14): it is not researched and not scored, its asks stay unanswered, and it
#: waits for the posting's text (the owner's browser reads it, `browser.texts`; `resume_waiting` puts it in the
#: analyst's line when it comes).
NO_TEXT = "no_text"
#: What an older research left on a role whose posting could not be read (a submission with nothing researched was sent
#: back once, on a count of its own): no research of a role without text happens now, so nothing sets it, and `ask`
#: clears it from a role that still has it.
UNREAD_ASKED = "unread_asked"
#: Which session took a role (`analysis.claim`): its id and its runner's, kept on the role's analysis.
SESSION_KEY, RUNNER_KEY = "session", "runner"
#: How many times a role was taken without findings coming back, kept on its analysis.
CLAIMS_KEY = "claims"
#: Kept on a role's analysis when findings were refused (their count): a stop for the analyst to read.
REFUSED_KEY = "refused"
#: Who asked for the research of a role that waits, kept on its analysis under `ASKED_BY`: the owner (`ask`). Such a
#: role goes first in the analyst's line (`line_order`) and is never taken back to the queue by a sweep of a night or of
#: a catch-up (`asked_by_owner`). It is dropped when the findings are taken (they replace the analysis) and when
#: anything else puts the role in line (`start`).
ASKED_BY, ASKED_BY_OWNER = "asked_by", "owner"
#: A researched role marked to be researched again (`requeue.reanalyse_all`, the `requeue` action's scope `all`), kept
#: on its analysis: the pool picks it like any role it researches (`startable`, `picks`), behind the new finds and the
#: highest fit first, and the mark goes when it is put in line (`analysis.start`). Its old research stays until the new
#: replaces it, and its stage stays as it is.
REANALYSE = "reanalyse"
#: Kept on the analysis of an unread role of First Impression that the best-of queue has chosen to be read next
#: (`pipeline.rebalance`): `startable` puts only such a role in front of the analyst. It stays in First Impression while
#: it is read; the research that follows replaces it.
READ_NEXT = "read_next"


def field(key: str):
    """What the role's analysis says under `key`, as text (SQL), or null."""
    return jsonsql.field(Opportunity.analysis, key)


class Standing(enum.Enum):
    """Where a role stands in the analyst's line."""

    OUT = "out"
    FREE = "free"
    TAKEN = "taken"
    NEEDS_TEXT = "needs_text"
    DONE = "done"
    FAILED = "failed"

    @staticmethod
    def of(role: Opportunity) -> "Standing":
        """The role's standing. A state the app does not know counts as out of line."""
        state = role.analysis_state
        if state == WAITING:
            return Standing.FREE if role.analysis_claimed_at is None else Standing.TAKEN
        known = {NO_TEXT: Standing.NEEDS_TEXT, DONE: Standing.DONE, FAILED: Standing.FAILED}
        return known.get(state or "", Standing.OUT)

    @staticmethod
    def where(*standings: "Standing", owner_ask: bool | None = None) -> ColumnElement[bool]:
        """The SQL twin of `of`: the roles standing in any of `standings`. `owner_ask` True keeps those carrying the
        owner's ask, False those that do not (a role with no mark is one of them), None ignores it."""
        state, claim = Opportunity.analysis_state, Opportunity.analysis_claimed_at
        parts = {
            Standing.OUT: state.is_(None),
            Standing.FREE: and_(state == WAITING, claim.is_(None)),
            Standing.TAKEN: and_(state == WAITING, claim.is_not(None)),
            Standing.NEEDS_TEXT: state == NO_TEXT,
            Standing.DONE: state == DONE,
            Standing.FAILED: state == FAILED,
        }
        clause = or_(*(parts[s] for s in standings))
        if owner_ask is None:
            return clause
        return and_(clause, owners_asks() if owner_ask else not_owners())


def asked_by_owner(role: Opportunity) -> bool:
    """Whether the role waits in the analyst's line because the owner asked for its research (`analysis.ask`). The
    sweeps that send a night's or a catch-up's roles back to the queue leave such a role in line."""
    return role.analysis_state == WAITING and (role.analysis or {}).get(ASKED_BY) == ASKED_BY_OWNER


def not_owners() -> ColumnElement[bool]:
    """The roles whose research the owner did not ask for (SQL; a role with no mark is one of them)."""
    return field(ASKED_BY).is_distinct_from(ASKED_BY_OWNER)


def owners_asks() -> ColumnElement[bool]:
    """The roles whose research the owner asked for and that still carry the ask (SQL): in the analyst's line
    (`asked_by_owner`) or waiting for the posting's text (`NO_TEXT`; the mark stays until the findings are taken). The
    browser's queue hands such a role's sourcing and reading out at any hour (`takeable.owners_item`)."""
    return field(ASKED_BY) == ASKED_BY_OWNER


# --- the marks the deck puts on a role ---------------------------------------------------------------------------


def marked_next() -> ColumnElement[bool]:
    """The roles marked to be read next (SQL): First Impression roles carrying `READ_NEXT`."""
    return and_(Opportunity.status == stages.FIRST_IMPRESSION, field(READ_NEXT).is_not(None))


def marked_again() -> ColumnElement[bool]:
    """The researched roles marked to be researched again (SQL): a done analysis carrying `REANALYSE`."""
    return and_(Opportunity.analysis_state == DONE, field(REANALYSE).is_not(None))


def startable() -> ColumnElement[bool]:
    """The roles the pool may put in front of the analyst now (SQL): on deck (Second Impression), Sourced, never
    researched. The one rule of what `analysis.picks` selects and what the pool counts as waiting
    (`pool.waiting_counts`), so the two cannot drift. Sonnet reads the unread challengers of the best-of queue and no
    others (`pipeline.rebalance`); a role the owner asks research of (`analysis.ask`) is started by that ask at any
    status. A role with no text of its posting (`NO_TEXT`) is not one: it waits for the text (docs/PRD.md 39.30). A
    researched role marked to be researched again (`REANALYSE`) is one too, at the status it is in."""
    return or_(and_(or_(Opportunity.status == stages.SECOND_IMPRESSION, marked_next()),
                    Opportunity.sourced_at.is_not(None),
                    Opportunity.analysis_state.is_(None)),
               and_(Opportunity.sourced_at.is_not(None), marked_again()))


class Mark(enum.Enum):
    """What the deck's queue keeps on a role's analysis: read it next, read it again, look its source up next. The
    key each is kept under is its value."""

    READ_NEXT = READ_NEXT
    REANALYSE = REANALYSE
    SOURCE_NEXT = SOURCE_NEXT


def put_mark(role: Opportunity, mark: Mark) -> bool:
    """Put `mark` on the role's research, in the one way the SQL above reads it: a role is read next or has its source
    looked up next, never both (each takes the other off), and a mark to read again goes beside no look-up. The look-up
    is refused (False, nothing changed) for a role that is in line or researched (`analysis_state` set): there is
    nothing to look up for it. Returns whether the mark was put on."""
    kept = role.analysis or {}
    if mark is Mark.SOURCE_NEXT:
        if role.analysis_state is not None:
            return False
        role.analysis = {**{k: v for k, v in kept.items() if k != READ_NEXT}, SOURCE_NEXT: True}
    else:
        role.analysis = {**{k: v for k, v in kept.items() if k != SOURCE_NEXT}, mark.value: True}
    return True


# --- leaving the line --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Cause:
    """Why a role leaves the line, as a row of the table below (the module's constants; callers never build one)."""

    name: str
    leaves: frozenset[Standing]  # the standings it acts on; a role in any other is left untouched (False)
    why: str  # what the cancelled instance is told, when the caller gives no words of its own
    spares_owner_ask: bool = False  # a role waiting because the owner asked for it stays (`asked_by_owner`)
    spares_statuses: frozenset[str] = frozenset()  # stages a role stays in line in
    spares_analysed: bool = False  # a role researched before (`analysed_at`) stays: it is back for new research
    drops_asked_at: bool = False  # the place the role held in line goes too
    pops: frozenset[str] = frozenset()  # keys of the research cleared besides the session's and the claims
    ends_in: Standing = Standing.OUT  # `NEEDS_TEXT`: the role is not out of line, it waits for the posting's text


_WAITING_NOW = frozenset({Standing.FREE, Standing.TAKEN})

#: A night, a catch-up or a stopped search sends back the roles that wait for the analyst, whether or not a session has
#: taken them (its claim lapses with it), and leaves the owner's asks in line. (`analysis.release_round`,
#: `kinds.analyse.release`, `catch_up`)
SWEEP = Cause("sweep", _WAITING_NOW, "sent back to the queue", spares_owner_ask=True)
#: The deck takes a role that waits, that no session has taken, out of line when it is not on deck: not the owner's ask,
#: and not a role in a stage a Ready to Launch role waits for the analyst in because Go sent its stale research back
#: (taking it out would leave it unwritten and asked again at every Go), a researched role back for new research, or one
#: that is ended, applied to or in talks. Its place in line goes with it. (`pipeline.stop_waiting`)
OFF_DECK = Cause(
    "off deck", frozenset({Standing.FREE}), "it is not on deck", spares_owner_ask=True, spares_analysed=True,
    spares_statuses=frozenset({*stages.CLOSED, stages.APPLIED, stages.ACTIVE_DISCUSSION, stages.READY_TO_LAUNCH}),
    drops_asked_at=True)
#: A gate stopped the role on the posting's facts before its analysis. It also leaves a role that waits for the text,
#: and clears the findings' refusals. The owner's ask stays on the role, as it did. (`gate_facts.gate_out`)
GATE_FAILED = Cause(
    "gate failed", frozenset({Standing.FREE, Standing.TAKEN, Standing.NEEDS_TEXT}),
    "a gate failed on the posting's facts", pops=frozenset({REFUSED_KEY}))
#: The role is not Sourced, so it is not researched; its sourcing is tried again every night. The refusals stay.
#: (`analysis.action_not_sourced`)
NOT_SOURCED = Cause(
    "not sourced", frozenset({Standing.FREE, Standing.TAKEN, Standing.NEEDS_TEXT, Standing.FAILED}),
    "the role is not sourced")
#: The role holds no text of its posting, so it waits for the text (`NO_TEXT`): from any standing. Its instance is kept
#: (the process step ends it, or the research is over); the refusals and the unread-ask count go, `SENT_BACK` and
#: `POSTING_NOT_FOUND` stay. (`analysis.wait_for_text`)
SEND_BACK_TEXT = Cause(
    "wait for text", frozenset(Standing), "the role waits for the posting's text",
    pops=frozenset({REFUSED_KEY, UNREAD_ASKED}), ends_in=Standing.NEEDS_TEXT)
#: The role ended (the posting is gone) or was closed as noise: out of line whatever its standing, except research that
#: is done, which stays done. (`quiet.end_gone`, `noise.close`)
ROLE_CLOSED = Cause(
    "role closed", frozenset({Standing.FREE, Standing.TAKEN, Standing.NEEDS_TEXT, Standing.FAILED}),
    "the role was closed")

CAUSES = (SWEEP, OFF_DECK, GATE_FAILED, NOT_SOURCED, SEND_BACK_TEXT, ROLE_CLOSED)

_IN_STEP: contextvars.ContextVar[bool] = contextvars.ContextVar("analysis_in_step", default=False)


@contextlib.contextmanager
def in_step() -> Iterator[None]:
    """Around the work of one of the analysis process's own steps: a role left from inside it keeps its instance id and
    is not cancelled (the engine ends the instance itself), however far down the call is made (`quiet.end_gone`, which
    the text fetch reaches, is not told by every caller between)."""
    token = _IN_STEP.set(True)
    try:
        yield
    finally:
        _IN_STEP.reset(token)


def _leave(role: Opportunity, cause: Cause, standing: Standing) -> bool:
    """Whether the cause acts on a role standing so, and spares it not."""
    if standing not in cause.leaves:
        return False
    if cause.spares_owner_ask and asked_by_owner(role):
        return False
    if cause.spares_statuses and role.status in cause.spares_statuses:
        return False
    return not (cause.spares_analysed and role.analysed_at is not None)


async def _go(session: AsyncSession, role: Opportunity, cause: Cause, *, inside: bool, why: str) -> bool:
    standing = Standing.of(role)
    if not _leave(role, cause, standing):
        return False
    inside = inside or _IN_STEP.get()
    waits_for_text = cause.ends_in is Standing.NEEDS_TEXT
    if not inside and not waits_for_text and standing in _WAITING_NOW and role.analysis_instance_id is not None:
        await stuck.cancel_instance(session, role.analysis_instance_id, why or cause.why,
                                    f"the analysis of role {role.id}")
    role.analysis_state = NO_TEXT if waits_for_text else None
    role.analysis_claimed_at = None
    if cause.drops_asked_at:
        role.analysis_asked_at = None
    dropped = {SESSION_KEY, RUNNER_KEY, CLAIMS_KEY, *cause.pops}
    role.analysis = {k: v for k, v in (role.analysis or {}).items() if k not in dropped}
    if not inside and not waits_for_text:
        role.analysis_instance_id = None
    return True


async def leave(session: AsyncSession, role: Opportunity, cause: Cause, *, inside: bool = False, why: str = "") -> bool:
    """Take the role out of the analyst's line for `cause`. Returns whether it left (or, for `SEND_BACK_TEXT`, moved
    to waiting for the text); False, with nothing changed, when its standing is not one the cause acts on, or the
    cause spares it (the owner's ask, a deck stage). A role waiting for the analyst has its process instance cancelled
    first (with `why`, or the cause's own words), unless `inside`: called from the process's own step, which ends the
    instance itself and keeps its id. Then the state, the claim and (where the cause says) the place in line are
    cleared, and the session's keys, the claims and the cause's own keys come off the research. Idempotent. Flushes."""
    left = await _go(session, role, cause, inside=inside, why=why)
    if left:
        await session.flush()
    return left


async def leave_many(session: AsyncSession, roles: Collection[Opportunity], cause: Cause, *,
                     why: str = "") -> list[Opportunity]:
    """`leave` for each of `roles` (the caller's selection, from outside the process), flushing once. Returns the roles
    that left, in order."""
    left = [role for role in roles if await _go(session, role, cause, inside=False, why=why)]
    if left:
        await session.flush()
    return left

