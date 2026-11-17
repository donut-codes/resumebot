"""The owner's first guard (2026-11-10): no read of the page program takes the write lock.

The page program serves every read. A read that takes the writer's turn (`BEGIN IMMEDIATE`, `db._guard`) waits behind
the engine's saves and makes the board slow, so no read may take it. Two checks over every read of the page app
(`app.jobsearch.page.app`: every GET, every HEAD, and the POSTs named in `READ_POSTS`, which only read):

- the dependency walk: no read has `db.get_session` (the writer's session, `db.Session`) among its dependencies;
- the recorded turn: the read is called on a temporary database with `db.turn_watchers` and `db.turn_ends` listening,
  and not one turn was asked for or taken. This one catches what the walk cannot see: a route that opens
  `db.session_factory()()` itself (`GET /api/v1/live` for its `hello`, `GET /api/v1/apply/go` for its stored run).

Both tests read the table from the app, so a route that joins the page is checked with no edit here. What a lane that
flips a read may have to add is named at the top of this file: `PATH_VALUES`, `REQUESTS`, `STREAMS`,
`MOUNTS_TESTED_ELSEWHERE`. Never loosen a check to get a route through: make the route read on a `ReadSession`.

The page app includes its routers (`app.include_router`). The FastAPI this app is locked to (0.143) keeps an included
router as ONE entry of `app.routes` that points at the router (`original_router`), not as its routes, so `routes_of`
goes into those entries. Without that, the guard would fail on the first route that joins the page.
"""

import ast
import asyncio
import contextlib
import re
import uuid
from pathlib import Path

import pytest
from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import StreamingResponse
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from starlette.routing import Mount

from app import db
from app.jobsearch import stages
from app.jobsearch.models import Opportunity
from tests.split import forbidden
from tests.support import NOON, OWNER

#: POST routes that only read (they take no write); they are called like a GET. A POST not named here is a write.
READ_POSTS = frozenset({
    "POST /api/v1/harness/processes/{key}/validate",
    "POST /api/v1/profile/roles/from-role/{subject_id}",
    "POST /api/v1/mail-login/test",
})
#: What a path parameter is called with. A name not here is called with `NO_SUCH` (a read that finds nothing answers
#: 404, which is fine). A lane whose read needs a real row to reach its branch makes the row in the test body, before
#: the loop (ids and builders: tests/builders.py), and puts its id here.
PATH_VALUES: dict[str, str] = {
    # An id no row has: the read answers 404, which is an answer. A number: version 1 of a file or a process named
    # NO_SUCH, which is a 404 too.
    "connection_id": "00000000-0000-4000-8000-000000000001",
    "task_id": "00000000-0000-4000-8000-000000000002",
    "instance_id": "00000000-0000-4000-8000-000000000003",
    "employer_id": "00000000-0000-4000-8000-000000000004",
    "version": "1",
    "number": "1",
    "site_id": "00000000-0000-0000-0000-000000000000",
    "packet_id": "00000000-0000-0000-0000-000000000000",
    "mission_id": "00000000-0000-0000-0000-000000000000",
}
NO_SUCH = "none-such"
#: The request a route is called with, beyond its path: httpx's `params` and `json`. Keyed "METHOD /path".
REQUESTS: dict[str, dict] = {
    "POST /api/v1/harness/processes/{key}/validate": {"json": {"xml": "<process/>"}},
    "GET /api/v1/employers/{employer_id}/merge": {"params": {"into": "00000000-0000-4000-8000-000000000005"}},
    "GET /api/v1/employers/{employer_id}/split": {
        "params": {"roles": "00000000-0000-4000-8000-000000000006", "name": "Made Up Works"}},
}
#: GET routes that never end (server-sent events): read up to the first message, then the client goes away.
STREAMS = frozenset({"/api/v1/live"})
#: Mounted doors the page serves (not routes of the router) and the test that covers each. A lane that mounts a door
#: on the page adds its path here, with the name of that test in a comment.
#: "/mcp-owner": tests/jobsearch/test_owner_door_page.py (every read action of the door takes no write turn).
MOUNTS_TESTED_ELSEWHERE: frozenset[str] = frozenset({"/mcp-owner"})


def label(method: str, route: APIRoute) -> str:
    return f"{method} {route.path}"


def routes_of(routes) -> list[APIRoute]:
    """Every API route under `routes` (a list of `app.routes`), going into the routers the app included. Fails on a
    mounted door not listed in MOUNTS_TESTED_ELSEWHERE, on a router included with a prefix or with dependencies of its
    own (the walk below would not see them), and on any other kind of route."""
    found: list[APIRoute] = []
    for route in routes:
        original = getattr(route, "original_router", None)
        if original is not None:
            assert not route.include_context.prefix and not route.include_context.dependencies, (
                "the page app includes a router with a prefix or with dependencies of its own: this guard reads the "
                "path and the dependencies from the route, so include it plain")
            found.extend(routes_of(original.routes))
        elif isinstance(route, APIRoute):
            found.append(route)
        elif isinstance(route, Mount):
            assert route.path in MOUNTS_TESTED_ELSEWHERE, (
                f"the page app mounts {route.path!r}, which this guard cannot walk: add it to MOUNTS_TESTED_ELSEWHERE "
                f"with the name of the test that proves its reads take no write turn")
        else:
            pytest.fail(f"the page app has a route this guard does not know how to read: {route!r}")
    return found


def page_reads(app: FastAPI) -> list[tuple[str, APIRoute]]:
    """(method, route) for every read the app answers: each GET, each HEAD and each POST named in READ_POSTS."""
    found: list[tuple[str, APIRoute]] = []
    for route in routes_of(app.routes):
        for method in sorted(route.methods):
            if method in ("GET", "HEAD") or f"{method} {route.path}" in READ_POSTS:
                found.append((method, route))
    return found


def needs(dependant, call) -> bool:
    """Whether `call` is among the dependencies of `dependant`, at any depth."""
    return any(d.call is call or needs(d, call) for d in dependant.dependencies)


def url_of(route: APIRoute) -> str:
    return re.sub(r"\{(\w+)(?::\w+)?\}", lambda m: PATH_VALUES.get(m.group(1), NO_SUCH), route.path)


class Turns:
    """The writer's turns asked for (`waits`) and let go (`ends`) while it was listening."""

    def __init__(self) -> None:
        self.waits: list[float] = []
        self.ends: list[tuple[float, float | None]] = []


@contextlib.contextmanager
def turns_recorded():
    """Listen to `db.turn_watchers` and `db.turn_ends`. The listeners only append: a watcher is called after the
    in-program lock is taken (db.py `begin`), so one that raised would leave the lock held."""
    turns = Turns()

    def waited(seconds: float) -> None:
        turns.waits.append(seconds)

    def ended(waited_for: float, held: float | None) -> None:
        turns.ends.append((waited_for, held))

    db.turn_watchers.append(waited)
    db.turn_ends.append(ended)
    try:
        yield turns
    finally:
        db.turn_watchers.remove(waited)
        db.turn_ends.remove(ended)


async def stream_status(app: FastAPI, url: str) -> int:
    """GET `url` straight on the ASGI app (httpx waits for a stream to end), take the first message, say the client
    has gone, wait for the app to finish, and return the status it started with."""
    gone = asyncio.Event()
    sent: list[dict] = []
    asked = False

    async def receive() -> dict:
        nonlocal asked
        if not asked:
            asked = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await gone.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        sent.append(message)
        if message["type"] == "http.response.body" and message.get("body"):
            gone.set()

    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET", "scheme": "http",
        "path": url, "raw_path": url.encode(), "query_string": b"", "root_path": "",
        "headers": [(b"host", b"test")], "client": ("127.0.0.1", 50000), "server": ("test", 80)}
    await asyncio.wait_for(app(scope, receive, send), timeout=10)
    return next(m["status"] for m in sent if m["type"] == "http.response.start")


async def call(app: FastAPI, method: str, route: APIRoute) -> int:
    """Call one read of the app as the owner would and return the status."""
    url = url_of(route)
    if method == "GET" and route.path in STREAMS:
        return await stream_status(app, url)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        answer = await http.request(method, url, **REQUESTS.get(label(method, route), {}))
    return answer.status_code


@pytest.fixture
def page_app() -> FastAPI:
    """The page program's app (`app.jobsearch.page.app`). It is called straight over ASGI, without its lifespan, so no
    program is named and no lock is taken by the start."""
    from app.jobsearch import page

    return page.app


def test_no_page_read_has_the_writer_session_among_its_dependencies(page_app):
    reads = page_reads(page_app)
    assert reads, "the page app answers no read at all, not even /health"
    writers = [label(method, route) for method, route in reads if needs(route.dependant, db.get_session)]
    assert not writers, f"reads that depend on the writer's session (make them ReadSession): {writers}"


async def test_no_page_read_takes_the_write_lock_when_called(js, page_app, monkeypatch):
    from app.settings import settings

    monkeypatch.setattr(settings, "owner", OWNER)
    # The role reads are called for a role that is there, so each one reads all it reads (flip-reads-c). A task and a
    # packet get a made-up id of the right shape, unless an earlier lane made a real one: nothing is found, a 404.
    shown_role = Opportunity(owner=OWNER, slug="g1", company="Co g1", title="Role g1", status=stages.READY_TO_LAUNCH,
                             origin="tracker", match_key="co g1|role g1", posted=NOON.date(), found=NOON.date())
    async with db.session_factory()() as session:
        session.add(shown_role)
        await session.commit()
    monkeypatch.setitem(PATH_VALUES, "role_id", str(shown_role.id))
    for name in ("task_id", "packet_id"):
        if name not in PATH_VALUES:
            monkeypatch.setitem(PATH_VALUES, name, str(uuid.UUID(int=0)))
    reads = page_reads(page_app)
    assert reads, "the page app answers no read at all, not even /health"
    took: dict[str, int] = {}
    refused: dict[str, int] = {}
    for method, route in reads:
        with turns_recorded() as turns:
            status = await call(page_app, method, route)
        if turns.waits or turns.ends:
            took[label(method, route)] = len(turns.waits)
        if status >= 500 or status == 422:
            refused[label(method, route)] = status
    assert not took, f"reads that asked for the write turn (a name and how many times): {took}"
    assert not refused, (
        f"reads that did not reach their handler or failed (add the request to REQUESTS or the id to PATH_VALUES): "
        f"{refused}")


# --- the guard is only worth having if it can fail: the same two checks over throwaway routes -----------------


def throwaway_app() -> FastAPI:
    app = FastAPI()

    @app.get("/reads")
    async def reads(session: db.ReadSession) -> dict:
        await session.execute(text("SELECT 1"))
        return {}

    @app.get("/writes")
    async def writes(session: db.Session) -> dict:
        await session.execute(text("SELECT 1"))
        return {}

    async def through(session: db.Session) -> int:
        return 1

    @app.get("/nested")
    async def nested(n: int = Depends(through)) -> dict:
        return {}

    @app.get("/sneaky")
    async def sneaky() -> dict:
        async with db.session_factory()() as session:
            await session.execute(text("SELECT 1"))
        return {}

    @app.get("/stream")
    async def stream(request: Request) -> StreamingResponse:
        async def frames():
            yield "event: hello\n\n"
            while not await request.is_disconnected():
                await asyncio.sleep(0.01)
                yield ": ping\n\n"

        return StreamingResponse(frames(), media_type="text/event-stream")

    return app


def test_the_walk_sees_a_writer_dependency_direct_or_nested_and_not_a_reader():
    routes = {route.path: route for route in throwaway_app().routes if isinstance(route, APIRoute)}
    seen = {path: needs(route.dependant, db.get_session) for path, route in routes.items()}
    assert seen == {"/reads": False, "/writes": True, "/nested": True, "/sneaky": False, "/stream": False}


async def test_the_recorder_sees_a_turn_taken_with_or_without_a_dependency(js):
    app = throwaway_app()
    routes = {route.path: route for route in app.routes if isinstance(route, APIRoute)}
    seen = {}
    for path in ("/reads", "/writes", "/sneaky"):
        with turns_recorded() as turns:
            assert await call(app, "GET", routes[path]) == 200
        seen[path] = bool(turns.waits) and bool(turns.ends)
    assert seen == {"/reads": False, "/writes": True, "/sneaky": True}


def test_a_read_in_an_included_router_is_found_and_judged():
    router = APIRouter()

    @router.get("/inside")
    async def inside(session: db.Session) -> dict:
        return {}

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.include_router(router)
    found = page_reads(app)
    assert [label(method, route) for method, route in found] == ["GET /inside"]
    assert needs(found[0][1].dependant, db.get_session)


async def test_the_stream_helper_takes_the_first_message_and_lets_the_stream_go():
    assert await stream_status(throwaway_app(), "/stream") == 200


def test_a_mounted_door_the_guard_cannot_walk_is_refused_until_it_is_listed():
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/door", FastAPI())
    with pytest.raises(AssertionError, match="/door"):
        page_reads(app)


# --- the page's handlers do not call the engine except through engine_calls ----------------------

#: The engine's calls a page handler must not make itself: module name as the code writes it -> the functions of it.
ENGINE_CALLS: dict[str, frozenset[str]] = {
    "wake": frozenset({"knock"}),
    "wake_ops": frozenset({"send_all"}),
    "dispatcher": frozenset({"dispatch_now"}),
    "schedule": frozenset({"send_knocks"}),
}
#: Modules the scan does not enter: the engine's own (`forbidden.FORBIDDEN`, which the import guard watches) and the one
#: door, `engine_calls`, which is where a page handler is meant to go.
SCAN_STOPS = frozenset(forbidden.FORBIDDEN) | {"app.jobsearch.work.engine_calls"}
#: Page code that is not a route handler but runs on the page: the owner's action door.
EXTRA_ROOTS = ("app.jobsearch.owner_actions.mcp",)


def engine_calls_in(source: str) -> list[str]:
    """The engine calls one module's source makes by name: `wake.knock(...)` and the like, and a
    `from <...>.wake import knock`. A call made through an alias is not seen (the code does not alias them)."""
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                if func.attr in ENGINE_CALLS.get(func.value.id, ()):
                    found.append(f"{func.value.id}.{func.attr}")
        elif isinstance(node, ast.ImportFrom):
            last = (node.module or "").rpartition(".")[2]
            found.extend(f"from {node.module} import {alias.name}"
                         for alias in node.names if alias.name in ENGINE_CALLS.get(last, ()))
    return sorted(set(found))


def app_modules_imported(module: str, is_package: bool, source: str) -> set[str]:
    """The `app.*` modules (and names that may be modules) one module's source imports, anywhere in it, a relative
    import resolved against `module`."""
    package = module if is_package else module.rpartition(".")[0]
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                parts = package.split(".")[: len(package.split(".")) - (node.level - 1)]
                base = ".".join([*parts, *([node.module] if node.module else [])])
            found.add(base)
            found.update(f"{base}.{alias.name}" for alias in node.names)
    return {name for name in found if name == "app" or name.startswith("app.")}


def file_of(module: str) -> tuple[Path, bool] | None:
    """(the module's file, whether it is a package) or None when `module` is not a module (it is a name in one)."""
    stem = Path(db.BACKEND_DIR).joinpath(*module.split("."))
    if stem.with_suffix(".py").is_file():
        return stem.with_suffix(".py"), False
    if (stem / "__init__.py").is_file():
        return stem / "__init__.py", True
    return None


def engine_calls_reachable_from(roots) -> dict[str, list[str]]:
    """Walk the imports from `roots` (every import statement, function-local ones too), never entering `SCAN_STOPS`,
    and return {module: the engine calls it makes} for each module that makes any."""
    seen: set[str] = set()
    found: dict[str, list[str]] = {}
    queue = list(roots)
    while queue:
        module = queue.pop()
        if module in seen or module in SCAN_STOPS:
            continue
        seen.add(module)
        located = file_of(module)
        if located is None:
            continue
        path, is_package = located
        source = path.read_text(encoding="utf-8")
        hits = engine_calls_in(source)
        if hits:
            found[module] = hits
        queue.extend(app_modules_imported(module, is_package, source))
    return found


def test_no_handler_of_the_page_calls_the_engine_except_through_engine_calls(page_app):
    roots = {route.endpoint.__module__ for route in routes_of(page_app.routes)} | set(EXTRA_ROOTS)
    assert len(roots) > 10, f"the scan found too few route modules to be looking at the page: {sorted(roots)}"
    found = engine_calls_reachable_from(roots)
    assert not found, (
        "page code that calls the engine itself (tell the engine through app.jobsearch.work.engine_calls: knock, "
        f"dispatch_now, knocks, send_wakes): {found}")


def test_the_scan_sees_a_direct_call_and_a_from_import_and_lets_the_door_through():
    direct = "from app.jobsearch.work import wake\n\nasync def go():\n    await wake.knock('a')\n"
    assert engine_calls_in(direct) == ["wake.knock"]
    imported = "from app.jobsearch.work.wake import knock\n"
    assert engine_calls_in(imported) == ["from app.jobsearch.work.wake import knock"]
    every = ("async def go():\n    await schedule.send_knocks([])\n    await dispatcher.dispatch_now()\n"
             "    await wake_ops.send_all([])\n")
    assert engine_calls_in(every) == ["dispatcher.dispatch_now", "schedule.send_knocks", "wake_ops.send_all"]
    door = "from app.jobsearch.work import engine_calls\n\nasync def go():\n    await engine_calls.knock('a')\n"
    assert engine_calls_in(door) == []


def test_the_scan_follows_relative_imports_and_names_only_app_modules():
    got = app_modules_imported("app.a.b", False, "from . import c\nfrom ..d import e\nimport app.f\nimport os\n")
    assert got == {"app.a", "app.a.c", "app.d", "app.d.e", "app.f"}


def test_the_scan_finds_the_calls_in_a_module_that_makes_them():
    # work/bell.py is the engine's own listener, so it makes all four calls; it is not on SCAN_STOPS.
    found = engine_calls_reachable_from(["app.jobsearch.work.bell"])
    assert found["app.jobsearch.work.bell"] == [
        "dispatcher.dispatch_now", "schedule.send_knocks", "wake.knock", "wake_ops.send_all"]


def test_the_scan_does_not_enter_a_stopped_module():
    assert engine_calls_reachable_from(["app.jobsearch.loops.schedule", "app.jobsearch.work.engine_calls"]) == {}
