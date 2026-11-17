"""Shared fixtures: `js` (a job-search database, a SQLite file of its own for each test; see support.py), `client` (the
app over ASGI, acting for the owner), `door` (the scout's door with its token) and `knocks` (the scout's wakes, kept
instead of sent). The process engine's own tests are the package's (app/packages/process-engine/tests)."""

import gc
import os
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event

# A test run has no data folder, whatever the shell that started it names. Where resumebot is installed,
# RESUMEBOT_DATA_DIR is set for the whole user, so every shell has it: left in, the app's settings would be read from
# that folder's `.env` as they are imported (the install's own database, owner and secret files: `app.settings`), and
# the code that works in the data folder would work in the install's. It goes before anything of the app is imported,
# here and in every xdist worker, which imports this file too. A test of the data folder names a temporary one itself
# (`monkeypatch.setenv`). `dev.py check` leaves the variable out of everything it starts as well.
os.environ.pop("RESUMEBOT_DATA_DIR", None)

from app.jobsearch.models import Opportunity
from tests.support import (
    AUTH,
    DEFAULT_OWNER,
    NOON,
    OWNER,
    TOKEN,
    go_that_leaves_nothing_running,
    js,  # noqa: F401  (a fixture, used by name)
    offline_internet,
    open_internet,
    pin_clock,
    schema_template,  # noqa: F401  (a fixture `js` asks for)
)


@event.listens_for(Opportunity, "before_insert")
def roles_are_sourced_unless_the_test_says_not(mapper, connection, target):
    """A role a test makes is Sourced (the owner, 2026-10-15: `postings/sourced.py`), so the many tests that research,
    draft or
    move roles need not each say where it came from. A test of sourcing says what it means: `sourced_at=None` (a role
    that is not Sourced) or its own `source_url`, `source_kind` and `sourced_at`. Roles made by the app's own code (the
    search round's Core insert) are not touched: they are not Sourced until the app sources them."""
    if "sourced_at" not in target.__dict__:
        target.sourced_at = datetime.now(UTC)
        said = target.__dict__
        target.source_url = said.get("source_url") or said.get("url") or "https://careers.example.test/jobs/1"
        target.source_kind = said.get("source_kind") or "employer"


@pytest.fixture(autouse=True)
def the_owners_zone_is_utc_and_the_unique_mark_starts_over(monkeypatch):
    """Clock pin: the owner's zone is UTC in every test, whatever `JOBSEARCH_TIMEZONE` or the
    machine's own settings file says, so no test's answer depends on where it runs (a test of a zone names its own, and
    `pin_clock(..., zone=...)` too). And the mark `utc_now_unique` keeps (the last instant it gave, so no two are the
    same) starts at the beginning of time: left from an earlier test it was ahead of a pinned clock, and every stamp
    after it came out a microsecond past the last, not at the moment the test pinned."""
    from app import timeutil

    monkeypatch.setattr(timeutil.settings, "timezone", "UTC")
    monkeypatch.setattr(timeutil, "_last_unique", datetime.min.replace(tzinfo=UTC))


@pytest.fixture(autouse=True)
def no_test_writes_the_users_environment(monkeypatch):
    """`resumebot init` sets the data folder variable for the user on Windows (the registry). No test does that for
    real: this one raises, so a test that needs the call replaces it with a recorder of its own."""
    from app import setup_cli

    def refused(name, value):
        raise OSError("tests never write the user's environment")

    monkeypatch.setattr(setup_cli, "_set_user_variable", refused)


@pytest.fixture(autouse=True)
def the_gc_threshold_is_put_back():
    """A start (`startup.tune_gc_threshold`) changes the process-wide collector thresholds; a test that reaches it
    leaves them as they were for the next test on the worker."""
    before = gc.get_threshold()
    yield
    gc.set_threshold(*before)


@pytest.fixture
def the_clock_says_noon(monkeypatch):
    """The app's clock says noon on 4 November 2026 for the test (`support.pin_clock`; a module asks for it with
    `pytestmark = pytest.mark.usefixtures("the_clock_says_noon")`). A test that makes its rows from the real clock and
    calls the app a moment later says two different days when UTC midnight falls between the two reads, and one that
    goes back a few hours says the day before in the first hours of the day."""
    pin_clock(monkeypatch, NOON)


@pytest.fixture
def the_queue_is_made_before_a_hand_out(monkeypatch):
    """The browser's queue is made (forced, as the work queue's `browser.reconcile` makes it) before each call of the
    door's `next_item` and of the code worker's `code_take`. The calls make nothing themselves: in the app the to-do has
    run before a session or the code asks. A test module whose subject is what a hand-out does with queued items asks
    for it with `pytestmark = pytest.mark.usefixtures("the_queue_is_made_before_a_hand_out")`; a single test with
    `@pytest.mark.usefixtures("the_queue_is_made_before_a_hand_out")`."""
    from app import db
    from app.jobsearch.browser import items as bd
    from app.jobsearch.browser import queue as bq

    real_next_item, real_code_take = bd.next_item, bd.code_take

    async def next_item(session, owner, now=None):
        await bq.refresh(session, owner, now)
        await db.release_if_free(session)
        return await real_next_item(session, owner, now)

    async def code_take(session, owner, sid, now=None):
        await bq.refresh(session, owner, now)
        await db.release_if_free(session)
        return await real_code_take(session, owner, sid, now)

    monkeypatch.setattr(bd, "next_item", next_item)
    monkeypatch.setattr(bd, "code_take", code_take)


@pytest.fixture(autouse=True)
def no_network_inside_a_transaction(monkeypatch):
    """Every test runs with the guard raising (`db.strict_io`): a page read, a wake or a mail read begun while the task
    holds a database transaction open raises `db.IoInTransaction`. The test also fails when the code that met it caught
    it (as a site that did not answer, say), so no new network read inside a transaction can land. The rule is
    `db.read_fetch_write`: read, commit, fetch, then write in a new transaction."""
    from app import db

    monkeypatch.setattr(db, "strict_io", True)
    db.io_caught.clear()
    yield
    caught, db.io_caught[:] = list(db.io_caught), []
    assert not caught, f"network I/O inside an open database transaction: {caught}"


@pytest.fixture(autouse=True)
def no_write_inside_a_reader(monkeypatch):
    """Every test runs with the guard raising (`db.strict_readers`): a statement that writes, begun in a reader's
    transaction (`db.ReadSession`, a tool registered `reads=True`), raises `db.WriteInReader`. The test also fails when
    the code that met it caught it, so no GET that writes can land unseen. A test that makes the write on purpose (the
    test of the guard) clears `db.reader_wrote` itself."""
    from app import db

    monkeypatch.setattr(db, "strict_readers", True)
    db.reader_wrote.clear()
    yield
    caught, db.reader_wrote[:] = list(db.reader_wrote), []
    assert not caught, f"a write inside a reader's transaction: {caught}"


@pytest.fixture(autouse=True)
def no_second_writer_inside_a_writer(monkeypatch):
    """Every test runs with the guard raising (`db.strict_nested`): a writer's transaction begun while this task holds
    the writer's turn in another session raises `db.NestedWriter` at once, where in the app it would wait for its own
    task and be told busy. The test also fails when the code that met it caught it. A test of the guard clears
    `db.nested_caught` itself."""
    from app import db

    monkeypatch.setattr(db, "strict_nested", True)
    db.nested_caught.clear()
    yield
    caught, db.nested_caught[:] = list(db.nested_caught), []
    assert not caught, f"a second writer inside a writer's transaction: {caught}"


@pytest.fixture(autouse=True)
def writes_without_a_to_do_are_refused(monkeypatch):
    """A direct write to a watched table that no `queue.post` or `queue.exempt` answers raises `MissedPost` at commit
    (`work/queue/hook.py`), so every test that exercises a writer is also a test that it posts. In the app the same
    write posts the catch-all `*` and is logged."""
    from app.jobsearch.work.queue import service

    monkeypatch.setattr(service, "strict", True)


@pytest.fixture(autouse=True)
def no_stops_carry_over():
    """The hosts a refusal stopped are kept in the process (`host_stops`); no test starts with another's."""
    from app.jobsearch.fetch import host_stops

    host_stops.reset()
    yield
    host_stops.reset()


@pytest.fixture(autouse=True)
def reads_no_web(monkeypatch):
    """Every read of the app's own code goes through one factory (`polite.make`): research fetching a posting's text
    before the analyst reads the role (docs/PRD.md 39.29), the pool reading the descriptions roles lack, Go's checks of
    a posting before its tab opens, a recording's plain reads. No test reaches the network for any of them: every
    connection is refused, so a role goes on as it would for a site that did not answer, unless a test puts its own
    answers in (`monkeypatch.setattr(polite, "make", ...)`)."""
    from app.jobsearch.fetch import polite

    monkeypatch.setattr(polite, "make", offline_internet())


@pytest.fixture(autouse=True)
def deck_employer_research_off(monkeypatch):
    """A role going on deck queues its employer's research, and the deck's read waits for it a short while
    (`employer_research.queue_for_deck`). The tests of the analysis flow run without that;
    tests/jobsearch/test_deck_employer_research.py turns it on where it tests it."""
    from app.jobsearch.research import employer as employer_research

    monkeypatch.setattr(employer_research, "DECK_RESEARCH", False)


@pytest.fixture(autouse=True)
def haiku_reads_off(monkeypatch):
    """The gate-facts step's short Haiku read is on in the app (`gate_facts.HAIKU_READS`), and a role that waits for it
    is held back from the analyst. The tests of the analysis flow run without it; tests/jobsearch/test_gate_facts.py
    turns it on where it tests the read."""
    from app.jobsearch.gating import gate_facts

    monkeypatch.setattr(gate_facts, "HAIKU_READS", False)


@pytest.fixture(autouse=True)
def go_reaches_no_mail_and_no_chrome(monkeypatch):
    """Go checks each role before its tab opens (`go_checks.py`): the owner's mail and the posting. No test reaches the
    mail (there is no mail login: none is saved), and the posting is read through `polite.make` (`reads_no_web`), so
    each posting counts as open, unless a test of the checks puts its own answers in."""
    from app.jobsearch.browser import go_checks

    go_checks._prechecked.clear()
    # Nor the machine's own Chrome: the browser is "up", every page it is asked for is unreadable, and Go's window is a
    # stand-in that opens nothing (tests.support.fake_go_window).
    from app.jobsearch.browser import code as browser_code
    from app.jobsearch.browser import go as apply_go
    from tests.support import fake_go_window

    async def up() -> str:
        return ""

    async def no_page(link):
        raise browser_code.BrowserUnavailable("no browser in tests")

    monkeypatch.setattr(go_checks, "start_browser", up)
    monkeypatch.setattr(go_checks, "browser_page", no_page)
    monkeypatch.setattr(apply_go, "go_window", fake_go_window([]))


@pytest.fixture(autouse=True)
async def go_starts_fresh_and_leaves_nothing_running():
    """Go keeps its progress and its running task for each owner in the app's one process (`browser/go.py`), and the
    tests share one event loop for the whole run, so a Go that one test started (a schedule pass that came due, a press
    of the button) stayed "running" for every test after it: the next one that asked got "Launch in Browser is running
    already". Every test starts with no run and no task, and a
    task still going when its test ends is cancelled and waited for, so it works on no later test's database. The work
    is `tests.support.go_that_leaves_nothing_running`, which a test of its own pins."""
    async with go_that_leaves_nothing_running():
        yield


@pytest.fixture(autouse=True)
def launch_takes_its_turn_where_it_is_asked(monkeypatch):
    """Launch (`browser/go.py`) asks the work queue's dispatcher for a turn and waits for it (`schedule.pipeline_turn`).
    No test runs a dispatcher unless it says so, so nothing would answer and Launch would wait out its whole wait. In a
    test, Launch's turn is the whole turn run in place (`schedule.pipeline_pass`), as it was when no dispatcher ran. The
    tests of `pipeline_turn` itself (tests/jobsearch/work_queue/test_turn.py) call it directly and are not changed."""
    from app.jobsearch.browser import go as apply_go
    from app.jobsearch.loops import schedule

    async def turn(owner: str) -> None:
        await schedule.pipeline_pass(owner)

    monkeypatch.setattr(apply_go, "_turn", turn)


@pytest.fixture(autouse=True)
def tests_never_reach_the_browser_bridge(monkeypatch):
    """The tab register closes tabs by asking the browser bridge (`tab_register.bridge_post`), which in a running
    install
    is the live one. No test reaches it: every ask is kept (the fixture's value) and answered as a bridge that closed
    what it was told to and found nothing gone, unless a test of the register answers differently."""
    from app.jobsearch.browser import tabs as tab_register

    asks: list[tuple[str, dict]] = []

    async def bridge_post(path: str, body: dict) -> dict | None:
        asks.append((path, body))
        if path == "/tabs/close":
            return {"closed": len(body.get("targets") or [])}
        return {"closed": 0, "gone": []}

    monkeypatch.setattr(tab_register, "bridge_post", bridge_post)
    monkeypatch.setattr(tab_register, "_go_rows", False)
    return asks


@pytest.fixture(autouse=True)
def probes_find_open_sites(monkeypatch):
    """A site added or switched to "ask resumebot to find out" is tried from the server in the request (docs/PRD.md
    30.3). No test reaches the network for that: every site answers as open, so the site is read by the app, unless
    a test of the probe puts its own answers in (`read_probe.make_fetcher`)."""
    from app.sites import read_probe

    monkeypatch.setattr(read_probe, "make_fetcher", open_internet())


@pytest.fixture(autouse=True)
def tests_call_as_host_test(monkeypatch):
    """The tests reach the app through the ASGI transport as `http://test`; the app answers only its own kinds of host
    (http_guard.py), so that name is listed here. tests/core/test_http_guard.py clears the list to test the rule
    itself."""
    from app.settings import settings

    monkeypatch.setattr(settings, "allowed_hosts", "test")


@pytest.fixture(autouse=True)
def no_packet_folder(monkeypatch):
    """A packet folder named in the machine's own settings file must not receive the tests' packets."""
    from app.settings import settings

    monkeypatch.setattr(settings, "packet_dir", "")


@pytest.fixture(autouse=True)
def no_background_reads(monkeypatch):
    """A scheduled pass would start the night's board read in the background (boards_night.py); tests that mean to
    read call it directly, so no read outlives its test."""
    from app.jobsearch.missions import boards_night

    monkeypatch.setattr(boards_night, "start_in_background", lambda *a: None)


@pytest.fixture(autouse=True)
def no_background_board_sweeps(monkeypatch):
    """The nightly sourcing sweep looks for roles on their employers' boards once its transaction has committed, in the
    background (`sourcing.start_board_sweep`); tests that mean to read the boards call `sourcing.sweep_boards`
    directly, so no read outlives its test."""
    from app.jobsearch.postings import sourced as sourcing

    monkeypatch.setattr(sourcing, "start_board_sweep", lambda owner: False)


#: An instant outside every default peak window (3 PM in Los Angeles), so a test never depends on the hour it runs.
OFF_PEAK = "2026-10-09T22:00:00+00:00"


@pytest.fixture(autouse=True)
def off_peak_clock(monkeypatch):
    """Claude's peak hours (work/hours.py) read the wall clock when no time is given, so a test run at 8 AM in Los
    Angeles
    found no work to hand out. Without a time, the peak checks read an off-peak instant; a test that means peak hours
    passes a time or patches them itself."""
    from datetime import datetime

    from app.jobsearch.work import hours as night

    moment = datetime.fromisoformat(OFF_PEAK)
    in_peak, blackout = night.in_peak, night.blackout
    monkeypatch.setattr(night, "in_peak", lambda s, now=None: in_peak(s, now or moment))
    monkeypatch.setattr(night, "blackout", lambda s, now=None: blackout(s, now or moment))


@pytest.fixture(autouse=True)
def always_off_hours_and_counts_fresh(monkeypatch):
    """The pool starts work of its own only in the owner's off hours (night.in_off_hours), which read the wall clock
    when no time is given: without a time a test is in off hours, so it never depends on the hour it runs. A test of the
    window passes the time. The page's waiting counts are kept 30 seconds (`pool.COUNTS_FOR`): no test keeps them."""
    from datetime import timedelta

    from app.jobsearch.pool import core as pool
    from app.jobsearch.work import hours as night

    in_off_hours = night.in_off_hours
    monkeypatch.setattr(night, "in_off_hours", lambda s, now=None: True if now is None else in_off_hours(s, now))
    monkeypatch.setattr(pool, "COUNTS_FOR", timedelta(0))
    pool._counts.clear()


@pytest.fixture(autouse=True)
def browser_work_starts_fresh(monkeypatch):
    """What the browser's queue and the pool keep in the app's one process (when the queue was last made, a drain, a
    stall already logged, the pool's hand-outs and asks) starts empty for every test, and the pace between pages is a
    fixed 8 seconds, so no test depends on chance (`browser_pace.gap`)."""
    from datetime import timedelta

    from app.jobsearch.browser import handback as bh
    from app.jobsearch.browser import pace as bp
    from app.jobsearch.browser import queue as bq
    from app.jobsearch.pool import core as pool
    from app.jobsearch.work import availability, dispatcher, session_kinds

    for kept in (bq._stall_logged,
                 pool._in_window,
                 bh._tab_failed, bp._code_on):
        kept.clear()
    availability.forget()  # a drain, and the usage limit the process remembers
    session_kinds.forget()  # the kinds just handed nothing (the backstop)
    dispatcher.forget()
    monkeypatch.setattr(bp, "gap", lambda: timedelta(seconds=8))
    yield
    for kept in (bq._stall_logged,
                 pool._in_window,
                 bh._tab_failed, bp._code_on):
        kept.clear()
    availability.forget()
    session_kinds.forget()
    dispatcher.forget()


@pytest.fixture
async def client(js, monkeypatch, request):  # noqa: F811  (the imported fixture, asked for by name)
    """The app over ASGI as `http://test`, acting for the owner `OWNER`. A test whose owner differs, or that needs more
    set up first, defines a `client` of its own.

    The profile routes refuse a line only a reader could judge (docs/PRD.md 30, 46, the owner, 2026-10-26). The made-up
    profile the older tests save through them is still shaped with such lines, and what those tests pin is how a
    profile is saved and read, not who judges a line: the refusal is switched off for them, and
    `test_reader_lines_refused.py` is the one that runs it."""
    from app.jobsearch.main import app
    from app.jobsearch.profile import store as profile_store
    from app.settings import settings

    monkeypatch.setattr(settings, "owner", OWNER)
    if request.module.__name__.rsplit(".", 1)[-1] != "test_reader_lines_refused":
        monkeypatch.setattr(profile_store, "refuse_reader_lines", lambda kind, data: None)
    if request.module.__name__.rsplit(".", 1)[-1] != "test_no_negative_points":
        # Likewise for negative points (docs/PRD.md 30.10): the older made-up profiles still hold some, and
        # `test_no_negative_points.py` is the one that runs the refusal.
        monkeypatch.setattr(profile_store, "refuse_negative_points", lambda kind, data, stored=None: None)
    async def the_engine_looks_first(request) -> None:
        """No test runs the engine's loop, and a read of the board settles nothing (it only reads the stored order).
        So before each read of the board this client makes the look the engine makes when a write has dirtied the
        order (`loop.Dispatcher.settle_order`), with the engine's own two functions: an ask on a reader, and a turn
        only when there is something to settle. A test of the read alone takes the look away:
        `client.event_hooks = {"request": [], "response": []}`."""
        if request.method == "GET" and request.url.path == "/api/v1/board":
            from app.jobsearch.work.queue import handlers
            from app.timeutil import utc_now

            now = utc_now()
            if await handlers.order_is_behind(settings.owner, now):
                await handlers.settle_order_at(settings.owner, now)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                           event_hooks={"request": [the_engine_looks_first]}) as c:
        yield c


@pytest.fixture
def scout_token(tmp_path, monkeypatch):
    """The token file the scout's door reads, holding `TOKEN`. Returns the headers that carry it."""
    from app.settings import settings

    path = tmp_path / "agent-token"
    path.write_text(TOKEN + "\n", encoding="utf-8")
    monkeypatch.setattr(settings, "agent_token_file", str(path))
    return AUTH


@pytest.fixture
def door(scout_token, monkeypatch):
    """The scout's context for a tool called straight on its door: the bearer token the door checks on every call, with
    the tools acting for the setting's default owner, `owner`."""
    from app.settings import settings

    monkeypatch.setattr(settings, "owner", DEFAULT_OWNER)
    return SimpleNamespace(headers={"authorization": scout_token["Authorization"]})


@pytest.fixture
def knocks(monkeypatch):
    """The scout's wake, replaced: every knock is kept as `(id, reason)`, the id being a search run's, a project's or a
    packet's."""
    from app.jobsearch.work import wake

    seen: list[tuple[str, str]] = []

    async def knock(run_id: str, reason: str = wake.SEARCH_STARTED) -> bool:
        from app import db

        db.outside_transaction(f"a wake of the scout ({reason})")  # as the real one: never inside a transaction
        seen.append((run_id, reason))
        return True

    monkeypatch.setattr(wake, "knock", knock)
    return seen


class Transactions:
    """What the app sent while a test ran, one transaction at a time. `ended` is one list of SQL statements for each
    transaction that ended (`db.watchers` says when, the engine's `before_cursor_execute` what), `timeline` is every
    statement in order with `END` where a transaction ended, and `open` the statements of the one still open."""

    END = "END"

    def __init__(self) -> None:
        self.ended: list[list[str]] = []
        self.timeline: list[str] = []
        self.open: list[str] = []

    def clear(self) -> None:
        self.ended.clear()
        self.timeline.clear()
        self.open.clear()

    @staticmethod
    def writes(statements: list[str]) -> bool:
        """Whether a transaction changed a row: an INSERT, an UPDATE or a DELETE (SQLite has no row lock to count)."""
        import re

        return any(re.search(r"\b(INSERT|UPDATE|DELETE)\b", s) for s in statements)


@pytest.fixture
def transactions(js):  # noqa: F811  (the imported fixture, asked for by name: the engine must exist)
    """A `Transactions` for the test: each transaction the app ends from here on, with the statements it sent."""
    from app import db

    seen = Transactions()

    def noted(_conn, _cursor, statement, _parameters, _context, _many) -> None:
        seen.open.append(statement)
        seen.timeline.append(statement)

    def ended(_held: float, _session) -> None:
        seen.ended.append(seen.open[:])
        seen.open.clear()
        seen.timeline.append(Transactions.END)

    engine = db.engine().sync_engine
    event.listen(engine, "before_cursor_execute", noted)
    db.watchers.append(ended)
    try:
        yield seen
    finally:
        db.watchers.remove(ended)
        event.remove(engine, "before_cursor_execute", noted)


class RecordedStarts:
    """A stand-in for the engine door's backend (`engine_calls`): it runs no start and keeps each one it was asked for
    as (name, key). Every other call the door makes (a knock, a dispatch, a nudge) is answered with nothing done."""

    def __init__(self) -> None:
        self.starts: list[tuple[str, object]] = []

    def start(self, name: str, key: object) -> None:
        self.starts.append((name, key))

    async def knock(self, run_id: str, reason: str) -> bool:
        return True

    async def dispatch_now(self) -> int:
        return 0

    async def knocks(self, pending: list) -> None:
        return None

    async def send_wakes(self, wakes) -> list[str]:
        return []

    def nudge(self) -> None:
        return None


@pytest.fixture
def fake_starts():
    """The engine door asks a recording backend instead of the real one for the test; the real one is back after it."""
    from app.jobsearch.work import engine_calls

    recorder = RecordedStarts()
    before = engine_calls._backend
    engine_calls.install(recorder)
    try:
        yield recorder
    finally:
        engine_calls.install(before)
