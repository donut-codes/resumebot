"""The one door route code uses to tell the engine something, so that a route module never imports the engine's own
modules (`work.wake`, `work.dispatcher`, `loops.schedule`, `sites.wake_ops`):

- `knock(run_id, reason)`: the signed wake of the scout's door for one run, packet, role or job (`wake.knock`).
- `dispatch_now()`: a pass of the runner dispatcher at once (`dispatcher.dispatch_now`).
- `knocks(pending)`: the knocks a mission run left, and a pass of the dispatcher after them (`schedule.send_knocks`).
- `send_wakes(wakes)`: the wakes of site projects, with their retries (`wake_ops.send_all`).
- `nudge()`: tell the dispatcher in this process to look now (`consume.wake`).
- `start(name, key)`: begin the background task that `register(name, starter)` named, for `key`, in this process.
- `ask(session, owner, name, args, wait_s)`: put a question to the engine and wait for its answer (see its docstring).
- `owner_do(session, owner, name, raw, dry_run)` and `owner_job(session, owner, job_id)`: the owner door's `act.do` and
  `act.job`. `Local` runs the action here (`runner.do`, `runner.job`); `Remote` writes the job's row and asks the engine
  (`asked.do`, `asked.job`).

Each is called once the caller's work is committed, never inside a transaction (`wake.knock` checks that itself).

A backend does the work. `Local`, the default, calls the engine's functions in this process and looks each one up at the
moment it is called, so a test that replaces `wake.knock` (the `knocks` fixture) still sees every knock. `install` swaps
the backend; `install(None)` puts `Local` back. This module imports nothing of the app at the top, so a module that
only needs a reason's name, or the door, does not bring the engine in with it.

The reasons are the values of the `wake` module's own (`test_engine_calls.py` pins that they are equal); they live
here so that a module which needs only a name need not import the wake."""

from __future__ import annotations

import json
import socket
import uuid
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.sites.agent_calls import Wake

#: The reasons a knock carries, each the kind of work the scout is knocked for (see `wake.py` for what each means).
SEARCH_STARTED = "search_started"
PACKET_ASKED = "packet_asked"
ANALYSIS_ASKED = "analysis_asked"
SITE_PROJECT = "site_project"
TAG_ROLES = "tag_roles"
PARSE_ASKED = "parse_asked"
CHECK_ASKED = "check_asked"
GAPS_ASKED = "gaps_asked"
EMPLOYERS_ASKED = "employers_asked"
MAIL_ASKED = "mail_asked"
COACH_ASKED = "coach_asked"
RUN_NOW = "run_now"

#: Every reason above. The engine checks a reason a bell hands it against this (`work/bell.py`): a bell is a hint, and a
#: reason nobody knows is not knocked for.
REASONS = frozenset({
    SEARCH_STARTED, PACKET_ASKED, ANALYSIS_ASKED, SITE_PROJECT, TAG_ROLES, PARSE_ASKED, CHECK_ASKED, GAPS_ASKED,
    EMPLOYERS_ASKED, MAIL_ASKED, COACH_ASKED, RUN_NOW,
})


class Backend(Protocol):
    """What does the telling. Every method but `nudge` and `start` is awaited."""

    async def knock(self, run_id: str, reason: str) -> bool: ...

    async def dispatch_now(self) -> int: ...

    async def knocks(self, pending: list[tuple[str, str]]) -> None: ...

    async def send_wakes(self, wakes: Iterable[tuple[uuid.UUID | str, Wake | None]]) -> list[str]: ...

    def nudge(self) -> None: ...

    def start(self, name: str, key: Any) -> None: ...

    async def owner_do(self, session: AsyncSession, owner: str, name: str, raw: dict[str, Any] | None,
                       dry_run: bool) -> dict[str, Any]: ...

    async def owner_job(self, session: AsyncSession, owner: str, job_id: str | None) -> dict[str, Any]: ...


class Local:
    """The engine's functions, called in this process. Each is looked up when the call is made, never kept."""

    async def knock(self, run_id: str, reason: str) -> bool:
        from app.jobsearch.work import wake

        return await wake.knock(run_id, reason=reason)

    async def dispatch_now(self) -> int:
        from app.jobsearch.work import dispatcher

        return await dispatcher.dispatch_now()

    async def knocks(self, pending: list[tuple[str, str]]) -> None:
        from app.jobsearch.loops import schedule

        await schedule.send_knocks(pending)

    async def send_wakes(self, wakes: Iterable[tuple[uuid.UUID | str, Wake | None]]) -> list[str]:
        from app.sites import wake_ops

        return await wake_ops.send_all(wakes)

    def nudge(self) -> None:
        from app.jobsearch.work.queue import consume

        consume.wake()

    def start(self, name: str, key: Any) -> None:
        starter = _starters.get(name)
        if starter is None:
            raise LookupError(f"nothing is registered to start {name!r}")
        starter(key)

    async def owner_do(self, session: AsyncSession, owner: str, name: str, raw: dict[str, Any] | None,
                       dry_run: bool) -> dict[str, Any]:
        from app.jobsearch.owner_actions import runner

        return await runner.do(owner, name, raw, dry_run)

    async def owner_job(self, session: AsyncSession, owner: str, job_id: str | None) -> dict[str, Any]:
        from app.jobsearch.owner_actions import runner

        return await runner.job(session, owner, job_id)


#: How many pairs one `knocks` bell carries and how many project ids one `wakes` bell carries: a datagram is at most
#: 1200 bytes (`notice`), and a longer list goes in more than one bell. The engine refuses a bell with more
#: (`work/bell.py`).
KNOCKS_PER_BELL = 8
WAKES_PER_BELL = 20

#: The starts the page may ask the engine for, each with what its bell carries from the caller's key. Only a start whose
#: work the engine can find again in a row is here: the bell is a hint, and the engine reads the row and ignores
#: everything else the datagram says (`bell.ROW_STARTS` has the same names; a test pins that).
_ASKED_BY_ROW: dict[str, Callable[[Any], dict[str, str]]] = {
    "go": lambda key: {},
    "boards_night": lambda key: {"id": str(key[0])},
}


def _ring(what: str, kind: str, **fields: Any) -> None:
    """One bell to the engine. Never raises and never waits: with no engine listening it costs one failed send."""
    from app import db, notice

    db.outside_transaction(what)
    notice.ring_engine(kind, **fields)


class Remote:
    """The page's door: the engine is another program, so each call is a bell, a hint that costs the page one datagram.
    Nothing that must not be lost is in a bell: it is a row first (a to-do, a run, an ask), and the engine finds a lost
    bell's work at its start and at its own 30 second look. What each call returns is what a caller that does not wait
    for the engine can say: `knock` True, `dispatch_now` 0, `send_wakes` no ids."""

    async def knock(self, run_id: str, reason: str) -> bool:
        _ring(f"a bell for a wake of the scout ({reason})", "knock", id=str(run_id), reason=reason)
        return True

    async def dispatch_now(self) -> int:
        _ring("a bell for a pass of the dispatcher", "dispatch")
        return 0

    async def knocks(self, pending: list[tuple[str, str]]) -> None:
        for first in range(0, len(pending), KNOCKS_PER_BELL):
            chunk = pending[first:first + KNOCKS_PER_BELL]
            _ring("a bell for the knocks of a mission", "knocks", pairs=[[str(key), reason] for key, reason in chunk])

    async def send_wakes(self, wakes: Iterable[tuple[uuid.UUID | str, Wake | None]]) -> list[str]:
        ids: list[str] = []
        for project_id, wake in wakes:
            if wake is not None and str(project_id) not in ids:
                ids.append(str(project_id))
        for first in range(0, len(ids), WAKES_PER_BELL):
            _ring("a bell for the wakes of site projects", "wakes", ids=ids[first:first + WAKES_PER_BELL])
        return []

    def nudge(self) -> None:
        from app import notice

        notice.ring_engine("look")

    def start(self, name: str, key: Any) -> None:
        fields_of = _ASKED_BY_ROW.get(name)
        if fields_of is None:
            raise LookupError(f"{name!r} is not asked of the engine by a row yet; the page cannot start it")
        _ring(f"a bell for a start ({name})", f"start:{name}", **fields_of(key))

    async def owner_do(self, session: AsyncSession, owner: str, name: str, raw: dict[str, Any] | None,
                       dry_run: bool) -> dict[str, Any]:
        from app.jobsearch.owner_actions import asked

        return await asked.do(session, owner, name, raw, dry_run)

    async def owner_job(self, session: AsyncSession, owner: str, job_id: str | None) -> dict[str, Any]:
        from app.jobsearch.owner_actions import asked

        return await asked.job(session, owner, job_id)


_backend: Backend = Local()


def install(backend: Backend | None = None) -> None:
    """Use `backend` for every call from now on; with nothing, the local one again."""
    global _backend
    _backend = backend if backend is not None else Local()


async def knock(run_id: str, reason: str = SEARCH_STARTED) -> bool:
    """True when the scout accepted the wake (`wake.knock`)."""
    return await _backend.knock(run_id, reason)


async def dispatch_now() -> int:
    """A pass of the runner dispatcher at once (`dispatcher.dispatch_now`); how many lanes it commanded."""
    return await _backend.dispatch_now()


async def knocks(pending: list[tuple[str, str]]) -> None:
    """The knocks a mission run left, as `(id, reason)`, then a pass of the dispatcher (`schedule.send_knocks`)."""
    await _backend.knocks(pending)


async def send_wakes(wakes: Iterable[tuple[uuid.UUID | str, Wake | None]]) -> list[str]:
    """The wakes of site projects, one knock for each project (`wake_ops.send_all`); the ids it took a knock for."""
    return await _backend.send_wakes(wakes)


def nudge() -> None:
    """A to-do was committed: the dispatcher in this process looks now (`consume.wake`)."""
    _backend.nudge()


_starters: dict[str, Callable[[Any], None]] = {}


def register(name: str, starter: Callable[[Any], None]) -> None:
    """Say which function begins the background task called `name`. `starter(key)` makes the task and keeps it in the
    module's own set, as it always did. Asking again with the same name replaces the first. A module that owns a task
    registers it when it is imported."""
    _starters[name] = starter


def start(name: str, key: Any) -> None:
    """Begin the background task called `name` for `key` (an owner, a list of ids: what its starter needs). It returns
    at once. With the `Local` backend the starter runs here and needs a running event loop; a backend that only records
    the start runs nothing."""
    _backend.start(name, key)


#: The kind an ask's notice, its waiter and its to-do are filed under (`engine_asks.KIND` is the same word).
ASK_KIND = "ask"

#: What a route says when the engine did not answer an ask in time. The ask is kept: the engine carries it out when it
#: next runs (its start-up looks at every ask younger than a day that has no answer).
WORKER_NOT_RUNNING = (
    "The part of resumebot that does the work isn't running. Your request is saved and is carried out when it starts.")


def _new_id() -> str:
    """A fresh id for an ask (a test replaces this to know it)."""
    return uuid.uuid4().hex


def _nothing_listens() -> bool:
    """True when nothing listens on the engine's port, so an ask to it would wait for nobody: the port (the page's own
    `--port` plus `notice.OFFSET`, `notice.engine_target`) can be bound here. False when it cannot be bound (the engine
    holds it), when the signal is off (tests, `resumebot serve`) and when it cannot be told: the wait is then as it
    always was. Instant: no connect, no timeout."""
    from app import notice

    target = notice.engine_target()
    if target is None:
        return False
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(target)
    except OSError:
        return False
    return True


async def ask(session: AsyncSession, owner: str, name: str, args: dict[str, Any], wait_s: float) -> Any:
    """Ask the engine to do the work called `name` (`engine_asks.register`) with `args` (plain data), and wait up to
    `wait_s` seconds for its answer. Returns the answer (what the engine's function returned, as JSON read back), or
    None when none came in time: the engine is stopped, busy or failed. The ask is kept either way, and the engine
    answers it when it can. When nothing listens on the engine's port (`_nothing_listens`) it returns None at once and
    does not wait.

    `session` is the caller's writer session. This commits it (and ends its transaction) before the wait, so what the
    caller wrote earlier in the request is saved with the ask; the caller writes nothing it cannot save yet.

    In this order: the fact `ask:<id>` and the to-do `ask` are written in the caller's transaction; the waiter is
    registered; the transaction is committed and released (no turn is held while we wait: a wait with the writer's turn
    held would stall every write of both programs and end in Busy); the engine is nudged; we wait for its notice; then,
    on a notice, at the timeout, or when the waiters are all woken because the engine restarted, `answer:<id>` is read
    once on a reader. The waiter exists before the commit, so a notice that comes at once is kept, and the one re-read
    is the catch-up for a notice that never comes.

    In the whole app in one process (`program.PROGRAM == "all"`: the test seam and `resumebot serve`) there is no other
    program to ask, so when a function is registered under `name` it runs here and now in the caller's session
    (`engine_asks.run_here`), no row is written, and the answer is what it returned, as JSON read back."""
    from app import db, notice, program
    from app.jobsearch import facts
    from app.jobsearch.work import queue
    from app.jobsearch.work.queue import topics
    from app.timeutil import iso_utc, utc_now

    if program.PROGRAM == "all":
        # The whole app in one process (the test seam, and `resumebot serve`) has no other program to ask: the function
        # of the ask runs here and now, in the caller's session, and nothing is written for the ask. In the page and in
        # the engine program the ask is a row and a wait, as below.
        from app.jobsearch.work import engine_asks

        if engine_asks.is_registered(name):
            return await engine_asks.run_here(session, owner, name, args)

    ask_id = _new_id()
    now = utc_now()
    asked = json.dumps({"name": name, "args": args, "asked_at": iso_utc(now)})
    await facts.put(session, owner, f"ask:{ask_id}", asked, now=now)
    queue.post(session, owner, topics.ENGINE_ASK, [ask_id], cause=topics.ASK, now=now)
    waiter = notice.waiters.wait_for(ASK_KIND, [ask_id])
    try:
        await db.release(session)
        nudge()
        if not _nothing_listens():  # the engine is stopped: no wait; the ask is kept and answered when it next runs
            await waiter.wait(wait_s)
        async with db.read_session_factory()() as reader:
            answer = await facts.get(reader, owner, f"answer:{ask_id}")
    finally:
        waiter.close()
    return None if answer is None else json.loads(answer)


async def owner_do(session: AsyncSession, owner: str, name: str, raw: dict[str, Any] | None,
                   dry_run: bool = False) -> dict[str, Any]:
    """The owner door's `act.do`. A one-program run runs the action here and now (`runner.do`); the page writes the
    job's row and asks the engine (`asked.do`). Either way the answer is the job's result, or the job as it stands
    when the wait is out. Raises what the door answers as a refusal."""
    return await _backend.owner_do(session, owner, name, raw, dry_run)


async def owner_job(session: AsyncSession, owner: str, job_id: str | None) -> dict[str, Any]:
    """The owner door's `act.job`: one job, or with no id the last jobs (`runner.job`, or `asked.job` on the page)."""
    return await _backend.owner_job(session, owner, job_id)
