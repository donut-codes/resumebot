"""Run now on a mission, as the page program serves it: the page saves the ask and waits, the engine
runs the mission (`missions.asked.run`), and the page answers the run, or the refusal the run met with its own
status and words. With the engine stopped the ask is kept, the page says so (503), and the engine's start-up pickup
carries the run out once. Everything here is made up."""

import uuid
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app import db
from app.jobsearch import facts, page
from app.jobsearch.factory import build_app
from app.jobsearch.missions import asked as mission_asked  # noqa: F401  (registers the ask mission.run)
from app.jobsearch.missions import boards_night
from app.jobsearch.missions import core as missions
from app.jobsearch.missions import routes as mission_routes
from app.jobsearch.models import Mission, MissionRun
from app.jobsearch.work import engine_calls
from tests.split import asked_support, fresh
from tests.split.test_route_programs import _keys
from tests.support import OWNER, load_profile

pytestmark = pytest.mark.usefixtures("the_clock_says_noon")

BACKEND = Path(__file__).resolve().parents[2]
RUN = "POST /api/v1/missions/{mission_id}/run"


async def _read_mission() -> uuid.UUID:
    """The id of the owner's board-reading mission, after the missions every install starts with are made."""
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        await missions.ensure(session, OWNER)
        await session.commit()
        return (await session.scalars(
            select(Mission.id).where(Mission.owner == OWNER, Mission.key == missions.READ))).one()


async def _runs(mission_id: uuid.UUID) -> int:
    async with db.read_session_factory()() as reader:
        counted = select(func.count(MissionRun.id)).where(MissionRun.mission_id == mission_id)
        return int(await reader.scalar(counted) or 0)


async def _run_now(mission_id: uuid.UUID):
    async with AsyncClient(transport=ASGITransport(app=page.app), base_url="http://test") as http:
        return await http.post(f"/api/v1/missions/{mission_id}/run")


def test_the_page_serves_run_now_on_a_mission_and_the_engine_does_not():
    page_keys, engine_keys = (set(_keys(build_app(role))) for role in ("page", "engine"))
    assert RUN in page_keys
    assert RUN not in engine_keys


def test_the_page_program_does_not_load_the_engines_side_of_run_now():
    (loaded,) = fresh.loaded_after_importing("app.jobsearch.page")
    assert "app.jobsearch.missions.routes" in loaded, "the page did not load the mission routes"
    assert "app.jobsearch.missions.asked" not in loaded, "the page loaded the engine side of Run now"


async def test_the_engine_runs_the_mission_the_page_asked_and_the_page_answers_the_run(js, monkeypatch):
    asked_support.as_the_page(monkeypatch)
    launched: list[tuple] = []
    monkeypatch.setattr(boards_night, "start_in_background", lambda *a: launched.append(a))
    mission_id = await _read_mission()
    with asked_support.the_engine() as told:
        ran = await _run_now(mission_id)
        await told.settle()
    assert len(launched) == 1, "the engine began the read once"
    assert ran.status_code == 200, ran.text
    assert ran.json()["outcome"] == "running" and ran.json()["started_by"] == "you"
    assert await _runs(mission_id) == 1


async def test_a_run_the_mission_refuses_is_answered_with_the_status_and_words_the_run_gave(js, monkeypatch):
    asked_support.as_the_page(monkeypatch)
    mission_id = await _read_mission()
    with asked_support.the_engine() as told:
        first = await _run_now(mission_id)
        await told.settle()
    with asked_support.the_engine() as told:
        second = await _run_now(mission_id)
        await told.settle()
    assert first.status_code == 200, first.text
    assert second.status_code == 409
    assert second.json() == {"detail": "Read the employers' boards is running already"}
    assert await _runs(mission_id) == 1, "the refused run left nothing behind"


async def test_a_mission_there_is_none_of_is_answered_404_by_the_engine(js, monkeypatch):
    asked_support.as_the_page(monkeypatch)
    with asked_support.the_engine() as told:
        gone = await _run_now(uuid.UUID(int=7))
        await told.settle()
    assert gone.status_code == 404
    assert gone.json() == {"detail": "no such mission"}


async def test_with_no_engine_the_page_says_so_and_the_start_up_pickup_runs_the_mission_once(js, monkeypatch):
    asked_support.as_the_page(monkeypatch)
    launched: list[tuple] = []
    monkeypatch.setattr(boards_night, "start_in_background", lambda *a: launched.append(a))
    monkeypatch.setattr(mission_routes, "WAIT_SECONDS", 0.01)
    mission_id = await _read_mission()
    with asked_support.the_engine(answers=False):
        ran = await _run_now(mission_id)
        assert ran.status_code == 503
        assert ran.json() == {"detail": engine_calls.WORKER_NOT_RUNNING}
        assert await _runs(mission_id) == 0, "the page wrote no run: the engine does the work"
        async with db.read_session_factory()() as reader:
            assert len(await facts.get_many(reader, OWNER, "ask:")) == 1, "the ask is kept"
        # The engine starts: its pickup answers every ask left without an answer, once.
        assert await asked_support.the_engine_looks() == 1
        assert await _runs(mission_id) == 1
        assert await asked_support.the_engine_looks() == 0
        assert await _runs(mission_id) == 1
        assert len(launched) == 1, "the pickup began the read once"
