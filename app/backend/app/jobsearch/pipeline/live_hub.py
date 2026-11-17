"""The page's live hub (the owner, 2026-11-10: the page is told, it does not ask).

The engine tells the page what changed in one small datagram after each commit (`app/notice.py`): kinds, ids and a few
short tags, never rows. The hub takes those arrivals on the page's event loop and does four things with them:

- every item, whether the browser hears of it or not (`ask`, `action_run` and `go_run` are for waiters only), wakes the
  waiters of its kind;
- an item the browser could hear of is dropped at once when no open stream wants it;
- the rest are grouped by owner and kind for one `live.COUNTS_AFTER_SECONDS` (0.25 s: a coalesce of notifications, not
  a poll; nothing here looks at a clock to find work), and then handed to a local sink (`live_sink.LocalSink`), which
  reads the rows on a reader, builds each message and publishes it to the open streams;
- a `resync` (a lost datagram, a restarted engine, an oversize commit, or a window of more than `MOST_ITEMS` items of
  one kind) is one `resync` to each stream of the owner per window, however many ask. The browser reads again on
  `resync`, so a night of hundreds of commits cannot make it reload the board once per commit.
"""

import asyncio
import logging
from collections.abc import Callable, Coroutine, Iterable, Sequence
from typing import Any

from app import notice
from app.jobsearch.pipeline import live, live_sink

log = logging.getLogger(__name__)

#: The kind of an item that only asks the browser to read again.
RESYNC = "resync"
#: The most distinct items of one kind one owner's window may hold; more are told as one `resync`.
MOST_ITEMS = 50


class Hub:
    """What the engine told, grouped for the browser. It lives on the page's event loop and is used from it only. Its
    sink is its own (never `live_sink.current()`): in the tests one process plays both programs."""

    def __init__(self, notify: Callable[[str, list[str]], object], sink: live_sink.Sink) -> None:
        self._notify = notify
        self._sink = sink
        #: (owner, kind) -> the distinct items of the window that is open, by key.
        self._window: dict[tuple[str, str], dict[str, live_sink.Item]] = {}
        #: The owners due a `resync` (None: every owner with an open stream).
        self._resyncs: set[str | None] = set()
        self._resync_wait: asyncio.Task | None = None
        self._tasks: set[asyncio.Task] = set()

    def arrive(self, owner: str, items: Iterable[Sequence[Any]]) -> None:
        """Take the items of one datagram: (kind, id, text tags) each."""
        for kind, ident, wire in items:
            if kind == RESYNC:
                self._resync_soon(owner)
                continue
            self._notify(kind, [ident])
            if kind not in live.TOPICS or not self._heard(owner, kind):
                continue
            window = self._window.get((owner, kind))
            if window is None:
                window = self._window[(owner, kind)] = {}
                self._spawn(self._send_window(owner, kind))
            window[ident + "|" + ",".join(wire)] = (kind, ident, live_sink.item_tags(list(wire)))

    def lost_track(self) -> None:
        """A datagram was lost, or the engine started again: every open stream reads again."""
        self._resync_soon(None)

    def busy(self) -> bool:
        """Whether a window or a `resync` is waiting to be sent."""
        return bool(self._tasks)

    async def settle(self) -> None:
        """Wait until every window that is open has been handed to the sink, and the sink has delivered (the tests, and
        `stop`)."""
        while True:
            running = [t for t in self._tasks if not t.done()]
            if not running:
                break
            await asyncio.wait(running)
        if isinstance(self._sink, live_sink.LocalSink):
            await self._sink.settle()

    async def stop(self) -> None:
        """The page is stopping: what waits is dropped, not sent."""
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._window.clear()
        self._resyncs.clear()
        self._resync_wait = None

    def _heard(self, owner: str, kind: str) -> bool:
        """Whether an open stream of the owner would be told of this kind: one that asked for it, or, for a role,
        one that asked for the counts (a role change moves them)."""
        return live.broker.wanted(owner, kind) or (kind == "role" and live.broker.wanted(owner, "counts"))

    def _spawn(self, work: Coroutine[Any, Any, None]) -> asyncio.Task:
        task = asyncio.get_running_loop().create_task(work)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _resync_soon(self, owner: str | None) -> None:
        self._resyncs.add(owner)
        if self._resync_wait is None:
            self._resync_wait = self._spawn(self._resync_after_window())

    async def _resync_after_window(self) -> None:
        try:
            await asyncio.sleep(live.COUNTS_AFTER_SECONDS)
        finally:
            self._resync_wait = None
        self._publish_resyncs()

    def _publish_resyncs(self) -> None:
        due, self._resyncs = self._resyncs, set()
        owners = set(live.broker.open_owners()) if None in due else {o for o in due if o is not None}
        for owner in owners:
            live.broker.publish(owner, RESYNC, {})

    async def _send_window(self, owner: str, kind: str) -> None:
        await asyncio.sleep(live.COUNTS_AFTER_SECONDS)
        items = list(self._window.pop((owner, kind), {}).values())
        if not items:
            return
        if len(items) > MOST_ITEMS:
            self._resyncs.add(owner)
            self._publish_resyncs()
            return
        try:
            self._sink.announce(owner, items)
        except Exception:  # what the page is told must never take the page down
            log.warning("the live %s messages were not sent", kind, exc_info=True)


def heard(hub: Hub, gaps: notice.Gaps, message: notice.Message) -> None:
    """What the page does with one datagram: a bell is the engine's and not for here; a sender that started again, or a
    counter that skipped, means a notice may be lost, so every stream reads again; then the items are taken."""
    if message.bell is not None:
        return
    if gaps.check(message) is not None:
        hub.lost_track()
    hub.arrive(message.owner, message.items)


class Listening:
    """What the page's start holds while the page runs: the hub, and the receiver when the page could bind."""

    def __init__(self, hub: Hub, receiver: notice.Receiver | None) -> None:
        self.hub = hub
        self.receiver = receiver

    async def stop(self) -> None:
        if self.receiver is not None:
            self.receiver.stop()
        await self.hub.stop()
        notice.BIND_PROBLEM = None


async def listen() -> Listening:
    """Start hearing the engine. With the signal off (no port) nothing is bound. A bind that fails does not stop the
    page: it is logged by name and shown on `/health`."""
    hub = Hub(notice.waiters.notify, live_sink.LocalSink())
    port = notice.page_port()
    if port is None:
        log.info("the page is not told of the engine's changes: the signal is off (no --port)")
        return Listening(hub, None)
    gaps = notice.Gaps()
    receiver = notice.Receiver(port, lambda message: heard(hub, gaps, message))
    try:
        await receiver.start()
    except OSError as error:
        notice.BIND_PROBLEM = (f"the page could not listen on UDP 127.0.0.1:{port} for the engine's notices ({error}); "
                               "changes made by the engine will not reach open pages")
        log.error("%s", notice.BIND_PROBLEM)
        return Listening(hub, None)
    return Listening(hub, receiver)
