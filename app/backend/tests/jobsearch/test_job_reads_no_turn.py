"""The progress GET of Check my roles only reads (the owner, 2026-11-10: the page program runs no work, and reading
must not lock everything): `GET /profile/check/{id}` asks for no writer's turn and changes no row, and a check that
took too long is still told failed at once, in the words the engine's pass fails it with. Made-up jobs, one pinned
clock."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient

from app import db
from app.jobsearch.jobs import check as role_check
from app.jobsearch.main import app
from app.jobsearch.models import CheckJob
from app.settings import settings
from tests.support import OWNER, clock_at

NOW = datetime(2026, 11, 3, 15, 0, tzinfo=UTC)
MINUTE = timedelta(minutes=1)
HOUR = timedelta(hours=1)
URL = "/api/v1/profile/check"


@pytest.fixture
async def client(js, monkeypatch):
    monkeypatch.setattr(settings, "owner", OWNER)
    monkeypatch.setattr(role_check, "datetime", clock_at(NOW))  # the read's clock: the rows below are made by it
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
def turns():
    """The writer's turns asked for while a test runs (the seconds each waited)."""
    asked: list[float] = []
    db.turn_watchers.append(asked.append)
    yield asked
    db.turn_watchers.remove(asked.append)


async def _check(state="waiting", *, age=timedelta(0), taken_ago=None, touched_ago=None, **more) -> str:
    """A check job made `age` before NOW (taken `taken_ago` before, last answered `touched_ago` before)."""
    job = CheckJob(owner=OWNER, scope="pursued", state=state, created_at=NOW - age,
                   taken_at=None if taken_ago is None else NOW - taken_ago,
                   touched_at=None if touched_ago is None else NOW - touched_ago,
                   items=[{"ref": "r1", "title": "Title 1", "company": "Fabrikam"}], answers={}, meta={"total": 1},
                   **more)
    async with db.session_factory()() as session:
        session.add(job)
        await session.commit()
    return str(job.id)


async def _state(job_id: str):
    async with db.session_factory()() as session:
        row = await session.get(CheckJob, uuid.UUID(job_id))
        return None if row is None else (row.state, row.error, row.finished_at)


async def test_reading_a_check_asks_for_no_turn_and_changes_no_row(client, turns):
    late = await _check(age=6 * MINUTE)
    old = await _check("done", age=25 * HOUR, result={"scope": "pursued", "checked": 0})
    turns.clear()
    answers = [await client.get(f"{URL}/{key}") for key in (late, old, "not-an-id")]
    assert turns == [], "the read is on a reader: it waits for no save"
    assert [a.status_code for a in answers] == [200, 404, 404]
    assert await _state(late) == ("waiting", None, None), "told failed by the read, saved by the engine's pass"
    assert await _state(old) == ("done", None, None), "gone for the page, dropped by the engine's hour"


async def test_a_check_that_took_too_long_is_told_failed_in_the_words_the_engine_fails_it_with(client):
    young = await _check(age=4 * MINUTE)
    quick = await _check("working", age=20 * MINUTE, taken_ago=9 * MINUTE, touched_ago=4 * MINUTE)
    untaken = await _check(age=6 * MINUTE)
    silent = await _check("working", age=20 * MINUTE, taken_ago=9 * MINUTE, touched_ago=6 * MINUTE)
    told = {key: (await client.get(f"{URL}/{key}")).json() for key in (young, quick, untaken, silent)}
    assert told[young]["state"] == "waiting" and told[quick]["state"] == "working"
    assert told[untaken] == {"id": untaken, "scope": "pursued", "state": "failed", "step": "Stopped",
                             "error": role_check.NOT_TAKEN}
    assert told[silent] == {"id": silent, "scope": "pursued", "state": "failed", "step": "Stopped",
                            "error": role_check.NOT_FINISHED}
    async with db.session_factory()() as session:  # the engine's minute tick, at the same moment
        await role_check.expire(session, OWNER, NOW, drop_old=False)
        await session.commit()
    assert (await _state(untaken))[:2] == ("failed", role_check.NOT_TAKEN)
    after = {key: (await client.get(f"{URL}/{key}")).json() for key in told}
    assert after == told, "the page saw no change when the engine's pass saved what the read had told"
