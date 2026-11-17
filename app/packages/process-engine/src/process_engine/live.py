"""The live engine: how an app runs a process on the engine, and holds a run that fails.

A process module says it can run live (`register_drivable`): its library, how each kind of waiting step is handed out,
the actions it calls and the steps the engine can't run inside its own run. `live_engine(key)` is then the engine that
acts: its service steps call the registered actions, its household steps raise the cards the app raises, its agent
steps go out through the app's hand-out, and the answer resumes it.

**Never lose work.** Everything the engine does live runs in `attempt`: one savepoint. If the engine raises, or an
instance it ran ends failed, the savepoint is rolled back (so nothing half-done is kept) and `attempt` says so
(`Attempt(ok=False)`), so the caller does nothing more for it. The one thing that is not a failure is an answer for a
step nobody is waiting at any more (`NotWaiting`: a double click, an agent reporting twice): that changes nothing, and
`attempt` lets it through.

**Held, not handed on.** `attempt` rolls the savepoint back as above and calls `config.on_drive_failure`, and does
nothing else: no live run is cancelled and no other code acts. The work is undone and the run stays where it was, held
at that step; the hook tells the person ("couldn't <what>: <why>", with a way to try again), and the process's
`Drivable.retry` (when it has one) runs its entry again from today's facts. `config.on_drive_success` is called after a
live attempt that worked, so what the hook raised can close by itself. With no hook set the failure is only logged.

**Agent steps.** An agent step is handed out through the door that always handed that work out, as the task the app
already has, with the step's own role, instruction file and response template. Work that commits as it goes (a try, a
runner call) can't run in the engine's savepoint: `attempt(savepoint=False)`, or a `deferred` step the code that does
the work feeds.

**Safety doesn't move.** Approval, a login never reaching a model, the fence around tools and the separation of duties
live inside the registered actions and the code they call, which the engine calls unchanged. A process routes work
between actions; it can't remove a safeguard because none is in the process.
"""

import logging
import uuid
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from process_engine.config import config
from process_engine.engine import Engine
from process_engine.errors import EngineError, NotWaiting
from process_engine.loader import ProcessLibrary
from process_engine.models import (
    CANCELLED,
    COMPLETED,
    FAILED,
    LIVE,
    RUNNING,
    WAITING,
    ProcInstance,
    ProcWait,
)
from process_engine.registry import ActionRegistry

log = logging.getLogger(__name__)

OPEN = (RUNNING, WAITING)


class DriveError(ValueError):
    """A process can't run live (it was never registered with `register_drivable`). Plain words."""


class DriveFailed(RuntimeError):
    """The engine's live run couldn't finish the work (an instance ended failed). Raised inside `attempt`, which
    rolls the work back and holds the run."""


#: `async (session, instance, wait)`: hand a waiting step out. The same signature as the engine's `on_wait`.
HandOut = Callable[[AsyncSession, ProcInstance, ProcWait], Awaitable[None]]
#: `async (session, entity_id, subject) -> None`: run the process's entry again for a subject (the string a caller gave
#: `attempt(..., subject=)`, else `None`: everything of the owner's that the process holds) from today's facts. The
#: app's "Try again" calls it. It works through `attempt`, so a second failure is held and reported the same way.
Retry = Callable[[AsyncSession, uuid.UUID, str | None], Awaitable[None]]


@dataclass
class Drivable:
    """A process that can run live: its library and how its hand-outs work."""

    key: str
    label: str
    library: Callable[[], ProcessLibrary]
    #: What running live means for this process, in plain words.
    plain: str = ""
    #: How each kind of waiting step is handed out.
    handouts: dict[str, HandOut] = field(default_factory=dict)  # step kind ("user" | "agent") -> hand-out
    #: The actions the process calls; None is the app's own registry (a test brings its own).
    registry: ActionRegistry | None = None
    #: Actions that can't run inside the engine's own run (they commit first, or retry on a clock of their own): a live
    #: step naming one waits, and the code that does the work feeds its outcome (`Engine.complete`).
    deferred: frozenset[str] = frozenset()
    #: Runs this process's entry again for a subject from today's facts (see `Retry`): what "Try again" calls after a
    #: failure was held (`config.on_drive_failure`). None: the process has no entry to run again, and the next time the
    #: work comes up is the retry.
    retry: Retry | None = None


_drivables: dict[str, Drivable] = {}


def register_drivable(
    key: str, label: str, library: Callable[[], ProcessLibrary], *, plain: str = "",
    handouts: dict[str, HandOut] | None = None, registry: ActionRegistry | None = None,
    deferred: frozenset[str] | set[str] = frozenset(), retry: Retry | None = None,
) -> Drivable:
    """A process module says it can run live: `handouts` maps a waiting step's kind (`user`, `agent`) to the function
    that hands it out (raises the same card, creates the same task the app does). `retry` runs the process's entry
    again for a subject, from today's facts (see `Retry`); without it "Try again" leaves the work for the next time it
    comes up."""
    spec = Drivable(key, label, library, plain, dict(handouts or {}), registry, frozenset(deferred), retry)
    _drivables[key] = spec
    return spec


def drivable(key: str) -> Drivable | None:
    return _drivables.get(key)


#: The processes whose engine is running live work in this task right now. An action the engine calls may reach back
#: into the app's code (an assignment asks for a login, which reconciles the household's needs); that code asks
#: `inside(key)` so it never starts a second run of a process that is already mid-step.
_inside: ContextVar[frozenset[str]] = ContextVar("proc_drive_inside", default=frozenset())


def inside(key: str) -> bool:
    return key in _inside.get()


@dataclass
class Attempt:
    """What `attempt` came to: `ok` False means the engine failed and the work was undone (the run is held and, with
    `config.on_drive_failure` set, the app has said so; nothing more is to be done); `value` is what the work
    returned."""

    ok: bool
    value: Any = None


async def attempt(
    session: AsyncSession, entity_id: uuid.UUID, key: str, what: str, work: Callable[[], Awaitable[Any]],
    *, savepoint: bool = True, subject: str | None = None,
) -> Attempt:
    """Run the engine's live `work` in one savepoint. On any failure (an exception, or `ensure_ok` on a failed
    instance) the savepoint is rolled back and the result is `Attempt(ok=False)`. Nothing else changes: no live run is
    cancelled and no other code acts. The run is held at the step it was at, and the app's hook is told:

    - `config.on_drive_failure` (when set) is called with `what`, the work in plain words ("start a look"), and
      `subject`, what the work was about, if the caller said. The hook runs in a savepoint of its own and can't raise:
      a hook that breaks is logged and the failure still stands as `ok=False`. It is the app's one place to tell the
      person what happened.
    - with no hook the failure is only logged.

    After work that worked, `config.on_drive_success` (when set) is called, so what the failure hook raised can close.

    `savepoint=False` is for work that commits as it goes by design (a try against the real system, a runner call:
    each saves its own state first because the runner asks the app's door for it, which reads committed rows). There is
    nothing to roll back to, so on a failure only the hook is told.

    When `work` returns a process instance that ended failed or cancelled, that is a failure too (`ensure_ok`), whether
    or not `work` checked.

    `NotWaiting` is not a failure of the process: an answer came for a step that was already answered (a double click,
    an agent reporting twice, a late answer from an earlier lap). The savepoint is rolled back, so nothing changed, and
    the error goes on up to the caller, whose route answers it as a conflict. Neither hook is called."""

    async def checked():
        value = await work()
        if isinstance(value, ProcInstance):
            ensure_ok(value)
        return value

    token = _inside.set(_inside.get() | {key})
    try:
        if savepoint:
            async with session.begin_nested():
                value = await checked()
        else:
            value = await checked()
    except NotWaiting:
        raise
    except Exception as exc:  # noqa: BLE001 - the engine failing must never lose the owner's work
        _inside.reset(token)
        token = None
        log.exception("process %s failed while running live (%s); holding it", key, what)
        await _held(session, entity_id, key, what, exc, subject)
        return Attempt(False)
    finally:
        if token is not None:
            _inside.reset(token)
    await _worked(session, entity_id, key, what, subject)
    return Attempt(True, value)


async def _held(
    session: AsyncSession, entity_id: uuid.UUID, key: str, what: str, exc: Exception, subject: str | None,
) -> None:
    """Tell the app a live attempt failed and was undone (`config.on_drive_failure`). Its own savepoint, and it never
    raises: an app's hook that breaks must not take its caller down, and the work stays undone either way."""
    if config.on_drive_failure is None:
        return
    try:
        async with session.begin_nested():
            await config.on_drive_failure(session, entity_id, key, what, exc, subject=subject)
    except Exception:  # noqa: BLE001 - see the docstring
        log.exception("couldn't record that %s was held (%s)", key, what)


async def _worked(session: AsyncSession, entity_id: uuid.UUID, key: str, what: str, subject: str | None) -> None:
    """Tell the app a live attempt worked (`config.on_drive_success`), so what it raised for a failure can close. Same
    rules as `_held`: its own savepoint, never raises."""
    if config.on_drive_success is None:
        return
    try:
        async with session.begin_nested():
            await config.on_drive_success(session, entity_id, key, what, subject=subject)
    except Exception:  # noqa: BLE001 - see `_held`
        log.exception("couldn't close what was raised for %s (%s)", key, what)


async def restore(session: AsyncSession, *objects) -> None:
    """After a failed attempt, reload the rows the app's code is about to use: a rolled-back savepoint leaves anything
    the engine changed expired, and async code can't lazy-load it."""
    for obj in objects:
        if obj is not None:
            await session.refresh(obj)


def ensure_ok(instance: ProcInstance) -> ProcInstance:
    """A live instance that ended failed (or was cancelled by the engine) is a failure of the live run."""
    if instance.status in (FAILED, CANCELLED):
        raise DriveFailed(instance.error or f"the process {instance.status}")
    return instance


def finished(instance: ProcInstance) -> bool:
    return instance.status == COMPLETED


# --- the engine, live -------------------------------------------------------------------------------------


async def dispatch(session: AsyncSession, instance: ProcInstance, wait: ProcWait) -> None:
    """The engine's `on_wait`: hand a waiting step out through its process's hand-out. A kind with no hand-out is an
    error (the engine can't leave an owner or an agent waiting for something nobody was told about), which `attempt`
    holds."""
    spec = _drivables.get(instance.process_key)
    handout = spec.handouts.get(wait.kind) if spec else None
    if handout is None:
        raise EngineError(f"'{instance.process_key}' has no way to hand out a {wait.kind} step ({wait.element_id})")
    await handout(session, instance, wait)


def live_engine(key: str) -> Engine:
    """An engine that acts: the registered actions are called, waiting steps are handed out."""
    spec = _drivables.get(key)
    if spec is None:
        raise DriveError(f"'{key}' isn't a process that can run live")
    return Engine(spec.library(), spec.registry, on_wait=dispatch, deferred=spec.deferred)


async def open_live(session: AsyncSession, key: str, subject: str) -> ProcInstance | None:
    return await session.scalar(select(ProcInstance).where(
        ProcInstance.process_key == key, ProcInstance.subject == subject, ProcInstance.mode == LIVE,
        ProcInstance.status.in_(OPEN),
    ).order_by(ProcInstance.created_at.desc()).limit(1))


async def last_end(session: AsyncSession, instance: ProcInstance) -> str | None:
    """The end event an ended instance reached."""
    from process_engine.models import ProcHistory

    return await session.scalar(
        select(ProcHistory.from_element).where(ProcHistory.instance_id == instance.id, ProcHistory.kind == "end")
        .order_by(ProcHistory.seq.desc()).limit(1)
    )
