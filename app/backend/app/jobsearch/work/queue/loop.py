"""The dispatcher: one loop that reads the to-dos and runs the handlers.

It reads the to-do table, which is nearly always empty (one read of an index, every 30 seconds when idle, and at once
when a commit that posted something wakes it: `consume.wake`), and starts the handler of each topic that has a to-do
due, on its lane, in the order of today's steps. With nothing to do it does nothing. A handler is today's step code
(`handlers.py`); the dispatcher decides only when.

**One look at a time for a topic.** The to-dos of one topic coalesce: the dispatcher reads the ids of all that are due,
runs the handler once, and deletes exactly those ids when it is done, so a to-do posted while the handler ran (a higher
id) stays and runs it again, and a handler cut off before it deleted them is run again, harmlessly, because it goes by
the data. A handler may take the to-dos of several topics (the pipeline's turn does): it is run once for all
of them, under its own topic. A topic is never started while its last run is going on, nor sooner than its pause after
it ended (`Handler.gap`): no topic runs more often than the loop it replaces. A run that found nothing owed does not
start the pause: it read next to nothing, and the change that comes next is not made to wait for it, and nor is a
to-do posted because someone is waiting for it (cause `ask`: Launch waits for a turn, as it did when the ask woke the
old loop). A handler that fails leaves its to-dos with one more attempt and a due time that backs off
(`service.fail`), and logs at error at the third and tenth attempt; nothing is deleted silently.

**Lanes.** Two `db` handlers run at once, one `net` handler (the read of descriptions: a page, outside any
transaction) and one `runner` handler (the walk: the pass that commands an idle lane). The dispatcher itself holds no
transaction while a handler runs.

**Start.** At every start each topic is posted once with the key `*` (the catch-up for what changed while the app was
down, and what no writer posted before the table existed) and the two ticks are made if none is there; a tick that is
overdue after downtime fires at once.

**The stored order.** After each look the loop asks, on a reader, whether a write has dirtied the order the lanes of
the board are read by (`handlers.order_is_behind`), and when it has, starts one settle of it on a `db` lane
(`Dispatcher.settle_order`). The board only reads the order (the owner, 2026-11-10: the page program runs no work), so
a role is put in its place as soon as the commit that moved it wakes the loop, and at the idle look at the latest. One
settle at a time. It has no to-do: the lines the triggers write (`QueueDirty`) are its record.

It always runs, started with the app (`main.lifespan`) beside the schedule."""

import asyncio
import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from app import db
from app.jobsearch.work import queue
from app.jobsearch.work.queue import consume, handlers, service, topics
from app.settings import settings
from app.timeutil import utc_now

log = logging.getLogger(__name__)

#: How long the loop waits when nothing is due and nothing is coming: the longest a missed wake costs.
IDLE = timedelta(seconds=30)
#: How many handlers of each lane run at once: the `runner` lane is the walk's, whose pass waits for a lane's answer and
#: must not wait behind the turn. The `ask` lane is the page's asks: one may fetch a page and a person waits for it,
#: so it holds no `db` lane and waits behind no turn.
LANES = {"db": 2, "net": 1, "runner": 1, "ask": 2}
#: The name the settle of the stored order runs under in `Dispatcher.in_flight`. It is no topic and has no to-do: the
#: lines the triggers write (`QueueDirty`) are its record.
ORDER = "stored-order"


@dataclass
class Started:
    """A pass's account of itself: the tasks it began, and how long to wait before looking again."""

    tasks: list[asyncio.Task]
    wait: timedelta


class Dispatcher:
    """The loop and its state: what is running, when each topic last ended, the tally of what was handled. `clock` is
    injectable, and so is `table` (a test's own handlers); `gaps` turns the pauses off for a test that drains
    the queue with no clock to move. `only` names the only topics it takes to-dos of (None: every topic of the
    table); a held engine takes just the page's asks (`main._start_queue`)."""

    def __init__(self, owner: str | None = None, *, clock: Callable[[], datetime] = utc_now,
                 table: dict[str, handlers.Handler] | None = None, gaps: bool = True,
                 only: frozenset[str] | None = None) -> None:
        self.owner = owner or settings.owner
        self.clock = clock
        self.table = handlers.HANDLERS if table is None else table
        self.gaps = gaps
        self.only = only
        self.event = asyncio.Event()
        self.in_flight: dict[str, asyncio.Task] = {}
        self.finished: dict[str, datetime] = {}
        self.tally = handlers.Tally()
        self.lanes = {lane: asyncio.Semaphore(n) for lane, n in LANES.items()}

    # --- one look -------------------------------------------------------------------------------------------------

    def topics(self) -> dict[str, handlers.Handler]:
        """The handlers the dispatcher takes to-dos of now, by topic."""
        return {topic: h for topic, h in self.table.items() if self.only is None or topic in self.only}

    def swept(self) -> tuple[str, ...]:
        """The topics a writer posts that the dispatcher takes to-dos of now: what a start and the hour's sweep post `*`
        for. The clock's own ticks are not among them."""
        return tuple(topic for topic, h in self.topics().items() if h.name is not None)

    async def dispatch(self) -> Started:
        """Read what is due (one short read-only transaction, committed before anything starts) and start the handler of
        each topic that is due, not running and past its pause. Returns the tasks and how long to wait before looking
        again: until the next to-do comes due or a pause ends, at most `IDLE`."""
        now = self.clock()
        wanted = self.topics()
        # Not a topic whose handler runs: its to-dos stay until its run ends with a wake, so they are not read now.
        idle = {topic for topic, h in wanted.items() if h.topic not in self.in_flight or h.topic in service.PER_KEY}
        async with db.read_session_factory()() as session:  # a reader: no writer turn, no wait
            groups, next_due = await service.due(session, self.owner, now, idle)
            await session.commit()
        wait = IDLE
        if next_due is not None:
            wait = min(wait, max(next_due - now, timedelta(0)))
        tasks: list[asyncio.Task] = []
        for group in self._by_handler(groups, wanted):
            handler = wanted[group.topic]
            if group.flight in self.in_flight:
                continue  # its run ends with a wake, and the to-dos stay until then
            ended = self.finished.get(group.topic)
            if (self.gaps and ended is not None and ended + handler.gap > now
                    and topics.ASK not in group.causes):  # someone waits for an asked-for look: no pause
                wait = min(wait, ended + handler.gap - now)
                continue
            task = asyncio.create_task(self._handle(group, handler), name=f"queue:{group.topic}")
            self.in_flight[group.flight] = task
            tasks.append(task)
        return Started(tasks, wait)

    @staticmethod
    def _by_handler(groups: list[service.Group], wanted: dict[str, handlers.Handler]) -> list[service.Group]:
        """The groups, one for each handler (the groups of the several topics a handler takes are merged under its own
        topic), in the order of the handlers' priority and then of their first to-do."""
        found: dict[str, list[service.Group]] = {}
        apart = [g for g in groups if g.unit]  # one key of a `PER_KEY` topic: its own run, never merged
        for group in (g for g in groups if not g.unit):
            found.setdefault(wanted[group.topic].topic, []).append(group)
        merged = [group[0] if len(group) == 1 and group[0].topic == topic else service.merged(topic, group)
                  for topic, group in found.items()]
        merged += apart
        return sorted(merged, key=lambda g: (wanted[g.topic].priority, g.ids[0]))

    async def look(self) -> timedelta:
        """One look of the loop: start what is due (`dispatch`), then start the settle of the stored order if a write
        has dirtied it (`settle_order`). Returns how long to wait before the next look."""
        started = await self.dispatch()
        await self.settle_order()
        return started.wait

    async def settle_order(self) -> asyncio.Task | None:
        """Start one settle of the stored order when it is behind (`handlers.order_is_behind`: asked on a reader, no
        turn), unless one is running. Returns the task it started, else None. Never raises: a failed ask is logged
        and the next look asks again."""
        if ORDER in self.in_flight:
            return None
        now = self.clock()
        try:
            if not await handlers.order_is_behind(self.owner, now):
                return None
        except Exception:
            log.warning("work-queue: asking whether the stored order is behind failed; the next look asks again",
                        exc_info=True)
            return None
        task = asyncio.create_task(self._settle_order(now), name=f"queue:{ORDER}")
        self.in_flight[ORDER] = task
        return task

    async def _settle_order(self, now: datetime) -> None:
        settled = False
        try:
            async with self.lanes["db"]:
                settled = await handlers.settle_order_at(self.owner, now)
        finally:
            self.in_flight.pop(ORDER, None)
            if settled:
                # A line written while it worked is seen by a look now. A settle that failed sets nothing: its lines
                # wait for the next wake or the idle look, so a broken settle cannot spin the loop.
                self.event.set()

    async def pass_once(self) -> int:
        """One look, and wait for every handler it started. Returns how many it started. What the tests and the first
        look use; the loop does not wait for them."""
        started = await self.dispatch()
        if started.tasks:
            await asyncio.gather(*started.tasks)
        return len(started.tasks)

    async def drain(self, passes: int = 100) -> int:
        """Look again and again until a look starts nothing (a test's way to let the queue settle). Returns how many
        handlers ran."""
        ran = 0
        for _ in range(passes):
            started = await self.pass_once()
            if not started:
                break
            ran += started
        return ran

    # --- one handler ----------------------------------------------------------------------------------------------

    async def _handle(self, group: service.Group, handler: handlers.Handler) -> None:
        paused = True  # a run starts its topic's pause, unless it found nothing owed and read next to nothing
        try:
            async with self.lanes[handler.lane]:
                started = self.clock()
                with service.suppressing(*handler.takes):  # its own writes are not another look at what it looks at
                    outcome = await handler.run(handlers.Run(self.owner, group, started, self.tally, self.swept()))
            paused = not outcome.cheap
            await self._finish(group, handler, outcome, started)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("work-queue: the handler of %s failed; its to-dos stay and back off", group.topic,
                        exc_info=True)
            await self._fail(group, exc)
        finally:
            if paused:
                self.finished[group.topic] = self.clock()
            self.in_flight.pop(group.flight, None)
            self.event.set()

    async def _finish(self, group: service.Group, handler: handlers.Handler, outcome: handlers.Outcome,
                      started: datetime) -> None:
        """Delete exactly the to-dos the handler read, and post what it asks for in the same transaction: itself again
        (`more`), or the tick's next due time. A to-do posted since has a higher id and is still there."""
        async with db.session_factory()() as session:
            await service.done(session, group.ids)
            now = self.clock()
            if outcome.more:
                queue.post(session, group.owner, group.topic, [""], cause="more", now=now)
            if outcome.again is not None:
                queue.post(session, group.owner, group.topic, [""], after=outcome.again, cause="tick", now=now)
            await session.commit()
        waited = max((started - group.waiting_since).total_seconds(), 0.0)
        for topic, rows in group.counts:
            self.tally.note(topic, rows, waited)
        if handler.name is not None and group.swept and outcome.changed > 0:
            if await self._told_meanwhile(group, handler):
                # Not a catch: the group was read before the handler waited for its lane and ran, and a writer posted
                # for one of its topics since. The run picked that change up; the to-do is still there.
                log.debug("work-queue: the swept run of %s changed %d, and a writer posted for it meanwhile",
                          group.topic, outcome.changed)
            else:
                self.tally.caught[group.topic] += outcome.changed
                log.warning("work-queue sweep_caught topic=%s n=%d: a writer did not post for it", group.topic,
                            outcome.changed)
        if handler.name is not None and group.swept and outcome.clocked:
            # Not a catch (time moves these), and not thrown away: a writer that did not post for one of these steps
            # hides in this number, so it is said where the owner can see how large it runs.
            moved = sum(n for _step, n in outcome.clocked)
            self.tally.clocked[group.topic] += moved
            log.info("work-queue sweep_clock topic=%s n=%d steps=%s: the steps time drives moved at the hour's look "
                     "(a missed writer in them would hide here)", group.topic, moved,
                     ", ".join(f"{step}={n}" for step, n in outcome.clocked))

    async def _told_meanwhile(self, group: service.Group, handler: handlers.Handler) -> bool:
        """Whether a writer posted a to-do of one of the handler's topics that this group did not read (one short
        read-only transaction, made only for a swept run that found work)."""
        async with db.read_session_factory()() as session:  # a reader: no writer turn, no wait
            told = await service.writer_posted(session, group.owner, handler.takes, group.ids)
            await session.commit()
        return told

    async def _fail(self, group: service.Group, exc: Exception) -> None:
        """Keep the to-dos with one more attempt and a later due time. A failure to say so is logged, not raised: the
        loop must outlive a database blip."""
        try:
            async with db.session_factory()() as session:
                await service.fail(session, group.ids, f"{type(exc).__name__}: {exc}", self.clock(), group.attempts)
                await session.commit()
        except Exception:
            log.warning("work-queue: the failure of %s could not be recorded", group.topic, exc_info=True)

    # --- the loop -------------------------------------------------------------------------------------------------

    async def start_up(self) -> None:
        """Post each topic once with `*`, and make the two ticks if none is there (an overdue one fires at once)."""
        await handlers.sweep(self.owner, self.swept(), cause="start")
        async with db.session_factory()() as session:
            for topic in self.topics():
                if topic in topics.CLOCK and await session.scalar(
                        service.waiting_statement(self.owner, topic)) is None:
                    queue.post(session, self.owner, topic, [""], cause="start", now=self.clock())
            await session.commit()

    async def run(self) -> None:
        """The loop: look, start what is due, wait for a wake or the next due time, again, until cancelled. Never raises
        for a failed look."""
        consume.listen(self.event)
        try:
            with db.background():
                while True:
                    try:
                        await self.start_up()
                        break
                    except Exception:
                        log.warning("work-queue: the start-up post failed; trying again", exc_info=True)
                        await asyncio.sleep(IDLE.total_seconds())
                while True:
                    self.event.clear()  # a wake during the look makes the next wait return at once
                    wait = IDLE
                    try:
                        wait = await self.look()
                    except Exception:
                        log.warning("work-queue: the look at what is due failed", exc_info=True)
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(self.event.wait(), max(wait.total_seconds(), 0.01))
        finally:
            consume.forget(self.event)
            running = list(self.in_flight.values())
            for task in running:
                task.cancel()
            await asyncio.gather(*running, return_exceptions=True)


async def run(owner: str | None = None, only: frozenset[str] | None = None) -> None:
    """The app's dispatcher, started with the app beside the schedule (`main.lifespan`). `only`: the topics it takes
    (a held engine takes just the page's asks)."""
    await Dispatcher(owner, only=only).run()
