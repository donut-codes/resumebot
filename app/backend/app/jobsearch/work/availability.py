"""May Claude work now? One answer, and the two things behind it: Claude's usage limit and the restart's drain.

Whether Claude may work now was asked in eight places, each with its own subset of the reasons and its own wording:
the pool (a session may start, or work be queued for one), Launch in Browser, the browser worker's next item, the
owner's waiting jobs (a check of the roles, a posting to split), the catch-up's hand-out, the schedule of the scout
night, the Agents page's health line and the health read. This module knows the reasons, once each: the lanes' drain
(a restart), the global pause, Claude's peak hours when the owner avoids them, Claude's usage limit, and, for the jobs
the owner waits at the screen for, no runner that has checked in lately. How a reason is known is written once (a
`_drain`, `_pause`, `_peak`, `_blackout`, `_absent` or `_limit` check); what an asker asks is one row of `_ROWS`.

The askers differ on purpose, and the table is where they are seen to differ: a row is the tuple of checks one
`Question` asks, in the order it asks them, each with the words that asker is told in. `barred` gives the first check
of the row that stands. A reason dropped from a row, or another order, is an edit of that tuple; a reason added is one
more check in it (a check with no words of its own is told in the pool's); a reason new to the module is a `Reason` and
one check function. Nothing else names a row. `standing` gives every check of the row that stands, for a caller that
applies a rule of its own: Launch in Browser is the owner's own action, which peak hours do not hold, so
`session_kinds` asks the pool's row and leaves `Reason.PEAK` out of what it counts.

What the rows say today, caller by caller: the browser worker never asks the usage limit (the pool does not start a
browser session into it, and the PRD says nothing of a worker that is running when it begins); the owner's own asks (the
worker and the waiting jobs) read peak hours as the blackout the owner is refused with (`hours.blackout`); the catch-up
asks the pause and the limit before the peak hours, as it always did, and stores when the window ends in its hold.

Claude's usage limit has two stores, read as one (`limit_until`): the process's memory, which is instant and survives a
savepoint that was rolled back and a stale row in a long pass; and the scout connection's `usage_limit_until`, which a
restart of the app does not lose and which only ever moves later. A session that ended at the limit writes both
(`limit_met`, from the runner's heartbeat). A running catch-up also keeps what it learned in its own run
(`catch_up.note_usage_limit`), for the Missions text; it is never later than the connection's, so it is no input here.

The drain is kept here too (`drain`, `draining`): it is one of the reasons, and this module must stay at the bottom of
the import graph, so that a caller of any layer can import it at the top. It imports the standard library, SQLAlchemy,
`roster`, the profile's store, `hours` and `timeutil`, and never the browser, the missions, the pool, the job queue or
the session kinds (`test_availability` pins it). Peak hours are always read as `hours.<function>(...)`, through the
module, so a test that patches them there binds.

It only reads (the one write, `limit_met`, is the caller's to commit): no flush, no commit, no lock, no network, and a
barred answer is a return value, never an exception. `barred` reads no reason after the first that stands; the scout
connection is read at most once a call, the profile only when a peak is reached.

What it does not hold, and why. The decision whether Claude may start work is here. A reader that asks one fact for a
purpose of its own, and says it in a stop or a refusal addressed to someone else, reads that fact where it lives and
keeps its own sentence: the pause is the column `AgentConnection.paused` (read by the scout's door to tell a running
session to stop, `doors/tools.py`; by `search_run.start_round` to refuse a search; by the site plans' checkout,
`sites/completion.py`), peak hours are `hours.in_peak` and `hours.blackout` (read by the schedule's pass, the round's
plan, the stuck sweep, the search's heartbeat and `hours.refuse_in_blackout`, each with the settings in hand), and the
paused kind (`pool.paused_kinds`), the nightly budget (`pool/budget.py`) and the off hours (`hours.in_off_hours`) have
one home each. When one of those facts changes meaning, `roster` or `hours` is the edit; when one of the sentences
changes, it is the edit of that caller, since it is that caller's voice. Claude's usage limit is named once
(`USAGE_LIMIT`) wherever a sentence says it."""

import enum
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.agents import roster
from app.jobsearch.profile import store as profile_store
from app.jobsearch.work import hours
from app.timeutil import aware, utc_now

#: When Claude's usage limit gave no time it lifts, the work asks again after this long (a failed session costs
#: nothing); and the furthest a time it was given is believed (a limit lasts a week at most).
RETRY_AFTER = timedelta(minutes=30)
LONGEST_LIMIT = timedelta(days=8)
#: How long a drain lasts unless the lanes start again first.
DRAIN_FOR = timedelta(minutes=5)
#: What Claude's usage limit is called, wherever a sentence names it.
USAGE_LIMIT = "Claude's usage limit"


class Reason(enum.StrEnum):
    """Why Claude may not work now. `PAUSED`, `PEAK` and `USAGE` are also what a running catch-up stores as its hold
    (`why`): the values never change, since runs in flight hold them."""

    RESTARTING = "restarting"  # the lanes' drain (a restart): nothing new starts
    PAUSED = "paused"  # the scout connection's global pause
    PEAK = "peak"  # the owner avoids Claude's peak hours and the window is open
    USAGE = "usage"  # Claude's usage limit, from either store
    ABSENT = "absent"  # no runner has checked in lately (only the owner's waiting jobs care)


class Question(enum.StrEnum):
    """What is being asked, named for the asker. Its row in `_ROWS` fixes the reasons, their order and the words."""

    POOL = "pool"  # may a lane session start, or work be queued for one?
    WORKER = "worker"  # may a browser worker be handed another item?
    JOB = "job"  # may a job the owner waits at the screen for be made?
    CATCH_UP = "catch_up"  # may the catch-up hand the analyst a role?


@dataclass(frozen=True)
class Barred:
    """Claude may not work now. `words` is one plain sentence in the asking question's own voice. `until` is when it
    lifts: always set for `USAGE`; for `PEAK` only where the asker's row asks for it (the catch-up's; None when the
    window's end cannot be told); None for the rest."""

    reason: Reason
    until: datetime | None
    words: str


# --- what is kept in the process -------------------------------------------------------------------------------------

#: When Claude's usage limit lifts, by owner, as a session of any lane learned it (kept in the app's one process, like
#: the pool's other counts: a restart forgets it, and the connection's column keeps it). It is what makes the pool start
#: no session before then, whatever kind of work is waiting: one session per lane used to meet the limit and fail.
_limit_memory: dict[str, datetime] = {}
#: Until when each owner's lanes are draining (a restart).
_draining: dict[str, datetime] = {}


def forget() -> None:
    """Empty what is kept in the process, the limit and the drain (the tests, between cases)."""
    _limit_memory.clear()
    _draining.clear()


def drain(owner: str, on: bool, now: datetime | None = None) -> None:
    """A restart of the lanes (a stop, a release) asks for a drain: no item is handed out and no browser session starts
    for `DRAIN_FOR`, and each worker finishes the item it holds. The lanes' next start ends it."""
    if on:
        _draining[owner] = (now or datetime.now(UTC)) + DRAIN_FOR
    else:
        _draining.pop(owner, None)


def draining(owner: str, now: datetime | None = None) -> bool:
    until = _draining.get(owner)
    return until is not None and (now or datetime.now(UTC)) < until


# --- the usage limit -------------------------------------------------------------------------------------------------


def _reset_time(said: dict, moment: datetime) -> datetime:
    """When the limit lifts, from what the runner read of Claude's words: that time when it is ahead and within a week
    and a day, else `RETRY_AFTER` from now."""
    try:
        when = aware(datetime.fromisoformat(str(said.get("resets_at"))))
    except (TypeError, ValueError):
        when = None
    if when is None or when <= moment or when - moment > LONGEST_LIMIT:
        return moment + RETRY_AFTER
    return when


async def limit_met(session: AsyncSession, owner: str, said: dict, *, now: datetime | None = None) -> datetime:
    """A session ended at Claude's usage limit: `said` is what the runner's heartbeat read of Claude's words
    (`resets_at`, `text`; anything but a dict counts as none). Returns when it lifts: Claude's time when it is ahead and
    within `LONGEST_LIMIT`, else `RETRY_AFTER` from now. Both stores note it, and neither is ever moved earlier, so a
    session that read a shorter time cannot cut a hold short. Flushes and commits nothing: the caller owns the
    transaction, and locks any running catch-ups first (`catch_up.note_usage_limit`)."""
    moment = now or utc_now()
    until = _reset_time(said if isinstance(said, dict) else {}, moment)
    kept = _limit_memory.get(owner)
    if kept is None or kept <= moment or until > kept:
        _limit_memory[owner] = until
    conn = await roster.connection_by_slug(session, owner, roster.SCOUT)
    if conn is not None and ((had := aware(conn.usage_limit_until)) is None or had < until):
        conn.usage_limit_until = until  # kept in the database too: a restart of the app must not forget the hold
    return until


def _limit_end(conn: roster.AgentConnection | None, owner: str) -> datetime | None:
    """The later of the two stores' ends, ahead or behind."""
    ends = [e for e in (_limit_memory.get(owner), aware(conn.usage_limit_until) if conn is not None else None)
            if e is not None]
    return max(ends) if ends else None


async def limit_until(session: AsyncSession, owner: str, *, now: datetime | None = None) -> datetime | None:
    """When Claude's usage limit lifts, while that is still ahead, or None when it holds none. The time is the one
    Claude gave, else the half hour work waits before it asks again (`RETRY_AFTER`)."""
    return _ahead(await _Reads(session, owner).conn(), owner, now or utc_now())


def _ahead(conn: roster.AgentConnection | None, owner: str, moment: datetime) -> datetime | None:
    held = _limit_memory.get(owner)
    if held is not None and held <= moment:
        _limit_memory.pop(owner, None)  # lifted: forgotten, as the memory only ever holds what is ahead
    end = _limit_end(conn, owner)
    return end if end is not None and end > moment else None


async def counts_from(session: AsyncSession, owner: str, *, now: datetime | None = None) -> datetime | None:
    """The moment the stuck sweeps count waiting time from, or None: the later of the end of the last peak window the
    owner avoids and the usage limit's last end, ahead or behind. While either held, no session could start, so that is
    not time work sat stuck: the sweep waits `stuck_minutes` from the moment it lifted."""
    settings = (await profile_store.current(session, owner)).settings
    conn = await roster.connection_by_slug(session, owner, roster.SCOUT)
    ends = [e for e in (hours.last_peak_end(settings, now), _limit_end(conn, owner)) if e is not None]
    return max(ends) if ends else None


# --- the reasons: how each is known, and said ------------------------------------------------------------------------


class _Reads:
    """What one call has read so far: the scout connection and the profile's settings, each at most once."""

    def __init__(self, session: AsyncSession, owner: str) -> None:
        self.session, self.owner = session, owner
        self._conn: roster.AgentConnection | None = None
        self._read = False
        self._settings: Any = None

    async def conn(self) -> roster.AgentConnection | None:
        if not self._read:
            self._conn = await roster.connection_by_slug(self.session, self.owner, roster.SCOUT)
            self._read = True
        return self._conn

    async def settings(self) -> Any:
        if self._settings is None:
            self._settings = (await profile_store.current(self.session, self.owner)).settings
        return self._settings


#: One reason, asked: whether it stands now (given what has been read and the caller's `now`): the answer, or None.
_Check = Callable[[_Reads, datetime | None], Awaitable[Barred | None]]


def _ago(age: timedelta) -> str:
    minutes = int(age.total_seconds() // 60)
    if minutes < 60:
        n, unit = max(minutes, 1), "minute"
    elif minutes < 48 * 60:
        n, unit = minutes // 60, "hour"
    else:
        n, unit = minutes // (24 * 60), "day"
    return f"{n} {unit}{'' if n == 1 else 's'}"


def _drain(words: str = "the app is restarting") -> _Check:
    """The lanes are draining (`Reason.RESTARTING`). Read from memory alone."""

    async def check(reads: _Reads, now: datetime | None) -> Barred | None:
        return Barred(Reason.RESTARTING, None, words) if draining(reads.owner, now) else None

    return check


def _pause(words: str = "the agents are paused") -> _Check:
    """The scout connection's global pause (`Reason.PAUSED`)."""

    async def check(reads: _Reads, now: datetime | None) -> Barred | None:
        conn = await reads.conn()
        return Barred(Reason.PAUSED, None, words) if conn is not None and conn.paused else None

    return check


def _peak(words: str = "busy hours", *, until: bool = False) -> _Check:
    """The owner avoids Claude's peak hours and the window is open (`Reason.PEAK`, read as `hours.in_peak`). `until`:
    the answer carries when the window ends."""

    async def check(reads: _Reads, now: datetime | None) -> Barred | None:
        settings = await reads.settings()
        if not hours.in_peak(settings, now):
            return None
        ends = hours.peak_ends(settings, now or utc_now()) if until else None
        return Barred(Reason.PEAK, ends, words)

    return check


def _blackout(*, hint: bool = True) -> _Check:
    """The same reason as the owner's own asks meet it (`hours.blackout`): the sentence that refuses the owner's work,
    with when the window ends. `hint` False leaves off how to run the work anyway, for a screen with no such box."""

    async def check(reads: _Reads, now: datetime | None) -> Barred | None:
        text = hours.blackout(await reads.settings(), now)
        if text is None:
            return None
        return Barred(Reason.PEAK, None, text if hint else text.removesuffix(hours.BLACKOUT_HINT))

    return check


def _absent() -> _Check:
    """No runner has checked in lately (`Reason.ABSENT`): the owner waits at the screen for a job, so one nobody will
    take is refused at once, saying how long it has been."""

    async def check(reads: _Reads, now: datetime | None) -> Barred | None:
        clock = now or datetime.now(UTC)
        conn = await reads.conn()
        heard = roster.last_heard(conn) if conn is not None else None
        if heard is None:
            return Barred(Reason.ABSENT, None, "No agent has checked in yet.")
        if clock - heard > roster.LIVE_FOR:
            return Barred(Reason.ABSENT, None, f"No agent has checked in for {_ago(clock - heard)}.")
        return None

    return check


def _limit(words: str | Callable[[str], str] = USAGE_LIMIT) -> _Check:
    """Claude's usage limit holds (`Reason.USAGE`, from either store: `limit_until`), and the answer carries when it
    lifts. `words` is the sentence, or a function of the time it lifts, in the owner's words."""

    async def check(reads: _Reads, now: datetime | None) -> Barred | None:
        lifts = _ahead(await reads.conn(), reads.owner, now or utc_now())
        if lifts is None:
            return None
        return Barred(Reason.USAGE, lifts, words if isinstance(words, str) else words(hours.when_words(lifts, now)))

    return check


# --- the askers ------------------------------------------------------------------------------------------------------

#: The checks each question asks, in the order it asks them: the first that stands is the answer. Orders and words
#: differ between questions on purpose; each row is what that asker did before, check by check.
_ROWS: dict[Question, tuple[_Check, ...]] = {
    Question.POOL: (_drain(), _pause(), _peak(), _limit()),
    Question.WORKER: (_pause("The owner has paused the agents."), _blackout()),
    Question.JOB: (
        _blackout(hint=False),
        _absent(),
        _pause("The agent is paused: it is handed no work until you resume it."),
        _limit(lambda when: f"{USAGE_LIMIT} is reached; it resets at {when}. Try again after that."),
    ),
    Question.CATCH_UP: (_pause(), _limit(), _peak(until=True)),
}


async def barred(session: AsyncSession, owner: str, question: Question, *,
                 now: datetime | None = None) -> Barred | None:
    """Why Claude may not work now for this question, or None (it may): the first check of the question's row that
    stands. `now` stays as the caller gave it (None is the wall clock, which the peak functions read), and the pool's
    callers give none."""
    reads = _Reads(session, owner)
    for check in _ROWS[question]:
        if (stop := await check(reads, now)) is not None:
            return stop
    return None


async def standing(session: AsyncSession, owner: str, question: Question, *,
                   now: datetime | None = None) -> list[Barred]:
    """Every check of the question's row that stands, in the row's order (empty: Claude may work), for a caller whose
    rule is its own: it counts some of the reasons and not others. Every check is read, so one call gives two answers
    from one read of the connection and the profile; `barred` is the one to ask when the first is all that is wanted."""
    reads = _Reads(session, owner)
    out: list[Barred] = []
    for check in _ROWS[question]:
        if (stop := await check(reads, now)) is not None:
            out.append(stop)
    return out
