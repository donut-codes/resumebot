"""Where a commit's live items go (`live.py` `_send`; the owner, 2026-11-11, the split's lane live-ids-sink).

A commit does not build its browser messages. `live._collect` keeps what changed as `(owner, kind, id, tags)` and
`_send` hands one owner's items to the sink in use (`current`). The local sink, when a stream of the owner is open,
reads the rows on a reader (`live_read.build`) and publishes them through the broker; with no stream open it reads
nothing. Later the engine installs a sink that sends a datagram instead.

Messages are built when they are delivered, so a message shows the row as it is then, which may be later than its
commit. Deliveries run one after another, in the order the commits announced them. This module imports nothing of the
app at the top, so `live.py` can import it."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

log = logging.getLogger(__name__)

#: One thing that changed in a commit: (kind, row id, tags). Tags are a tuple of short words for what the row cannot
#: say once it is committed (`new`, `deleted`, `was:<status>`, `changed:status`, `changed:favorite`, `was_read:yes`,
#: `was_read:no`; the top of live_read.py lists them), never row data.
Item = tuple[str, str, tuple[str, ...]]
#: Builds the messages of one run of items: (owner, kind, ids, tags) -> the messages, in order; `tags[i]` is for
#: `ids[i]`.
Build = Callable[[str, str, list[str], list[tuple[str, ...]]], Awaitable[list[dict[str, Any]]]]


class Sink(Protocol):
    def announce(self, owner: str, items: list[Item]) -> None: ...


async def from_rows(owner: str, kind: str, ids: list[str], tags: list[tuple[str, ...]]) -> list[dict[str, Any]]:
    """The messages for `ids` of one kind, read from the rows on a reader (it takes no write turn)."""
    from app import db
    from app.jobsearch.pipeline import live_read

    async with db.read_session_factory()() as session:
        return await live_read.build(session, owner, kind, ids, tags)


def runs(items: list[Item]) -> list[tuple[str, list[str], list[tuple[str, ...]]]]:
    """The items cut into runs of one kind in a row, each as (kind, ids, tags), `tags[i]` for `ids[i]`. A row named
    twice in one run starts the next run, so each of its changes is a message of its own, as it was when messages were
    built at commit."""
    out: list[tuple[str, list[str], list[tuple[str, ...]]]] = []
    for kind, row_id, tags in items:
        if out and out[-1][0] == kind and row_id not in out[-1][1]:
            out[-1][1].append(row_id)
            out[-1][2].append(tags)
        else:
            out.append((kind, [row_id], [tags]))
    return out


class LocalSink:
    """Publishes to this program's own streams. Nothing is read when the owner has no stream, or none that asked for the
    kind."""

    def __init__(self, build: Build = from_rows) -> None:
        self._build = build
        self._tasks: set[asyncio.Task[None]] = set()
        self._last: asyncio.Task[None] | None = None

    def announce(self, owner: str, items: list[Item]) -> None:
        from app.jobsearch.pipeline import live

        if not live.broker.listening(owner):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # a commit outside the app's loop (a script): nobody is listening there
            return
        last = self._last
        previous = last if last is not None and not last.done() and last.get_loop() is loop else None
        task = loop.create_task(self._deliver(previous, owner, items))
        self._last = task
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _deliver(self, previous: "asyncio.Task[None] | None", owner: str, items: list[Item]) -> None:
        from app.jobsearch.pipeline import live

        if previous is not None:
            await asyncio.wait([previous])  # one delivery after another: the messages keep the commits' order
        try:
            recount = False
            for kind, ids, tags in runs(items):
                if not live.broker.listening(owner):
                    return
                if live.broker.wanted(owner, kind):
                    for data in await self._build(owner, kind, ids, tags):
                        live.broker.publish(owner, kind, data)
                recount = recount or kind == "role"
            if recount:
                live.broker.counts_soon(owner)
        except Exception:  # a page's messages must never take the app down
            log.warning("a live message was not sent", exc_info=True)

    async def settle(self) -> None:
        """Wait until every delivery announced so far in this loop is done (for tests, and for a stop)."""
        loop = asyncio.get_running_loop()
        while True:
            running = [t for t in self._tasks if t.get_loop() is loop and not t.done()]
            if not running:
                return
            await asyncio.wait(running)


_sink: Sink | None = None


def current() -> Sink:
    """The sink in use: the local one unless `use` put another."""
    global _sink
    if _sink is None:
        _sink = LocalSink()
    return _sink


def use(sink: Sink | None) -> None:
    """Put `sink` in use; None puts the local one back."""
    global _sink
    _sink = sink


async def settle() -> None:
    """`LocalSink.settle` for the sink in use (nothing to wait for when it is another kind)."""
    sink = current()
    if isinstance(sink, LocalSink):
        await sink.settle()


class WireSink:
    """The engine's sink: after a commit, tell the page what changed (`app/notice.py`): ids, kinds and a few short tags,
    once per commit. It does not look for a listener (the engine has no streams; a page that is not running costs one
    failed send) and it builds and publishes nothing itself."""

    def announce(self, owner: str, items: list[Item]) -> None:
        from app import notice

        notice.tell_page(owner, [(kind, row_id, wire_tags(tags)) for kind, row_id, tags in items])


def tell_resync(owner: str) -> None:
    """Tell the page that every stream of the owner should read again. The engine sends it once when it is ready (lane
    engine-start-order)."""
    from app import notice

    notice.tell_page(owner, [("resync", "")])


def wire_tags(tags: tuple[str, ...]) -> list[str]:
    """The text tags of an item as the wire carries them."""
    return [str(tag) for tag in tags]


def item_tags(wire: list[str]) -> tuple[str, ...]:
    """The tags a sink item carries, from the text tags of the wire."""
    return tuple(wire)
