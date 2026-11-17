"""The page program: `uvicorn app.jobsearch.page:app`, on the owner's port.

It serves the built page, every read and every owner click, and the live stream; it runs no schedule and no work, takes
no instance lock (the engine holds it) and starts no loop: the one thing it starts is the notice receiver (a datagram
socket and the live hub, app/notice.py and pipeline/live_hub.py), which wakes only when the engine tells it something
and is bound only once the schema is current (`Gate`: the page waits for the engine's migration, not for the engine,
and never migrates).
It mounts the owner's action door (`/mcp-owner`) and runs that door's session manager.
Which routes it serves is `ROUTERS` below, and which doors it mounts is `DOORS` (the engine's are in engine.py).
Importing it loads none of the engine's loops, route modules or doors: `factory` imports a router only for the program
that lists it. Its start loads one engine-side module, `work.queue.hook` (the queue's session listeners, imported by
`db.configure`); that is no loop, so it is accepted."""

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable

from fastapi import FastAPI
from starlette.types import ASGIApp, Receive, Scope, Send

from app import db, log_setup, program, stopping, web
from app.jobsearch.factory import build_app, session_managers
from app.jobsearch.pipeline import live, live_hub
from app.jobsearch.work import door, engine_calls
from app.settings import APP_NAME, VERSION, settings

log = logging.getLogger(__name__)

#: The routers the page puts on its app ("package.module:attribute"), in the order they are tried (a literal path
#: before a pattern that would take it). `factory.build_app` imports each only for the program that lists it, so the
#: page loads no router of the engine's. Every route not in `engine.ROUTERS` is the page's. A router is in one list only
#: (the router-lists test in `tests/split`).
ROUTERS: tuple[str, ...] = (
    "app.agents.routes:router",
    "app.agents.how_routes:router",
    "app.agents.edit_routes:router",
    "app.inbox.routes:router",
    "app.jobsearch.jobs.check_routes:router",
    "app.jobsearch.routes.mail:router",
    "app.jobsearch.gaps.routes:router",
    "app.jobsearch.jobs.parse_routes:router",
    "app.jobsearch.routes.profile:router",
    "app.jobsearch.routes.site:router",
    "app.jobsearch.routes.profile:documents_router",
    "app.jobsearch.routes.role:router",
    "app.jobsearch.interviews.routes:router",
    "app.jobsearch.missions.routes:router",
    "app.jobsearch.employers.routes:router",
    "app.jobsearch.writing.routes:router",
    "app.jobsearch.routes.agent_ready:router",
    "app.jobsearch.pool.routes:router",
)

#: The doors the page mounts (paths in `factory.MOUNTS`): the owner's action door. The scout's and the browser's doors
#: are the engine's (`engine.DOORS`).
DOORS: tuple[str, ...] = ("/mcp-owner",)


class Gate:
    """Whether the database is at the code's newest migration, so the page may serve. The engine migrates; the page only
    looks (`db.schema_is_current`), never migrates and never runs a fix-up. Closed, `ensure` looks again each time it
    is asked: nothing waits on a timer. The first look that finds the schema current runs `on_open` (the page binds the
    notice receiver there, after the schema is current) and opens the gate until `reset`."""

    def __init__(self) -> None:
        self.reset()

    def reset(self, on_open: Callable[[], Awaitable[None]] | None = None) -> None:
        """Close the gate, forget the receiver, and say what to run when it opens."""
        self.is_open = False
        self.on_open = on_open
        self.listening: live_hub.Listening | None = None
        self._lock = asyncio.Lock()

    async def ensure(self) -> bool:
        """Whether the gate is open, looking at the database if it is not."""
        if self.is_open:
            return True
        async with self._lock:
            if self.is_open:
                return True
            if not await db.schema_is_current():
                return False
            if self.on_open is not None:
                await self.on_open()
            self.is_open = True
            return True


gate = Gate()


class SchemaGate:
    """ASGI middleware: while the gate is closed, `/api/*` is answered 503 "starting" (Retry-After 1) and `/health`
    says `status: starting`, 503. `/health` never asks the engine. If the database itself does not answer, `/health`
    goes on to the ordinary answer, which says so. Every other path (the page's own files) passes."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        watched = scope["type"] == "http" and (path == "/health" or path == "/api" or path.startswith("/api/"))
        if not watched or await gate.ensure():
            await self.app(scope, receive, send)
            return
        if path == "/health":
            if not await db.ping():
                await self.app(scope, receive, send)
                return
            body = {"app": APP_NAME, "status": "starting", "version": VERSION, "db": "ok"}
        else:
            body = {"detail": "starting"}
        raw = json.dumps(body).encode()
        await send({"type": "http.response.start", "status": 503, "headers": [
            (b"content-type", b"application/json"), (b"content-length", str(len(raw)).encode()),
            (b"cache-control", b"no-store"), (b"retry-after", b"1")]})
        await send({"type": "http.response.body", "body": raw})


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """The page's start: it says which program it is, sets up the log, checks the zone, says where the page is served
    from and opens the database."""
    program.start("page")
    engine_calls.install(engine_calls.Remote())  # the engine is another program: its work is asked for by bell
    log_setup.configure_logging()
    log.info(door.start_line("page"))
    settings.check_timezone()
    web.log_page()
    db.configure()

    async def listen() -> None:
        gate.listening = await live_hub.listen()

    # A stop request ends the live streams as it arrives (`app.stopping`): the server waits for every open response
    # before it runs the shutdown below, so a stream left open would keep the page from ever stopping.
    with stopping.heard(lambda: live.broker.close()):
        gate.reset(on_open=listen)
        await gate.ensure()  # a current schema opens the gate here, so the receiver is bound before the page serves
        try:
            async with contextlib.AsyncExitStack() as doors:
                for manager in session_managers("page"):  # the doors this program mounts (`factory.MOUNTS`)
                    await doors.enter_async_context(manager.run())
                yield
        finally:
            if gate.listening is not None:
                await gate.listening.stop()
            gate.reset()  # the next start looks at the schema again
            engine_calls.install(None)  # whatever runs next in this process gets the local door


app = build_app("page", lifespan)
app.add_middleware(SchemaGate)  # outermost: a closed gate answers before the page's own layers
