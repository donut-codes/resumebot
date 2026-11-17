"""The BPMN engine: moves work through a process, deterministically, to the next wait (requirement 15.1).

Run-to-wait: `start` and every event (`complete`, `deliver_message`, `fire_timer`, `tick`, a child's end)
advance the instance until its token waits (an agent's task, a household card, a timer, a message, a
sub-process) or the process ends. The engine never calls a model; the only code it runs is the registered
action a service step names. It never commits: services flush, the route or startup hook that owns the request
commits.

Every move is written to `proc_history` (from, to, the condition that chose it, the data it read, the time), so
"where is this?" and the Activity log are read from the instance, not from code.

**Files.** An instance finishes on the file it started on. The library holds only the files loaded now, so each start
also keeps the file's text by hash (`proc_files`), and `_def` finds an instance's file in this order: the exact file if
it is loaded; a loaded file of that process with the same behaviour hash (a release that only changed words); the
text kept at the start, loaded on the side and never made the file new work starts on.

**Locks.** One order everywhere, so two requests can't wait on each other: an instance's ancestors first (parent
before child), then the instance, then its waits. A locked read refreshes the row (`populate_existing`), so a request
that waited for the lock sees what the one before it committed. `tick` never waits for a lock. (On SQLite there is one
writer, so there is no row lock to take or to wait for: a request that reaches the engine already has its turn. See
`portable`.)
"""

import copy
import json
import logging
import uuid
import weakref
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from process_engine import feel, portable
from process_engine.errors import (
    ActionError,
    ConditionError,
    EngineError,
    NotWaiting,
    ProcessLoadError,
    RunOver,
    Stranded,
)
from process_engine.loader import ProcessLibrary, load_process
from process_engine.loader import owner_of as loader_owner_of
from process_engine.model import (
    AGENT,
    BOUNDARY,
    CALL,
    CATCH,
    END,
    GATEWAY,
    SERVICE,
    START,
    USER,
    Element,
    Flow,
    ProcessDef,
    parse_duration,
)
from process_engine.models import (
    ACTIVE,
    CANCELLED,
    CLOSED,
    COMPLETED,
    DONE,
    FAILED,
    LIVE,
    OPEN,
    RUNNING,
    VOID,
    WAITING,
    WAITS,
    ProcFile,
    ProcHistory,
    ProcInstance,
    ProcToken,
    ProcWait,
)
from process_engine.registry import ActionRegistry, Catalog, actions

#: The most moves one run-to-wait makes, so a process that loops with no wait can't spin forever.
MAX_MOVES = 500
#: A value read by a condition is recorded whole up to this many characters, then summarised.
READ_LIMIT = 2000
#: The most instances one `tick` works on, so a backlog is worked off over several ticks, not in one long transaction.
TICK_LIMIT = 100

log = logging.getLogger(__name__)

#: Files read back from `proc_files` for an instance whose own file is no longer loaded, by library and file hash. Kept
#: beside the library (never added to it: adding would make an old file the one new work starts on).
_kept_files: weakref.WeakKeyDictionary[ProcessLibrary, dict[str, ProcessDef]] = weakref.WeakKeyDictionary()

OnWait = Callable[[AsyncSession, ProcInstance, ProcWait], Awaitable[None]]


def _clean(value):
    """JSON-safe, detached copy: what is stored is what was seen."""
    return json.loads(json.dumps(value, default=str)) if value is not None else None


def get_path(data: dict, path: str):
    value = data
    for seg in path.split("."):
        value = value.get(seg) if isinstance(value, dict) else None
    return value


def _as_uuid(value) -> uuid.UUID | None:
    """`value` as a UUID, or None when it isn't one (an id that can't match any row)."""
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except ValueError:
        return None


def _summarise(value):
    text = json.dumps(value, default=str)
    if len(text) <= READ_LIMIT:
        return json.loads(text)
    if isinstance(value, list):
        return {"truncated": True, "count": len(value), "first": json.loads(json.dumps(value[:3], default=str))}
    return {"truncated": True, "type": type(value).__name__, "text": text[:200]}


async def keep_files(session: AsyncSession, library: ProcessLibrary) -> int:
    """Keep the text of each file `library` starts new work on (`proc_files`), as an app does with its shipped files
    on start-up: an instance started on one of them can then finish on it after a release changes the file, even if
    the instance started before the engine kept files itself. Flushes; the caller commits. Returns how many it saw."""
    seen = 0
    for key in library.keys():
        defn = library.shipped(key)
        if defn is None or not defn.xml:
            continue
        await portable.insert_ignore(
            session, ProcFile, {"file_hash": defn.file_hash, "process_key": key, "xml": defn.xml},
            conflict=("file_hash",))
        seen += 1
    await session.flush()
    return seen


class Engine:
    def __init__(
        self, library: ProcessLibrary, registry: ActionRegistry | None = None, *,
        deferred: frozenset[str] | set[str] = frozenset(),
        on_wait: OnWait | None = None, clock: Callable[[], datetime] | None = None, max_moves: int = MAX_MOVES,
        catalog: Catalog | None = None,
    ):
        self.library = library
        self.registry = registry or actions
        #: The names a file read back from `proc_files` may use (`None`: the app's own catalog, `default_catalog()`).
        self.catalog = catalog
        #: The actions that can't run inside the engine's own run (a runner call commits first, and retries on a clock
        #: the engine doesn't have). A service step naming one waits instead (`deferred`), and the code that does it
        #: feeds the outcome with `Engine.complete`.
        self.deferred = frozenset(deferred)
        self.on_wait = on_wait
        self.clock = clock or (lambda: datetime.now(UTC))
        self.max_moves = max_moves

    # --- starting --------------------------------------------------------------------------------------

    async def start(
        self, session: AsyncSession, process_key: str, data: dict | None = None, *, subject: str | None = None,
        start_event: str | None = None, message: str | None = None,
    ) -> ProcInstance:
        """Begin a process and run it to its first wait (or its end). `data` is the starting data: every key
        must be a field the process's data model declares. `message` picks the start event waiting for that
        message."""
        return await self._start(session, process_key, data, subject=subject,
                                 start_event=start_event, message=message)

    async def start_by_message(
        self, session: AsyncSession, message: str, data: dict | None = None, *, subject: str | None = None,
    ) -> list[ProcInstance]:
        """Start every loaded process with a start event waiting for `message`."""
        started = []
        for key in self.library.keys():
            defn = self.library.get(key)
            if any(defn.elements[s].trigger.type == "message" and defn.elements[s].trigger.message == message
                   for s in defn.start_events):
                started.append(await self.start(session, key, data, subject=subject, message=message))
        return started

    async def _start(
        self, session, process_key, data, *, subject=None, start_event=None, message=None,
        parent: ProcInstance | None = None, parent_element: str | None = None, item_index: int | None = None,
    ) -> ProcInstance:
        owner = loader_owner_of(data, subject) or (
            loader_owner_of(parent.data, parent.subject) if parent is not None else None)
        defn = self.library.get(process_key, entity_id=owner)
        if defn is None:
            raise EngineError(f"no process '{process_key}' is loaded")
        data = _clean(data or {})
        for key in data:
            if defn.data_model.resolve(key) is None:
                raise EngineError(f"'{key}' is not in {process_key}'s data model, so it can't be started with it")
        try:
            start = defn.start_for(message=message, start_event=start_event)
        except KeyError as exc:
            raise EngineError(f"{process_key} has no start event for {exc}") from exc
        instance = ProcInstance(
            process_key=process_key, file_hash=defn.file_hash, behaviour_hash=defn.behaviour_hash or None,
            status=RUNNING, mode=LIVE, subject=subject,
            data=data, parent_id=parent.id if parent else None, parent_element=parent_element,
            item_index=item_index,
        )
        session.add(instance)
        await session.flush()
        await self._keep_file(session, process_key, defn)
        session.add(ProcToken(instance_id=instance.id, element_id=start.id, state=ACTIVE, created_at=self.clock()))
        await self._log(session, instance, "start", to_element=start.id,
                        detail={"trigger": start.trigger.type, "mode": LIVE})
        await session.flush()
        await self._advance(session, instance)
        return instance

    # --- events ----------------------------------------------------------------------------------------

    async def complete(
        self, session: AsyncSession, instance_id, element_id: str, result: dict | None = None, *,
        error: str | None = None, error_message: str = "", wait_id: uuid.UUID | str | None = None,
    ) -> ProcInstance:
        """The agent or household finished the step waiting at `element_id` (or the code that carries out a deferred
        service step reports its outcome; `error` is the named error it raised).

        `wait_id` is the wait the answer is for (`ProcWait.id`, what the hand-out was given): an answer for a wait
        that is already answered, or that belongs to an earlier lap of a loop, completes nothing. Raises `NotWaiting`
        (an `EngineError`) when nothing is waiting there, the instance has ended, or the wait is not the open one;
        nothing has changed then."""
        instance = await self._lock(session, instance_id)
        wait = await self._open_wait(session, instance, element_id, ("agent", "user", "deferred"), wait_id)
        if wait is None:
            raise NotWaiting(f"nothing is waiting to be completed at '{element_id}' in this instance")
        defn = await self._live_def(session, instance)
        element = defn.element(element_id)
        if error is not None and wait.kind != "deferred":
            raise EngineError("only a deferred service step can be completed with an error")
        token = await self._token(session, instance)
        await self._close_waits(session, instance, token)
        wait.status, wait.closed_at, wait.result = CLOSED, self.clock(), _clean(result)
        instance.status = RUNNING
        if error is not None:
            await self._raise_error(session, instance, defn, token, element, error, error_message)
        else:
            await self._log(session, instance, "complete", from_element=element_id,
                            detail={"wait": wait.kind, "result_keys": sorted((result or {}).keys())})
            self._store_result(instance, element, result)
            await self._take_only_way(session, instance, defn, token, element)
        await self._advance(session, instance)
        return instance

    async def deliver_message(
        self, session: AsyncSession, instance_id, message: str, payload: dict | None = None, *,
        wait_id: uuid.UUID | str | None = None,
    ) -> ProcInstance:
        """A message arrived for an instance waiting on it (an intermediate message catch event). `wait_id` is as for
        `complete`. Raises `NotWaiting` when the instance isn't waiting for it."""
        instance = await self._lock(session, instance_id)
        query = select(ProcWait).where(
            ProcWait.instance_id == instance.id, ProcWait.status == OPEN, ProcWait.kind == "catch_message",
            ProcWait.message == message)
        if wait_id is not None:
            wanted = _as_uuid(wait_id)
            query = query.where(ProcWait.id == wanted) if wanted is not None else query.where(False)
        wait = await session.scalar(query.limit(1).execution_options(populate_existing=True))
        if wait is None:
            raise NotWaiting(f"this instance isn't waiting for the message '{message}'")
        defn = await self._live_def(session, instance)
        token = await self._token(session, instance)
        element = defn.element(wait.element_id)
        wait.status, wait.closed_at, wait.result = CLOSED, self.clock(), _clean(payload)
        instance.status = RUNNING
        await self._log(session, instance, "complete", from_element=element.id, detail={"message": message})
        self._store_result(instance, element, payload)
        await self._take_only_way(session, instance, defn, token, element)
        await self._advance(session, instance)
        return instance

    async def tick(self, session: AsyncSession, now: datetime | None = None, *, limit: int = TICK_LIMIT) -> int:
        """Fire every timer that is due on a live instance (a boundary timer cancels its step and takes its way
        out; a timer catch event lets the token on). Returns how many fired.

        Works on at most `limit` instances, the longest overdue first. It never waits for a lock: an instance (or one
        of its ancestors) another request holds is left for the next tick, so a tick can't stand in the way of an
        answer or deadlock with one. Each timer fires in a savepoint of its own; one that breaks fails only its
        instance, with the reason written on it, and the other timers still fire."""
        now = now or self.clock()
        overdue = (
            select(func.min(ProcWait.due_at))
            .where(ProcWait.instance_id == ProcInstance.id, ProcWait.status == OPEN, ProcWait.due_at.is_not(None),
                   ProcWait.due_at <= now)
            .scalar_subquery()
        )
        candidates = (await session.scalars(
            select(ProcInstance.id)
            .where(ProcInstance.mode == LIVE, ProcInstance.status == WAITING, overdue.is_not(None))
            .order_by(overdue, ProcInstance.id).limit(limit)
        )).all()
        fired = 0
        for instance_id in candidates:
            instance = await self._try_lock(session, instance_id)
            if instance is None or instance.status != WAITING:
                continue  # busy with another request (it will be looked at again), or no longer waiting
            due_ids = (await session.scalars(
                select(ProcWait.id).where(
                    ProcWait.instance_id == instance_id, ProcWait.status == OPEN, ProcWait.due_at.is_not(None),
                    ProcWait.due_at <= now,
                ).order_by(ProcWait.due_at, ProcWait.id)
            )).all()
            for wait_id in due_ids:
                wait = await session.get(ProcWait, wait_id, populate_existing=True)
                if wait is None or wait.status != OPEN:
                    continue  # an earlier timer in this tick already closed it (its step was cancelled)
                if await self._fire_safely(session, instance_id, wait):
                    fired += 1
                status = await session.scalar(select(ProcInstance.status).where(ProcInstance.id == instance_id))
                if status not in (RUNNING, WAITING):
                    break
        return fired

    async def _fire_safely(self, session, instance_id, wait: ProcWait) -> bool:
        """Fire one timer in a savepoint. When it breaks, undo it and fail that instance with the reason."""
        element_id = wait.element_id  # read now: a rolled-back savepoint expires what it touched
        try:
            async with session.begin_nested():
                await self._fire(session, wait)
            return True
        except Exception as exc:  # noqa: BLE001 - one bad timer must not stop the rest, whatever broke
            log.exception("the timer at '%s' of instance %s couldn't be fired", element_id, instance_id)
            reason = f"the timer at '{element_id}' couldn't be fired: {type(exc).__name__}: {exc}"
            try:
                async with session.begin_nested():
                    instance = await self._lock(session, instance_id)
                    await self._end(session, instance, FAILED, "fail", reason)
            except Exception:  # noqa: BLE001 - it stays as it was and is looked at again by the next tick
                log.exception("couldn't record that the timer at '%s' of instance %s broke it", element_id, instance_id)
            return False

    async def fire_timer(self, session: AsyncSession, instance_id, element_id: str) -> ProcInstance:
        """Fire the timer at `element_id` now (a boundary timer or a timer catch event), whatever its due time. Raises
        `NotWaiting` when none is waiting."""
        instance = await self._lock(session, instance_id)
        wait = await self._open_wait(session, instance, element_id, ("boundary_timer", "catch_timer"))
        if wait is None:
            raise NotWaiting(f"no timer is waiting at '{element_id}' in this instance")
        await self._fire(session, wait)
        return instance

    async def cancel(self, session: AsyncSession, instance_id, reason: str = "") -> ProcInstance:
        """Stop an instance (and any sub-process it is waiting on). Its history stays."""
        instance = await self._lock(session, instance_id, allow_ended=True)
        if instance.status not in (COMPLETED, FAILED, CANCELLED):
            await self._end(session, instance, CANCELLED, "cancel", reason)
        return instance

    # --- reading ---------------------------------------------------------------------------------------

    async def where_is(self, session: AsyncSession, instance_id) -> dict:
        """Where an instance is: its status, its token's step, and what that step waits for."""
        instance = await session.get(ProcInstance, instance_id)
        if instance is None:
            raise EngineError(f"no process instance {instance_id}")
        defn = await self._def(session, instance)
        token = await self._token(session, instance, any_state=True)
        waits = list((await session.scalars(select(ProcWait).where(
            ProcWait.instance_id == instance.id, ProcWait.status == OPEN))).all())
        # what hands work out first, then the timers that would interrupt it
        waits.sort(key=lambda w: (w.kind == "boundary_timer", w.created_at, w.element_id))
        element = defn.elements.get(token.element_id) if token else None
        return {
            "process": instance.process_key, "status": instance.status, "mode": instance.mode,
            "step": element.id if element else None, "step_name": element.label if element else None,
            "token": token.state if token else None, "error": instance.error,
            "waiting_for": [{"step": w.element_id, "kind": w.kind, "payload": w.payload,
                             "due_at": w.due_at.isoformat() if w.due_at else None, "message": w.message}
                            for w in waits],
        }

    async def history(self, session: AsyncSession, instance_id) -> list[ProcHistory]:
        return list((await session.scalars(
            select(ProcHistory).where(ProcHistory.instance_id == instance_id).order_by(ProcHistory.seq)
        )).all())

    # --- the run loop ----------------------------------------------------------------------------------

    async def _def(self, session: AsyncSession, instance: ProcInstance) -> ProcessDef:
        """The file an instance runs on. It finishes on the file it started on, found in this order:

        1. that exact file, when it is loaded;
        2. a loaded file of the same process with the same behaviour hash (a release that only changed words):
           behaviour is all the engine reads, so the run can't tell the difference;
        3. the text kept when the instance started (`proc_files`), loaded on the side. It is never added to the
           library, so it can't become the file new work starts on.
        """
        key = instance.process_key
        defn = self.library.get(key, instance.file_hash)
        if defn is not None:
            return defn
        if instance.behaviour_hash:
            owner = loader_owner_of(instance.data, instance.subject)
            for candidate in (self.library.get(key, entity_id=owner), self.library.shipped(key)):
                if candidate is not None and candidate.behaviour_hash == instance.behaviour_hash:
                    return candidate
        return await self._kept_def(session, instance)

    async def _live_def(self, session: AsyncSession, instance: ProcInstance) -> ProcessDef:
        """`_def` for work that moves the instance: one whose own file can't load any more (a release removed a step's
        card or instruction file) is moved onto the file new work starts on first (`_move_to_current`), never failed."""
        try:
            return await self._def(session, instance)
        except Stranded:
            return await self._move_to_current(session, instance)

    async def move_stranded(self, session: AsyncSession) -> int:
        """Move every running or waiting instance whose own file can no longer load onto the current file
        (`_move_to_current`), as an app does at start-up so no release leaves work that can't be answered. Locks each
        instance in turn; flushes, the caller commits. Returns how many it moved. Only runs of the processes this
        engine's library holds: a run of another library's process (an app keeps more than one) is that library's
        engine's to sweep, not a stranded run here."""
        ids = (await session.scalars(select(ProcInstance.id).where(
            ProcInstance.status.in_((RUNNING, WAITING)), ProcInstance.mode == LIVE,
            ProcInstance.process_key.in_(self.library.keys())).order_by(ProcInstance.id))).all()
        moved = 0
        for one in ids:
            instance = await self._try_lock(session, one)
            if instance is None or instance.status not in (RUNNING, WAITING):
                continue
            try:
                await self._def(session, instance)
            except Stranded:
                try:
                    async with session.begin_nested():
                        await self._move_to_current(session, instance)
                except EngineError:
                    log.exception("couldn't move the instance %s onto the current file", one)
                    continue
                except Exception:  # noqa: BLE001 - a step that raised anything must not stop the sweep for the others
                    log.exception("the instance %s raised while it was moved onto the current file; the sweep goes on",
                                  one)
                    continue
                moved += 1
        return moved

    async def _move_to_current(self, session: AsyncSession, instance: ProcInstance) -> ProcessDef:
        """Put an instance whose own file can't load on the file new work starts on. Where its token waits stays when
        that step is still there as the same kind of step (its open wait carries on, so an answer on the way in still
        lands); when the step is gone or changed, the token goes back to the nearest step it passed that is still
        there (the start, failing that) and the run goes on from it: the step's wait is void, a new one is handed
        out. Raises `Stranded` when there is no current file for the process."""
        owner = loader_owner_of(instance.data, instance.subject)
        defn = self.library.get(instance.process_key, entity_id=owner)
        if defn is None:
            raise Stranded(f"no process '{instance.process_key}' is loaded to move this instance onto")
        was = instance.file_hash
        token = await self._token(session, instance)
        wait = None
        was_at = token.element_id if token is not None else None
        if token is not None:
            wait = await self._open_wait(session, instance, token.element_id,
                                         ("agent", "user", "catch_message", "catch_timer", "call"))
        element = defn.elements.get(token.element_id) if token is not None else None
        same = (element is not None and wait is not None
                and (wait.kind, element.kind) in (("agent", AGENT), ("user", USER), ("catch_message", CATCH),
                                                  ("catch_timer", CATCH), ("call", CALL)))
        instance.file_hash, instance.behaviour_hash = defn.file_hash, defn.behaviour_hash or None
        await self._keep_file(session, instance.process_key, defn)
        if same:
            await self._log(session, instance, "migrate", from_element=token.element_id, to_element=token.element_id,
                            detail={"from_file": was, "to_file": defn.file_hash})
            await session.flush()
            return defn
        target = await self._nearest_step(session, instance, defn)
        if token is None:
            token = ProcToken(instance_id=instance.id, element_id=target, state=ACTIVE, created_at=self.clock())
            session.add(token)
        else:
            await self._close_waits(session, instance, token)
            await self._cancel_children(session, instance)
            token.element_id, token.state = target, ACTIVE
        instance.status = RUNNING
        await self._log(session, instance, "migrate", from_element=was_at,
                        to_element=target, detail={"from_file": was, "to_file": defn.file_hash})
        await session.flush()
        await self._advance(session, instance)
        return defn

    async def _nearest_step(self, session: AsyncSession, instance: ProcInstance, defn: ProcessDef) -> str:
        """The step the run passed most recently that the current file still has and that can be run again from (not
        an end, not a boundary event), or the file's start."""
        seen = (await session.scalars(select(ProcHistory.to_element).where(
            ProcHistory.instance_id == instance.id, ProcHistory.to_element.is_not(None))
            .order_by(ProcHistory.seq.desc()))).all()
        for name in seen:
            found = defn.elements.get(name)
            if found is not None and found.kind not in (END, BOUNDARY):
                return name
        return defn.start_events[0]

    async def _kept_def(self, session: AsyncSession, instance: ProcInstance) -> ProcessDef:
        kept = _kept_files.setdefault(self.library, {})
        defn = kept.get(instance.file_hash)
        if defn is not None:
            return defn
        where = f"the version of '{instance.process_key}' this instance started under"
        row = await session.get(ProcFile, instance.file_hash)
        if row is None:
            raise Stranded(f"{where} isn't loaded any more, and its text wasn't kept")
        try:
            defn = load_process(row.xml, key=row.process_key, catalog=self.catalog, registry=self.registry)
        except ProcessLoadError as exc:
            raise Stranded(f"{where} no longer loads: {'; '.join(exc.problems)}") from exc
        kept[instance.file_hash] = defn
        return defn

    async def _keep_file(self, session: AsyncSession, process_key: str, defn: ProcessDef) -> None:
        """Keep the text of the file an instance starts on, once per file (a second start finds it there)."""
        if not defn.xml:
            return
        await portable.insert_ignore(
            session, ProcFile, {"file_hash": defn.file_hash, "process_key": process_key, "xml": defn.xml},
            conflict=("file_hash",))

    async def _lineage(self, session: AsyncSession, instance_id) -> list[uuid.UUID]:
        """The instance's id and its ancestors', the topmost first: the order locks are taken in."""
        ids: list[uuid.UUID] = []
        current = _as_uuid(instance_id)
        while current is not None and current not in ids:
            ids.append(current)
            current = await session.scalar(select(ProcInstance.parent_id).where(ProcInstance.id == current))
        return ids[::-1]

    async def _lock(self, session, instance_id, *, allow_ended: bool = False) -> ProcInstance:
        """Lock an instance for the rest of the transaction, after its ancestors (parent before child), and read it
        fresh: a request that waited here sees what the one before it committed. An instance that has ended
        raises `NotWaiting` (nothing can happen to it any more) unless `allow_ended`."""
        lineage = await self._lineage(session, instance_id)
        instance = None
        for one in lineage:
            instance = await portable.get_locked(session, ProcInstance, one)
            if instance is None:
                break
        if instance is None or not lineage:
            raise EngineError(f"no process instance {instance_id}")
        if not allow_ended and instance.status in (COMPLETED, FAILED, CANCELLED):
            raise NotWaiting(f"this instance has already {instance.status}")
        return instance

    async def _try_lock(self, session, instance_id) -> ProcInstance | None:
        """The same locks as `_lock`, taken without waiting: None when any of them is held by another request."""
        instance = None
        for one in await self._lineage(session, instance_id):
            instance = await session.scalar(
                portable.for_update(select(ProcInstance).where(ProcInstance.id == one), skip_locked=True)
                .execution_options(populate_existing=True)
            )
            if instance is None:
                return None
        return instance

    async def _token(self, session, instance, any_state: bool = False) -> ProcToken | None:
        query = select(ProcToken).where(ProcToken.instance_id == instance.id)
        if not any_state:
            query = query.where(ProcToken.state != DONE)
        return await session.scalar(query.order_by(ProcToken.created_at, ProcToken.id).limit(1))

    async def _open_wait(self, session, instance, element_id, kinds, wait_id=None) -> ProcWait | None:
        """The open wait at `element_id` of one of `kinds`; with `wait_id`, only that wait (None when it is another,
        or already answered)."""
        query = select(ProcWait).where(
            ProcWait.instance_id == instance.id, ProcWait.status == OPEN, ProcWait.element_id == element_id,
            ProcWait.kind.in_(kinds))
        if wait_id is not None:
            wanted = _as_uuid(wait_id)
            if wanted is None:
                return None
            query = query.where(ProcWait.id == wanted)
        return await session.scalar(
            query.order_by(ProcWait.created_at, ProcWait.id).limit(1).execution_options(populate_existing=True))

    async def _log(self, session, instance, kind, *, from_element=None, to_element=None, flow_id=None,
                   condition=None, reads=None, detail=None) -> None:
        seq = (await session.scalar(
            select(func.coalesce(func.max(ProcHistory.seq), 0)).where(ProcHistory.instance_id == instance.id)
        )) + 1
        session.add(ProcHistory(
            instance_id=instance.id, seq=seq, kind=kind, from_element=from_element, to_element=to_element,
            flow_id=flow_id, condition=condition, reads=reads, detail=detail, at=self.clock(),
        ))
        await session.flush()

    async def _advance(self, session: AsyncSession, instance: ProcInstance) -> None:
        """Run the instance until its token waits or the process ends."""
        defn = await self._live_def(session, instance)
        moves = 0
        while instance.status == RUNNING:
            token = await self._token(session, instance)
            if token is None or token.state != ACTIVE:
                break
            moves += 1
            if moves > self.max_moves:
                await self._end(session, instance, FAILED, "fail",
                                f"stopped after {self.max_moves} steps without waiting: the process loops")
                return
            await self._step(session, instance, defn, token, defn.element(token.element_id))
        if instance.status == RUNNING:
            token = await self._token(session, instance)
            if token is not None and token.state == WAITS:
                instance.status = WAITING
        await session.flush()

    async def _step(self, session, instance, defn: ProcessDef, token: ProcToken, element: Element) -> None:
        kind = element.kind
        if kind == START:
            await self._take_only_way(session, instance, defn, token, element)
        elif kind == END:
            token.state = DONE
            await self._end(session, instance, COMPLETED, "end", None, from_element=element.id)
        elif kind == GATEWAY:
            await self._decide(session, instance, defn, token, element)
        elif kind == SERVICE:
            await self._service(session, instance, defn, token, element)
        elif kind in (AGENT, USER):
            await self._hand_out(session, instance, defn, token, element)
        elif kind == CALL:
            await self._call(session, instance, defn, token, element)
        elif kind == CATCH:
            await self._catch(session, instance, defn, token, element)
        else:  # a boundary event is never where a token sits
            await self._end(session, instance, FAILED, "fail", f"the token reached '{element.label}', which it can't")

    async def _move(self, session, instance, token, flow: Flow, *, kind="move", condition=None, reads=None,
                    detail=None, from_element=None) -> None:
        """Take a way out. `from_element` is where the token was, when that isn't the flow's own source (a
        boundary event's way out starts from the step it interrupted)."""
        await self._log(session, instance, kind, from_element=from_element or flow.source, to_element=flow.target,
                        flow_id=flow.id, condition=condition, reads=reads, detail=detail)
        token.element_id = flow.target
        token.state = ACTIVE

    async def _take_only_way(self, session, instance, defn, token, element) -> None:
        await self._move(session, instance, token, defn.out_flows(element.id)[0])

    def _store_result(self, instance: ProcInstance, element: Element, result: dict | None) -> None:
        data = dict(instance.data or {})
        data["result"] = _clean(result) if result is not None else {}
        if element.output:
            data[element.output] = data["result"]
        instance.data = data

    # --- the kinds of step -----------------------------------------------------------------------------

    async def _decide(self, session, instance, defn, token, gateway: Element) -> None:
        ways = defn.out_flows(gateway.id)
        if len(ways) == 1:
            await self._move(session, instance, token, ways[0])
            return
        reads: dict = {}
        chosen, why = None, None
        for flow in ways:
            if flow.is_default:
                continue
            try:
                holds, seen = feel.evaluate_condition(flow.tree, instance.data or {})
            except ConditionError as exc:
                await self._end(session, instance, FAILED, "fail",
                                f"the decision '{gateway.label}': {exc} ({flow.condition})")
                return
            reads.update(seen)
            if holds:
                chosen, why = flow, flow.effective
                break
        if chosen is None:
            chosen = next((f for f in ways if f.is_default), None)
            why = "default"
        if chosen is None:
            await self._end(session, instance, FAILED, "fail",
                            f"the decision '{gateway.label}' had no way out for this data and no default")
            return
        await self._move(session, instance, token, chosen, kind="decision", condition=why,
                         reads={k: _summarise(v) for k, v in reads.items()})

    async def _service(self, session, instance, defn, token, element: Element) -> None:
        spec = self.registry.get(element.action)
        if spec is None:
            await self._end(session, instance, FAILED, "fail", f"the action '{element.action}' isn't registered")
            return
        if element.action in self.deferred:
            await self._wait(session, instance, token, element, "deferred",
                             {"name": element.label, "action": element.action, "element": element.id},
                             hand_out=False)
            return
        try:
            result = await spec.fn(session, instance, copy.deepcopy(instance.data or {}))
        except ActionError as exc:
            await self._raise_error(session, instance, defn, token, element, exc.code, exc.message)
            return
        except RunOver as exc:
            await self._end(session, instance, CANCELLED, "cancel", exc.message)
            return
        await self._log(session, instance, "complete", from_element=element.id,
                        detail={"action": element.action, "result_keys": sorted((result or {}).keys())})
        self._store_result(instance, element, result)
        await self._take_only_way(session, instance, defn, token, element)

    async def _raise_error(self, session, instance, defn, token, element, code, message) -> None:
        """A step raised a named error: the matching error boundary event takes the token, else the instance fails."""
        boundaries = [defn.element(b) for b in element.boundaries if defn.element(b).trigger.type == "error"]
        catcher = next((b for b in boundaries if b.trigger.error_code == code), None) or next(
            (b for b in boundaries if b.trigger.error_code is None), None)
        if catcher is None:
            await self._end(session, instance, FAILED, "fail", f"'{element.label}' raised {code}: {message}")
            return
        data = dict(instance.data or {})
        data["error"] = {"code": code, "message": message}
        instance.data = data
        flow = defn.out_flows(catcher.id)[0]
        await self._move(session, instance, token, flow, kind="error", from_element=element.id,
                         detail={"boundary": catcher.id, "code": code, "message": message})

    async def _wait(self, session, instance, token, element, kind, payload, *, hand_out=True,
                    due_at=None, message=None) -> ProcWait:
        wait = ProcWait(instance_id=instance.id, token_id=token.id, element_id=element.id, kind=kind, status=OPEN,
                        payload=payload, due_at=due_at, message=message, created_at=self.clock())
        session.add(wait)
        token.state = WAITS
        await session.flush()
        await self._log(session, instance, "wait", from_element=element.id,
                        detail={"kind": kind, **({"due_at": due_at.isoformat()} if due_at else {})})
        if hand_out and self.on_wait is not None:
            await self.on_wait(session, instance, wait)
        return wait

    async def _boundary_timers(self, session, instance, defn, token, element) -> None:
        for bid in element.boundaries:
            boundary = defn.element(bid)
            if boundary.trigger.type != "timer":
                continue
            due = self._due(boundary)
            session.add(ProcWait(instance_id=instance.id, token_id=token.id, element_id=bid, kind="boundary_timer",
                                 status=OPEN, payload={"attached_to": element.id}, due_at=due,
                                 created_at=self.clock()))
            await self._log(session, instance, "wait", from_element=bid,
                            detail={"kind": "boundary_timer", "due_at": due.isoformat()})
        await session.flush()

    def _due(self, timer_element: Element) -> datetime:
        trigger = timer_element.trigger
        if trigger.timer_kind == "duration":
            return self.clock() + parse_duration(trigger.timer_text or "")
        due = datetime.fromisoformat((trigger.timer_text or "").strip())
        return due.astimezone(UTC) if due.tzinfo else due.replace(tzinfo=UTC)  # no zone: UTC, as `UtcDateTime`

    async def _hand_out(self, session, instance, defn, token, element: Element) -> None:
        if element.kind == AGENT:
            payload = {"name": element.label, "element": element.id, "lane": element.lane, "role": element.role,
                       "instructions": element.instructions, "template": element.template}
        else:
            payload = {"name": element.label, "element": element.id, "lane": element.lane, "card": element.card}
        await self._boundary_timers(session, instance, defn, token, element)
        await self._wait(session, instance, token, element, "agent" if element.kind == AGENT else "user", payload)

    async def _catch(self, session, instance, defn, token, element: Element) -> None:
        if element.trigger.type == "message":
            await self._wait(session, instance, token, element, "catch_message",
                             {"name": element.label, "element": element.id, "message": element.trigger.message},
                             hand_out=False, message=element.trigger.message)
        else:
            await self._wait(session, instance, token, element, "catch_timer",
                             {"name": element.label, "element": element.id}, hand_out=False,
                             due_at=self._due(element))

    async def _close_waits(self, session, instance, token) -> None:
        """Void every other open wait of the instance: a finished step's timers, a cancelled step's leftovers."""
        waits = (await session.scalars(select(ProcWait).where(
            ProcWait.instance_id == instance.id, ProcWait.status == OPEN
        ).order_by(ProcWait.id).execution_options(populate_existing=True))).all()
        for wait in waits:
            wait.status, wait.closed_at = VOID, self.clock()

    # --- sub-processes ---------------------------------------------------------------------------------

    async def _call(self, session, instance, defn, token, element: Element) -> None:
        child_def = self.library.get(
            element.called, entity_id=loader_owner_of(instance.data, instance.subject))
        if child_def is None:
            await self._end(session, instance, FAILED, "fail", f"'{element.label}' calls '{element.called}', "
                            "which isn't loaded")
            return
        items = get_path(instance.data or {}, element.multi.collection) if element.multi is not None else None
        base = {child_var: _clean(get_path(instance.data or {}, source)) for child_var, source in element.in_map}
        await self._boundary_timers(session, instance, defn, token, element)
        if element.multi is None:
            await self._wait(session, instance, token, element, "call",
                             {"name": element.label, "element": element.id, "called": element.called,
                              "children": 1, "done": 0}, hand_out=False)
            await self._start_child(session, instance, element, base, None)
            return
        if not isinstance(items, list):
            await self._end(session, instance, FAILED, "fail",
                            f"'{element.label}' runs once per item, but '{element.multi.collection}' isn't a list")
            return
        if not items:  # nothing to do: the step is done at once, with empty results
            await self._log(session, instance, "complete", from_element=element.id, detail={"items": 0})
            data = dict(instance.data or {})
            for parent_var, _child_path in element.out_map:
                data[parent_var] = []
            data["result"] = {parent_var: [] for parent_var, _ in element.out_map}
            instance.data = data
            await self._take_only_way(session, instance, defn, token, element)
            return
        wait = await self._wait(
            session, instance, token, element, "call",
            {"name": element.label, "element": element.id, "called": element.called, "children": len(items),
             "done": 0, "next": 0, "sequential": element.multi.sequential, "results": [None] * len(items)},
            hand_out=False)
        first = 1 if element.multi.sequential else len(items)
        wait.payload = {**wait.payload, "next": first}
        for index in range(first):
            if instance.status != WAITING and instance.status != RUNNING:
                break
            await self._start_child(session, instance, element, base, (index, items[index]))

    async def _start_child(self, session, parent: ProcInstance, element: Element, base: dict, item) -> None:
        data = dict(base)
        index = None
        if item is not None:
            index, value = item
            data[element.multi.element_variable] = _clean(value)
        await self._start(session, element.called, data, subject=parent.subject, parent=parent,
                          parent_element=element.id, item_index=index)

    async def _on_child_end(self, session, child: ProcInstance) -> None:
        """A sub-process ended: bring back what the call asked for, and let the parent's token on once every
        child it waits for is done."""
        parent = await portable.get_locked(session, ProcInstance, child.parent_id)
        if parent is None or parent.status in (COMPLETED, FAILED, CANCELLED):
            return
        defn = await self._live_def(session, parent)
        element = defn.element(child.parent_element)
        wait = await self._open_wait(session, parent, element.id, ("call",))
        token = await self._token(session, parent)
        if wait is None or token is None:
            return
        brought = {pv: _clean(get_path(child.data or {}, path)) for pv, path in element.out_map}
        payload = dict(wait.payload)
        payload["done"] = payload.get("done", 0) + 1
        if element.multi is None:
            joined = {pv: v for pv, v in brought.items()}
        else:
            results = list(payload["results"])
            results[child.item_index] = brought
            payload["results"] = results
            if payload["sequential"] and payload["next"] < payload["children"]:
                index = payload["next"]
                payload["next"] = index + 1
                wait.payload = payload
                await session.flush()
                items = get_path(parent.data or {}, element.multi.collection)
                base = {cv: _clean(get_path(parent.data or {}, src)) for cv, src in element.in_map}
                await self._start_child(session, parent, element, base, (index, items[index]))
                return
            if payload["done"] < payload["children"]:
                wait.payload = payload
                await session.flush()
                return
            joined = {pv: [r[pv] for r in results] for pv, _ in element.out_map}
        wait.payload = payload
        wait.status, wait.closed_at = CLOSED, self.clock()
        await self._close_waits(session, parent, token)
        data = dict(parent.data or {})
        data.update(joined)
        data["result"] = joined
        if element.output:
            data[element.output] = joined
        parent.data = data
        parent.status = RUNNING
        await self._log(session, parent, "complete", from_element=element.id,
                        detail={"called": element.called, "children": payload["children"]})
        await self._take_only_way(session, parent, defn, token, element)
        await self._advance(session, parent)

    # --- timers ----------------------------------------------------------------------------------------

    async def _fire(self, session, wait: ProcWait) -> None:
        instance = await self._lock(session, wait.instance_id)
        defn = await self._live_def(session, instance)
        token = await self._token(session, instance)
        element = defn.element(wait.element_id)
        wait.status, wait.closed_at = CLOSED, self.clock()
        if wait.kind == "catch_timer":
            await self._close_waits(session, instance, token)
            instance.status = RUNNING
            await self._log(session, instance, "fire", from_element=element.id, detail={"timer": "catch"})
            await self._take_only_way(session, instance, defn, token, element)
        else:
            host = defn.element(element.attached_to)
            await self._close_waits(session, instance, token)
            await self._cancel_children(session, instance, host.id)
            instance.status = RUNNING
            await self._move(session, instance, token, defn.out_flows(element.id)[0], kind="fire",
                             from_element=host.id, detail={"timer": "boundary", "boundary": element.id})
        await self._advance(session, instance)

    # --- ending ----------------------------------------------------------------------------------------

    async def _cancel_children(self, session, instance: ProcInstance, parent_element: str | None = None) -> None:
        query = select(ProcInstance).where(ProcInstance.parent_id == instance.id, ProcInstance.status.in_(
            (RUNNING, WAITING)))
        if parent_element:
            query = query.where(ProcInstance.parent_element == parent_element)
        # the parent is locked already; its children follow, in one fixed order
        query = portable.for_update(query.order_by(ProcInstance.id)).execution_options(populate_existing=True)
        for child in (await session.scalars(query)).all():
            await self._end(session, child, CANCELLED, "cancel", "its parent stopped waiting for it",
                            tell_parent=False)

    async def _end(self, session, instance: ProcInstance, status: str, kind: str, message: str | None, *,
                   from_element: str | None = None, tell_parent: bool = True) -> None:
        instance.status = status
        instance.ended_at = self.clock()
        if status in (FAILED, CANCELLED):
            instance.error = message
        token = await self._token(session, instance)
        if token is not None and status != COMPLETED:
            token.state = DONE
        for wait in (await session.scalars(select(ProcWait).where(
                ProcWait.instance_id == instance.id, ProcWait.status == OPEN
        ).order_by(ProcWait.id).execution_options(populate_existing=True))).all():
            wait.status, wait.closed_at = VOID, self.clock()
        await self._log(session, instance, kind, from_element=from_element,
                        detail={"message": message} if message else None)
        await self._cancel_children(session, instance)
        await session.flush()
        if instance.parent_id and tell_parent:
            if status == COMPLETED:
                await self._on_child_end(session, instance)
            else:
                parent = await portable.get_locked(session, ProcInstance, instance.parent_id)
                if parent is not None and parent.status in (RUNNING, WAITING):
                    why = f"the sub-process '{instance.process_key}' {status}"
                    await self._end(session, parent, FAILED, "fail", f"{why}: {message}" if message else why)
