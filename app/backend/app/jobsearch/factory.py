"""`build_app(role)`: the FastAPI app of one program.

Roles: `page`, `engine`, and `all`, the whole app in one process, kept as the in-code test seam (`jobsearch.main.app`,
which `tests/conftest.py` builds hundreds of tests from) and for `resumebot serve`. This module imports only what every
program needs, so `app.jobsearch.page` does not load the engine: route modules and doors are imported by the roles
that serve them (each program's `ROUTERS` and `DOORS`, `MOUNTS`)."""

import contextlib
import functools
import importlib
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp

from app import db, errors, notice, program, web
from app.http_guard import HttpGuard
from app.settings import APP_NAME, VERSION

#: The doors mounted on the app, in the order they are mounted: (path, module, the function that builds the door's ASGI
#: app, the module's MCP server). `page.DOORS` and `engine.DOORS` say which program serves each.
MOUNTS = (
    ("/mcp", "app.jobsearch.doors.scout", "build_streamable_http_app", "mcp"),
    ("/mcp-browser", "app.jobsearch.browser.mcp", "build_app", "mcp"),
    ("/mcp-owner", "app.jobsearch.owner_actions.mcp", "build_app", "mcp"),
)


@functools.cache
def _door(module: str, build: str) -> ASGIApp:
    """A door's ASGI app, built once in this process. Building it again makes the MCP server a new session manager, and
    a door answers only while the manager it was built with is running: every app of the process mounts this one."""
    return getattr(importlib.import_module(module), build)()


def _program_module(role: str) -> Any:
    """The module of the program `role` (`page` or `engine`), which holds that program's `ROUTERS` and `DOORS`. It is
    imported when asked, so a program loads its own module and never the other's (role `all` asks for both)."""
    return importlib.import_module({"page": "app.jobsearch.page", "engine": "app.jobsearch.engine"}[role])


def routers_of(role: str) -> tuple[str, ...]:
    """The routers the app of `role` puts on, in order: its program's `ROUTERS`; role `all` (the test seam) the page's
    and then the engine's."""
    if role == "all":
        return routers_of("page") + routers_of("engine")
    return tuple(_program_module(role).ROUTERS)


def serves_mount(path: str, role: str) -> bool:
    """Whether `role` mounts the door at `path`: role `all` mounts every door, a program the ones in its `DOORS`."""
    return role == "all" or path in _program_module(role).DOORS


def session_managers(role: str) -> list[Any]:
    """The session managers of the doors `role` mounts, for its lifespan to run (a door answers only while its manager
    runs)."""
    managers = []
    for path, module, build, server in MOUNTS:
        if serves_mount(path, role):
            _door(module, build)  # the manager exists once the door is built
            managers.append(getattr(importlib.import_module(module), server).session_manager)
    return managers


def build_app(
        role: str, lifespan: Callable[[FastAPI], contextlib.AbstractAsyncContextManager[None]] | None = None,
) -> FastAPI:
    """The app of the program `role` (`page`, `engine` or `all`), with the start-up and stop `lifespan` its program
    runs. The page's layer (`app.web`) is on every app but the engine's."""
    if role not in program.ROLES:
        raise ValueError(f"unknown program {role!r}; one of {', '.join(program.ROLES)}")
    # No /docs, /redoc or /openapi.json: with the page served by the app those addresses
    # are the page's. (`app.openapi()` still describes the routes: tests/core/test_api_paths.py reads it.)
    app = FastAPI(
        title=APP_NAME, version=VERSION, lifespan=lifespan, strict_content_type=True,
        docs_url=None, redoc_url=None, openapi_url=None)
    errors.install(app)
    # Every call (the MCP doors, which are mounted below, included) passes the Host and Origin checks first.
    app.add_middleware(HttpGuard)
    if role != "engine":
        # The page, the security headers and the cache rules (app/web.py). Added after the guard, so the headers are
        # outside it and its refusals carry them too, and the guard still stands in front of every route and the page.
        web.install(app)

    # The order is the order the routes are tried in: a literal path before a pattern that would take it.
    for spec in routers_of(role):
        module, attribute = spec.split(":")
        app.include_router(getattr(importlib.import_module(module), attribute))

    @app.get("/health")
    async def health() -> JSONResponse:
        """Whether the app and its database answer. It asks on the lifeline pool (`db.ping`), never the shared one, so
        an app that is only busy says ok, and the stack's supervisor restarts only one that truly does not answer."""
        if await db.ping():
            body = {"app": APP_NAME, "status": "ok", "version": VERSION, "db": "ok"}
            if notice.BIND_PROBLEM:
                # Status stays ok: a supervisor that restarted a page for a port it cannot take would do it forever.
                body["notice"] = notice.BIND_PROBLEM
            return JSONResponse(body)
        return JSONResponse(
            {"app": APP_NAME, "status": "degraded", "version": VERSION, "db": "unreachable"}, status_code=503)

    # The scout's door (/mcp), the browser door (/mcp-browser, docs/PRD.md 30.8, behind its own token) and the owner's
    # action door (/mcp-owner, docs/PRD.md path 50, a third token, loopback only). Each answers on its program's own
    # port, behind its token and the Host and Origin guard.
    for path, module, build, _ in MOUNTS:
        if serves_mount(path, role):
            app.mount(path, _door(module, build))
    return app
