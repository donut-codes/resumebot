"""What the dispatcher does with each topic it takes to-dos of.

A handler is today's step code, run when a to-do says to look: the queue decides when, the step decides what, and the
step still goes by the data as it is now, never by the to-do (a to-do says where to look, never what to do), so a
duplicate, a late or a reordered one cannot make a wrong move.

It takes these topics:

- `posting.sift`, `posting.roles`: the cheap filter and the making of roles, which the pipeline's turn did first
  of all. Each asks its probe (below) before it builds anything, so a run with nothing owed reads nothing of the 85,000
  postings and does not build every employer's rules to find that out (the old turn did, 86 times an hour).
- `role.details`: the code read of the descriptions roles lack (`pool.queue_details`), on the network lane, in no
  transaction while a page is read.
- `role.settle`, `deck.dirty`, `employer.limit`, `employer.research`: the topics of the pipeline's turn
  (`consume.TURN_TOPICS`). The handler runs the whole turn once for the to-dos of all four (whole form: a key is read as
  the whole owner; scoped reads are a later phase), and says when it stopped short at a cap (`more`).
- `pool.owed`: the pool's pull (`session_kinds.pull`: the Haiku ranking, the deck's Sonnet reads, the
  employers' research), once, whole form, for a free pool session that asked lately; with none it reads nothing.
- `browser.reconcile`: the browser queue's refresh (`browser.queue.refresh`), once (the to-do says it is
  time), never oftener than its own 30 second pace.
- the thirteen `ready.<kind>` and `dispatch.idle`: the runner dispatcher's pass (`dispatcher.dispatch_now`: an
  idle lane is commanded with the work a session could take), once, for the to-dos of every kind and every lane that
  went idle (the idle edge is taken here: a lane freed while work waited would otherwise
  wait for the minute tick).
- `tick.minute`, `tick.hour`: two persisted to-dos that post themselves again with a due time. The minute tick runs the
  small housekeeping parts that notice something only the clock can (an orphaned search, a job no session took, a lease
  that ran out), the probes, a nudge to the pull while a free session asked lately (the hours, the day's room and the
  gap between searches change with no write) and the runner dispatcher's pass, as the quiet pace of a minute did; the
  hour tick runs the rest (`housekeeping.HOUR`), the janitor, the clearing of the stored copies of postings that a
  newer copy replaced over seven days ago (`housekeeping.clear_old_copies`), the log line of what the dispatcher
  handled, the age-out (once for each day of the owner's: the roles the day has put past the owner's freshness pass
  are stopped, `gating.age_out`), and then, in one transaction, the sweep and the clock's own look at the deck and the
  employers.

**Probes.** Where the owed state is already a column predicate, one bounded count through a narrow partial index
asks "is anything owed?": a posting that waits for the filter, one that waits for a role. If something is owed and
no to-do of its topic waits, a writer did not post: the minute tick posts it (the miss costs a minute, not an hour)
and logs it.

**The sweep.** Once an hour, and at every start, each topic is posted once with the key `*` ("anything of this topic may
have changed"); the handler runs its whole-owner path. A run that only the sweep asked for and that finds work is a
catch, logged as `sweep_caught topic=... n=...`: it means one writer needs wiring. It is never shown in the app.
The hour's own look at what only time changes is posted with it, in the same transaction, so that no turn can read
the one without the other. What the clock's look moves in the four steps time drives is logged apart
(`sweep_clock`, at info), not thrown away: a writer that did not post for one of those steps hides inside it, so
the zero-catch week proves the other steps and the writer-by-writer test is the reference for these four.

A handler returns what it changed and whether it stopped short (`more`: it posts itself again), and a tick says when it
comes due again (`again`)."""

import logging
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import ColumnElement, Select, func, select

from app import db
from app.jobsearch.models import JobPosting
from app.jobsearch.work import housekeeping, queue
from app.jobsearch.work.queue import consume, janitor, service, topics
from app.settings import settings
from app.timeutil import iso_utc, owner_today, utc_now

log = logging.getLogger(__name__)

#: What the turn's steps change (by their names in the count `pipeline.run_in_steps` returns) that the passing of time
#: alone moves: a role leaving its first 72 hours or growing stale changes the places on the deck and the roles marked
#: to be read again, and an employer's research grows old; and a role the clock moved into Ready to Launch has its
#: posting checked by the same turn (`ready_check`, docs/PRD.md 24.16). The hour tick asks for those looks (cause
#: `clock`), so what they move at that look is not counted as a catch (the sweep's net for a writer that did not post)
#: but logged apart (`Outcome.clocked`): the net cannot tell the clock's moves from a missed writer's in these five
#: steps.
TIME_DRIVEN = ("marked", "ready", "deck", "employers", "ready_check")
#: The pause a topic keeps between two runs: no topic runs more often than the loop it replaced. The three that were
#: steps of the pipeline's turn keep that loop's pause of 30 seconds (a test pins it); the first run after a quiet
#: spell is at once.
TURN_GAP = timedelta(seconds=30)
#: The pause the pull and the walk keep between two runs: 10 seconds, the pause of the housekeeping loop they were parts
#: of (a test pins it). The browser's queue keeps the pace it always had (30 seconds, pinned too).
HOUSEKEEPING_GAP = timedelta(seconds=10)
BROWSER_GAP = timedelta(seconds=30)
#: How soon a tick comes due again.
MINUTE_EVERY = timedelta(minutes=1)
HOUR_EVERY = timedelta(hours=1)
#: The most a probe counts: it only asks whether anything is owed, and how much, roughly, for the log.
PROBE_LIMIT = 1000


@dataclass
class Tally:
    """What the dispatcher handled since the hour tick last said so: for the hour's one log line (the recording of R,
    and how long to-dos waited), and for the tests. Never shown in the app."""

    handled: Counter = field(default_factory=Counter)
    waited: dict[str, float] = field(default_factory=dict)
    caught: Counter = field(default_factory=Counter)
    missed: Counter = field(default_factory=Counter)
    #: What the clock's look moved at the hour's sweep in the steps time drives, by the topic of the run: not a catch,
    #: and not lost (`Outcome.clocked`).
    clocked: Counter = field(default_factory=Counter)
    #: The owner's day the age-out last finished for (`age_out`): kept here, in the dispatcher's own memory, so a start
    #: looks once and a test's new dispatcher looks again.
    aged_on: date | None = None

    def note(self, topic: str, rows: int, waited: float) -> None:
        self.handled[topic] += rows
        self.waited[topic] = max(self.waited.get(topic, 0.0), waited)

    def take(self) -> str:
        """The hour's line (empty when nothing was handled), and nothing kept after it."""
        line = ", ".join(f"{topic}={n} (waited at most {self.waited.get(topic, 0.0):.0f}s)"
                         for topic, n in sorted(self.handled.items()))
        self.handled.clear()
        self.waited.clear()
        return line


@dataclass(frozen=True)
class Run:
    """What a handler is told: whose, which to-dos (the group it read), when, the dispatcher's tally, and the topics the
    dispatcher takes to-dos of that a writer posts (what the hour's sweep posts `*` for)."""

    owner: str
    group: service.Group
    now: datetime
    tally: Tally
    swept: tuple[str, ...] = ()


@dataclass(frozen=True)
class Outcome:
    """What a handler did: how much it changed (for the sweep's count of what it caught), whether it stopped short and
    posts itself again (`more`), and, for a tick, when it comes due again (`again`)."""

    changed: int = 0
    more: bool = False
    again: timedelta | None = None
    #: The run found nothing owed and read next to nothing (a probe's one indexed count): it starts no pause, so the
    #: change that comes a moment later is not made to wait out the pause of a look that did no work.
    cheap: bool = False
    #: What the steps time drives moved at a look the clock asked for, by step (`TIME_DRIVEN`): not in `changed`, so
    #: not a catch, and said apart by the dispatcher when the sweep and the clock were all that asked.
    clocked: tuple[tuple[str, int], ...] = ()


@dataclass(frozen=True)
class Handler:
    """One topic's handler. `name` is the loop's name (None: the clock's, which runs whenever the dispatcher does),
    `lane` the lane it runs on (`db`: two at once, `net`: one, for a page read), `priority` the order groups are
    started in (the order of today's steps), `gap` the pause kept between two runs. A handler may take the to-dos of
    several topics (`also`, beside its own `topic`): it runs once for all of them, whatever the mix, and its group is
    under its own topic."""

    topic: str
    name: str | None
    lane: str
    priority: int
    gap: timedelta
    run: Callable[[Run], Awaitable[Outcome]]
    also: tuple[str, ...] = ()

    @property
    def takes(self) -> tuple[str, ...]:
        """Every topic whose to-dos this handler takes."""
        return (self.topic, *self.also)


# --- the probes -----------------------------------------------------------------------------------------------------


def owed_statement(where: tuple[ColumnElement[bool], ...]) -> Select:
    """How many postings match `where` (at most `PROBE_LIMIT`): one read through the narrow partial index that holds
    them."""
    return select(func.count()).select_from(select(JobPosting.id).where(*where).limit(PROBE_LIMIT).subquery())


def _sift_where(owner: str) -> tuple[ColumnElement[bool], ...]:
    from app.jobsearch.gating import sift

    return sift.waiting_where(owner)


def _roles_where(owner: str) -> tuple[ColumnElement[bool], ...]:
    from app.jobsearch.pipeline import from_posting

    return from_posting.makeable_where(owner, probing=True)


#: (loop name, topic, the predicate of what is owed, what to say): the work the postings leave undone.
PROBES: tuple[tuple[str, str, Callable[[str], tuple[ColumnElement[bool], ...]], str], ...] = (
    (consume.SIFT, topics.POSTING_SIFT, _sift_where, "wait for the filter"),
    (consume.ROLES, topics.POSTING_ROLES, _roles_where, "wait for a role"),
)


async def owed(where: tuple[ColumnElement[bool], ...]) -> int:
    """How many postings match `where`, in a reader's session of its own: the look takes no writer turn (inside the
    writer's session it would hold the turn)."""
    async with db.read_session_factory()() as session:
        count = await session.scalar(owed_statement(where))
    return int(count or 0)


async def probe(owner: str, tally: Tally | None = None) -> list[str]:
    """Ask each probe whether anything is owed; where it is and no to-do of the topic waits (due or not, running or
    not), a writer did not post: post it now and say so. Returns the topics it posted. A probe that fails is logged and
    the others still ask: the minute tick must not stop for it. Both looks are made in a reader's session, so a minute
    that finds nothing takes no turn; the turn is taken only to post, and the to-do's absence is asked again there, so
    two probes never post twice."""
    posted: list[str] = []
    for _name, topic, where_of, words in PROBES:
        try:
            async with db.read_session_factory()() as reader:
                count = await reader.scalar(owed_statement(where_of(owner)))
                if not count or await reader.scalar(service.waiting_statement(owner, topic)) is not None:
                    continue
            async with db.session_factory()() as session:
                if await session.scalar(service.waiting_statement(owner, topic)) is not None:
                    await session.commit()
                    continue
                queue.post(session, owner, topic, [""], cause="probe")
                await session.commit()
        except Exception:
            log.warning("work-queue probe: the look for postings that %s failed", words, exc_info=True)
            continue
        posted.append(topic)
        if tally is not None:
            tally.missed[topic] += int(count)
        log.warning("work-queue probe: %s postings %s and no %s to-do was posted: a writer did not post", count, words,
                    topic)
    return posted


# --- the posting steps ----------------------------------------------------------------------------------------------


async def _ask_for_a_turn(owner: str) -> None:
    """Ask for a turn now, as the old turn ran again straight after it made roles or read a description: two to-dos
    (cause `ask`, so the turn does not wait out its pause), saved in a transaction of their own
    (`schedule.ask_for_turn`)."""
    from app.jobsearch.loops import schedule

    await schedule.ask_for_turn(owner)


async def posting_sift(run: Run) -> Outcome:
    """`sift.sift`, as the turn ran it (a transaction of its own; the filter writes in slices of 250 and commits each),
    when a posting waits for it. A posting another run filtered meanwhile, or one a transaction held, is left for the
    next look: `more` says so."""
    from app.jobsearch.gating import sift
    from app.jobsearch.profile import store as profile_store

    where = _sift_where(run.owner)
    before = await owed(where)
    if not before:
        return Outcome(cheap=True)
    async with db.session_factory()() as session:
        profile = await profile_store.current(session, run.owner)
        await db.release_if_free(session)  # the profile read took the writer's turn: the filter's reads take none
        await sift.sift(session, run.owner, profile)
        await session.commit()
    return Outcome(changed=before, more=await owed(where) > 0)


async def posting_roles(run: Run) -> Outcome:
    """`role_from_posting.make_roles`, as the turn ran it (a batch of 300, newest first, in a transaction of its own),
    when a posting waits for a role. More waiting than the batch takes: `more`."""
    from app.jobsearch.pipeline import from_posting

    where = _roles_where(run.owner)
    before = await owed(where)
    if not before:
        return Outcome(cheap=True)
    async with db.session_factory()() as session:
        made = await from_posting.make_roles(session, run.owner)
        await session.commit()
    if made:
        await _ask_for_a_turn(run.owner)
    return Outcome(changed=before, more=await owed(where) > 0)


async def role_details(run: Run) -> Outcome:
    """`pool.queue_details`, as the turn ran it (40 postings or 40 seconds, in no transaction while a page is read).
    Anything filled: the turn is asked for. Anything settled, which is a posting no longer owed, whether its page gave
    text or not (a read that was begun marks the posting tried, so it leaves the owed set until its retry comes due,
    `index._retry_when_due`): `more`, since the next batch is owed as the turn took it 30 seconds later. Nothing settled
    stops it: what no read was begun for waits for a change that posts, or the sweep."""
    from app.jobsearch.pool import core as pool

    read = await pool.read_details(run.owner, pool.DETAILS_PER_TURN, pool.DETAILS_TURN_SECONDS)
    if read.filled:
        await _ask_for_a_turn(run.owner)
    return Outcome(changed=read.filled, more=read.settled > 0)


# --- the turn -------------------------------------------------------------------------------------------------------


async def turn(run: Run) -> Outcome:
    """The pipeline's whole turn (`schedule.queued_turn`), once for the to-dos of the four topics of its steps: the gate
    facts of the roles before Ready, then each step in a transaction of its own, as `pipeline_pass` runs them. What it
    changed is the outcome's count, for the sweep's catch, less what time alone moves when the clock asked too
    (`TIME_DRIVEN`), which is the outcome's `clocked`. A turn that stopped short at a cap (the rounds, the gate facts
    read) posts itself again: the old loop's next turn took what was left, and a turn run from to-dos has no clock to
    do it. Its own writes post none of the four topics (the dispatcher suppresses them), since the turn goes on until
    its rounds move nothing."""
    from app.jobsearch.loops import schedule

    report = await schedule.queued_turn(run.owner)
    timed = topics.CLOCK_CAUSE in run.group.causes
    clocked = {step: n for step, n in report.moved.items() if timed and step in TIME_DRIVEN and n}
    changed = sum(n for step, n in report.moved.items() if step not in clocked)
    return Outcome(changed=changed, more=report.more, clocked=tuple(sorted(clocked.items())))


# --- the pull, the browser's queue and the walk -----------------------------------------------------------


async def pool_owed(run: Run) -> Outcome:
    """`session_kinds.pull`, as the housekeeping pass ran it: start the next job for a free pool session, under every
    limit an ask keeps to. Only while a free pool session asked lately: with none the look reads nothing and starts no
    pause. The job it starts is queued ahead of the session that takes it, and the taking (a state the hook watches)
    asks for the next."""
    from app.jobsearch.work import session_kinds

    if not session_kinds.pool_asked(run.owner):
        return Outcome(cheap=True)
    async with db.session_factory()() as session:
        started = await session_kinds.pull(session, run.owner)
        await session.commit()
    return Outcome(changed=started)


async def browser_reconcile(run: Run) -> Outcome:
    """`browser.queue.refresh`, since a to-do says it is time: an item for
    each piece of browser work that has none open, and an open one no longer called for dropped. What it changed is the
    outcome's count, for the sweep's catch."""
    from app.jobsearch.browser import queue as browser_queue

    async with db.session_factory()() as session:
        changed = await browser_queue.refresh(session, run.owner)
        await session.commit()
    return Outcome(changed=changed)


async def walk(run: Run) -> Outcome:
    """The runner dispatcher's pass (`dispatcher.dispatch_now`: the pace of a pass that found nothing is dropped, since
    work was just made ready or a lane just went idle), once for the to-dos of every kind and every lane: each idle
    lane is commanded with the work a session could take. More waiting for a lane that is not idle yet: it posts
    itself again, as the old pass was a few seconds on while it found work. Without a wake address the pass does
    nothing, and the look is cheap."""
    from app.jobsearch.work import dispatcher

    if not settings.wake_url:
        return Outcome(cheap=True)
    commanded = await dispatcher.dispatch_now(run.owner)
    return Outcome(changed=commanded, more=commanded > 0)


async def nudge_pool(owner: str, now: datetime | None = None) -> bool:
    """Ask for a pull while a free pool session asked lately, at most once in `session_kinds.NUDGE_EVERY` (five minutes:
    the reason is there): what the hours, the day's room, the gap between searches and Claude's usage limit change with
    no write, so no writer can post it. The minute tick does it. True when asked."""
    from app.jobsearch.work import session_kinds

    if not session_kinds.nudge_due(owner, now):
        return False
    async with db.session_factory()() as session:
        queue.post(session, owner, topics.POOL_OWED, [""], cause="nudge")
        await session.commit()
    return True


# --- the ticks ------------------------------------------------------------------------------------------------------


async def _housekeeping(owner: str, parts: tuple[str, ...]) -> None:
    """Run these parts of the housekeeping, and say so for the health door: the last pass is the queue's own."""
    async with db.session_factory()() as session:
        await housekeeping.settle(session, owner, parts)
        await session.commit()
    housekeeping.last_pass = utc_now()


async def stamp_passes(owner: str) -> None:
    """Write when housekeeping and the runner dispatcher last passed (`housekeeping.last_pass`, `dispatcher.last_pass`:
    this program's memory) to the engine's facts `last_pass:housekeeping` and `last_pass:dispatcher`, where the owner
    door's `health` reads them in any program. A pass not made since the start is not written: its fact keeps the time
    it had. It always has something to write once a pass was made, so it takes the writer's turn, briefly. It is the
    tick's own work and never stops the rest of it: a write that fails is logged and tried again at the next tick."""
    from app.jobsearch import facts
    from app.jobsearch.work import dispatcher

    passed = {"last_pass:housekeeping": housekeeping.last_pass, "last_pass:dispatcher": dispatcher.last_pass}
    stamps = {key: iso_utc(at) for key, at in passed.items() if at is not None}
    if not stamps:
        return
    try:
        async with db.session_factory()() as session:
            for key, value in stamps.items():
                await facts.put(session, owner, key, value)
            await session.commit()
    except Exception:
        log.warning("work-queue: writing the last passes for the health door failed; the next minute tries again",
                    exc_info=True)


#: What the engine last saw of whether an agent is ready to take a piece of Claude work, by owner: the answer of
#: `job_queue.ready`, (ready, why). The engine's own memory, never shown: it only decides whether a minute's look is
#: worth a notice.
_told_ready: dict[str, tuple[bool, str | None]] = {}


async def tell_ready(owner: str) -> bool:
    """Whether an agent is ready (`GET /agent/ready`) changes with the clock as well as with writes: Claude's peak hours
    begin and end, the runner's last check-in grows old. No write tells the page, so the minute tick looks
    (`job_queue.ready`, on a reader: the look takes no writer's turn) and, when the answer differs from the one it last
    saw, tells the page with a `changed` item of the area `ready`, through the sink in use. The first look after the
    engine starts only records the answer (the engine says `resync` when it is ready). It is the tick's own work and
    never stops the rest of it: a look that fails is logged and tried again at the next tick. True when it told."""
    from app.jobsearch.pipeline import live_read, live_sink
    from app.jobsearch.work import job_queue

    try:
        async with db.read_session_factory()() as reader:
            answer = await job_queue.ready(reader, owner)
    except Exception:
        log.warning("work-queue: the look at whether an agent is ready failed; the next minute tries again",
                    exc_info=True)
        return False
    before = _told_ready.get(owner)
    _told_ready[owner] = answer
    if before is None or before == answer:
        return False
    live_sink.current().announce(owner, [(live_read.CHANGED, "ready", ("ready",))])
    return True


async def tick_minute(run: Run) -> Outcome:
    """The housekeeping parts that notice a change only the clock makes (`housekeeping.MINUTE` and `LEASES`), then the
    probes, the pool's nudge and the runner dispatcher's pass, and last the time of the last passes written for the
    health door (`stamp_passes`), and then whether an agent is ready, told to the page when it changed with the clock
    (`tell_ready`)."""
    await _housekeeping(run.owner, (*housekeeping.MINUTE, *housekeeping.LEASES))
    posted = await probe(run.owner, run.tally)
    await nudge_pool(run.owner, run.now)
    from app.jobsearch.work import dispatcher

    await dispatcher.dispatch_now(run.owner)  # the tick is the pace of a minute: a pass that found nothing is over
    await stamp_passes(run.owner)
    await tell_ready(run.owner)
    return Outcome(changed=len(posted), again=MINUTE_EVERY)


async def tick_hour(run: Run) -> Outcome:
    """The rest of the housekeeping (`housekeeping.HOUR`), the janitor, the old stored copies of postings, the hour's
    line in the log, the age-out, and the sweep with the clock's look, posted in one transaction: two commits let the
    dispatcher, woken by the first, read the sweep's to-dos alone and run a turn whose time-driven moves it then counts
    as catches."""
    await _housekeeping(run.owner, housekeeping.HOUR)
    await janitor.prune(run.now)
    await clear_old_copies(run)
    if line := run.tally.take():
        log.info("work-queue handled in the last hour: %s", line)
    await age_out(run)
    await settle_order(run)
    await forget_old_facts(run)
    async with db.session_factory()() as session:
        post_sweep(session, run.owner, run.swept)
        post_clock(session, run.owner, run.swept)
        await session.commit()
    return Outcome(again=HOUR_EVERY)


async def clear_old_copies(run: Run) -> None:
    """The hour tick clears the stored copies of postings that a newer copy replaced over seven days ago by the tick's
    clock (`housekeeping.clear_old_copies`, docs/PRD.md 47.9), a capped number at a tick. It is the tick's own work and
    never stops the rest of it: a clearing that fails is logged and tried again at the next tick, and what it cleared
    before it failed stays cleared."""
    try:
        await housekeeping.clear_old_copies(run.now)
    except Exception:
        log.warning("work-queue: clearing the old stored copies of postings failed; the next hour tries again",
                    exc_info=True)


async def age_out(run: Run) -> None:
    """The first hour tick of each of the owner's days stops the roles the day has aged past the owner's freshness pass
    (`gating.age_out`, docs/PRD.md 40.6): no write says a role crossed it, so only the clock can ask. The look is a read
    of stored dates and runs once for a day (and once at each start, whose dispatcher has no memory of one), not
    once for each tick. It is the tick's own work and never stops the rest of it: a look that fails is logged and
    tried again at the next tick, and so is one that left a role another transaction held."""
    from app.jobsearch.gating import age_out as ageing

    today = owner_today(run.now)
    if run.tally.aged_on == today:
        return
    try:
        async with db.session_factory()() as session:
            aged = await ageing.run(session, run.owner, today=today)
            await session.commit()
    except Exception:
        log.warning("work-queue: the look at roles aged past the freshness pass failed; the next hour tries again",
                    exc_info=True)
        return
    if not aged.left:
        run.tally.aged_on = today
    if aged.closed or aged.left:
        log.info("work-queue age-out: %d of %d roles stopped for their age, %d held by another transaction "
                 "(tried again at the next hour)", aged.closed, aged.checked, aged.left)


async def forget_old_facts(run: Run) -> None:
    """The engine's facts that matter for a day only (the answers and said-lines of `facts.age_out`) are forgotten at
    the hour tick. It is the tick's own work and never stops the rest of it: a look that fails is logged and tried again
    at the next tick."""
    from app.jobsearch import facts

    try:
        async with db.session_factory()() as session:
            gone = await facts.age_out(session, run.owner, run.now)
            await session.commit()
    except Exception:
        log.warning("work-queue: the age-out of old engine facts failed; the next hour tries again", exc_info=True)
        return
    if gone:
        log.info("work-queue: %d old engine facts forgotten", gone)


async def order_is_behind(owner: str, now: datetime) -> bool:
    """Whether the stored order has anything to settle (`stored_order.has_work`: a line a trigger wrote, or a role
    whose instant has passed), asked on a reader: two reads that stop at the first row, and no writer's turn. What the
    loop asks at every look (`loop.Dispatcher.settle_order`)."""
    from app.jobsearch.pipeline import stored_order  # the pipeline reads this package

    async with db.read_session_factory()() as session:  # a reader: no writer turn, no wait
        behind = await stored_order.has_work(session, owner, now)
        await session.commit()
    return behind


async def settle_order_at(owner: str, now: datetime) -> bool:
    """Settle the stored order (`pipeline/stored_order.py`, docs/PRD.md 24.19) in a transaction of its own: the roles
    the triggers have named, and the ones whose instant has passed by `now`, are worked out again. This is what puts a
    role in its place on the board, which only reads the order (the owner, 2026-11-10: the page program runs no work).
    It never raises: a look that fails is logged and the lines stay for the next one. True when the look was made,
    False when it failed."""
    from app.jobsearch.pipeline import stored_order  # the pipeline reads this package

    try:
        async with db.session_factory()() as session:
            done = await stored_order.settle(session, owner, now)
            await session.commit()
    except Exception:
        log.warning("work-queue: the look at the stored order failed; the next look tries again", exc_info=True)
        return False
    if done:
        log.info("work-queue stored order: %d roles settled", done)
    return True


async def settle_order(run: Run) -> None:
    """The hour's look at the stored order: a role whose first hours end or whose posting turns stale as a day turns is
    changed by no write, so only the clock can ask. What a write names is settled by the loop as soon as it looks
    (`loop.Dispatcher.settle_order`); the tick takes what is left. It is the tick's own work and never stops the rest
    of it (`settle_order_at` never raises)."""
    await settle_order_at(run.owner, run.now)


def post_sweep(session: Any, owner: str, which: tuple[str, ...], cause: str = "sweep") -> int:
    """Post `*` once for each of `which`, the topics a writer posts that the dispatcher takes to-dos of: the handlers
    then run their whole-owner path, which is today's code unchanged. The net under every writer, and the catch-up
    after a start (cause `start`). In `session`'s transaction; never commits. Returns how many it posted."""
    return sum(queue.post(session, owner, topic, [topics.ANY], cause=cause) for topic in which)


def post_clock(session: Any, owner: str, which: tuple[str, ...]) -> int:
    """The hour tick's own look at what only time changes (cause `clock`): the deck (the first 72 hours, the stale days,
    the owner's day rolling over) and the employers of the deck (research grown old). Posted only when the turn's
    topics are among `which` (the topics the dispatcher takes to-dos of). In
    `session`'s transaction, beside the sweep's; never commits. Returns how many it posted."""
    if topics.DECK_DIRTY not in which:
        return 0
    return (queue.post(session, owner, topics.DECK_DIRTY, [""], cause=topics.CLOCK_CAUSE)
            + queue.post(session, owner, topics.EMPLOYER_RESEARCH, [topics.ANY], cause=topics.CLOCK_CAUSE))


async def sweep(owner: str, which: tuple[str, ...], cause: str = "sweep") -> int:
    """`post_sweep` in a transaction of its own: the catch-up after a start (cause `start`). Returns how many it
    posted."""
    async with db.session_factory()() as session:
        made = post_sweep(session, owner, which, cause)
        await session.commit()
    return made


async def engine_ask(run: Run) -> Outcome:
    """The questions the page put to the engine (`engine_calls.ask`): each `ask:<id>` fact the to-dos name (every one
    for `*`, which the start and the hour's sweep post) that has no `answer:<id>` yet is answered by the function
    registered under its name (`engine_asks`), and the page is told. A look that finds nothing owed reads on a reader
    and takes no turn. It counts nothing as changed: an ask is a request, never a writer's miss, so a sweep that
    answers one is not a catch."""
    from app.jobsearch.work import engine_asks

    answered = await engine_asks.answer_due(run.owner, run.group.keys, run.now)
    return Outcome(cheap=not answered)


#: The one handler of the turn's four topics (priority after the posting steps: the roles they make are the turn's).
TURN_HANDLER = Handler(topics.ROLE_SETTLE, consume.TURN, "db", 5, TURN_GAP, turn,
                       also=tuple(t for t in consume.TURN_TOPICS if t != topics.ROLE_SETTLE))

#: The one handler of the thirteen `ready.<kind>` topics and `dispatch.idle`. Its own lane: a pass that waits for a
#: lane's answer must not wait behind the turn.
WALK_HANDLER = Handler(consume.WALK_TOPICS[0], consume.WALK, "runner", 8, HOUSEKEEPING_GAP, walk,
                       also=consume.WALK_TOPICS[1:])

HANDLERS: dict[str, Handler] = {
    **{h.topic: h for h in (
        Handler(topics.TICK_MINUTE, None, "db", 0, timedelta(0), tick_minute),
        Handler(topics.TICK_HOUR, None, "db", 1, timedelta(0), tick_hour),
        Handler(topics.POSTING_SIFT, consume.SIFT, "db", 2, TURN_GAP, posting_sift),
        Handler(topics.POSTING_ROLES, consume.ROLES, "db", 3, TURN_GAP, posting_roles),
        Handler(topics.ROLE_DETAILS, consume.DETAILS, "net", 4, TURN_GAP, role_details),
        Handler(topics.BROWSER_RECONCILE, consume.BROWSER, "db", 6, BROWSER_GAP, browser_reconcile),
        Handler(topics.POOL_OWED, consume.PULL, "db", 7, HOUSEKEEPING_GAP, pool_owed),
        Handler(topics.ENGINE_ASK, consume.ASKS, "ask", 9, timedelta(0), engine_ask),
    )},
    **dict.fromkeys(TURN_HANDLER.takes, TURN_HANDLER),
    **dict.fromkeys(WALK_HANDLER.takes, WALK_HANDLER),
}
