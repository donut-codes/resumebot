"""The page rings the engine: a commit that posted a to-do is a `look` bell in the page program, the page's start
installs the `Remote` door and its end takes it away. `notice.ring_engine` is replaced by a recorder. Everything is made
up."""

import pytest

from app import db, log_setup, notice, program, web
from app.jobsearch import page
from app.jobsearch.work import engine_calls, queue
from tests.support import OWNER


@pytest.fixture
def rung(monkeypatch):
    seen: list[tuple[str, dict]] = []
    monkeypatch.setattr(notice, "ring_engine", lambda kind, **fields: seen.append((kind, fields)))
    return seen


@pytest.fixture
def remote_door():
    engine_calls.install(engine_calls.Remote())
    yield
    engine_calls.install(None)


async def test_a_commit_that_posted_a_todo_rings_look_in_the_page(js, rung, remote_door):
    async with db.session_factory()() as session:
        queue.post(session, OWNER, "test.first", cause="test")
        await session.commit()
    assert rung == [("look", {})]


async def test_a_rollback_and_a_commit_that_posted_nothing_ring_nothing(js, rung, remote_door):
    async with db.session_factory()() as session:
        queue.post(session, OWNER, "test.first", cause="test")
        await session.rollback()
    async with db.session_factory()() as session:
        await session.commit()
    assert rung == []


async def _schema_is_current(seconds: float = 3.0) -> bool:
    return True


async def test_the_pages_start_installs_the_remote_door_and_its_end_takes_it_away(monkeypatch):
    monkeypatch.setattr(program, "PROGRAM", "all")  # `lifespan` sets it; the test's own value comes back afterwards
    for owner, name in ((db, "configure"), (web, "log_page"), (log_setup, "configure_logging")):
        monkeypatch.setattr(owner, name, lambda *args, **kwargs: None)
    monkeypatch.setattr(page, "session_managers", lambda role: [])
    monkeypatch.setattr(db, "schema_is_current", _schema_is_current)  # no database is set up in this test
    async with page.lifespan(page.app):
        assert isinstance(engine_calls._backend, engine_calls.Remote)
        assert program.PROGRAM == "page"
    assert isinstance(engine_calls._backend, engine_calls.Local)
