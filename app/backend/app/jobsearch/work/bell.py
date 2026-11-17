"""The engine's side of the bell. The page rings with `engine_calls.Remote`; the engine's listener
(`start`, added by a later card) hands each datagram to `handle`, which maps a whitelisted name to the real call.

A bell is a hint, never the record. Everything that must not be lost is a row first (a to-do, a run, an ask), and a bell
that is lost, forged or stale is found by the engine's start-up pickup and its own 30 second look. So a bell starts
nothing without its row: `start:<name>` runs only the function in `ROW_STARTS`, which reads the row and ignores every
other field of the datagram. Plain data in; the names and the shape of each field are checked here, not trusted."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import select

from app import db, notice
from app.jobsearch.work import engine_calls
from app.jobsearch.work.queue import consume
from app.settings import settings

log = logging.getLogger(__name__)

Fields = dict[str, Any]

#: The prefix of a bell that asks the engine to begin a background task.
START = "start:"


def _text(value: object) -> bool:
    return isinstance(value, str) and bool(value)


async def _look(fields: Fields) -> None:
    consume.wake()


async def _dispatch(fields: Fields) -> None:
    from app.jobsearch.work import dispatcher

    await dispatcher.dispatch_now()


async def _knock(fields: Fields) -> None:
    from app.jobsearch.work import wake

    run_id, reason = fields.get("id"), fields.get("reason")
    if not isinstance(run_id, str) or not run_id or not isinstance(reason, str) or reason not in engine_calls.REASONS:
        log.info("bell: a knock that is not one the page rings was ignored")
        return
    await wake.knock(run_id, reason=reason)


async def _knocks(fields: Fields) -> None:
    from app.jobsearch.loops import schedule

    raw = fields.get("pairs")
    pairs: list[tuple[str, str]] = []
    if isinstance(raw, list) and 0 < len(raw) <= engine_calls.KNOCKS_PER_BELL:
        for item in raw:
            if (isinstance(item, list) and len(item) == 2 and isinstance(item[0], str) and item[0]
                    and isinstance(item[1], str) and item[1] in engine_calls.REASONS):
                pairs.append((item[0], item[1]))
    if not isinstance(raw, list) or not pairs or len(pairs) != len(raw):
        log.info("bell: knocks that are not the ones the page rings were ignored")
        return
    await schedule.send_knocks(pairs)


def _uuid_text(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


async def _wakes(fields: Fields) -> None:
    from app.jobsearch.work import wake
    from app.sites import wake_ops
    from app.sites.agent_calls import Wake

    ids = fields.get("ids")
    if not (isinstance(ids, list) and 0 < len(ids) <= engine_calls.WAKES_PER_BELL and all(_uuid_text(i) for i in ids)):
        log.info("bell: wakes that are not the ones the page rings were ignored")
        return
    await wake_ops.send_all([(project_id, Wake(wake.SITE_PROJECT, "plan")) for project_id in ids])


#: The bells that are not a start: name -> what the engine does.
HANDLERS: dict[str, Callable[[Fields], Awaitable[None]]] = {
    "look": _look,
    "dispatch": _dispatch,
    "knock": _knock,
    "knocks": _knocks,
    "wakes": _wakes,
}

#: The starts the engine takes, by the name after `start:`. Each reads its row and starts only when the row says to.
ROW_STARTS: dict[str, Callable[[Fields], Awaitable[None]]] = {}


@db.background_work
async def handle(name: str, fields: Fields | None = None) -> None:
    """Do what the bell `name` asks, once. A name that is not on the lists is ignored; nothing raises."""
    fields = fields if isinstance(fields, dict) else {}
    try:
        if isinstance(name, str) and name.startswith(START):
            starter = ROW_STARTS.get(name[len(START):])
            if starter is None:
                log.info("bell: no start of that name; ignored")
                return
            await starter(fields)
            return
        run = HANDLERS.get(name) if isinstance(name, str) else None
        if run is None:
            log.info("bell: no bell of that name; ignored")
            return
        await run(fields)
    except Exception:
        log.warning("bell %r failed", name, exc_info=True)


#: The runs of the boards read this process has begun on a bell: a second bell for one run starts nothing.
_begun: set[uuid.UUID] = set()


async def _go(fields: Fields) -> None:
    from app.jobsearch.browser import go
    from app.jobsearch.models import GoRun

    owner = settings.owner
    async with db.read_session_factory()() as reader:
        row = await reader.get(GoRun, owner)
        asked = row is not None and bool(row.state) and row.state.get("phase") == "asked"
    if not asked:
        log.info("bell: start:go with no asked row; nothing started")
        return
    async with db.session_factory()() as session:
        await go.settle_runs(session, owner)
        await session.commit()


async def _boards_night(fields: Fields) -> None:
    from app.jobsearch.missions import record
    from app.jobsearch.models import Mission, MissionRun

    try:
        entry_id = uuid.UUID(str(fields.get("id")))
    except ValueError:
        log.info("bell: start:boards_night without a run id; nothing started")
        return
    if entry_id in _begun:
        return
    async with db.read_session_factory()() as reader:
        found = (await reader.execute(
            select(MissionRun.owner, MissionRun.outcome, MissionRun.started, Mission.key)
            .join(Mission, Mission.id == MissionRun.mission_id).where(MissionRun.id == entry_id))).first()
    if found is None:
        log.info("bell: start:boards_night with no such run; nothing started")
        return
    owner, outcome, started, key = found
    started = started or {}
    if key != record.READ or outcome != "running" or not started.get("reading"):
        log.info("bell: start:boards_night for a run that is not a read waiting to begin; nothing started")
        return
    _begun.add(entry_id)
    engine_calls.start("boards_night", (entry_id, owner, int(started.get("details_per_night") or 0), False))


ROW_STARTS["go"] = _go
ROW_STARTS["boards_night"] = _boards_night


#: The handlers running now, one task for each bell, so a slow call does not hold up the next datagram.
_tasks: set[asyncio.Task[None]] = set()


def on_bell(name: str, fields: Fields) -> None:
    """The receiver heard a bell: run it as a task of its own. Needs the running loop (the receiver calls it on it)."""
    task = asyncio.get_running_loop().create_task(handle(name, fields))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def _heard(message: Any) -> None:
    """The receiver's callback: a bell becomes a task; anything else (a page-bound message, garbage) is dropped."""
    name = getattr(message, "bell", None)
    fields = getattr(message, "fields", None)
    if isinstance(name, str) and isinstance(fields, dict):
        on_bell(name, fields)


class Listening:
    """What `start` returns: stop it with `close`."""

    def __init__(self, receiver: notice.Receiver) -> None:
        self._receiver = receiver

    async def close(self) -> None:
        """Stop hearing bells, then cancel the handlers still running and wait for them."""
        self._receiver.stop()
        running = list(_tasks)
        for task in running:
            task.cancel()
        for task in running:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        _tasks.difference_update(running)


async def start() -> Listening | None:
    """Listen on the engine's notice port and hand each bell to `handle`. None when the signal is off, or when the
    port cannot be bound (the dispatcher's own look still finds the work)."""
    port = notice.listen_port()
    if port is None:
        return None
    receiver = notice.Receiver(port, _heard)
    try:
        await receiver.start()
    except OSError as error:
        log.warning("bell: could not listen on port %s (%s); the engine looks on its own timer", port, error)
        return None
    return Listening(receiver)
