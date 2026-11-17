"""Run now on the agents, as the page program serves it: the page checks the kind, saves the ask and
waits; the engine queues the work, keeps the owner's ask as rows and wakes the lanes (`pool.asked`); the page answers
what the route always answered. With the engine stopped the answer says so in `held` and the ask is kept for the
engine's start-up. Everything here is made up."""

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app import db
from app.jobsearch import facts, page
from app.jobsearch.factory import build_app
from app.jobsearch.pool import asked as pool_asked  # noqa: F401  (registers the asks pool.run_all and pool.run_kind)
from app.jobsearch.pool import core as pool
from app.jobsearch.pool import routes as pool_routes
from app.jobsearch.work import engine_calls
from app.jobsearch.work import hours as night
from tests.builders import analysis_setup
from tests.split import asked_support, fresh
from tests.split.test_route_programs import _keys
from tests.support import OWNER

BACKEND = Path(__file__).resolve().parents[2]
KINDS = "/api/v1/agent-kinds"
RUN_ALL = "POST /api/v1/agent-kinds/run-all"
RUN_KIND = "POST /api/v1/agent-kinds/{kind}/run"


@pytest.fixture(autouse=True)
def _no_peak_hours(monkeypatch):
    """The made-up owner has no busy hours: no limit but the ones a test sets holds the pool."""
    monkeypatch.setattr(night, "in_peak", lambda s, now=None: False)


async def _post(path: str):
    async with AsyncClient(transport=ASGITransport(app=page.app), base_url="http://test") as http:
        return await http.post(path)


async def _asks_kept() -> int:
    async with db.read_session_factory()() as reader:
        return len(await facts.get_many(reader, OWNER, "ask:"))


def test_the_page_serves_both_run_now_routes_of_the_agents_and_the_engine_serves_neither():
    page_keys, engine_keys = (set(_keys(build_app(role))) for role in ("page", "engine"))
    assert {RUN_ALL, RUN_KIND} <= page_keys
    assert not {RUN_ALL, RUN_KIND} & engine_keys


def test_the_page_program_does_not_load_the_engines_side_of_the_agents_run_now():
    (loaded,) = fresh.loaded_after_importing("app.jobsearch.page")
    assert "app.jobsearch.pool.routes" in loaded, "the page did not load the pool routes"
    assert "app.jobsearch.pool.asked" not in loaded, "the page loaded the engine side of Run now"


async def test_the_engine_queues_a_kinds_work_and_the_page_answers_what_the_route_always_did(js, monkeypatch):
    asked_support.as_the_page(monkeypatch)
    await analysis_setup()
    with asked_support.the_engine() as told:
        ran = await _post(f"{KINDS}/writer/run")
        await told.settle()
    assert ran.status_code == 200, ran.text
    assert ran.json() == {"kind": "writer", "queued": 0, "held": None}
    assert [call for call in told.told() if call[0] == "knock"] == [], "nothing was queued, so no lane is woken"
    async with db.read_session_factory()() as reader:
        assert await pool.owner_started_at(reader, OWNER, "writer") is not None, "the owner's ask is a row"


async def test_a_kind_that_is_paused_is_held_by_the_engine_and_the_page_says_why(js, monkeypatch):
    asked_support.as_the_page(monkeypatch)
    await analysis_setup()
    async with db.session_factory()() as session:
        await pool.pause(session, OWNER, "writer")
        await session.commit()
    with asked_support.the_engine() as told:
        ran = await _post(f"{KINDS}/writer/run")
        await told.settle()
    assert ran.json() == {"kind": "writer", "queued": 0, "held": "this kind is paused"}


async def test_run_all_is_answered_by_the_engine_and_wakes_the_lanes_only_when_something_was_queued(js, monkeypatch):
    asked_support.as_the_page(monkeypatch)
    await analysis_setup()
    with asked_support.the_engine() as told:
        ran = await _post(f"{KINDS}/run-all")
        await told.settle()
    assert ran.status_code == 200, ran.text
    body = ran.json()
    assert body["held"] is None and body["queued"]["writer"] == 0
    assert body["queued"]["researcher"] >= 1, "the made-up roles wait for their research"
    assert ("knock", "run-all", "run_now") in told.told(), "work was queued, so the lanes are woken"


async def test_a_kind_there_is_none_of_is_404_on_the_page_and_no_ask_is_saved(js, monkeypatch):
    asked_support.as_the_page(monkeypatch)
    with asked_support.the_engine(answers=False) as told:
        gone = await _post(f"{KINDS}/nobody/run")
    assert gone.status_code == 404
    assert await _asks_kept() == 0
    assert told.told() == []


async def test_with_no_engine_the_page_says_so_in_held_and_the_start_up_pickup_queues_the_work_once(js, monkeypatch):
    asked_support.as_the_page(monkeypatch)
    monkeypatch.setattr(pool_routes, "WAIT_SECONDS", 0.01)
    await analysis_setup()
    with asked_support.the_engine(answers=False):
        one = await _post(f"{KINDS}/writer/run")
        every = await _post(f"{KINDS}/run-all")
        assert one.json() == {"kind": "writer", "queued": 0, "held": engine_calls.WORKER_NOT_RUNNING}
        assert every.json() == {"queued": {}, "held": engine_calls.WORKER_NOT_RUNNING}
        assert await _asks_kept() == 2, "both asks are kept"
        async with db.read_session_factory()() as reader:
            assert await pool.owner_started_at(reader, OWNER, "writer") is None, "the engine has not run them"
        # The engine starts: its pickup answers every ask left without an answer, once.
        assert await asked_support.the_engine_looks() == 2
        async with db.read_session_factory()() as reader:
            assert await pool.owner_started_at(reader, OWNER, "writer") is not None
        assert await asked_support.the_engine_looks() == 0
