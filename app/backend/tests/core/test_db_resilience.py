"""The database cannot freeze the app (a freeze seen before). Each was one long transaction holding a row
every other call needs (the browser mission's, the scout's connection) while the others queued behind it, each holding
a pooled connection, until the pool was gone and even /health could not answer. These tests hold such rows, or every
pooled connection, on purpose and check that the app keeps answering: a waiter is told "busy" within its lock wait,
/health and the heartbeats answer on their own pool, background work gets longer limits, no page is read inside a
transaction (the guard raises in every test: `tests/conftest.py`), and no transaction is held while pages load: not
by the pipeline's turn, a route, a tool call, the night's board read or Go's night check (`app/db.py`).

The first two tests froze the code before these guards: the hand-outs waited on the held rows for ever, a connection
each, and /health and the heartbeat waited 30 seconds for a connection that never came."""

import asyncio
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError

from app import db, errors
from app.agents import roster
from app.jobsearch import adapters, stages
from app.jobsearch.browser import go_checks
from app.jobsearch.browser import items as browser_door
from app.jobsearch.browser import pace as browser_pace
from app.jobsearch.employers import core as employers
from app.jobsearch.fetch import polite
from app.jobsearch.fetch.polite import Fetcher
from app.jobsearch.loops import schedule
from app.jobsearch.measures.pay_place import match_key
from app.jobsearch.missions import core as missions
from app.jobsearch.models import JobPosting, Opportunity
from app.jobsearch.postings import index as postings
from app.jobsearch.research import analysis
from tests.builders import PAGE, WHOLE, analysis_setup, counted, mission_of
from tests.jobsearch import test_employer_research as er
from tests.support import EXAMPLE_SYSTEM, OWNER, load_profile

#: A careers page that holds a whole posting.
WHOLE_PAGE = f"<html><head><title>Programs Lead</title></head><body><main>{WHOLE}</main></body></html>"

#: How long a page that waits for every transaction to end (`Pages.load`) is given before it says one is held across it.
#: Nothing in a passing run comes near it: a transaction that is not waiting on the read ends in milliseconds.
GIVE_UP_AFTER = 30.0


def _url(js) -> str:
    return js.url.render_as_string(hide_password=False)


@pytest.fixture
async def own_engines(js, monkeypatch):
    """The app's engines made afresh for one test (`db.configure`, with a lock wait of one second), disposed after it,
    and the test database's own put back."""
    monkeypatch.setattr(db, "LOCK_TIMEOUT_MS", 1_000, raising=False)
    for name in ("_engine", "_sessions", "_lifeline_engine", "_lifeline_sessions"):
        monkeypatch.setattr(db, name, getattr(db, name, None), raising=False)
    db.configure(_url(js))
    yield
    for name in ("_engine", "_lifeline_engine"):
        made = getattr(db, name, None)
        if made is not None:
            await made.dispose()


async def _hold_hot_rows():
    """The browser mission's turn held from outside the calls under test, as a long transaction held
    its row: the in-app turn every hand-out takes first (`browser_pace.lock_mission`; the database has one writer, so
    there is no row to hold). It is let go when the returned session's transaction ends: `await _let_go(held)`."""
    held = db.read_session_factory()()
    await held.execute(text("select 1"))  # a reader's transaction: it takes no writer turn, and ending it lets go
    await browser_pace.lock_mission(held, OWNER)
    return held


async def _let_go(held) -> None:
    await held.rollback()
    await held.close()


async def _hot_rows_exist() -> None:
    async with db.session_factory()() as session:
        await roster.ensure_roster(session, OWNER)
        await missions.resume(session, await mission_of(session, OWNER, missions.BROWSER), by="Tester")
        await session.commit()


async def test_a_transaction_holding_the_hot_rows_never_freezes_the_app(js, client, own_engines):
    await _hot_rows_exist()
    held = await _hold_hot_rows()

    async def browser_hand_out():
        async with db.session_factory()() as session:
            return await browser_door.code_take(session, OWNER, "code-test")

    async def analyst_claim():
        async with db.session_factory()() as session:
            return await analysis.claim_next(session, OWNER)

    # More callers than the shared pool has connections (the pool): before the guards each waited on the held row with a
    # connection, the rest waited for a connection, and nothing else got one.
    work = [asyncio.create_task(browser_hand_out()) for _ in range(8)]
    work += [asyncio.create_task(analyst_claim()) for _ in range(8)]
    try:
        await asyncio.sleep(0.3)
        health = await asyncio.wait_for(client.get("/health"), 5)
        assert health.status_code == 200 and health.json()["db"] == "ok"
        started = time.monotonic()
        done = await asyncio.wait_for(asyncio.gather(*work, return_exceptions=True), 15)
        assert time.monotonic() - started < 10
    finally:
        await _let_go(held)
        for task in work:
            task.cancel()
        await asyncio.gather(*work, return_exceptions=True)
    assert done == [None] * 16, "each waiter skipped its turn (nothing handed out), none failed"
    async with db.session_factory()() as session:
        assert await session.scalar(text("select 1")) == 1


async def test_health_and_the_heartbeat_answer_while_every_shared_connection_is_taken(js, client, own_engines,
                                                                                     scout_token):
    await _hot_rows_exist()
    taken = [await db.engine().connect() for _ in range(db.engine().pool.size() + db.engine().pool._max_overflow)]
    try:
        health = await asyncio.wait_for(client.get("/health"), 5)
        assert health.status_code == 200
        beat = await asyncio.wait_for(client.post("/api/v1/agent/heartbeat", json={"state": "idle"},
                                                  headers=scout_token), 8)
        assert beat.status_code == 200 and beat.json()["ok"] is True
    finally:
        for conn in taken:
            await conn.close()


# --- a busy database is a "busy" answer, never an error -------------------------------------------------------------


class _LockNotGranted(Exception):
    sqlstate = "55P03"


def _busy_error() -> OperationalError:
    return OperationalError("SELECT ... FOR UPDATE", {}, _LockNotGranted("canceling statement due to lock timeout"))


def test_a_lock_wait_or_a_statement_past_its_limit_or_a_full_pool_is_busy_and_nothing_else_is():
    from sqlalchemy.exc import TimeoutError as PoolTimeout

    class Cancelled(Exception):
        sqlstate = "57014"

    class Unique(Exception):
        sqlstate = "23505"

    assert db.is_busy(_busy_error())
    assert db.is_busy(OperationalError("SELECT", {}, Cancelled()))
    assert db.is_busy(PoolTimeout("QueuePool limit of size 5 overflow 5 reached"))
    assert not db.is_busy(OperationalError("INSERT", {}, Unique()))
    assert not db.is_busy(ValueError("no"))


async def test_a_route_that_meets_a_busy_database_answers_503_with_retry_after():
    app = FastAPI()
    errors.install(app)

    @app.get("/held")
    async def held() -> dict:
        raise _busy_error()

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        reply = await c.get("/held")
    assert reply.status_code == 503
    assert reply.headers["retry-after"] == str(errors.RETRY_AFTER_SECONDS)
    assert "busy" in reply.json()["detail"]


async def test_a_tool_call_that_meets_a_busy_database_is_refused_to_be_made_again(js, door):
    from app.jobsearch.doors.scout import door as scout_door

    async def body(session, owner):
        raise _busy_error()

    reply = await scout_door.call(door, "jobs.test", body)
    assert reply["ok"] is False and "busy" in reply["reason"] and "again" in reply["how_to_fix"]


# --- the limits each kind of work runs under ----------------------------------------------------------------------


async def test_requests_get_short_limits_and_background_work_longer_ones(js):
    """The guard's limits (`db._limits`: the seconds to wait for the writer's turn and the seconds a statement may run),
    and the wait a call is given for a lock (`db.lock_wait_seconds`), by kind of work."""
    assert db._limits() == (5.0, 30.0) and db.lock_wait_seconds() == 5.0
    with db.background():
        assert db._limits() == (20.0, 300.0) and db.lock_wait_seconds() == 20.0

    @db.background_work
    async def a_pass() -> tuple[tuple[float, float], float]:
        return db._limits(), db.lock_wait_seconds()

    assert await asyncio.create_task(a_pass()) == ((20.0, 300.0), 20.0)
    assert db._lifeline_limits() == (3.0, 5.0), "the lifeline's own, short"


# --- no network I/O inside a transaction ----------------------------------------------------------------------------


def _answering_fetcher(seconds: float = 0.0) -> Fetcher:
    async def answer(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(seconds)
        return httpx.Response(404)

    return Fetcher(httpx.AsyncClient(transport=httpx.MockTransport(answer)), per_host_seconds=0)


async def test_a_page_read_inside_an_open_transaction_is_caught(js, monkeypatch):
    monkeypatch.setattr(db, "strict_io", True)
    fetch = _answering_fetcher()
    try:
        async with db.session_factory()() as session:
            await session.execute(text("select 1"))
            with pytest.raises(db.IoInTransaction):
                await fetch.get("https://jobs.example.test/robots.txt", patient=False)
            assert len(db.io_caught) == 1, "noted, so a test fails on one the code caught"
            db.io_caught.clear()  # this one was made on purpose
            await session.commit()
        await fetch.get("https://jobs.example.test/robots.txt", patient=False)  # none open now: allowed

        async def read(session):
            return await session.scalar(text("select 2"))

        async def fetch_it(two):
            await fetch.get("https://jobs.example.test/robots.txt", patient=False)
            return two

        async def write(session, two):
            return await session.scalar(text(f"select {two} + 1"))

        assert await db.read_fetch_write(read, fetch_it, write) == 3
    finally:
        await fetch.close()


async def test_the_read_of_read_fetch_write_takes_no_turn_of_the_writer(js):
    """`read` is a reader's session: a writer's session began `BEGIN IMMEDIATE` at its first statement and held the
    file's one turn through the whole read (seconds for the descriptions roles lack, on a large database).
    Old behaviour: the read held the turn. New: a writer may take it while the read runs (here a second writer's
    session, which raises `db.NestedWriter` in the tests if this task's read held the turn)."""

    async def read(session):
        two = await session.scalar(text("select 2"))  # a writer's session takes the turn here, a reader's does not
        async with db.session_factory()() as other:  # raises NestedWriter when `session` holds the turn
            await other.execute(text("select 1"))
            await other.commit()
        return two

    async def fetch_it(two):
        return two

    async def write(session, two):
        return await session.scalar(text(f"select {two} + 1"))

    assert await db.read_fetch_write(read, fetch_it, write) == 3


async def test_outside_the_tests_a_page_read_inside_a_transaction_is_logged_once_for_its_place(js, caplog,
                                                                                             monkeypatch):
    monkeypatch.setattr(db, "strict_io", False)  # as the app runs: the tests raise (`tests/conftest.py`)
    fetch = _answering_fetcher()
    try:
        async with db.session_factory()() as session:
            await session.execute(text("select 1"))
            for _ in range(3):
                await fetch.get("https://jobs.example.test/robots.txt", patient=False)
    finally:
        await fetch.close()
    said = [r for r in caplog.records if "network I/O inside an open database transaction" in r.getMessage()]
    assert len(said) == 1 and "test_db_resilience.py" in said[0].getMessage()


async def test_release_saves_what_was_done_and_refuses_inside_a_savepoint_or_a_process_engines_call(js):
    async with db.session_factory()() as session:
        await session.execute(text("select 1"))
        assert db.in_transaction()
        await db.release(session)
        assert not db.in_transaction(), "the transaction ended, its connection handed back"
        async with session.begin_nested():
            assert not db.can_release(session)
            with pytest.raises(RuntimeError):
                await db.release(session)
        await session.rollback()
        with db.holding("a call of the process engine"):
            await session.execute(text("select 1"))
            assert not db.can_release(session) and await db.release_if_free(session) is False
        assert await db.release_if_free(session) is True


async def test_a_read_started_beside_a_transaction_holds_none_of_it_and_a_known_debt_is_not_raised(js):
    fetch = _answering_fetcher()
    try:
        async with db.session_factory()() as session:
            await session.execute(text("select 1"))
            await asyncio.get_running_loop().create_task(
                fetch.get("https://jobs.example.test/robots.txt", patient=False), context=db.detached())
            with db.io_held_knowingly("a test of the escape"):
                await fetch.get("https://jobs.example.test/robots.txt", patient=False)
            await session.commit()
    finally:
        await fetch.close()
    assert db.io_caught == []


# --- the harness: no transaction of the pipeline's turn is held while its pages are read -------------------------


class Pages:
    """What a page takes in these tests: as long as it takes for no transaction of the app to be open. `await load()` is
    the page, and it returns the first moment none is (the sessions begin and end their transactions through
    `db.AppSession`, whichever task holds them). A transaction held across a page read is a read waiting on a
    transaction that waits on the read, so nothing ends and `load` gives up after `GIVE_UP_AFTER` and says so. That
    is the rule ("no transaction is held while a page loads") with no clock in it: a pass takes as long as the machine
    takes, and a transaction another task holds for a moment while the page is asked for is waited out, not failed."""

    def __init__(self) -> None:
        self.open: set[int] = set()
        self.loaded = 0
        self.stuck: list[str] = []

    async def load(self) -> None:
        give_up = time.monotonic() + GIVE_UP_AFTER
        while self.open:
            if time.monotonic() > give_up:
                said = f"{len(self.open)} transaction(s) still open: held across the page that was being read"
                self.stuck.append(said)
                raise AssertionError(said)
            await asyncio.sleep(0.005)
        self.loaded += 1


@pytest.fixture
def pages():
    """The test's `Pages`, watching the app's sessions until it ends; a page left waiting fails it, even when the code
    that met the error caught it as a site that did not answer."""
    from sqlalchemy import event

    seen = Pages()

    def began(session, _transaction, _connection) -> None:
        seen.open.add(id(session))

    def ended(session, transaction) -> None:
        if transaction.parent is None:
            seen.open.discard(id(session))

    event.listen(db.AppSession, "after_begin", began)
    event.listen(db.AppSession, "after_transaction_end", ended)
    yield seen
    event.remove(db.AppSession, "after_begin", began)
    event.remove(db.AppSession, "after_transaction_end", ended)
    assert not seen.stuck, seen.stuck


class _SlowBoard:
    """A job system whose every posting page takes `Pages.load`."""

    def __init__(self, pages: Pages) -> None:
        self.pages = pages

    async def read(self, fetch, board):
        return None

    async def detail(self, fetch, board, posting):
        await self.pages.load()
        return {"text": f"The whole posting for {posting['title']}. " * 20}


async def test_the_pipelines_turn_holds_no_transaction_while_its_pages_are_read(js, monkeypatch, pages):
    monkeypatch.setattr(adapters, "ADAPTERS", {EXAMPLE_SYSTEM: _SlowBoard(pages)})
    monkeypatch.setattr(polite, "make", _answering_fetcher)
    monkeypatch.setattr(db, "strict_io", True)
    seen = datetime.now(UTC) - timedelta(days=2)
    async with db.session_factory()() as session:
        acme = await employers.find_or_make(session, OWNER, "Acme")
        acme.ats, acme.board = EXAMPLE_SYSTEM, {"slug": "acme"}
        await session.flush()
        for n in range(3):
            role = Opportunity(owner=OWNER, slug=f"quiet-{n}", company="Acme", title=f"quiet {n}",
                               match_key=f"acme|quiet {n}", status=stages.FOLLOWUP_NEEDED)
            session.add(role)
            await session.flush()
            session.add(JobPosting(owner=OWNER, employer_id=acme.id, ext_id=f"q{n}", company="Acme",
                                   title=f"quiet {n}", match_key=match_key("Acme", f"q{n}"), first_seen=seen,
                                   last_seen=seen, links=[], url=f"https://jobs.example.test/q{n}", ats=EXAMPLE_SYSTEM,
                                   filtered_at=seen, route=None, opportunity_id=role.id))
        await session.commit()
    assert await schedule.pipeline_pass(OWNER) is True
    async with db.session_factory()() as session:
        texts = (await session.scalars(select(JobPosting.text).where(JobPosting.owner == OWNER))).all()
    assert all(t and "The whole posting" in t for t in texts)
    assert pages.loaded == 3, "every posting's page was read, none inside a transaction"


# --- the harness, for the routes, the tool calls and the background passes that read the web ---------------------


def _slow_web(monkeypatch, pages: Pages, body: str = WHOLE_PAGE) -> SimpleNamespace:
    """Every page of the made-up web takes `Pages.load` (robots.txt: none, at once), for every reader the app has.
    `pages` on what it returns is what was asked."""
    seen = SimpleNamespace(pages=[])

    async def route(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        seen.pages.append(str(request.url))
        await pages.load()
        return httpx.Response(200, text=body, headers={"content-type": "text/html"})

    def make() -> Fetcher:
        return Fetcher(httpx.AsyncClient(transport=httpx.MockTransport(route)), per_host_seconds=0)

    monkeypatch.setattr(polite, "make", make)  # the one factory every reader goes through
    return seen


def _read_with_none_held(pages: Pages) -> None:
    assert pages.loaded, "a page was read, and not inside a transaction"


async def test_asking_for_research_reads_the_posting_before_the_analysis_process_starts(js, client, monkeypatch,
                                                                                       pages):
    role_id = (await analysis_setup(text=None))["Fabrikam"]  # Sourced, no text: the process's first steps read it
    seen = _slow_web(monkeypatch, pages, PAGE)
    asked = await client.post(f"/api/v1/roles/{role_id}/research")
    assert asked.status_code == 200, asked.text
    assert seen.pages == ["https://jobs.example.test/fabrikam"], "its source, read once, ahead of the process"
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, role_id)
    assert role is not None and role.description and role.analysis_state == analysis.WAITING
    _read_with_none_held(pages)


async def test_a_tool_call_that_reads_the_web_holds_no_transaction_while_it_reads(js, monkeypatch, pages):
    """The researcher's report (`jobs.employer_report`): the step after it looks for the employer's job system in its
    pages and the open feeds, outside the engine's call (`process.run_deferred`)."""
    await er._world()
    employer = await er._employer("Acme")
    await er._send(employer.id)
    seen = _slow_web(monkeypatch, pages)
    reply = await er._report(employer.id)
    assert reply["ok"] is True and seen.pages, "the open feeds were asked"
    _read_with_none_held(pages)


class _SlowFailingBoard:
    """A job system whose every board takes `Pages.load` to answer that it can't be read."""

    def __init__(self, pages: Pages) -> None:
        self.pages = pages

    async def read(self, fetch, board):
        await self.pages.load()
        raise adapters.AdapterError("the board did not answer")


async def test_the_nights_board_read_holds_no_transaction_while_its_boards_are_read(js, monkeypatch, pages):
    monkeypatch.setattr(adapters, "ADAPTERS", {EXAMPLE_SYSTEM: _SlowFailingBoard(pages)})
    async with db.session_factory()() as session:
        for name in ("Acme", "Globex", "Initech"):
            row = await employers.find_or_make(session, OWNER, name)
            row.ats, row.board = EXAMPLE_SYSTEM, {"slug": name.lower()}
        await session.commit()
    fetch = _answering_fetcher()
    try:
        async with db.session_factory()() as session:
            read = await postings.refresh(session, OWNER, fetch)
            await session.commit()
    finally:
        await fetch.close()
    assert read["failed"] == 3, read
    _read_with_none_held(pages)


async def test_gos_night_check_holds_no_transaction_while_it_reads_the_postings(js, monkeypatch, pages):
    seen = _slow_web(monkeypatch, pages)
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        for n in range(2):
            session.add(Opportunity(owner=OWNER, slug=f"ready-{n}", company=f"Acme {n}", title="Programs Lead",
                                    match_key=f"acme {n}|programs lead", status=stages.READY_TO_LAUNCH,
                                    url=f"https://careers.acme{n}.example.test/jobs/{n}",
                                    apply_url=f"https://careers.acme{n}.example.test/jobs/{n}", **counted()))
        await session.commit()
    await go_checks.precheck(OWNER)
    assert seen.pages, "the postings were read"
    _read_with_none_held(pages)


async def test_hand_outs_waiting_on_the_mission_lock_hold_no_pooled_connection_so_an_ordinary_call_still_gets_one(
        js, monkeypatch):
    """The browser sessions
    and the code-first workers each ask for the mission's row FOR UPDATE, and the one that holds it does its queue work
    first. Every other hand-out waited on the row (up to the lock wait) holding a pooled connection, so about ten
    callers ate the pool and the board read could not save a board. A caller now waits its turn in the app, before it
    has a connection, and the row is waited for by one at a time."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    await _hot_rows_exist()
    small = db._new_engine(_url(js), 3, 0, 1, 3_000)  # three connections, one second for a free one
    monkeypatch.setattr(db, "_engine", small)
    monkeypatch.setattr(db, "_sessions", async_sessionmaker(small, expire_on_commit=False,
                                                            sync_session_class=db.AppSession))
    held = await _hold_hot_rows()  # another call holds the mission's turn

    async def hand_out() -> str:
        async with db.session_factory()() as session:
            try:
                await browser_pace.lock_mission(session, OWNER)
            except errors.Busy:
                return "busy"
            await session.commit()
            return "took"

    async def ordinary() -> int:
        await asyncio.sleep(0.3)
        async with db.session_factory()() as session:
            return await session.scalar(text("select 1"))

    work = [asyncio.create_task(hand_out()) for _ in range(6)]
    try:
        assert await asyncio.wait_for(ordinary(), 5) == 1
    finally:
        await _let_go(held)
        done = await asyncio.wait_for(asyncio.gather(*work, return_exceptions=True), 15)
        await small.dispose()
    assert all(d in ("took", "busy") for d in done), done
