"""Shared set-up for the job-search tests: a database of its own (never the install's), a synthetic profile, a
synthetic tracker workbook and role builders. Nothing here is real data: every name, number and sentence is
made up."""

import asyncio
import contextlib
import io
import re
import shutil
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import openpyxl
import pytest
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, event, func, not_, select, text

from app import db
from app.jobsearch.imports.tracker import COLUMNS
from app.sites import registry

#: The owner the app acts for in most tests (the `client` fixture sets `settings.owner` to it).
OWNER = "tester"
#: The key of a job system the install ships a runbook for (the first the registry asks that a person read the terms of
#: and found open to a reader): what a posting made in a test says it came from, where the test does not care which. No
#: test types a system's name for this; it is the problem, "a job system's posting", that they need.
EXAMPLE_SYSTEM = next(
    runbook.key for runbook in registry.current().runbooks
    if runbook.kind == "job_system" and registry.current().verdict(runbook.hosts[0]) in ("allows", "silent"))

#: The setting's default owner, the one the scout's tools act for in the tests that call its door straight.
DEFAULT_OWNER = "owner"

#: A made-up posting, the whole of it (headings, lines, over the least a page may give: `whole.why_not`):
#: what a role holds in the tests that research it, since a role with no text of its posting is never researched.
POSTING_WORDS = """Programs Lead

About the job
We need someone to lead the programs team and keep the books in order.

What the job needs
- Five years of programs experience
- A willingness to travel one week a quarter

What we offer
A fair salary, health cover and four weeks off. Apply on our own site.

About us
""" + "We are a small team that builds programs for the public good. " * 6
POSTING_WORDS = POSTING_WORDS.strip()

#: The scout's bearer token in the tests that go through its door (the `scout_token` and `door` fixtures write it to the
#: token file the door reads), and the header that carries it.
TOKEN = "js_test_token_1234567890"
AUTH = {"Authorization": f"Bearer {TOKEN}"}

#: Why the owner is asked about a plan in the tests that drive the owner's step (PR-7; the `owner_asked` fixture): made
#: up, since a page that needs a browser or a site that asks not to be read no longer asks the owner anything (the site
#: is switched off instead, `sites/unreadable.py`).
OWNER_ASKED = "the owner wants to look at every plan"

#: Three made-up asks worth at most 5 points, the scoring most tests of fit as a percent use (`load_profile(...,
#: scoring=FIVE)`), and `answered`, a report's answers to them.
FIVE = {"asks": [
    {"id": "ask-a", "section": "description", "says": "The work is building programs", "points": 2},
    {"id": "ask-b", "section": "responsibilities", "says": "The role leads a team", "points": 2},
    {"id": "ask-c", "section": "qualifications", "says": "Asks for a field the owner knows", "points": 1},
]}


#: FIVE with the scale roles scored before asks are shown on (0 to 5), so a role whose stored fit is a raw 1 to 5 and
#: has no breakdown has a percent to be ordered by (`scoring.percent_of`): what the queue tests of tracker roles use.
ON_A_SCALE_OF_FIVE = {**FIVE, "old_scale": {"low": 0, "high": 5}}

#: Five made-up numbers for "grade the requirements for me", ticked. The model holds no default for them (built to
#: share), so every test that grades names its own: `{**GRADED_ON, "required_unproven": -5}` changes one.
GRADED_ON = {"on": True, "required_proven": 2, "required_transferable": 0, "required_unproven": -2,
             "preferred_proven_or_transferable": 1, "preferred_unproven": 0}


def bare(answer: dict) -> dict:
    """A save's or a restore's answer without the whole profile it carries (`profile`, for the page), so a test reads
    what the save said."""
    return {k: v for k, v in answer.items() if k != "profile"}


def answered(a: str = "met", b: str = "met", c: str = "not") -> dict:
    """A report's `fit_breakdown` answering FIVE: the default earns 4 of the 5 possible (80%)."""
    return {"asks": [{"id": f"ask-{x}", "answer": answer, "evidence": f"made-up evidence for {x}"}
                     for x, answer in (("a", a), ("b", b), ("c", c))]}


#: A made-up profile with the shapes a real one has (target roles, the work, a work location, an
#: amount, exclusions, an age modifier the app checks). Not anyone's real preferences. Its scoring is in
#: the older shape (a base score and modifiers), which loads as asks and keeps the old scale: the tests of plain fits
#: and percents run on it.
SPRINGDALE = {"id": "springdale", "name": "Springdale area", "towns": ["Oakfield", "Riverton", "Maple Heights",
                                                                      "Springdale"]}
LAKESIDE = {"id": "lakeside", "name": "Lakeside, IL"}
#: Where the made-up owner would work on site, as one list of locations (what the two places above flatten to).
LOCATIONS = ["Oakfield", "Riverton", "Maple Heights", "Springdale", "Lakeside, IL"]
PROFILE = {
    "criteria": {
        "positioning": "A director who builds programs.",
        "company": "A mid-sized public agency or nonprofit, run by a small board.",
        "target_roles": [{
            "id": "builders", "name": "Builders", "titles": ["Director of Programs"],
            "description": "Builds and runs programs: plans them, staffs them and reports on them.",
            "responsibilities": "Plans the year, staffs each program and reports to the board.",
            "qualifications": "A degree or the same in experience; no licence required.",
            "preferred": "Grant writing; a second language.",
            "compensation": "A salary over the floor, health cover and four weeks off.",
        }],
    },
    "gates": {
        "location": {
            "remote": {"on": True},
            "hybrid": {"on": True, "locations": LOCATIONS},
            "in_person": {"on": True, "locations": LOCATIONS},
        },
        "compensation": {"amount": 95_000, "against": "ask"},
        "other": [{"id": "travel", "kind": "rule", "text": "Travel of 20% or more."}],
        "employer_names": ["Acme Holdings", "Acme", "Globex"],
    },
    "scoring": {
        "asks": [
            {"id": "aging", "section": "posting", "says": "Posted more than 30 days ago", "points": -1, "days": 30,
             "checked_by": "app"},
            {"id": "promotion", "section": "posting", "says": "A promotion", "points": 1},
        ],
        # The scale roles scored before asks were shown on: the base score ran 1 to 4, the modifiers took it to 0 to 5.
        "old_scale": {"low": 0, "high": 5},
        "notes": [{
            "id": "old-base-score", "source": "the earlier scoring",
            "text": "The base score ran 1 to 4: 4: building is the job; 1: neither. Every role's score began there, "
                    "and the modifiers added to it or took from it.",
        }],
    },
    # Peak hours off, so a test's outcome never depends on the time of day it runs (tests of the blackout turn it on).
    # The pay method's rounding and range in dollars, and the sentence it assumes, so the numbers tests expect don't
    # move with the defaults (a new install's are a share of the ask and no sentence: tests/jobsearch/test_comp.py).
    "settings": {"avoid_peak": False, "comp_round_to": 5000, "comp_range_width": 10000,
                 "comp_assumptions": "standard benefits; any bonus is separate"},
}


def gate_says() -> dict[str, str]:
    """The sentence of each gate that is on in the made-up profile: the reason a role it stops is given."""
    from app.jobsearch.profile import schema as p
    from app.jobsearch.profile import views as pv

    return pv.engine_gates(p.Profile.model_validate(PROFILE)).says


async def load_profile(session, owner: str, **parts) -> None:
    """Save the made-up profile for `owner`, with any part replaced (`gates={...}`), as the owner would in the app."""
    from app.jobsearch.profile import store as profile_store

    for kind, data in {**PROFILE, **parts}.items():
        await profile_store.save(session, owner, kind, data, by="test")


async def a_backend_waits_for_a_lock() -> bool:
    """Whether any call is waiting for the writer's turn (or another named lock) now: how a test of two transactions
    knows the second has run into the first, with no sleep to guess it by. SQLite has one writer, so a second
    transaction that wants to write queues on the turn (`db._guard`) until the first commits; the turn is an
    `asyncio.Lock`, and its queue is what this reads. (`db.turn_watchers` is told only after a wait has ended.)"""
    return any(lock._waiters for _loop, lock in list(db._named.values()) if lock._waiters)  # type: ignore[attr-defined]


@contextlib.contextmanager
def reads_on(table: str):
    """The statements that read `table` while the block runs, in order, each as `(statement, ids)`: the ids a
    statement's parameters hold, in the order they were bound. SQLite has no row lock, so a test of what a pass locks
    reads the plain `SELECT ... FROM <table>` that `as_read.lock` sends (and checks it names no lock clause). A test
    of order reads the statements, never a clock."""
    seen: list[tuple[str, list[uuid.UUID]]] = []

    def noted(_conn, _cursor, statement, parameters, _context, _many) -> None:
        if not re.search(rf"\bFROM {re.escape(table)}\b", statement):
            return
        ids = []
        for value in parameters if isinstance(parameters, (tuple, list)) else []:
            try:
                ids.append(value if isinstance(value, uuid.UUID) else uuid.UUID(str(value)))
            except ValueError:
                pass
        seen.append((statement, ids))

    engine = db.engine().sync_engine
    event.listen(engine, "before_cursor_execute", noted)
    try:
        yield seen
    finally:
        event.remove(engine, "before_cursor_execute", noted)


#: The most arguments asyncpg takes in one statement (a 16-bit count): the statement with one more is refused with
#: "the number of query arguments cannot exceed 32767".
STATEMENT_ARGUMENT_LIMIT = 32_767
#: What a statement that binds no list of ids carries at most: its owner, a stage or two, a limit. A test of a path that
#: once bound a list of ids holds every statement of the path under this.
FEW_ARGUMENTS = 100


@contextlib.contextmanager
def bound_arguments():
    """How many arguments each statement the app sends carries while the block runs, in order, one number for each. A
    test of a query over a list of ids reads this with more ids than a statement may carry, never a clock or a guess."""
    seen: list[int] = []

    def noted(_conn, _cursor, statement, parameters, _context, _many) -> None:
        if statement.lstrip().upper().startswith("BEGIN"):
            return  # SQLite's writer sessions open with BEGIN IMMEDIATE: not a statement the app asked for
        seen.append(len(parameters) if isinstance(parameters, (tuple, list, dict)) else 0)

    engine = db.engine().sync_engine
    event.listen(engine, "before_cursor_execute", noted)
    try:
        yield seen
    finally:
        event.remove(engine, "before_cursor_execute", noted)


@contextlib.asynccontextmanager
async def go_that_leaves_nothing_running():
    """Go's progress and its running task, kept for each owner in the app's one process (`browser/go.py`), start empty
    for the block, and a task still going when the block ends is cancelled and waited for, so it works on nothing
    after. The `go_starts_fresh_and_leaves_nothing_running` fixture (tests/conftest.py) wraps every test in it."""
    from app.jobsearch.browser import go as apply_go

    tasks: dict[str, asyncio.Task] = {}
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(apply_go, "_runs", {})
        patch.setattr(apply_go, "_tasks", tasks)
        try:
            yield
        finally:
            left = [task for task in tasks.values() if not task.done()]
            for task in left:
                task.cancel()
            await asyncio.gather(*left, return_exceptions=True)


#: Noon on 4 November 2026: the moment `the_clock_says_noon` (conftest.py) pins the app's clock at. Noon in UTC is the
#: same day in the Americas and in Europe.
NOON = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)


def clock_at(moment: datetime) -> SimpleNamespace:
    """A stand-in for the `datetime` class that a module reads its clock from (`datetime.now(UTC)`): it says `moment`,
    always, in the zone the caller asks for."""
    assert moment.tzinfo is not None, "a moment with its zone"
    return SimpleNamespace(now=lambda tz=None: moment.astimezone(tz))


def pin_clock(monkeypatch, moment: datetime, zone: str | None = None) -> None:
    """The app's own clock says `moment` for the rest of the test, in the owner's time `zone` when one is given: what
    `utc_now()` and `owner_today()` (`app/timeutil.py`) read in every module, so a test makes its rows and calls the app
    by one clock. A test that reads the real clock for its rows and again for its call is made twice, once at each
    read: across UTC midnight (7 pm in New York) it says two different days.
    Code that calls `datetime.now(UTC)` itself is not pinned: its stamps are the real moment."""
    from app import timeutil

    monkeypatch.setattr(timeutil, "datetime", clock_at(moment))
    if zone is not None:
        monkeypatch.setattr("app.timeutil.settings.timezone", zone)


def before_the_lock(monkeypatch, change) -> None:
    """The first `as_read.lock` of the test is held up until `change()` (an async function that commits in a session of
    its own) has run: another transaction changes a row after the pass read it and before the pass locks it. A test of
    what a pass leaves alone then sees the lock's own answer, never a clock."""
    from app.jobsearch import as_read

    real, done = as_read.lock, []

    async def lock(session, *args, **kwargs):
        if not done:
            done.append(True)
            # The pass's own read holds the one writer turn; end it so `change()` (a session of its own) can write.
            await db.release_if_free(session)
            await change()
        return await real(session, *args, **kwargs)

    monkeypatch.setattr(as_read, "lock", lock)


async def waiting_count(session, owner: str) -> int:
    """How many postings wait to become a role (`role_from_posting.waiting_where`, less those no one has named yet)."""
    from app.jobsearch.models import JobPosting
    from app.jobsearch.pipeline import from_posting as role_from_posting
    from app.jobsearch.work import titles

    return int(await session.scalar(select(func.count()).select_from(JobPosting).where(
        *role_from_posting.waiting_where(owner), not_(titles.unnamed()))) or 0)


def browser_recipe(host: str = "listings.example.test", **more) -> dict:
    """A valid `browser` recipe for a made-up site (docs/PRD.md 37.1), with any top-level part replaced."""
    return {
        "kind": "browser", "start_url": f"https://www.{host}/jobs",
        "search": {"how": "boxes", "phrase_box": "the Search jobs box at the top",
                   "place_box": "the Where box beside it",
                   "recency": {"control": "the Date posted menu", "choices": ["Past 24 hours", "Past week"]},
                   "newest": "the Sort by menu, choice Newest"},
        "results": {"row": "one card in the list on the left", "next": "the Next button under the list",
                    "fields": {"title": "the bold link text", "company": "the line under the title",
                               "location": "the grey text after the company", "posted": "the small text below"}},
        "posting": {"description": "the text under About the job", "apply": "the Apply button at the top right"},
        "notes": ["Results show only after the Search button is pressed."], **more}


async def learn_sites(session, *sites) -> None:
    """Give each site a `browser` recipe, as a learn run would have (so the night's browser session reads it)."""
    from urllib.parse import urlsplit

    from app.sites import skills

    for site in sites:
        host = (urlsplit(site.url).hostname or "").removeprefix("www.")
        await skills.save_skill(session, site.owner, site.id, browser_recipe(host), None)


def tracker_workbook(rows: list[dict], header: list[str] | None = None) -> io.BytesIO:
    """A tracker as the owner keeps it: the `Tracker` sheet, the 25 columns, one row per dict (keyed by column name)."""
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Tracker"
    sheet.append(header or COLUMNS)
    for row in rows:
        sheet.append([row.get(c) for c in (header or COLUMNS)])
    buffer = io.BytesIO()
    workbook.save(buffer)
    buffer.seek(0)
    return buffer


def tracker_row(company: str, title: str, **fields) -> dict:
    return {"company": company, "title": title, "status": "second_impression", "fit": 3, **fields}


def reported(company: str, title: str, fit: int = 5, **fields) -> dict:
    """One role as the scout reports it."""
    return {
        "company": company, "title": title, "url": f"https://jobs.example.test/{company}/{title}".replace(" ", "-"),
        "fit": fit, "fit_evidence": f"base {fit}; test evidence", "work_mode": "remote", **fields,
    }


#: A page with plenty on it, such as a listing page anyone can open (the made-up site answers it for any address).
OPEN_PAGE = (
    "<html><head><title>Open positions</title></head><body><h1>Open positions</h1>"
    + "".join(f"<p>Position {n}: a role on the team, with its place, its pay and what the work is.</p>"
              for n in range(1, 60))
    + "</body></html>"
)


def open_internet():
    """A fetcher factory whose every site is open: no robots.txt, and a page with content for any address. What a probe
    sees in a test that does not mean to test the probe (the real network is never reached)."""
    import httpx

    from app.jobsearch.fetch.polite import Fetcher

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, html=OPEN_PAGE)

    return lambda: Fetcher(httpx.AsyncClient(transport=httpx.MockTransport(handle), follow_redirects=True),
                           per_host_seconds=0)


def offline_internet():
    """A fetcher factory with no internet: every connection is refused, so a read of any address ends as a site that did
    not answer. What a test that does not mean to read the web is given (the real network is never reached)."""
    import httpx

    from app.jobsearch.fetch.polite import Fetcher

    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no internet in tests", request=request)

    return lambda: Fetcher(httpx.AsyncClient(transport=httpx.MockTransport(handle)), per_host_seconds=0)


def _scripts():
    """Alembic's migration scripts."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    config = Config(str(db.BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(db.BACKEND_DIR / "migrations"))
    return ScriptDirectory.from_config(config)


def migration_head() -> str:
    """The newest migration, read from Alembic's scripts, so a new migration needs no edit to the tests that name it."""
    return _scripts().get_current_head()


@pytest.fixture(scope="session")
def schema_template(tmp_path_factory) -> Path:
    """One SQLite file with every table the app owns, made once for the worker by `db.create_schema` (a second and a
    half: it is why a test does not make its own). Each test works on a copy of it (`js`). The file is left in
    write-ahead log mode with the log emptied into it, so a copy is the whole database and starts in the mode the app
    asks for."""
    path = tmp_path_factory.mktemp("template") / "app.db"
    maker = create_engine(f"sqlite:///{path.as_posix()}")
    try:
        with maker.begin() as conn:
            db.create_schema(conn)
            # Stamped at head, as Alembic's environment stamps a new file (`migrations/env.py`): a start that runs
            # `db.init_schema` on the copy (the CLI's import) then has nothing to migrate, and does not run the
            # Postgres-era migrations on tables that are made already.
            MigrationContext.configure(conn).stamp(_scripts(), "head")
    finally:
        maker.dispose()
    kept = sqlite3.connect(path)
    try:
        kept.execute("PRAGMA journal_mode=WAL")
        kept.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        kept.close()
    return path


async def empty_the_tables(conn) -> None:
    """Every table the app owns is empty afterwards, for a test that runs one scenario twice on the one file (the file
    `js` made is not made again: it is the template's tables, emptied). The foreign keys are checked at the commit and
    not before (`defer_foreign_keys`), and by then no row is left pointing at one that is gone. The stored order's
    triggers name roles in `js_queue_dirty` as the roles are deleted, so that table is emptied last. Nothing here
    resets a row number: the next rows are numbered as the app numbers them after a delete."""
    await conn.execute(text("PRAGMA defer_foreign_keys = ON"))
    held = [t for t in sorted(db.OWNED_TABLES) if t != "js_queue_dirty"]
    for table in held:
        await conn.execute(text(f"DELETE FROM {table}"))
    await conn.execute(text("DELETE FROM js_queue_dirty"))


async def let_go() -> None:
    """Close the app's engines and forget them."""
    if db._engine is not None:
        await db._engine.dispose()
    if db._lifeline_engine is not None:
        await db._lifeline_engine.dispose()
    db._engine = db._sessions = db._lifeline_engine = db._lifeline_sessions = None


@pytest.fixture
async def js(schema_template, tmp_path_factory):
    """A clean job-search database for one test: its own SQLite file, a copy of the worker's template, and the app's
    engines pointed at it. The file is removed afterwards."""
    from process_engine import loader

    folder = tmp_path_factory.mktemp("js")
    path = folder / "app.db"
    shutil.copyfile(schema_template, path)
    engine = db.configure(db.sqlite_url(path))
    # The owner's saved versions of the process files live in memory too (what the engine runs); a test starts with
    # none, as the table is.
    loader.set_owner_versions({}, {})
    from app.jobsearch.work import session_kinds

    session_kinds.forget()  # no free pool session has asked yet (`session_kinds.pull`)
    yield engine
    loader.set_owner_versions({}, {})
    await engine.dispose()
    await let_go()
    shutil.rmtree(folder, ignore_errors=True)


#: A live page with more than `posting_match.MIN_PAGE_WORDS` words and no closed words: a page under that reads unknown.
OPEN_GO_PAGE = "<html><body><h1>Product Lead</h1>" + "<p>Lead the product. Apply below.</p>" * 20 + "</body></html>"


def fake_go_window(opened: list[str], pages: dict | None = None, kept: list | None = None):
    """Stands in for Go's window in the app's Chrome (`apply_go.go_window`): records each link a tab opens (in
    `opened`), shows `pages[link]` (a `browser_code.Page`; else `OPEN_GO_PAGE`, which counts as open), and
    puts the links of the tabs it hands to the owner in `kept`."""
    from app.jobsearch.browser import code as browser_code

    class FakeGoWindow:
        def __init__(self) -> None:
            self.tabs: dict[str, str] = {}

        async def open(self) -> None:
            return None

        async def tab(self, url: str, role_id=None) -> str:
            opened.append(url)
            self.tabs[f"tab-{len(opened)}"] = url
            return f"tab-{len(opened)}"

        async def read(self, target: str, url: str):
            return (pages or {}).get(url) or browser_code.Page(url=url, status=200, html=OPEN_GO_PAGE)

        async def close_tab(self, target: str) -> None:
            self.tabs.pop(target, None)

        async def keep(self) -> None:
            if kept is not None:
                kept.extend(self.tabs.values())

    return FakeGoWindow


def fake_open_links(opened: list[str]):
    """Stands in for Go's browser launch: records the links, opens nothing, has no note."""

    async def open_links(links: list[str], pause: float | None = None) -> None:
        opened.extend(links)

    return open_links


async def committed(call, *args, **kwargs):
    """Run `call(session, *args, **kwargs)` in a writer's session of its own and commit it: what one program wrote, as
    every other session then reads it (the pool's asks and the browser-down note are rows, `facts.py`). Returns what the
    call returned."""
    async with db.session_factory()() as session:
        out = await call(session, *args, **kwargs)
        await session.commit()
    return out


async def forget_asks(owner: str) -> None:
    """Drop every Run now ask of `owner` (the facts `asked:<kind>` and `owner_asked:<kind>`), committed: a test that
    goes on to a second case starts it unasked."""
    from app.jobsearch import facts

    async with db.session_factory()() as session:
        for prefix in ("asked:", "owner_asked:"):
            for key in await facts.get_many(session, owner, prefix):
                await facts.drop(session, owner, key)
        await session.commit()
