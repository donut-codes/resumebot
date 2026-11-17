"""The reads check: every table the passes
read (the pool's pull, the browser queue's plan and the runner dispatcher's walk) is one the work queue
watches, or is listed here with the reason no writer needs to post for it.

It catches what neither the scan of the direct writes nor the writer-by-writer test can: a table a consumer's predicate
reads that nobody watches, so that no writer of it, present or future, is ever asked to post (the review found
`SiteRecording` this way: the owner's ask for a recording waited for the hour's sweep). The reads are recorded from the
statements the passes run, so a new table in a pass's query fails here at once, with no list to keep in step by hand.
What it cannot see: a branch the made-up world does not reach. Everything is made up."""

import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from process_engine.base import Base as EngineBase
from sqlalchemy import event, select

from app import db
from app.database import Base
from app.jobsearch.browser import queue as browser_queue
from app.jobsearch.models import AgentSpend
from app.jobsearch.work import session_kinds
from app.jobsearch.work.queue import watch
from tests.builders import analysis_setup, browser_world
from tests.sites.builders import new_project
from tests.support import DEFAULT_OWNER, OWNER

#: The tables the passes read that nothing watches, each with why no writer needs to post for it. A list that only
#: shrinks: a table that comes to be watched is taken off (the test below fails until it is), and one no pass reads in
#: the made-up world is taken off too.
UNWATCHED: dict[str, str] = {
    "js_agent_spend": "what a session cost: it only adds to the spent amount of a window, which can close the budget "
                      "gate and never opens it; the window is the clock's (the nudge, the minute tick)",
    "js_raw_postings": "the copies of a posting `rank.unranked` reads are written by `raw_copies`, which posts "
                       "`pool.owed` for a posting that has a role, and a link to a role posts it (`watch.py`)",
    "js_cleansed_postings": "written with its raw copy (`js_raw_postings`)",
    "js_stage_moves": "the log of a role's moves, written beside its creation or status change (`models."
                      "_record_stage_moves`, `actions.store`), which post",
    "js_mission_runs": "a session's run record: it opens and closes with the session, whose runner (`AgentRunner`) and "
                       "whose items (`BrowserItem`, `HandOut`) are watched",
    "plans": "the plan a site project's tasks are of: its approval makes the tasks in the same transaction "
             "(`sites/plans.py::_approve_plan`, the only place a plan becomes approved), which post `ready.site`; its "
             "other changes take work away or are the owner's reading",
    "proc_instances": "the process engine's own state: the work it leaves a session is a wait (`ProcWait`, watched)",
    "proc_tokens": "the process engine's own state: the work it leaves a session is a wait (`ProcWait`, watched)",
    "proc_history": "the process engine's own log",
    "js_engine_facts": "the browser-down note and the owner's Run now asks (`facts.py`): a runner that says the "
                       "browser is back asks for work again at its own next check-in, and the click that writes an "
                       "ask wakes the engine itself after its commit (`engine_calls.knock`, a run's `wake`)",
    "proc_versions": "an owner's saved versions of the process files: the engine reads them when it begins a run "
                     "(`process.READS_VERSIONS_FIRST`), so an edit changes what the next run reads and nothing waits "
                     "on a version",
}

FROM = re.compile(r'\b(?:from|join)\s+"?([a-z_][a-z0-9_]*)"?', re.IGNORECASE)


def _known_tables() -> set[str]:
    return {m.local_table.name for base in (Base, EngineBase) for m in base.registry.mappers}


async def _read_by(*passes: Callable[[], Awaitable[object]]) -> set[str]:
    """The tables the statements of these passes read (a `select` or a `with`, by its `from` and `join`), of the tables
    the app knows (a name that is no table is a CTE or a function)."""
    known, found = _known_tables(), set[str]()

    def noted(_conn, _cursor, statement, *_rest) -> None:
        if statement.lstrip().lower().startswith(("select", "with")):
            found.update(name.lower() for name in FROM.findall(statement) if name.lower() in known)

    engine = db.engine().sync_engine
    event.listen(engine, "before_cursor_execute", noted)
    try:
        for one in passes:
            await one()
    finally:
        event.remove(engine, "before_cursor_execute", noted)
    return found


async def _the_browsers_plan() -> None:
    async with db.session_factory()() as session:
        await browser_queue._plan(session, DEFAULT_OWNER, datetime.now(UTC))
        await session.commit()


async def _the_pull() -> None:
    """The pull until it starts nothing: each call starts one job at most, and the later builders (the employers, the
    ranks) are read only once the earlier ones have nothing to start."""
    for _ in range(6):
        session_kinds._pool_asked[OWNER] = datetime.now(UTC)
        async with db.session_factory()() as session:
            started = await session_kinds.pull(session, OWNER)
            await session.commit()
        if not started:
            return


async def _the_walk() -> None:
    for owner in (OWNER, DEFAULT_OWNER):
        async with db.session_factory()() as session:
            await session_kinds.next_kind(session, owner)
            await session.commit()


async def test_every_table_the_passes_of_phase_4_read_is_watched_or_listed_with_its_reason(js):
    await browser_world()
    await analysis_setup()
    read = await _read_by(_the_browsers_plan, _the_pull, _the_walk)
    # Work that waits ends the walk, and the pull's look, at the first kind that has some, so the later rungs are read
    # in a world where the earlier ones have none: a site project (what the `site` kind reads) waiting, then stopped.
    async with db.session_factory()() as session:
        project = await new_project(session)
        await session.commit()
    read |= await _read_by(_the_walk)
    async with db.session_factory()() as session:
        (await session.get(type(project), project.id)).stopped_at = datetime.now(UTC)
        await session.commit()
    read |= await _read_by(_the_walk)
    unwatched = read - watch.WATCHED_TABLES
    assert not unwatched - set(UNWATCHED), (
        f"a pass reads a table nothing watches, so no writer of it posts: {sorted(unwatched - set(UNWATCHED))}. Watch "
        "it (watch.py: what a change to it asks for) or list it here with why no writer needs to post for it")
    assert not set(UNWATCHED) & watch.WATCHED_TABLES, (
        f"watched now, so off the list: {sorted(set(UNWATCHED) & watch.WATCHED_TABLES)}")
    assert set(UNWATCHED) <= read, (
        f"no pass reads these in the made-up world, so take them off the list: {sorted(set(UNWATCHED) - read)}")
    assert all(len(why) >= 20 for why in UNWATCHED.values()), "each says why"
    assert {"js_site_recordings", "js_browser_domains", "projects"} <= read | watch.WATCHED_TABLES, (
        "the tables the review of phase 4 found unwatched are watched now")


async def test_the_audit_sees_a_read_of_a_table_nothing_watches(js):
    async def reads_the_spend() -> None:
        async with db.session_factory()() as session:
            await session.scalar(select(AgentSpend.owner).limit(1))
            await session.rollback()

    read = await _read_by(reads_the_spend)
    assert read == {"js_agent_spend"} and not read & watch.WATCHED_TABLES
