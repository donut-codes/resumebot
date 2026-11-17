"""The engine program: `uvicorn app.jobsearch.engine:app`, on the page's port plus 100.

It runs the schedule, the work queue, the fetchers and the doors the lanes use: the lane doors (`ROUTERS`, the runner's
routes under `/api/v1/agent/`) and the mounts `/mcp` and `/mcp-browser` (`DOORS`), and it answers its own `/health`.
Every other route is the page's (`page.ROUTERS`). Its start-up is `main.lifespan_for("engine")`: the instance lock,
the schema (the only migration), the start-up fix-ups, the lookups (the roles index and the classifier, built on
readers), then the doors, the schedule, the dispatcher and the workers.
`main` is imported when the engine starts, not when this module is imported: `main` builds the whole app, which asks
this module for its lists, so a top-level import would be circular. That costs memory, not behaviour, and the doors are
built once for the process (`factory._door`). `_start` is the one place that reaches `main`, and a test replaces it."""

import contextlib
from collections.abc import AsyncIterator

from fastapi import FastAPI

from app.jobsearch.factory import build_app
from app.jobsearch.pipeline import live_sink

#: The routers the engine puts on its app ("package.module:attribute"): the lane doors (the runner's hand-out,
#: instructions, context, heartbeat, drain, busy, tabs and reconcile, capabilities and seat).
ROUTERS: tuple[str, ...] = ("app.jobsearch.routes.runner:router",)

#: The doors the engine mounts (paths in `factory.MOUNTS`): the scout's door and the browser door, which the pool's
#: sessions call and whose tools hold the browser's state.
DOORS: tuple[str, ...] = ("/mcp", "/mcp-browser")


def _start(application: FastAPI) -> contextlib.AbstractAsyncContextManager[None]:
    """The engine's start and stop as `main` has them. `main` is imported here and not at the top: see the module
    docstring. `tests/split/test_live_signal.py` replaces this function with a recorder, so it keeps this name."""
    from app.jobsearch import main

    return main.lifespan_for("engine")(application)


@contextlib.asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    """`_start`'s start and stop, with one thing added: while the engine runs, a commit's live items are sent to the
    page as a datagram (`live_sink.WireSink`) and not published here, where no stream is open. The local sink is put
    back at the end, because the tests enter more than one program's start in one process."""
    live_sink.use(live_sink.WireSink())
    try:
        async with _start(application):
            yield
    finally:
        live_sink.use(None)


app = build_app("engine", lifespan)
