"""The engine builds its lookups before it starts any work. The lookups are the
index of the owner's roles (`same_role.known_roles`) and the classifier (`classifier.train`): each is read on a reader,
takes no writer turn, and is kept for the first pass that asks for it. Everything here is made up."""

import asyncio
import logging

from app import db, program
from app.jobsearch import main
from app.jobsearch.browser import code as browser_code
from app.jobsearch.browser import tabs as tab_register
from app.jobsearch.fit import classifier
from app.jobsearch.loops import schedule, startup
from app.jobsearch.pipeline import live_sink
from app.jobsearch.postings import same_role
from app.jobsearch.work.queue import loop as queue_loop
from app.settings import settings
from app.sites import asks
from tests.support import OWNER, load_profile


async def test_the_lookups_are_built_on_readers_and_take_no_writer_turn(js):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        await session.commit()
    same_role.clear_cache()
    classifier.clear_cache()
    asked: list[float] = []
    db.turn_watchers.append(asked.append)
    try:
        await startup.build_lookups(OWNER)
    finally:
        db.turn_watchers.remove(asked.append)
    assert asked == [], "a lookup asked for the writer's turn"
    assert OWNER in same_role._KNOWN, "the roles index was not kept"
    assert OWNER in classifier._CACHE, "the classifier's answer was not kept"


async def test_a_lookup_that_fails_is_logged_and_the_next_one_still_runs(js, monkeypatch, caplog):
    reached: list[str] = []

    async def broken(session, owner):
        raise RuntimeError("no index today")

    async def trained(session, owner):
        reached.append(owner)

    monkeypatch.setattr(same_role, "known_roles", broken)
    monkeypatch.setattr(classifier, "train", trained)
    with caplog.at_level(logging.ERROR):
        await startup.build_lookups(OWNER)
    assert reached == [OWNER], "the classifier was not built after the roles index failed"
    assert "start-up lookup the roles index failed" in caplog.text


#: What starts work. Every one of them is recorded by the stand-ins below.
WORK = ["doors", "schedule", "dispatcher", "code workers", "tab reconcile"]


def _loop(order: list[str], name: str):
    """A stand-in for a function that makes a background task: it notes `name` when the task is made and gives a
    coroutine that only waits (the start cancels it when it ends)."""

    def make(*_args):
        order.append(name)
        return asyncio.sleep(3600)

    return make


async def _start(monkeypatch, order: list[str], role: str = "engine") -> None:
    """Start and stop the program `role` once (`main._running`), with the steps replaced by recorders that write their
    name in `order` when they run. The database is the test's own."""
    monkeypatch.setattr(program, "PROGRAM", "all")
    monkeypatch.setattr(settings, "owner", OWNER)
    monkeypatch.setattr(settings, "run_schedule", True)
    monkeypatch.setattr(settings, "database_url", db.engine().url.render_as_string(hide_password=False))
    monkeypatch.setattr(db, "configure", lambda url=None: db.engine())

    async def fixups(session, owner):
        order.append("fixups")

    async def lookups(owner):
        order.append("lookups start")
        await asyncio.sleep(0)  # a lookup that yields the loop must still end before any work starts
        order.append("lookups end")

    async def code_workers(owner):
        order.append("code workers")
        return []

    monkeypatch.setattr(startup, "run_startup_fixups", fixups)
    monkeypatch.setattr(startup, "build_lookups", lookups)
    monkeypatch.setattr(main, "session_managers", lambda role: order.append("doors") or [])
    monkeypatch.setattr(schedule, "schedule_loop", _loop(order, "schedule"))
    monkeypatch.setattr(queue_loop, "run", _loop(order, "dispatcher"))
    monkeypatch.setattr(browser_code, "start", code_workers)
    monkeypatch.setattr(tab_register, "reconcile_at_app_start", _loop(order, "tab reconcile"))
    try:
        async with main._running(role):
            await asyncio.sleep(0)  # let the tasks the start made take their first step
    finally:
        asks.set_ask_owner(None)  # the start wired the site loop's asks to the Inbox; the next test gets the default


async def test_the_lookups_end_before_any_work_starts_and_after_the_fixups(js, monkeypatch):
    order: list[str] = []
    await _start(monkeypatch, order)
    assert set(WORK) <= set(order), f"a start the test expects did not happen: {order}"
    assert order.index("fixups") < order.index("lookups start")
    first_work = min(order.index(name) for name in WORK)
    assert order.index("lookups end") < first_work, f"work started before the lookups were built: {order}"


async def test_the_whole_app_in_one_process_builds_its_lookups_before_its_work_too(js, monkeypatch):
    order: list[str] = []
    await _start(monkeypatch, order, role="all")
    first_work = min(order.index(name) for name in WORK)
    assert order.index("lookups end") < first_work, f"work started before the lookups were built: {order}"


async def test_the_engine_tells_the_page_to_re_read_once_it_is_ready(js, monkeypatch):
    order: list[str] = []
    told: list[str] = []

    def tell_resync(owner):
        order.append("resync")
        told.append(owner)

    monkeypatch.setattr(live_sink, "tell_resync", tell_resync)
    await _start(monkeypatch, order)
    assert told == [OWNER]
    assert order.index("resync") > max(order.index(name) for name in WORK), "the page was told before the work started"


async def test_the_whole_app_in_one_process_tells_no_page(js, monkeypatch):
    told: list[str] = []
    monkeypatch.setattr(live_sink, "tell_resync", told.append)
    await _start(monkeypatch, [], role="all")
    assert told == []
