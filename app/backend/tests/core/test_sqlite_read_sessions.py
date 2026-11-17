"""Read sessions. A call that never writes takes a reader's session
(`db.ReadSession`, a tool registered `reads=True`): its transactions begin a plain `BEGIN`, take no turn and, in WAL,
never wait for the writer, so a page of the app answers while a long writer holds the file. A write in one raises
(`db.WriteInReader`) in the tests and is logged in the app. The board is such a call: it reads the stored order as it
stands and settles nothing, whatever a write has left to settle (the engine settles it: `work/queue/loop.py`).
"""

import asyncio
import contextlib
import logging
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, text

from app import db
from app.jobsearch import stages
from app.jobsearch.doors import scout as mcp_server
from app.jobsearch.models import Opportunity, QueueDirty
from app.jobsearch.pipeline import stored_order
from app.jobsearch.work.door import Door
from app.timeutil import owner_today
from tests.core.test_sqlite_writer_turn import ADD, COUNT, engine  # noqa: F401  (a fixture, asked for by name)
from tests.support import NOON, OWNER, pin_clock


@contextlib.asynccontextmanager
async def a_writer_holding_the_turn():
    """A transaction that holds the writer's turn until the block ends, in a task of its own (a call of the app that
    asks for the turn in this task while this task held it would be `db.NestedWriter`, not a wait). Any statement of a
    writer's transaction takes the turn at its `BEGIN IMMEDIATE`; the block gets the session, to write in or not."""
    taken, let_go = asyncio.Event(), asyncio.Event()
    holder: list = []

    async def hold() -> None:
        async with db.session_factory()() as writer:
            await writer.execute(text("SELECT 1"))
            holder.append(writer)
            taken.set()
            await let_go.wait()
            await writer.rollback()

    task = asyncio.create_task(hold())
    await taken.wait()
    try:
        yield holder[0]
    finally:
        let_go.set()
        await task


def a_little_app() -> FastAPI:
    app = FastAPI()
    router = APIRouter()

    @router.get("/count")
    async def count(session: db.ReadSession) -> dict:
        return {"notes": (await session.execute(COUNT)).scalar_one()}

    @router.get("/count-after-a-commit")
    async def count_after_a_commit(session: db.ReadSession) -> dict:
        first = (await session.execute(COUNT)).scalar_one()
        await db.release(session)  # a route that ends its transaction between its steps goes on as a reader
        return {"first": first, "second": (await session.execute(COUNT)).scalar_one()}

    @router.get("/writes")
    async def writes(session: db.ReadSession) -> dict:
        await session.execute(ADD, {"body": "from a GET"})
        return {}

    app.include_router(router)
    return app


@pytest.fixture
async def little(engine):  # noqa: F811
    async with AsyncClient(transport=ASGITransport(app=a_little_app()), base_url="http://test") as client:
        yield client


# --- the dependency ---------------------------------------------------------------------------------------------


async def test_a_read_session_answers_while_a_writer_holds_the_turn(little):
    async with a_writer_holding_the_turn() as writer:
        await writer.execute(ADD, {"body": "not yet committed"})
        async with asyncio.timeout(1):
            answered = await little.get("/count")
    assert answered.json() == {"notes": 0}


async def test_a_read_session_is_still_a_reader_after_it_commits(little):
    async with a_writer_holding_the_turn():
        async with asyncio.timeout(1):
            answered = await little.get("/count-after-a-commit")
    assert answered.json() == {"first": 0, "second": 0}


async def test_a_read_session_sees_what_was_committed_before_it_began(little, engine):  # noqa: F811
    async with db.session_factory()() as session:
        await session.execute(ADD, {"body": "kept"})
        await session.commit()
    assert (await little.get("/count")).json() == {"notes": 1}


async def test_a_write_in_a_read_session_raises_and_nothing_is_written(little, engine):  # noqa: F811
    with pytest.raises(db.WriteInReader):
        await little.get("/writes")
    assert db.reader_wrote and "INSERT INTO notes" in db.reader_wrote[0]
    db.reader_wrote.clear()  # said and caught: the suite's check for a write in a reader is for the ones nobody meant
    async with db.session_factory()() as session:
        assert (await session.execute(COUNT)).scalar_one() == 0


async def test_a_write_in_a_reader_outside_the_tests_is_logged_once_for_each_statement(engine, monkeypatch, caplog):  # noqa: F811
    monkeypatch.setattr(db, "strict_readers", False)
    db._reader_logged.clear()
    with caplog.at_level(logging.WARNING, logger="app.db"):
        for _ in range(2):
            async with db.read_session_factory()() as session:
                await session.execute(ADD, {"body": "careless"})
                await session.rollback()
    assert len([r for r in caplog.records if "a write in a reader's transaction" in r.message]) == 1
    assert not db.reader_wrote


async def test_is_reader_says_which_factory_a_session_came_from(engine):  # noqa: F811
    async with db.read_session_factory()() as reading, db.session_factory()() as writing:
        assert db.is_reader(reading) and not db.is_reader(writing)


# --- the routes -------------------------------------------------------------------------------------------------

#: The GET routes that still take the writer's session, each with what it writes. Every other GET route that has a
#: session takes `ReadSession`.
WRITING_GETS = {
    "/api/v1/agent/work": "hands out the next piece of work: a claim",
}


def _get_routes():
    """Each GET route of the app with its full path (the routers are included lazily: their own routes, and the prefix
    of the include)."""
    from app.jobsearch.main import app

    for included in app.routes:
        router = getattr(included, "original_router", None)
        if router is None:
            continue
        prefix = getattr(included.include_context, "prefix", "") or ""
        for route in router.routes:
            if isinstance(route, APIRoute) and "GET" in route.methods:
                yield prefix + route.path, route


def _sessions_of(route: APIRoute) -> set:
    found, todo = set(), list(route.dependant.dependencies)
    while todo:
        one = todo.pop()
        todo.extend(one.dependencies)
        if one.call in (db.get_session, db.get_read_session, db.get_lifeline_session):
            found.add(one.call)
    return found


def test_every_get_route_that_has_a_session_reads_without_the_turn_or_says_what_it_writes():
    writers, readers = set(), set()
    for path, route in _get_routes():
        sessions = _sessions_of(route)
        if db.get_session in sessions:
            writers.add(path)
        if db.get_read_session in sessions:
            readers.add(path)
    assert writers == set(WRITING_GETS), (
        f"a GET route takes the writer's session without a reason, or a reason is for a route that no longer does: "
        f"{sorted(writers ^ set(WRITING_GETS))}")
    assert {"/api/v1/board", "/api/v1/roles/{role_id}", "/api/v1/roles/{role_id}/row", "/api/v1/profile",
            "/api/v1/favorites", "/api/v1/search"} <= readers


# --- the pages of the app, with a long writer holding the turn --------------------------------------------------


async def _roles() -> dict[str, uuid.UUID]:
    """A few roles in lanes of the board, committed, with the stored order settled (nothing for the board to settle)."""
    day = owner_today()
    async with db.session_factory()() as session:
        roles = [Opportunity(owner=OWNER, slug=f"read-{status}", company=f"Co {status}", title=f"Role {status}",
                             status=status, origin="scout", match_key=f"co {status}|role {status}", posted=day,
                             found=day)
                 for status in (stages.FIRST_IMPRESSION, stages.READY_TO_LAUNCH, stages.APPLIED)]
        session.add_all(roles)
        await session.flush()
        await stored_order.settle(session, OWNER)
        await session.commit()
        return {r.status: r.id for r in roles}


async def test_the_board_the_role_page_and_the_profile_answer_while_a_writer_holds_the_turn(client):
    ids = await _roles()
    async with a_writer_holding_the_turn():
        async with asyncio.timeout(2):
            board = await client.get("/api/v1/board")
            page = await client.get(f"/api/v1/roles/{ids[stages.READY_TO_LAUNCH]}")
            row = await client.get(f"/api/v1/roles/{ids[stages.READY_TO_LAUNCH]}/row")
            favorites = await client.get("/api/v1/favorites")
            profile = await client.get("/api/v1/profile")
            found = await client.get("/api/v1/search", params={"q": "Role"})
    assert [r.status_code for r in (board, page, row, favorites, profile, found)] == [200] * 6
    cards = {c["key"]: c["count"] for c in board.json()["columns"]}
    assert cards[stages.READY_TO_LAUNCH] == 1 and cards[stages.APPLIED] == 1


async def test_the_board_with_nothing_to_settle_asks_for_no_turn(client):
    await _roles()
    asked: list[float] = []
    db.turn_watchers.append(asked.append)
    try:
        board = await client.get("/api/v1/board")
    finally:
        db.turn_watchers.remove(asked.append)
    assert board.status_code == 200 and asked == []


async def test_the_board_with_something_to_settle_asks_for_no_turn_and_leaves_it_to_the_engine(client, monkeypatch):
    pin_clock(monkeypatch, NOON)
    client.event_hooks = {"request": [], "response": []}  # no engine beside the page: only the read itself runs
    await _roles()
    async with db.session_factory()() as session:
        session.add(Opportunity(owner=OWNER, slug="late", company="Co late", title="Role late",
                                status=stages.READY_TO_LAUNCH, origin="scout", match_key="co late|role late",
                                posted=owner_today(), found=owner_today()))
        await session.commit()
        lines = await session.scalar(select(func.count()).select_from(QueueDirty))
        assert lines > 0  # the trigger's line for it
    asked: list[float] = []
    db.turn_watchers.append(asked.append)
    try:
        board = await client.get("/api/v1/board", params={"column": stages.READY_TO_LAUNCH})
    finally:
        db.turn_watchers.remove(asked.append)
    assert asked == [], "the read asked for no turn"
    async with db.read_session_factory()() as session:
        assert await session.scalar(select(func.count()).select_from(QueueDirty)) == lines  # left for the engine
    assert board.json()["columns"][0]["count"] == 2


async def test_the_board_with_something_to_settle_answers_while_a_writer_holds_the_turn(client, monkeypatch):
    pin_clock(monkeypatch, NOON)
    client.event_hooks = {"request": [], "response": []}  # no engine beside the page: only the read itself runs
    await _roles()
    async with db.session_factory()() as session:
        session.add(Opportunity(owner=OWNER, slug="late", company="Co late", title="Role late",
                                status=stages.READY_TO_LAUNCH, origin="scout", match_key="co late|role late",
                                posted=owner_today(), found=owner_today()))
        await session.commit()
    async with a_writer_holding_the_turn():
        async with asyncio.timeout(2):
            board = await client.get("/api/v1/board", params={"column": stages.READY_TO_LAUNCH})
    assert board.status_code == 200 and board.json()["columns"][0]["count"] == 2
    async with db.read_session_factory()() as session:
        assert await session.scalar(select(func.count()).select_from(QueueDirty)) > 0  # left for the engine


# --- the scout's door -------------------------------------------------------------------------------------------


def a_door() -> Door:
    """A door of its own with no check-in step (`before` is a write of its own, which waits for its turn like any)."""
    return Door("read-test", "", log=logging.getLogger("read-test"), label="read-test", token_ok=lambda header: True,
                token_file=lambda: Path("unused"), no_token=("no token", "send one"), if_it_fails="try again")


async def test_a_tool_registered_as_a_read_answers_while_a_writer_holds_the_turn(js):
    async def reading(session, owner):
        counted = await session.execute(select(func.count()).select_from(Opportunity))
        return {"ok": True, "count": counted.scalar_one()}

    async with a_writer_holding_the_turn():
        async with asyncio.timeout(2):
            answered = await a_door().call(SimpleNamespace(headers={}), "jobs.read", reading, reads=True)
    assert answered == {"ok": True, "count": 0}


async def test_a_tool_registered_as_a_read_that_writes_is_refused(js):
    async def careless(session, owner):
        await session.execute(text("UPDATE js_opportunities SET title = title"))
        return {"ok": True}

    answered = await a_door().call(SimpleNamespace(headers={}), "jobs.careless", careless, reads=True)
    assert answered["ok"] is False
    assert db.reader_wrote and "UPDATE js_opportunities" in db.reader_wrote[0]
    db.reader_wrote.clear()


def test_the_tools_that_only_read_are_registered_as_reads():
    from app.jobsearch.browser import mcp as browser_mcp
    from app.jobsearch.owner_actions import mcp as owner_mcp

    assert mcp_server.door.reads == {"item.lookup", "review.read", "lesson.list", "doc.read"}
    assert owner_mcp.door.reads == {"actions.list", "act.read"}
    assert browser_mcp.door.reads == set()


# --- the board that is handed a reader settles nothing ------------------------------------------------------------


async def test_the_board_handed_a_reader_settles_nothing_and_takes_no_turn(js, monkeypatch):
    from app.jobsearch.pipeline import row as role_row

    pin_clock(monkeypatch, NOON)
    await _roles()
    async with db.session_factory()() as session:
        session.add(Opportunity(owner=OWNER, slug="late", company="Co late", title="Role late",
                                status=stages.APPLIED, origin="scout", match_key="co late|role late",
                                posted=owner_today(), found=owner_today()))
        await session.commit()
    asked = SimpleNamespace(count=0)

    def counted(_waited: float) -> None:
        asked.count += 1

    db.turn_watchers.append(counted)
    try:
        async with db.read_session_factory()() as session:
            columns = await role_row.columns(session, OWNER, stages.APPLIED)
            lines = await session.scalar(select(func.count()).select_from(QueueDirty))
    finally:
        db.turn_watchers.remove(counted)
    assert columns[0]["count"] == 2 and asked.count == 0 and lines > 0  # something to settle, and still no turn


async def test_the_agents_page_answers_while_a_writer_holds_the_turn_once_the_roster_is_there(client):
    from app.agents import roster

    async with db.session_factory()() as session:  # the roster as the engine's start leaves it
        await roster.ensure_roster(session, OWNER)
        await session.commit()
    async with a_writer_holding_the_turn():
        async with asyncio.timeout(2):
            answered = await client.get("/api/v1/agents")
    assert answered.status_code == 200 and answered.json()
