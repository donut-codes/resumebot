"""The owner door's `health` reports the engine's last passes from the database (the split of the app into a page and
an engine, the owner's point 3; docs/PRD.md path 50.5). The engine's minute tick writes when housekeeping and the runner
dispatcher last passed to the engine's facts (`handlers.stamp_passes`), and `health` reads the facts, never the memory
of the program that answers. The times here are fixed ones: no test reads the real clock. Everything is made up."""

import logging
from datetime import UTC, datetime, timedelta

from app import db
from app.jobsearch import facts
from app.jobsearch.owner_actions import runner
from app.jobsearch.work import dispatcher, housekeeping
from app.jobsearch.work.queue import handlers
from app.timeutil import iso_utc
from tests.support import OWNER

HOUSEKEEPING_AT = datetime(2026, 11, 4, 11, 58, 30, tzinfo=UTC)
DISPATCHER_AT = datetime(2026, 11, 4, 11, 59, 10, tzinfo=UTC)


def _passes(monkeypatch, housekeeping_at: datetime | None, dispatcher_at: datetime | None) -> None:
    """What this program remembers of its own last passes."""
    monkeypatch.setattr(housekeeping, "last_pass", housekeeping_at)
    monkeypatch.setattr(dispatcher, "last_pass", dispatcher_at)


async def _health() -> dict:
    """The `health` action, read through the door's runner in a session of its own."""
    async with db.session_factory()() as session:
        got = await runner.read(session, OWNER, "health", {})
    return got["result"]


async def _stamps() -> dict[str, str]:
    async with db.session_factory()() as session:
        return await facts.get_many(session, OWNER, "last_pass:")


async def test_health_reports_the_passes_the_engine_wrote_when_this_program_remembers_none(js, monkeypatch):
    _passes(monkeypatch, HOUSEKEEPING_AT, DISPATCHER_AT)
    await handlers.stamp_passes(OWNER)
    _passes(monkeypatch, None, None)  # the page program: it never ran a pass
    got = await _health()
    assert got["housekeeping_last_pass"] == iso_utc(HOUSEKEEPING_AT)
    assert got["dispatcher_last_pass"] == iso_utc(DISPATCHER_AT)


async def test_health_reports_no_pass_until_the_engine_has_written_one(js, monkeypatch):
    _passes(monkeypatch, HOUSEKEEPING_AT, DISPATCHER_AT)  # remembered here, written nowhere
    got = await _health()
    assert got["housekeeping_last_pass"] is None and got["dispatcher_last_pass"] is None


async def test_a_pass_not_made_since_the_start_keeps_the_time_it_had(js, monkeypatch):
    _passes(monkeypatch, HOUSEKEEPING_AT, DISPATCHER_AT)
    await handlers.stamp_passes(OWNER)
    later = HOUSEKEEPING_AT + timedelta(minutes=1)
    _passes(monkeypatch, later, None)  # the engine started again: housekeeping passed, the dispatcher not yet
    await handlers.stamp_passes(OWNER)
    assert await _stamps() == {"last_pass:dispatcher": iso_utc(DISPATCHER_AT), "last_pass:housekeeping": iso_utc(later)}


async def test_with_no_pass_made_nothing_is_written(js, monkeypatch):
    _passes(monkeypatch, None, None)
    await handlers.stamp_passes(OWNER)
    assert await _stamps() == {}


async def test_a_write_of_the_passes_that_fails_is_logged_and_does_not_raise(js, monkeypatch, caplog):
    async def broken(session, owner, key, value, *, now=None):
        raise RuntimeError("no room")

    monkeypatch.setattr(facts, "put", broken)
    _passes(monkeypatch, HOUSEKEEPING_AT, DISPATCHER_AT)
    caplog.set_level(logging.WARNING)
    await handlers.stamp_passes(OWNER)
    assert [m for m in caplog.messages if "writing the last passes" in m]
    assert await _stamps() == {}
