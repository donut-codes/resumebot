"""The schedule: every few minutes the clock is looked at, every mission that is due runs (docs/PRD.md path 18), and the
scout is woken for what they started. Started with the app (`main.lifespan`)."""

import asyncio
import logging
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import db
from app import settings as app_settings
from app.jobsearch.browser import go_checks
from app.jobsearch.gating import check as gating
from app.jobsearch.gating import gate_facts
from app.jobsearch.missions import core as missions
from app.jobsearch.models import Todo
from app.jobsearch.pipeline import deck as pipeline
from app.jobsearch.pool import core as pool
from app.jobsearch.pool import cost_report
from app.jobsearch.work import dispatcher, queue, wake
from app.jobsearch.work.queue import consume, coverage, topics
from app.settings import settings
from app.sites import agent_calls, wake_ops
from app.timeutil import utc_now

log = logging.getLogger(__name__)

SCHEDULE_CHECK_SECONDS = 300

#: The wake for a project the night's mission just started: its first task is a plan to write.
NEW_PROJECT_WAKE = agent_calls.Wake("project_created", "plan")


async def send_knocks(knocks: list[tuple[str, str]]) -> None:
    """Wake the scout for what a mission started, once its commit has landed. A site project's wake is sent with its
    retries (`wake_ops`); the others are one knock each."""
    sites = []
    for key, reason in knocks:
        if reason == wake.SITE_PROJECT and key != "waiting":
            sites.append((key, NEW_PROJECT_WAKE))
        else:
            await wake.knock(key, reason=reason)
    await wake_ops.send_all(sites)
    if knocks:  # work was just made ready: a pass of the dispatcher now, not at the next housekeeping pass (path 47.1)
        await dispatcher.dispatch_now()


@db.background_work
async def schedule_pass() -> list[str]:
    """One look at the clock: every mission that is due runs (docs/PRD.md path 18), and the scout is woken for what
    they started. Returns the ids it woke the scout for. Never raises."""
    try:
        async with db.session_factory()() as session:
            knocks = await missions.schedule_pass(session, settings.owner)
            await session.commit()
        await send_knocks(knocks)
        return [key for key, _ in knocks]
    except Exception:  # the schedule must outlive any one bad pass (a database blip)
        log.warning("the schedule pass failed", exc_info=True)
        return []


#: How seldom the owner's mail is read for the roles Go opened, and when it was last read.
OPENED_MAIL_EVERY = timedelta(minutes=30)
_opened_read: datetime | None = None


async def settle_opened(now: datetime | None = None) -> int:
    """Read the mail for the roles Go opened and move the ones it confirms to Applied, at most every
    `OPENED_MAIL_EVERY`. Returns how many moved."""
    global _opened_read
    now = now or datetime.now(UTC)
    if _opened_read is not None and now - _opened_read < OPENED_MAIL_EVERY:
        return 0
    _opened_read = now
    async with db.session_factory()() as session:
        moved = await go_checks.applied_by_mail(session, settings.owner, now)
        await session.commit()
    return moved


#: How long `pipeline_turn` waits for the turn it asked for, and how often it looks.
TURN_WAIT_SECONDS, TURN_LOOK_SECONDS = 300.0, 0.25
#: One turn at a time: a turn that is still reading descriptions when the next is due makes the next wait its turn.
_pipeline_lock = asyncio.Lock()


def signal_after_commit(session: AsyncSession) -> None:
    """Ask for a turn with this session's change: the two to-dos that ask for one (`turn_to_dos`) are saved in its
    transaction, so the dispatcher's turn answers what the request wrote, and a rollback takes them. Nothing runs after
    the commit."""
    turn_to_dos(session, settings.owner)


def turn_to_dos(session: AsyncSession, owner: str, *, now: datetime | None = None) -> int:
    """Post the to-dos that ask for a turn, in `session`'s transaction: `role.settle` for the whole owner and
    `deck.dirty` (cause `ask`, so the dispatcher does not make them wait out the pause of the turn before). Never
    commits. Returns how many it added."""
    return (queue.post(session, owner, queue.ROLE_SETTLE, [queue.ANY], cause=topics.ASK, now=now)
            + queue.post(session, owner, queue.DECK_DIRTY, [""], cause=topics.ASK, now=now))


async def ask_for_turn(owner: str) -> list[int]:
    """Ask the dispatcher for a turn in a transaction of its own (`turn_to_dos`), and return the ids of the to-dos it
    posted: a turn that has read them has begun after the ask (`turn_waiting`). The commit wakes the dispatcher."""
    now = utc_now()
    async with db.session_factory()() as session:
        turn_to_dos(session, owner, now=now)
        await session.flush()
        ids = list(await session.scalars(select(Todo.id).where(
            Todo.owner == owner, Todo.topic.in_(consume.TURN_TOPICS), Todo.cause == topics.ASK,
            Todo.posted_at == now)))
        await session.commit()
    return ids


async def turn_waiting(ids: list[int]) -> bool:
    """Whether the turn has still to answer these to-dos: any of them is there and no run has failed on it. A run that
    read them deletes them when it ends; one that failed leaves them with an attempt counted, and then the ask is
    covered by that turn, as a failed turn covered the asks before it in the old loop (no one waits on it for ever)."""
    async with db.read_session_factory()() as session:
        waiting = await session.scalar(
            select(func.count()).select_from(Todo).where(Todo.id.in_(ids), Todo.attempts == 0))
    return bool(waiting)


async def pipeline_turn(owner: str | None = None, *, wait: float = TURN_WAIT_SECONDS) -> bool:
    """Ask for a turn and wait until one that began after the ask has finished: for Launch, which takes the roles that
    moved up into Ready to Launch next. The ask is two to-dos (`ask_for_turn`) and the wait is for them: a run of the
    dispatcher that read them has finished (they are deleted), or failed on them. False when no such turn finished
    within `wait` seconds (logged)."""
    return await _turn_from_the_queue(owner or settings.owner, wait)


async def _turn_from_the_queue(owner: str, wait: float) -> bool:
    deadline = time.monotonic() + wait
    ids = await ask_for_turn(owner)
    while True:
        if not await turn_waiting(ids):
            return True
        if time.monotonic() >= deadline:
            log.warning("the pipeline's turn asked for did not finish within %s seconds", wait)
            return False
        await asyncio.sleep(TURN_LOOK_SECONDS)


#: The gate facts are read this many roles to a transaction, and at most this many transactions a turn
#: (`read_gate_facts`).
GATE_FACTS_BATCH, GATE_FACTS_BATCHES = 8, 50


async def read_gate_facts(owner: str) -> int:
    """Read the gate facts of the roles before Ready not read in their present state: which they are is read once
    (`gate_facts.stale`: a transaction that takes no lock, and none that writes when nothing is stale), then each
    batch of `GATE_FACTS_BATCH` of them (`gate_facts.read_batch`) in a transaction of its own, committed before the
    next begins, so no role's row lock or connection is held for longer than one batch. The browser's queue holds back
    a role not read yet (`browser_queue._wanted`). Returns how many were read. Never raises: a failure is logged and
    the turn goes on."""
    read = 0
    try:
        async with db.read_session_factory()() as session:
            ids = await gate_facts.stale(session, owner, limit=GATE_FACTS_BATCH * GATE_FACTS_BATCHES)
            await session.commit()
        if len(ids) >= GATE_FACTS_BATCH * GATE_FACTS_BATCHES:
            pipeline.note_more()  # the most it may take: more may be stale, and the turn is owed again
        for start in range(0, len(ids), GATE_FACTS_BATCH):
            async with db.read_session_factory()() as reader:  # the rules are built before the writer turn begins
                rules = await gating.rules(reader, owner)
                await reader.commit()
            async with db.session_factory()() as session:
                read += (await gate_facts.read_batch(
                    session, owner, ids[start:start + GATE_FACTS_BATCH], rules))["looked"]
                await session.commit()
    except Exception:
        log.warning("the gate facts were not read", exc_info=True)
    return read


@dataclass
class TurnReport:
    """What a turn did, for the queue's handler: what its steps changed by their names (`pipeline.run_in_steps`'s count,
    and `gate_facts` for the roles whose gate facts were read), and whether it stopped short at a cap, so another turn
    is owed."""

    moved: dict[str, int] = field(default_factory=dict)
    more: bool = False


#: The steps of the turn that the work queue's dispatcher does on their own, from the to-dos of their topics
#: (`posting.sift`, `posting.roles`, `role.details`): its turn (`queued_turn`) leaves them out. `pipeline_pass`, the
#: whole turn that tests and tools run, leaves none out.
QUEUE_STEPS = frozenset({"sift", "make_roles", "details"})


async def run_turn(owner: str, excluded: frozenset[str] = frozenset()) -> dict[str, int]:
    """One turn's work, with no lock and no accounting: the gate facts the roles before Ready lack (`read_gate_facts`),
    the steps (`pipeline.run_in_steps`), and the read of descriptions. `excluded` names the steps left out: `sift` and
    `make_roles` are steps of `run_in_steps`, and `details` is the read of descriptions made here. Returns what the
    steps changed, by name, summed. Raises what the steps raise."""
    moved: Counter[str] = Counter()
    window = coverage.begin(owner, "gate_facts")
    read = coverage.counted(await read_gate_facts(owner))
    await coverage.judge(owner, "gate_facts", queue.ROLE_SETTLE, window, read)
    moved["gate_facts"] = read
    # Each step commits before the next begins (`pipeline.run_in_steps`), and the descriptions are read with no
    # transaction open (`pool.queue_details`): no role's lock is held across the turn, nor any while a page is read.
    moved.update(await pipeline.run_in_steps(owner, excluded) or {})
    if "details" not in excluded:
        window = coverage.begin(owner, "details")
        filled = await pool.queue_details(owner, pool.DETAILS_PER_TURN, pool.DETAILS_TURN_SECONDS)
        await coverage.judge(owner, "details", queue.ROLE_DETAILS, window, coverage.counted(filled))
        if filled:
            moved.update(await pipeline.run_in_steps(owner, excluded) or {})
    return dict(moved)


async def queued_turn(owner: str) -> TurnReport:
    """One turn for the work queue's handler (`queue/handlers.turn`): the whole turn without the steps the dispatcher
    does from their own to-dos (`QUEUE_STEPS`), one at a time (it waits for a turn another way is running rather than
    skipping), raising what fails so the to-dos keep and back off, and saying whether it stopped short at a cap."""
    async with _pipeline_lock:
        with pipeline.accounting() as account:
            moved = await run_turn(owner, QUEUE_STEPS)
    return TurnReport(moved, account["more"])


@db.background_work  # the background limits, wherever a turn is run from (`db.background`)
async def pipeline_pass(owner: str | None = None) -> bool:
    """The whole turn of the pipeline, on its own and in its own transactions: the roles move on through the statuses
    (`pipeline.run`), the descriptions the roles in play lack are read by code (`pool.queue_details`, a bounded batch),
    and the roles that got their text move on. Code only, no model session, so it runs at any hour.

    It is the whole turn for tests and tools. The app's turn is the dispatcher's (`queued_turn`), run from the to-dos
    that ask for it (`signal_after_commit`, `pipeline_turn`); it leaves the filter, the making of roles and the read of
    descriptions to their own to-dos (`QUEUE_STEPS`). Each part commits before the next begins, so no lock is held while
    a page is read. False when a turn is already running or it failed (logged)."""
    owner = owner or settings.owner
    if _pipeline_lock.locked():
        return False
    async with _pipeline_lock:
        try:
            await run_turn(owner)
        except Exception:  # a database blip, a busy row past its lock wait
            log.warning("the roles were not moved on through the statuses", exc_info=True)
            return False
    return True


@db.background_work
async def schedule_loop() -> None:
    while True:
        await schedule_pass()
        try:  # at the end of the off-hours window: Go's checks on the next batch, in the background (go_checks.py)
            go_checks.start_precheck(settings.owner)
        except Exception:
            log.warning("the night's pre-check for Go did not start", exc_info=True)
        try:  # roles Go opened become Applied when the owner's mail confirms the application (`go_checks`)
            await settle_opened()
        except Exception:
            log.warning("the mail was not read for the roles Go opened", exc_info=True)
        try:  # when the off-hours window ends: what each kind left undone, for its health line (`pool.note_window`)
            async with db.session_factory()() as session:
                if await pool.note_window(session, settings.owner):
                    await session.commit()
        except Exception:
            log.warning("what the night left undone was not taken", exc_info=True)
        try:  # once the off-hours window has ended: the night's Claude cost, kept for the Agents page (cost_report.py)
            async with db.session_factory()() as session:
                if await cost_report.note_night(session, settings.owner, app_settings.data_dir()):
                    await session.commit()
        except Exception:
            log.warning("the night's cost was not worked out", exc_info=True)
        await asyncio.sleep(SCHEDULE_CHECK_SECONDS)
