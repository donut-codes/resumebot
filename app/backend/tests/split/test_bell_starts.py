"""The bell's two starts (`work/bell.py`): `start:go` and `start:boards_night` begin the engine's task only when the
engine reads a matching row. The recording door (`fake_starts`, tests/conftest.py) keeps each start it is asked for and
runs none. Everything is made up."""

import uuid

import pytest
from sqlalchemy import select

from app import db
from app.jobsearch.browser import go_status
from app.jobsearch.missions import core as missions
from app.jobsearch.models import GoRun, Mission, MissionRun
from app.jobsearch.work import bell, engine_calls
from app.settings import settings
from app.timeutil import utc_now
from tests.support import OWNER, load_profile

pytestmark = pytest.mark.usefixtures("the_clock_says_noon")


@pytest.fixture(autouse=True)
def the_owner_and_a_fresh_guard(monkeypatch):
    monkeypatch.setattr(settings, "owner", OWNER)
    monkeypatch.setattr(bell, "_begun", set())


async def _go_row(**state) -> None:
    async with db.session_factory()() as session:
        session.add(GoRun(owner=OWNER, state={**go_status.fresh(), **state}))
        await session.commit()


async def _go_phase() -> str | None:
    async with db.read_session_factory()() as session:
        row = await session.get(GoRun, OWNER)
        return None if row is None else row.state["phase"]


async def _run(key: str, *, outcome: str = "running", started: dict | None = None) -> uuid.UUID:
    """A run of the mission `key`, saved; its id."""
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        await missions.ensure(session, OWNER)
        mission = await session.scalar(select(Mission).where(Mission.owner == OWNER, Mission.key == key))
        assert mission is not None
        entry = MissionRun(owner=OWNER, mission_id=mission.id, started_by="schedule", outcome=outcome,
                           summary="Reading the employers' boards.", started_at=utc_now(),
                           started={"reading": True, "details_per_night": 150} if started is None else started)
        session.add(entry)
        await session.flush()
        run_id = entry.id
        await session.commit()
    return run_id


def test_the_page_and_the_engine_know_the_same_starts():
    assert set(engine_calls._ASKED_BY_ROW) == set(bell.ROW_STARTS) == {"go", "boards_night"}


async def test_start_go_with_no_row_starts_nothing(js, fake_starts):
    await bell.handle("start:go", {})
    assert fake_starts.starts == []


async def test_start_go_with_a_row_that_is_not_asked_starts_nothing_and_leaves_the_row(js, fake_starts):
    await _go_row(phase="building")
    await bell.handle("start:go", {})
    assert fake_starts.starts == []
    assert await _go_phase() == "building"


async def test_start_go_with_an_asked_row_begins_go_once_for_the_owner(js, fake_starts):
    await _go_row(phase="asked")
    await bell.handle("start:go", {"owner": "someone else"})
    assert fake_starts.starts == [("go", OWNER)]
    assert await _go_phase() == "checking"
    await bell.handle("start:go", {})  # the row says checking now: a second bell starts nothing
    assert fake_starts.starts == [("go", OWNER)]


async def test_start_boards_night_for_a_read_waiting_to_begin_starts_it_with_what_the_row_says(js, fake_starts):
    run_id = await _run(missions.READ)
    await bell.handle("start:boards_night", {"id": str(run_id), "owner": "someone else"})
    assert fake_starts.starts == [("boards_night", (run_id, OWNER, 150, False))]


async def test_a_second_bell_for_one_run_starts_nothing_more(js, fake_starts):
    run_id = await _run(missions.READ)
    await bell.handle("start:boards_night", {"id": str(run_id)})
    await bell.handle("start:boards_night", {"id": str(run_id)})
    assert len(fake_starts.starts) == 1


@pytest.mark.parametrize("fields", [{}, {"id": "not-a-uuid"}, {"id": None}, {"id": str(uuid.UUID(int=77))}])
async def test_start_boards_night_without_a_real_run_starts_nothing(js, fake_starts, fields):
    await bell.handle("start:boards_night", fields)
    assert fake_starts.starts == []


async def test_start_boards_night_for_a_run_that_has_ended_starts_nothing(js, fake_starts):
    run_id = await _run(missions.READ, outcome="done")
    await bell.handle("start:boards_night", {"id": str(run_id)})
    assert fake_starts.starts == []


async def test_start_boards_night_for_a_run_of_another_mission_starts_nothing(js, fake_starts):
    run_id = await _run(missions.GO)
    await bell.handle("start:boards_night", {"id": str(run_id)})
    assert fake_starts.starts == []


async def test_start_boards_night_for_a_read_not_marked_reading_starts_nothing(js, fake_starts):
    run_id = await _run(missions.READ, started={"reading": False})
    await bell.handle("start:boards_night", {"id": str(run_id)})
    assert fake_starts.starts == []
