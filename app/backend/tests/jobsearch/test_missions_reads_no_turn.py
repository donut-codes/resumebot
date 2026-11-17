"""The mission GETs write nothing, and show a run as the schedule pass will leave it (the page
program runs no work). A read works out, on a copy of each running run that is in no session, what its kind's `settle`
would write (`Kind.reads_as`, `core.shown`): the owner sees a run that has ended as ended at once, no writer's turn is
taken and no row changes. The schedule pass is what saves it (`core._tick`). Everything here is made up."""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app import db
from app.jobsearch.missions import core as missions
from app.jobsearch.models import BrowserItem, MissionRun
from tests.builders import ONE, WORKER, browser_world, lease
from tests.split.test_page_reads_take_no_write_lock import turns_recorded
from tests.support import DEFAULT_OWNER, OWNER, load_profile, pin_clock

#: When the made-up browser session last called: long before any day this test runs on.
LONG_AGO = datetime(2026, 10, 9, 22, 0, tzinfo=UTC)
#: The app's clock in the tests that pin it.
NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
#: What a read of the mail whose process is gone is closed with (`mail/sync.py`, `settle_run`).
LOST = "The read of the mail was lost: its process is gone."


def _copy_of(run: MissionRun) -> MissionRun:
    """The run as a row that is in no session, as a read makes it."""
    return MissionRun(id=run.id, owner=run.owner, mission_id=run.mission_id, started_by=run.started_by,
                      started_at=run.started_at, ended_at=run.ended_at, outcome=run.outcome, summary=run.summary,
                      started=dict(run.started or {}))


async def test_a_silent_browser_session_is_closed_on_a_copy_in_the_words_the_engine_will_write(js):
    await browser_world()
    await lease("scan", ONE, WORKER, LONG_AGO)
    async with db.session_factory()() as session:
        mission = await missions.get(session, DEFAULT_OWNER, missions.BROWSER)
        run = (await session.scalars(select(MissionRun).where(MissionRun.mission_id == mission.id))).one()
        seen = _copy_of(run)
        await missions.kind_of(missions.BROWSER).reads_as(session, seen)
        assert seen.outcome == "failed" and "ended holding an item" in seen.summary
        held = (await session.scalars(select(BrowserItem).where(BrowserItem.leased_by == WORKER))).all()
        assert run.outcome == "running" and len(held) == 1, "the read closed the run or gave its item back"
        assert not session.dirty and not session.new, "the read left a change for the next flush"
        await missions.refresh(session, run)  # the schedule pass's step for a running run
        assert (run.outcome, run.summary) == (seen.outcome, seen.summary), "the engine writes what the read showed"
        assert held[0].state == "waiting" and held[0].leased_by is None, "and gives the item back"


async def _a_mail_read_whose_process_is_gone() -> tuple[uuid.UUID, uuid.UUID]:
    """Read the mail with one run still marked running whose process is no row: the schedule pass would close it as
    failed. Returns the mission's id and the run's."""
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        await missions.ensure(session, OWNER)
        mission = await missions.get(session, OWNER, missions.READ_MAIL)
        run = MissionRun(owner=OWNER, mission_id=mission.id, started_by="schedule", outcome="running",
                         summary="Reading the mail.", started={"mail": True, "instance": str(uuid.uuid4())},
                         started_at=NOW - timedelta(hours=1))
        session.add(run)
        await session.commit()
        return mission.id, run.id


async def _stored(run_id: uuid.UUID) -> tuple[str, bool, str]:
    """The run's row as it is saved: its outcome, whether it has an end, its summary."""
    async with db.read_session_factory()() as session:
        run = await session.get(MissionRun, run_id)
        return run.outcome, run.ended_at is not None, run.summary


async def test_the_mission_gets_take_no_turn_write_no_row_and_show_an_ended_run_as_ended(client, monkeypatch):
    pin_clock(monkeypatch, NOW)
    mission_id, run_id = await _a_mail_read_whose_process_is_gone()
    with turns_recorded() as turns:
        listed = await client.get("/api/v1/missions")
        detail = await client.get(f"/api/v1/missions/{mission_id}")
    assert (listed.status_code, detail.status_code) == (200, 200)
    assert not turns.waits and not turns.ends, "a read of the missions asked for the writer's turn"
    last = next(m for m in listed.json()["missions"] if m["key"] == missions.READ_MAIL)["last_run"]
    assert (last["id"], last["outcome"], last["summary"]) == (str(run_id), "failed", LOST)
    assert last["ended_at"] is not None, "a run that has ended reads as ended"
    assert detail.json()["last_run"] == last and detail.json()["runs"] == [last]
    assert await _stored(run_id) == ("running", False, "Reading the mail."), "the read wrote the run"


async def test_the_schedule_pass_saves_what_the_read_showed(client, monkeypatch):
    pin_clock(monkeypatch, NOW)
    mission_id, run_id = await _a_mail_read_whose_process_is_gone()
    before = (await client.get(f"/api/v1/missions/{mission_id}")).json()["last_run"]
    async with db.session_factory()() as session:
        mission = await missions.get(session, OWNER, mission_id)
        assert await missions._tick(session, OWNER, mission, False, NOW) == []
        await session.commit()
    assert await _stored(run_id) == ("failed", True, LOST)
    assert (await client.get(f"/api/v1/missions/{mission_id}")).json()["last_run"] == before


async def test_a_read_in_a_writers_session_leaves_it_nothing_to_flush(js, monkeypatch):
    pin_clock(monkeypatch, NOW)
    mission_id, run_id = await _a_mail_read_whose_process_is_gone()
    async with db.session_factory()() as session:
        listed = await missions.listing(session, OWNER)
        detail = await missions.detail(session, OWNER, mission_id)
        assert next(m for m in listed if m["key"] == missions.READ_MAIL)["last_run"]["outcome"] == "failed"
        assert detail["runs"][0]["outcome"] == "failed"
        assert not session.dirty and not session.new, "the read left a change for the next flush"
        assert (await session.get(MissionRun, run_id)).outcome == "running", "the read changed the run's row"
