"""The resumebot API: `uvicorn app.jobsearch.main:app`.

The engine's start-up and the schedule (`lifespan_for`, `_running`) are here, and so is the whole app in one process,
`app = build_app("all", lifespan)`: the in-code test seam and what `resumebot serve` runs. `build_app(role)`
(`app.jobsearch.factory`) builds the app of any one program: the routers, a health check, the one handler for the app's
refusals (`app.errors`), the guard, the page's layer (`app.web`) and the doors: the scout's MCP door (`/mcp`, behind a
bearer token), the browser door (`/mcp-browser`, behind its own token) and the owner's action door (`/mcp-owner`,
loopback only). `app.jobsearch.page` and `app.jobsearch.engine` are the two programs. The
JSON the React screens call (`/api/v1/...`) lives in the route modules, one for each part of the screens:

- `agents.routes` (who is here, the roster, Activity), `agents.how_routes` and `agents.edit_routes` (How they work)
- `inbox.routes` (the Inbox, the task list, notifications)
- `role_routes` (the Pipeline and a role), `packet_routes` (packets and the export), `employer_routes`, `mission_routes`
- `profile_routes`, `check_routes` and `parse_routes` (the owner's profile and what is done with it), `site_routes`
- `gap_routes` (Find Gaps, on My Resume)
- `agent_ready_routes` (`GET /agent/ready`, the page's own route: whether an agent can take work)
- `runner_routes` (what the scout's runner says and asks)

The page is served by the app itself (`app.web`, from the folder `npm run build:package` fills, with its own headers and
cache rules).
"""

import asyncio
import contextlib
import logging
import os
import sys
from collections.abc import AsyncIterator, Callable
from typing import NoReturn

from fastapi import FastAPI

from app import db, instance_lock, program, stopping, turn_log, web
from app.jobsearch.browser import code as browser_code
from app.jobsearch.browser import tabs as tab_register
from app.jobsearch.factory import build_app, session_managers
from app.jobsearch.loops import schedule, startup
from app.jobsearch.missions import asked as mission_asks  # noqa: F401  (registers the ask "mission.run")
from app.jobsearch.missions import boards_night
from app.jobsearch.owner_actions import runner as owner_actions_runner
from app.jobsearch.pipeline import live, live_sink
from app.jobsearch.pool import asked as pool_asks  # noqa: F401  (registers the asks "pool.run_all" and "pool.run_kind")
from app.jobsearch.work import bell, door
from app.jobsearch.work.queue import loop as queue_loop
from app.jobsearch.work.queue import topics as queue_topics
from app.settings import settings
from app.sites import inbox_asks


def lifespan_for(role: str) -> Callable[[FastAPI], contextlib.AbstractAsyncContextManager[None]]:
    """The start and stop of the program `role` runs: `engine`, or `all` (the whole app in one process, the seam)."""

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        """One app server per database (`app.instance_lock`): the lock is taken before anything else, so a second copy
        on this database refuses here, with the database unopened, and holds it until this one ends.

        A request to stop ends the page's live-updates streams as it arrives (`app.stopping`): the server waits for
        every open response before it runs the shutdown below, so a stream left open would keep that shutdown from
        ever coming."""
        try:
            with instance_lock.hold(db.database_url()), stopping.heard(lambda: live.broker.close()):
                async with _running(role):
                    yield
        except instance_lock.AlreadyRunning as refused:
            _stop_refused(refused)

    return lifespan


lifespan = lifespan_for("all")


def _stop_refused(refused: instance_lock.AlreadyRunning) -> NoReturn:
    """A refused copy says why once, plainly, and ends non-zero. Raising out of the lifespan would make the server print
    the same text again as the last line of a traceback under 'Application startup failed', so the process ends here;
    nothing is open to close (the lock is taken before the database, and the system frees it)."""
    print(refused, file=sys.stderr, flush=True)
    os._exit(1)


def _start_queue(role: str) -> asyncio.Task[None] | None:
    """The work queue's dispatcher as a task, or None. With the schedule on it takes every topic. With the schedule held
    (`stack.py start --hold`) the engine still answers the page's asks, because a held instance must re-gate after a
    save and answer a question like any other: it takes the topic `ask` only. The whole app in one process, held, runs
    asks on the spot (`engine_calls.ask`), so it starts none."""
    if settings.run_schedule:
        return asyncio.create_task(queue_loop.run())
    if role == "engine":
        return asyncio.create_task(queue_loop.run(only=frozenset({queue_topics.ENGINE_ASK})))
    return None


@contextlib.asynccontextmanager
async def _running(role: str = "all") -> AsyncIterator[None]:
    """The start and stop of the program `role` runs: the engine, or the whole app (`all`)."""
    if role not in ("all", "engine"):
        raise ValueError(f"main._running starts the engine or the whole app, not {role!r}")
    startup.tune_gc_threshold()
    program.start(role)
    startup.configure_logging()
    logging.getLogger(__name__).info(door.start_line(role))
    settings.check_timezone()
    if role != "engine":  # the engine serves no page
        web.log_page()
    db.configure()
    turn_log.install_from_environment()
    await db.init_schema()
    # A site project's questions to the owner become Inbox tasks, and their answers go back to the round (step 5).
    inbox_asks.install()
    with db.background():  # the start's repairs: the background limits (`db.background`)
        async with db.session_factory()() as session:
            await startup.run_startup_fixups(session, settings.owner)
            await session.commit()
    # The lookups the work asks for (the owner's roles index, the classifier) are built now, on readers, before any
    # door, loop or worker starts: the first pass of the night finds them kept and holds no turn while it builds them.
    # Anything that makes their calculation cheaper (the helper pool) starts above this line.
    with db.background():
        await startup.build_lookups(settings.owner)
    async with contextlib.AsyncExitStack() as doors:
        if role == "engine":  # the page's bells (`work/bell.py`); the seam and `resumebot serve` hear none
            listening = await bell.start()
            if listening is not None:
                doors.push_async_callback(listening.close)
        for manager in session_managers(role):  # the doors this program mounts (`factory.MOUNTS`), in their order
            await doors.enter_async_context(manager.run())
        with db.background():  # the loops below are the app's own background work; their tasks inherit it
            task = asyncio.create_task(schedule.schedule_loop()) if settings.run_schedule else None
            # The work queue's dispatcher: the cheap filter, the making of roles, the read of descriptions, the small
            # housekeeping parts, the pipeline's turn itself, the pool's pull, the browser's queue and the runner
            # dispatcher's pass run from to-dos and two ticks.
            dispatching = _start_queue(role)
            # The app's own code tries the browser's read and board-source items before any session (browser/code.py).
            code_tasks = await browser_code.start(settings.owner) if settings.run_schedule else []
            # The tab register is what stays open: every other page of the app's own profile is closed (PRD 52.3).
            if settings.run_schedule:
                code_tasks.append(asyncio.create_task(tab_register.reconcile_at_app_start(settings.owner)))
        if role == "engine":  # the whole app in one process has no separate page to tell
            # Notices sent while this engine was down are gone; one resync makes every open browser read again.
            live_sink.tell_resync(settings.owner)
        try:
            yield
        finally:
            browser_code.stop(settings.owner)
            for t in [*code_tasks, *(t for t in (task, dispatching) if t)]:
                t.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await t
            await boards_night.stop_all()
            await owner_actions_runner.stop_all()


# The whole app in one process: the test seam (`tests/conftest.py` and most tests build from this `app`) and what
# `resumebot serve` runs. The instance runs `app.jobsearch.page` and `app.jobsearch.engine`: two programs, one database.
app = build_app("all", lifespan)
