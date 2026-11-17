"""The progress GET of Fill from a posting and Import your resume only reads (the owner, 2026-11-10: the page program
runs no work, and reading must not lock everything): `GET /profile/parse/{id}` asks for no writer's turn and changes no
row, and a job that took too long is still told failed at once, in the words the engine's pass fails it with. Made-up
jobs, one pinned clock."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient

from app import db
from app.jobsearch.jobs import parse as role_parse
from app.jobsearch.main import app
from app.jobsearch.models import ParseJob
from app.settings import settings
from tests.support import OWNER, clock_at

NOW = datetime(2026, 11, 3, 15, 0, tzinfo=UTC)
MINUTE = timedelta(minutes=1)
HOUR = timedelta(hours=1)
URL = "/api/v1/profile/parse"


@pytest.fixture
async def client(js, monkeypatch):
    monkeypatch.setattr(settings, "owner", OWNER)
    monkeypatch.setattr(role_parse, "datetime", clock_at(NOW))  # the read's clock: the rows below are made by it
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
def turns():
    """The writer's turns asked for while a test runs (the seconds each waited)."""
    asked: list[float] = []
    db.turn_watchers.append(asked.append)
    yield asked
    db.turn_watchers.remove(asked.append)


async def _parse(state="waiting", *, age=timedelta(0), taken_ago=None, **more) -> str:
    """A parse job made `age` before NOW (and taken `taken_ago` before)."""
    job = ParseJob(owner=OWNER, text="The posting, as it was pasted. " * 4, state=state, created_at=NOW - age,
                   taken_at=None if taken_ago is None else NOW - taken_ago, **more)
    async with db.session_factory()() as session:
        session.add(job)
        await session.commit()
    return str(job.id)


async def _state(job_id: str):
    async with db.session_factory()() as session:
        row = await session.get(ParseJob, uuid.UUID(job_id))
        return None if row is None else (row.state, row.error, row.finished_at)


def _failed(job_id: str, error: str) -> dict:
    return {"id": job_id, "state": "failed", "step": "Stopped", "role_id": None, "error": error}


async def test_reading_a_parse_job_asks_for_no_turn_and_changes_no_row(client, turns):
    late = await _parse(age=6 * MINUTE)
    old = await _parse("done", age=25 * HOUR, fields={"name": "Operations manager"})
    turns.clear()
    answers = [await client.get(f"{URL}/{key}") for key in (late, old, "not-an-id")]
    assert turns == [], "the read is on a reader: it waits for no save"
    assert [a.status_code for a in answers] == [200, 404, 404]
    assert await _state(late) == ("waiting", None, None), "told failed by the read, saved by the engine's pass"
    assert await _state(old) == ("done", None, None), "gone for the page, dropped by the engine's hour"


async def test_a_parse_job_that_took_too_long_is_told_failed_in_the_words_the_engine_fails_it_with(client):
    young = await _parse(age=4 * MINUTE)
    quick = await _parse("reading", age=10 * MINUTE, taken_ago=2 * MINUTE)
    untaken = await _parse(age=6 * MINUTE)
    slow = await _parse("reading", age=10 * MINUTE, taken_ago=4 * MINUTE)
    told = {key: (await client.get(f"{URL}/{key}")).json() for key in (young, quick, untaken, slow)}
    assert told[young]["state"] == "waiting" and told[quick]["state"] == "reading"
    assert told[untaken] == _failed(untaken, role_parse.NOT_TAKEN)
    assert told[slow] == _failed(slow, role_parse.NOT_FINISHED)
    async with db.session_factory()() as session:  # the engine's minute tick, at the same moment
        await role_parse.expire(session, OWNER, NOW, drop_old=False)
        await session.commit()
    assert (await _state(untaken))[:2] == ("failed", role_parse.NOT_TAKEN)
    after = {key: (await client.get(f"{URL}/{key}")).json() for key in told}
    assert after == told, "the page saw no change when the engine's pass saved what the read had told"


async def test_a_background_job_waits_however_long_and_a_recipe_has_its_fifteen_minutes(client):
    patient = await _parse(age=3 * HOUR, kind="profile")
    fixing = await _parse("reading", age=20 * MINUTE, taken_ago=14 * MINUTE, kind="recipe")
    given_up = await _parse("reading", age=20 * MINUTE, taken_ago=16 * MINUTE, kind="recipe")
    told = {key: (await client.get(f"{URL}/{key}")).json() for key in (patient, fixing, given_up)}
    assert told[patient]["state"] == "waiting", "nobody is at the screen for it: it is never told failed for waiting"
    assert told[fixing]["state"] == "reading"
    assert told[given_up] == _failed(given_up, role_parse.NOT_FINISHED)
