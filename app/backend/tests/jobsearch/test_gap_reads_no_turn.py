"""The Find Gaps progress GETs only read (the owner, 2026-11-10: the page program runs no work, and reading must not
lock everything): the dashboard, a run and a drafted line each ask for no writer's turn and change no row, and a run or
a line that took too long is still told failed at once, in the words the engine's pass fails it with. Made-up runs, one
pinned clock."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient

from app import db
from app.jobsearch.gaps import drafts as gap_drafts
from app.jobsearch.gaps import runs as find_gaps
from app.jobsearch.main import app
from app.jobsearch.models import GapDraft, GapRun
from app.settings import settings
from tests.support import OWNER, clock_at

NOW = datetime(2026, 11, 3, 15, 0, tzinfo=UTC)
MINUTE = timedelta(minutes=1)
HOUR = timedelta(hours=1)
API = "/api/v1/find-gaps"


@pytest.fixture
async def client(js, monkeypatch):
    monkeypatch.setattr(settings, "owner", OWNER)
    monkeypatch.setattr(find_gaps, "datetime", clock_at(NOW))  # the reads' clock: the rows below are made by it
    monkeypatch.setattr(gap_drafts, "datetime", clock_at(NOW))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
def turns():
    """The writer's turns asked for while a test runs (the seconds each waited)."""
    asked: list[float] = []
    db.turn_watchers.append(asked.append)
    yield asked
    db.turn_watchers.remove(asked.append)


async def _run(state="waiting", *, age=timedelta(0), taken_ago=None, touched_ago=None, kind="role",
               role_id: str | None = "builders", **more) -> str:
    """A run made `age` before NOW (taken `taken_ago` before, last heard of `touched_ago` before), with no process."""
    run = GapRun(owner=OWNER, role_id=role_id, title="Builders", kind=kind, state=state, created_at=NOW - age,
                 taken_at=None if taken_ago is None else NOW - taken_ago,
                 touched_at=None if touched_ago is None else NOW - touched_ago, **more)
    async with db.session_factory()() as session:
        session.add(run)
        await session.commit()
    return str(run.id)


async def _draft(run_id: str, state="waiting", *, age=timedelta(0), taken_ago=None) -> str:
    """A request for a drafted line made `age` before NOW (and taken `taken_ago` before), about a run."""
    draft = GapDraft(owner=OWNER, run_id=uuid.UUID(run_id), theme="Programs at scale", theme_kind="required",
                     words="What I did, in my own words.", state=state, created_at=NOW - age,
                     taken_at=None if taken_ago is None else NOW - taken_ago)
    async with db.session_factory()() as session:
        session.add(draft)
        await session.commit()
    return str(draft.id)


async def _state(model, key: str):
    async with db.session_factory()() as session:
        row = await session.get(model, uuid.UUID(key))
        return None if row is None else (row.state, row.error, row.finished_at)


async def _minute_tick() -> None:
    """The failing half of the engine's expiry, at the same moment."""
    async with db.session_factory()() as session:
        await find_gaps.expire(session, OWNER, NOW, drop_old=False)
        await gap_drafts.expire(session, OWNER, NOW, drop_old=False)
        await session.commit()


async def test_reading_the_dashboard_a_run_and_a_line_asks_for_no_turn_and_changes_no_row(client, turns):
    done = await _run("done", age=2 * HOUR, finished_at=NOW - HOUR)
    late = await _run(age=6 * MINUTE)
    line = await _draft(done, age=6 * MINUTE)
    turns.clear()
    paths = (API, f"{API}/runs/{late}", f"{API}/drafts/{line}", f"{API}/runs/not-an-id", f"{API}/drafts/not-an-id")
    answers = [await client.get(path) for path in paths]
    assert turns == [], "the reads are on a reader: they wait for no save"
    assert [a.status_code for a in answers] == [200, 200, 200, 404, 404]
    assert await _state(GapRun, late) == ("waiting", None, None), "told failed by the read, saved by the engine's pass"
    assert await _state(GapDraft, line) == ("waiting", None, None)


async def test_a_run_that_took_too_long_is_told_failed_in_the_words_the_engine_fails_it_with(client):
    young = await _run(age=4 * MINUTE)
    quick = await _run("working", age=40 * MINUTE, taken_ago=30 * MINUTE, touched_ago=19 * MINUTE)
    untaken = await _run(age=6 * MINUTE)
    silent = await _run("working", age=40 * MINUTE, taken_ago=30 * MINUTE, touched_ago=21 * MINUTE)
    told = {key: (await client.get(f"{API}/runs/{key}")).json() for key in (young, quick, untaken, silent)}
    assert (told[young]["state"], told[young]["step"]) == ("waiting", "Waiting for an agent")
    assert (told[quick]["state"], told[quick]["step"]) == ("working", "Starting")
    assert (told[untaken]["state"], told[untaken]["step"], told[untaken]["error"]) == (
        "failed", "Stopped", find_gaps.NOT_TAKEN)
    assert (told[silent]["state"], told[silent]["step"], told[silent]["error"]) == (
        "failed", "Stopped", find_gaps.NOT_FINISHED)
    assert await _state(GapRun, untaken) == ("waiting", None, None), "the read told it and saved nothing"
    assert await _state(GapRun, silent) == ("working", None, None)
    await _minute_tick()
    assert (await _state(GapRun, untaken))[:2] == ("failed", find_gaps.NOT_TAKEN)
    after = {key: (await client.get(f"{API}/runs/{key}")).json() for key in told}
    assert after == told, "the page saw no change when the engine's pass saved what the read had told"


async def test_the_dashboard_tells_a_run_that_took_too_long_as_the_failed_one(client):
    run = await _run(age=6 * MINUTE)
    told = (await client.get(API)).json()
    assert told["active"] is None
    assert told["failed"] == {"id": run, "role_id": "builders", "title": "Builders", "kind": "role", "state": "failed",
                              "step": "Stopped", "read": 0, "total": 0, "error": find_gaps.NOT_TAKEN}
    assert await _state(GapRun, run) == ("waiting", None, None), "the read told it and saved nothing"
    await _minute_tick()
    assert (await client.get(API)).json() == told, "the same dashboard once the engine's pass has saved it"


async def test_a_line_that_took_too_long_is_told_failed_in_the_words_the_engine_fails_it_with(client):
    done = await _run("done", age=2 * HOUR, finished_at=NOW - HOUR)
    young = await _draft(done, age=4 * MINUTE)
    quick = await _draft(done, "working", age=10 * MINUTE, taken_ago=2 * MINUTE)
    untaken = await _draft(done, age=6 * MINUTE)
    slow = await _draft(done, "working", age=10 * MINUTE, taken_ago=4 * MINUTE)
    told = {key: (await client.get(f"{API}/drafts/{key}")).json() for key in (young, quick, untaken, slow)}
    assert told[young] == {"id": young, "state": "waiting", "step": "Waiting for an agent"}
    assert told[quick] == {"id": quick, "state": "working", "step": "Drafting the line"}
    assert told[untaken] == {"id": untaken, "state": "failed", "step": "Stopped", "error": gap_drafts.NOT_TAKEN}
    assert told[slow] == {"id": slow, "state": "failed", "step": "Stopped", "error": gap_drafts.NOT_FINISHED}
    assert await _state(GapDraft, untaken) == ("waiting", None, None), "the read told it and saved nothing"
    await _minute_tick()
    assert (await _state(GapDraft, untaken))[:2] == ("failed", gap_drafts.NOT_TAKEN)
    after = {key: (await client.get(f"{API}/drafts/{key}")).json() for key in told}
    assert after == told, "the page saw no change when the engine's pass saved what the read had told"


async def test_a_row_kept_past_its_day_is_gone_for_the_page_and_still_in_the_table(client):
    failed = await _run("failed", age=25 * HOUR, error=find_gaps.NOT_TAKEN)
    typed = await _run("done", age=25 * HOUR, kind="new", role_id=None, finished_at=NOW - 24 * HOUR)
    kept = await _run("done", age=25 * HOUR, finished_at=NOW - 24 * HOUR)
    line = await _draft(kept, "done", age=25 * HOUR)
    paths = (f"{API}/runs/{failed}", f"{API}/runs/{typed}", f"{API}/runs/{kept}", f"{API}/drafts/{line}")
    assert [(await client.get(path)).status_code for path in paths] == [404, 404, 200, 404], (
        "a target role's finished run stays; a failed run, a typed title's run and a drafted line go after a day")
    assert (await client.get(API)).json()["failed"] is None, "a failed run past its day is not shown"
    for model, key in ((GapRun, failed), (GapRun, typed), (GapDraft, line)):
        assert await _state(model, key) is not None, "the engine's hour drops the row, never the read"
