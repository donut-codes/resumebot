"""The page waits for the schema. `db.schema_is_current` is a read on the lifeline pool that
compares `alembic_version` with the code's head. Everything here is made up."""

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app import db, log_setup, program, web
from app.jobsearch import page
from app.settings import APP_NAME, VERSION, settings
from tests.support import OWNER, migration_head

BEHIND = "0000_one_revision_behind"


async def _stamp(version: str) -> None:
    """Write `version` as the database's revision, as an engine that has not yet migrated leaves it."""
    async with db.session_factory()() as session:
        await session.execute(text("UPDATE alembic_version SET version_num = :v"), {"v": version})
        await session.commit()


def test_the_codes_head_is_the_newest_migration():
    assert db.code_head() == migration_head()


async def test_a_database_at_the_codes_head_is_current(js):
    assert await db.schema_is_current() is True


async def test_a_database_one_revision_behind_is_not_current(js):
    await _stamp(BEHIND)
    assert await db.schema_is_current() is False


async def test_a_database_with_no_version_table_is_not_current(js):
    async with db.session_factory()() as session:
        await session.execute(text("DROP TABLE alembic_version"))
        await session.commit()
    assert await db.schema_is_current() is False


READY = "/api/v1/agent/ready"


@pytest.fixture
def quiet_start(monkeypatch):
    """The page's start without its logging, its zone check and its doors; the database stays the test's own."""
    monkeypatch.setattr(program, "PROGRAM", "all")  # `lifespan` sets it; the test's own value comes back afterwards
    monkeypatch.setattr(settings, "owner", OWNER)
    monkeypatch.setattr(log_setup, "configure_logging", lambda: None)
    monkeypatch.setattr(web, "log_page", lambda: None)
    monkeypatch.setattr(db, "configure", lambda url=None: None)
    monkeypatch.setattr(page, "session_managers", lambda role: [])
    page.gate.reset()
    yield
    page.gate.reset()


async def _version() -> str:
    async with db.read_session_factory()() as session:
        return (await session.execute(text("SELECT version_num FROM alembic_version"))).scalar_one()


async def test_a_page_started_behind_answers_starting_then_serves_once_migrated(js, quiet_start):
    await _stamp(BEHIND)
    async with page.lifespan(page.app):
        async with AsyncClient(transport=ASGITransport(app=page.app), base_url="http://test") as client:
            api = await client.get(READY)
            health = await client.get("/health")
            assert api.status_code == 503
            assert api.json() == {"detail": "starting"}
            assert api.headers["retry-after"] == "1"
            assert health.status_code == 503
            assert health.json() == {"app": APP_NAME, "status": "starting", "version": VERSION, "db": "ok"}
            assert page.gate.listening is None, "the notice receiver is bound only after the gate opens"
            assert await _version() == BEHIND, "the page migrated nothing"
            await _stamp(migration_head())  # what the engine's migration leaves behind
            served = await client.get(READY)
            assert served.status_code == 200
            assert served.json() == {"ready": False, "why": "No agent has checked in yet."}
            assert page.gate.listening is not None, "the gate opened and the receiver was started"
            assert (await client.get("/health")).json()["status"] == "ok"
    assert page.gate.is_open is False, "a stop closes the gate for the next start"


async def test_a_page_started_at_the_head_serves_at_once_and_listens_from_its_start(js, quiet_start):
    async with page.lifespan(page.app):
        assert page.gate.is_open
        assert page.gate.listening is not None
        async with AsyncClient(transport=ASGITransport(app=page.app), base_url="http://test") as client:
            assert (await client.get(READY)).status_code == 200


async def test_health_still_says_degraded_when_the_database_does_not_answer(js, quiet_start, monkeypatch):
    async def down(seconds: float = 3.0) -> bool:
        return False

    monkeypatch.setattr(db, "schema_is_current", down)
    monkeypatch.setattr(db, "ping", down)
    async with AsyncClient(transport=ASGITransport(app=page.app), base_url="http://test") as client:
        health = await client.get("/health")
    assert health.status_code == 503
    assert health.json()["status"] == "degraded"


async def test_the_pages_own_files_are_served_while_the_gate_is_closed(js, quiet_start):
    await _stamp(BEHIND)
    async with AsyncClient(transport=ASGITransport(app=page.app), base_url="http://test") as client:
        got = await client.get("/not-an-api-path")
    assert got.status_code != 503
